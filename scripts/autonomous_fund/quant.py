"""Deterministic quantitative portfolio construction limits.

The AI proposes a complete long-only target portfolio. This module turns those
weights into a risk-compliant portfolio by ONLY reducing them, using completed
daily history that already exists. It has no model, broker or network access.

Invariants:
- Every transformation is a monotone reduction: name caps, group caps, the
  drawdown exposure budget, then one uniform scale for the volatility target,
  the 97.5% expected-shortfall limit and the historical stress-window loss
  limit. A reduction can never create a new limit breach, so the result
  satisfies every constraint at once.
- Correlations come from a longer, slower window than volatilities and are
  shrunk toward the average correlation; stress windows are used only after
  they ended and a name without history in a window is proxied pessimistically.
- Missing or insufficient history is never filled in. Such a symbol cannot be
  increased and is valued with a deliberately pessimistic volatility and
  correlation, so missing data shrinks, never expands, the permitted risk.
- Returns are simple close-to-close returns of COMPLETED sessions supplied by the
  caller; the caller is responsible for excluding the current session.
"""
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR

import numpy as np

from .risk import RiskError, number

TRADING_DAYS = 252
# Normal-distribution expected shortfall at 97.5%: phi(z) / (1 - 0.975), z = 1.959964.
NORMAL_ES_975 = 2.337802
WEIGHT_STEP = Decimal("0.000001")

# Operator-reviewed defaults for the shipped universe. A symbol may belong to
# several groups; each group limits the combined target weight of its members.
DEFAULT_GROUPS = {
    "technology": ("0.30", ["AAPL", "MSFT", "NVDA", "XLK"]),
    "communication": ("0.20", ["GOOGL", "META"]),
    "consumer_discretionary": ("0.20", ["AMZN", "HD"]),
    "financials": ("0.30", ["BRK.B", "JPM", "V", "MA", "XLF"]),
    "health_care": ("0.25", ["UNH", "JNJ", "XLV"]),
    "energy": ("0.15", ["XOM", "XLE"]),
    "consumer_staples": ("0.25", ["COST", "PG", "XLP"]),
    "us_growth_megacap": ("0.35", ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "QQQ", "XLK"]),
    "us_broad_index": ("0.40", ["SPY", "VTI", "DIA", "IWM", "QQQ"]),
    "treasury_duration": ("0.30", ["IEF", "TLT"]),
}

# Historical stress windows (peak to trough of the benchmark, approximately).
# Each is applied as a buy-and-hold loss of the CURRENT weights; a window is used
# only after it has ended, so a historical run never sees a later crisis.
DEFAULT_SCENARIOS = {
    "gfc_2008": ("2008-09-01", "2009-03-09"),
    "covid_2020": ("2020-02-19", "2020-03-23"),
    "rates_2022": ("2022-01-03", "2022-10-12"),
}


def _fraction(value, name, low=Decimal("0"), high=Decimal("1"), inclusive_low=False):
    result = number(value)
    if not ((low <= result) if inclusive_low else (low < result)) or result > high:
        raise RiskError(f"Invalid quantitative risk setting: {name}")
    return result


def _scenarios(config):
    """{name: [start, end]} -> sorted ((name, start, end)); ISO dates with start < end, at most 10."""
    from datetime import date
    if not isinstance(config, dict) or len(config) > 10:
        raise RiskError("stress_scenarios must map up to 10 names to [start, end] dates")
    result = []
    for name, window in sorted(config.items()):
        try:
            start, end = (str(value) for value in window)
            if not name or date.fromisoformat(start) >= date.fromisoformat(end):
                raise ValueError
        except (TypeError, ValueError):
            raise RiskError(f"stress_scenarios.{name} must be [start, end] ISO dates with start before end") from None
        result.append((str(name), start, end))
    return tuple(result)


def window_return(rows, start, end, tolerance_days=7):
    """Close-to-close return over [start, end] from completed daily closes, or None if not covered.

    Coverage means the history begins within `tolerance_days` after `start` and
    reaches within `tolerance_days` of `end`; a name listed during the window, or
    whose history stops before it ended, has no return (never extrapolated).
    """
    from datetime import date, timedelta
    ordered = sorted((str(day)[:10], float(close)) for day, close in rows or [] if close and close > 0)
    inside = [(day, close) for day, close in ordered if start <= day <= end]
    if not inside:
        return None
    slack = timedelta(days=tolerance_days)
    first, last = date.fromisoformat(inside[0][0]), date.fromisoformat(inside[-1][0])
    if first > date.fromisoformat(start) + slack or last < date.fromisoformat(end) - slack:
        return None
    return inside[-1][1] / inside[0][1] - 1


@dataclass(frozen=True)
class QuantPolicy:
    vol_lookback_days: int = 63
    es_lookback_days: int = 252
    min_history_days: int = 20
    es_min_observations: int = 120
    ewma_lambda: Decimal = Decimal("0.94")
    min_annual_vol: Decimal = Decimal("0.10")
    unknown_annual_vol: Decimal = Decimal("0.60")
    target_portfolio_vol: Decimal = Decimal("0.12")
    max_portfolio_es: Decimal = Decimal("0.02")
    max_name_risk: Decimal = Decimal("0.025")
    # Diversified unlevered ETFs carry little idiosyncratic jump risk; a wider
    # budget avoids cutting broad-market exposure in calm markets (see the
    # 2019 real-data replay in docs/AUTONOMOUS_PAPER_FUND.md).
    max_etf_name_risk: Decimal = Decimal("0.05")
    max_adv_participation: Decimal = Decimal("0.01")
    drawdown_soft: Decimal = Decimal("0.04")
    drawdown_floor_scale: Decimal = Decimal("0.25")
    daily_loss_limit: Decimal = Decimal("0.02")
    stress_benchmark: str = "SPY"
    stress_day_drop: Decimal = Decimal("0.03")
    stress_annual_vol: Decimal = Decimal("0.35")
    rebalance_band: Decimal = Decimal("0.005")
    min_trade_notional: Decimal = Decimal("100")
    # Correlations need far more data than volatilities: a slower decay over a
    # longer window, shrunk toward the average correlation (constant-correlation
    # target), so a few unusual weeks do not swing the portfolio volatility.
    correlation_lookback_days: int = 252
    correlation_lambda: Decimal = Decimal("0.97")
    correlation_shrinkage: Decimal = Decimal("0.25")
    # Buy-and-hold loss of the current weights in each historical stress window.
    max_scenario_loss: Decimal = Decimal("0.35")
    stress_scenarios: tuple = tuple((name, start, end) for name, (start, end) in sorted(DEFAULT_SCENARIOS.items()))
    groups: tuple = ()
    etf_symbols: tuple = ()

    @classmethod
    def from_config(cls, section, universe, limits):
        section = dict(section or {})
        groups_config = section.pop("groups", None)
        if "stress_scenarios" in section:
            section["stress_scenarios"] = _scenarios(section["stress_scenarios"])
        unknown = set(section) - {f for f in cls.__dataclass_fields__ if f not in {"groups", "etf_symbols"}}
        if unknown:
            raise RiskError("Unknown quantitative risk settings: " + ", ".join(sorted(unknown)))
        if groups_config is None:
            # Defaults apply only to members actually admitted by the operator.
            groups_config = {name: {"limit": cap, "members": [s for s in members if s in universe]}
                             for name, (cap, members) in DEFAULT_GROUPS.items()}
        groups = []
        for name, spec in sorted(groups_config.items()):
            members = frozenset(spec.get("members", []))
            if members - set(universe):
                raise RiskError(f"Risk group {name} contains symbols outside the eligible universe")
            if members:
                groups.append((name, str(_fraction(spec["limit"], f"groups.{name}.limit")), tuple(sorted(members))))
        # The operator's universe classification (stock / unlevered_etf) selects the name budget.
        etfs = tuple(sorted(s for s, kind in universe.items() if kind == "unlevered_etf")) if isinstance(universe, dict) else ()
        policy = cls(**{**section, "groups": tuple(groups), "etf_symbols": etfs})
        policy.validate(limits)
        return policy

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key in {"vol_lookback_days", "es_lookback_days", "min_history_days", "es_min_observations",
                       "stress_benchmark", "groups", "etf_symbols", "correlation_lookback_days", "stress_scenarios"}:
                continue
            object.__setattr__(self, key, number(value))

    def validate(self, limits):
        for key in ("vol_lookback_days", "es_lookback_days", "min_history_days", "es_min_observations",
                    "correlation_lookback_days"):
            if type(getattr(self, key)) is not int or not 5 <= getattr(self, key) <= 1000:
                raise RiskError(f"Invalid quantitative risk setting: {key}")
        if self.min_history_days > min(self.vol_lookback_days, self.es_lookback_days):
            raise RiskError("min_history_days must not exceed the risk lookbacks")
        _fraction(self.ewma_lambda, "ewma_lambda", Decimal("0.5"), Decimal("0.999"))
        _fraction(self.correlation_lambda, "correlation_lambda", Decimal("0.5"), Decimal("0.999"))
        _fraction(self.correlation_shrinkage, "correlation_shrinkage", inclusive_low=True)
        _fraction(self.max_scenario_loss, "max_scenario_loss")
        _scenarios({name: (start, end) for name, start, end in self.stress_scenarios})
        for key in ("min_annual_vol", "unknown_annual_vol", "target_portfolio_vol", "max_portfolio_es",
                    "max_name_risk", "max_etf_name_risk", "max_adv_participation", "drawdown_soft", "daily_loss_limit",
                    "stress_day_drop", "stress_annual_vol"):
            _fraction(getattr(self, key), key, high=Decimal("5"))
        _fraction(self.drawdown_floor_scale, "drawdown_floor_scale", inclusive_low=True)
        _fraction(self.rebalance_band, "rebalance_band", high=Decimal("0.1"), inclusive_low=True)
        if self.min_trade_notional < 0:
            raise RiskError("Invalid quantitative risk setting: min_trade_notional")
        if self.unknown_annual_vol < self.min_annual_vol:
            raise RiskError("unknown_annual_vol must be at least min_annual_vol")
        if not self.drawdown_soft < limits.drawdown:
            raise RiskError("drawdown_soft must be below the hard drawdown latch")
        if not isinstance(self.stress_benchmark, str) or not self.stress_benchmark:
            raise RiskError("A stress benchmark symbol is required")

    def name_risk(self, symbol):
        return self.max_etf_name_risk if symbol in self.etf_symbols else self.max_name_risk

    @property
    def group_map(self):
        return {name: (Decimal(cap), frozenset(members)) for name, cap, members in self.groups}

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()


def daily_returns(rows, sessions=None):
    """[(date, close), ...] -> {date: simple return}, consecutive completed sessions only.

    With `sessions` (every session date seen across the universe), a pair of rows
    that skips a session another symbol traded is not a one-day return and is
    dropped, instead of entering the scenarios as a two-day move.
    """
    ordered = sorted((str(day), float(close)) for day, close in rows)
    previous_session = None
    if sessions is not None:
        sessions = sorted(set(map(str, sessions)))
        previous_session = dict(zip(sessions[1:], sessions))
    result = {}
    for (before, previous), (day, close) in zip(ordered, ordered[1:]):
        if previous_session is not None and previous_session.get(day) != before:
            continue
        if previous > 0 and close > 0 and math.isfinite(previous) and math.isfinite(close):
            result[day] = close / previous - 1
    return result


class RiskEstimate:
    """EWMA volatilities, shrunk EWMA correlations, historical return scenarios and stress windows.

    Volatility uses the fast `ewma_lambda` over `vol_lookback_days`. Correlation
    uses the slower `correlation_lambda` over `correlation_lookback_days`, then is
    shrunk toward the average pairwise correlation by `correlation_shrinkage`.
    A pair involving a flat series stays perfectly correlated (conservative) and
    is excluded from the shrinkage target. `scenarios` maps a stress-window name
    to {"benchmark": return or None, "returns": {symbol: return or None}}.
    """

    def __init__(self, histories, policy, scenarios=None):
        self.policy = policy
        self.benchmark = policy.stress_benchmark
        self.scenarios = dict(scenarios or {})
        sessions = {str(day) for rows in histories.values() for day, _ in rows}
        returns = {symbol: daily_returns(rows, sessions) for symbol, rows in histories.items()}
        usable = {s for s, r in returns.items() if len(r) >= policy.min_history_days}
        # Drop the shortest histories until the common window is long enough.
        while usable:
            common = sorted(set.intersection(*(set(returns[s]) for s in usable)))
            if len(common) >= policy.min_history_days:
                break
            usable.remove(min(usable, key=lambda s: (len(returns[s]), s)))
        else:
            common = []
        self.symbols = tuple(sorted(usable))
        self.missing = frozenset(histories) - frozenset(self.symbols)
        self.observations = len(common)
        self.through = common[-1] if common else None
        if not self.symbols:
            self.raw_vol, self.vol, self.cov = {}, {}, np.zeros((0, 0))
            self.correlation, self.scenarios_history = np.zeros((0, 0)), np.zeros((0, 0))
            return
        matrix = np.array([[returns[s][day] for s in self.symbols] for day in common], dtype=float)
        self.scenarios_history = matrix[-policy.es_lookback_days:]
        raw = np.sqrt(np.clip(np.diag(_ewma_cov(matrix[-policy.vol_lookback_days:], policy.ewma_lambda)), 0, None)
                      * TRADING_DAYS)
        vol = np.maximum(raw, float(policy.min_annual_vol))
        self.correlation = _shrunk_correlation(_ewma_cov(matrix[-policy.correlation_lookback_days:],
                                                         policy.correlation_lambda),
                                               float(policy.correlation_shrinkage))
        self.cov = np.outer(vol, vol) * self.correlation
        self.raw_vol = dict(zip(self.symbols, raw.tolist()))
        self.vol = dict(zip(self.symbols, vol.tolist()))

    def annual_vol(self, symbol):
        return self.vol.get(symbol, float(self.policy.unknown_annual_vol))

    def beta(self, symbol):
        """Ex-ante beta to the stress benchmark, or None when either has no estimate."""
        if symbol == self.benchmark:
            return 1.0
        if symbol not in self.symbols or self.benchmark not in self.symbols:
            return None
        i, b = self.symbols.index(symbol), self.symbols.index(self.benchmark)
        return float(self.cov[i, b] / self.cov[b, b]) if self.cov[b, b] > 0 else None

    def _scenario_return(self, name, symbol):
        """(return, proxied). A missing window is never milder than the benchmark's own loss."""
        scenario = self.scenarios[name]
        value = (scenario.get("returns") or {}).get(symbol)
        if value is not None:
            return float(value), False
        market = float(scenario["benchmark"])
        if market >= 0:
            return 0.0, True
        beta = self.beta(symbol)
        if beta is None:
            # No history at all: the same pessimism as its volatility assumption.
            beta = float(self.policy.unknown_annual_vol) / max(self.annual_vol(self.benchmark), 1e-9)
        return market * max(1.0, beta), True

    def scenario_losses(self, weights):
        losses, proxied, unavailable = {}, {}, []
        for name in sorted(self.scenarios):
            if self.scenarios[name].get("benchmark") is None:
                unavailable.append(name)
                continue
            total, used = 0.0, []
            for symbol, weight in weights.items():
                value, proxy = self._scenario_return(name, symbol)
                total += weight * value
                if proxy:
                    used.append(symbol)
            losses[name] = -total
            if used:
                proxied[name] = sorted(used)
        return losses, proxied, unavailable

    def metrics(self, weights):
        """Ex-ante annual volatility, 1-day 97.5% ES and stress-window losses as fractions of equity."""
        weights = {s: float(w) for s, w in weights.items() if float(w) > 0}
        known = [s for s in self.symbols if s in weights]
        unknown = [s for s in weights if s not in self.vol]
        w = np.array([weights[s] for s in known])
        index = [self.symbols.index(s) for s in known]
        variance = float(w @ self.cov[np.ix_(index, index)] @ w) if known else 0.0
        if unknown:
            # Unknown assets: pessimistic volatility, perfectly correlated with
            # everything else, i.e. their risk adds linearly to the known risk.
            sigma_known = math.sqrt(max(variance, 0.0))
            variance = (sigma_known + sum(weights[s] for s in unknown) * float(self.policy.unknown_annual_vol)) ** 2
        vol = math.sqrt(max(variance, 0.0))
        parametric = vol / math.sqrt(TRADING_DAYS) * NORMAL_ES_975
        historical = None
        if known and len(self.scenarios_history) >= self.policy.es_min_observations:
            pnl = np.sort(self.scenarios_history[:, index] @ w)
            tail = max(1, math.ceil(0.025 * len(pnl)))
            historical = float(-pnl[:tail].mean())
            if unknown:
                historical += sum(weights[s] for s in unknown) * float(self.policy.unknown_annual_vol) / math.sqrt(
                    TRADING_DAYS) * NORMAL_ES_975
        es = max(parametric, historical if historical is not None else 0.0)
        losses, proxied, unavailable = self.scenario_losses(weights)
        worst = max(losses, key=losses.get) if losses else None
        return {"annual_vol": vol, "es_975_1d": es, "es_parametric": parametric, "es_historical": historical,
                "gross_exposure": sum(weights.values()), "scenario_losses": losses,
                "worst_scenario": worst, "worst_scenario_loss": losses[worst] if worst else None,
                "scenario_proxied": proxied, "scenarios_unavailable": unavailable}


def _ewma_cov(returns, lam):
    """Exponentially weighted (uncentered) daily covariance of the rows of `returns`, newest last."""
    lam = float(lam)
    weights = np.array([(1 - lam) * lam ** k for k in range(len(returns))][::-1])
    weights /= weights.sum()
    return (returns * weights[:, None]).T @ returns


def _shrunk_correlation(cov, shrinkage):
    """Correlation from `cov`, shrunk toward the average defined pairwise correlation.

    Undefined pairs (a flat series) are set to 1 and kept there. A convex mix of
    a correlation matrix and a constant-correlation matrix stays positive semidefinite.
    """
    n = len(cov)
    sd = np.sqrt(np.clip(np.diag(cov), 0, None))
    corr = np.ones((n, n))
    defined = np.zeros((n, n), dtype=bool)
    for i in range(n):
        for j in range(n):
            if i != j and sd[i] > 0 and sd[j] > 0:
                corr[i, j] = np.clip(cov[i, j] / (sd[i] * sd[j]), -1, 1)
                defined[i, j] = True
    if shrinkage > 0 and defined.any():
        target = float(corr[defined].mean())
        corr = np.where(defined, (1 - shrinkage) * corr + shrinkage * target, corr)
    return corr


def drawdown_scale(drawdown, policy, hard):
    """Exposure budget multiplier: 1 until the soft level, linear to the floor at the hard latch."""
    drawdown, hard = number(drawdown), number(hard)
    if drawdown <= policy.drawdown_soft:
        return Decimal("1")
    if drawdown >= hard:
        return policy.drawdown_floor_scale
    span = (drawdown - policy.drawdown_soft) / (hard - policy.drawdown_soft)
    return Decimal("1") - (Decimal("1") - policy.drawdown_floor_scale) * span


def stress_state(rows, last_price, policy):
    """Market stress from the benchmark's completed history and current price; None = unknown."""
    returns = daily_returns(rows)
    if len(returns) < policy.min_history_days:
        return {"stressed": None, "reason": "insufficient_benchmark_history"}
    ordered = [returns[day] for day in sorted(returns)]
    recent = np.array(ordered[-policy.vol_lookback_days:])
    lam = float(policy.ewma_lambda)
    weights = np.array([(1 - lam) * lam ** k for k in range(len(recent))][::-1])
    vol = math.sqrt(float((weights / weights.sum()) @ (recent ** 2)) * TRADING_DAYS)
    last_close = float(sorted((str(d), float(c)) for d, c in rows)[-1][1])
    intraday = float(last_price) / last_close - 1 if last_price and last_close > 0 else None
    reasons = []
    if ordered[-1] <= -float(policy.stress_day_drop):
        reasons.append("benchmark_prior_session_drop")
    if intraday is not None and intraday <= -float(policy.stress_day_drop):
        reasons.append("benchmark_intraday_drop")
    if vol >= float(policy.stress_annual_vol):
        reasons.append("benchmark_volatility")
    return {"stressed": bool(reasons), "reasons": reasons, "prior_session_return": ordered[-1],
            "intraday_return": intraday, "ewma_annual_vol": vol}


def _floor(value):
    return Decimal(str(max(0.0, float(value)))).quantize(WEIGHT_STEP, rounding=ROUND_FLOOR)


def construct(targets, current, estimate, policy, limits, exposure_budget):
    """Project AI targets onto every quantitative limit by reductions only.

    targets/current: {symbol: weight of equity}. A symbol without usable history
    may be held or reduced, never increased above its current weight.
    """
    weights = {s: float(w) for s, w in targets.items() if float(w) > 0}
    binding = []
    for symbol in list(weights):
        if symbol in estimate.missing or symbol not in estimate.vol:
            allowed = min(weights[symbol], float(current.get(symbol, 0)))
            if allowed < weights[symbol]:
                binding.append(f"missing_history:{symbol}")
            weights[symbol] = allowed
            continue
        cap = min(float(limits.max_position), float(policy.name_risk(symbol)) / estimate.annual_vol(symbol))
        if weights[symbol] > cap:
            weights[symbol] = cap
            binding.append(f"name_risk:{symbol}")
    for _ in range(20):  # Overlapping groups converge: each pass only reduces.
        changed = False
        for name, cap, members in policy.groups:
            total = sum(weights.get(s, 0) for s in members)
            if total > float(cap) + 1e-12:
                factor = float(cap) / total
                for s in members:
                    if s in weights:
                        weights[s] *= factor
                changed = True
                if f"group:{name}" not in binding:
                    binding.append(f"group:{name}")
        if not changed:
            break
    budget = min(float(exposure_budget), float(limits.max_exposure))
    total = sum(weights.values())
    if total > budget:
        weights = {s: w * budget / total for s, w in weights.items()}
        binding.append("exposure_budget")
    before = estimate.metrics(weights)
    scale = 1.0
    if before["annual_vol"] > float(policy.target_portfolio_vol):
        scale = min(scale, float(policy.target_portfolio_vol) / before["annual_vol"])
        binding.append("volatility_target")
    if before["es_975_1d"] > float(policy.max_portfolio_es):
        scale = min(scale, float(policy.max_portfolio_es) / before["es_975_1d"])
        binding.append("expected_shortfall")
    worst = before["worst_scenario_loss"]
    if worst is not None and worst > float(policy.max_scenario_loss):
        # Losses are linear in the weights, so one uniform scale meets the limit exactly.
        scale = min(scale, float(policy.max_scenario_loss) / worst)
        binding.append("scenario_stress")
    result = {s: _floor(w * scale) for s, w in weights.items()}
    result = {s: w for s, w in result.items() if w > 0}
    after = estimate.metrics(result)
    # Independent post-condition: never return a portfolio outside the limits.
    tolerance = 1e-9
    if (after["annual_vol"] > float(policy.target_portfolio_vol) + tolerance or
            after["es_975_1d"] > float(policy.max_portfolio_es) + tolerance or
            # Flooring a name that GAINED in a window can add at most one weight step of loss per name.
            (after["worst_scenario_loss"] or 0) > float(policy.max_scenario_loss) + tolerance + len(result) * float(
                WEIGHT_STEP) or
            sum(result.values()) > number(budget) + Decimal("0.000001")):
        raise RiskError("Quantitative construction failed its own post-condition")
    for name, cap, members in policy.groups:
        if sum(result.get(s, 0) for s in members) > Decimal(cap) + Decimal("0.000001"):
            raise RiskError("Quantitative construction failed its group post-condition")
    return result, {
        "requested": {s: str(w) for s, w in targets.items()},
        "constructed": {s: str(w) for s, w in result.items()},
        "binding": binding, "scale": scale, "exposure_budget": budget,
        "requested_metrics": estimate.metrics({s: float(w) for s, w in targets.items()}),
        "constructed_metrics": after,
        "name_caps": {s: min(float(limits.max_position), float(policy.name_risk(s)) / estimate.annual_vol(s))
                      for s in sorted(set(targets) | set(current)) if s in estimate.vol},
        "missing_history": sorted(estimate.missing & (set(targets) | set(current))),
        "observations": estimate.observations, "history_through": estimate.through,
    }


def risk_budget(targets, estimate, iterations=500, tolerance=1e-10):
    """Re-weight the AI's names so each one's risk contribution is proportional to its AI weight.

    The AI's weights are treated as risk budgets b (selection and conviction), the
    gross exposure is kept, and the risk engine's limits still apply afterwards.
    Solved by cyclical coordinate descent on min 1/2 x'Cx - sum(b ln x), whose
    unique positive solution has risk contributions x_i (Cx)_i proportional to b_i
    even with negative correlations. Names without a covariance estimate keep the
    AI's weight. Returns Decimal weights rounded down to the weight step.
    """
    positive = {s: float(w) for s, w in targets.items() if float(w) > 0}
    names = [s for s in sorted(positive) if s in estimate.vol]
    if len(names) < 2:
        return {s: number(w) for s, w in targets.items()}
    index = [estimate.symbols.index(s) for s in names]
    cov = estimate.cov[np.ix_(index, index)]
    budgets = np.array([positive[s] for s in names])
    budgets = budgets / budgets.sum()
    x = budgets / np.sqrt(np.diag(cov))
    for _ in range(iterations):
        previous = x.copy()
        for i in range(len(names)):
            other = float(cov[i] @ x - cov[i, i] * x[i])
            x[i] = (-other + math.sqrt(other * other + 4 * cov[i, i] * budgets[i])) / (2 * cov[i, i])
        if np.max(np.abs(x - previous) / np.maximum(previous, 1e-12)) < tolerance:
            break
    gross = sum(positive[s] for s in names)
    x = x / x.sum() * gross
    result = {s: number(w) for s, w in targets.items() if s not in names}
    result.update({s: _floor(value) for s, value in zip(names, x)})
    return result


def risk_contributions(weights, estimate):
    """Fraction of ex-ante portfolio variance from each name (known names only)."""
    names = [s for s in sorted(weights) if float(weights[s]) > 0 and s in estimate.vol]
    if not names:
        return {}
    index = [estimate.symbols.index(s) for s in names]
    w = np.array([float(weights[s]) for s in names])
    marginal = estimate.cov[np.ix_(index, index)] @ w
    total = float(w @ marginal)
    return {s: float(w[i] * marginal[i] / total) for i, s in enumerate(names)} if total > 0 else {}
