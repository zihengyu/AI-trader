"""Is the track record skill or luck? Professional performance statistics for the acceptance report.

All from the journal's own daily account marks (the last equity of each New York
session) and the benchmark marks recorded with them; nothing is estimated for a
missing day. Following Bailey and Lopez de Prado:

- probabilistic Sharpe ratio (PSR): probability that the true Sharpe ratio
  exceeds a threshold, given the sample length, skewness and kurtosis;
- deflated Sharpe ratio (DSR): the same against the best Sharpe ratio expected
  from luck alone after N configurations were tried (prompt versions x risk
  policies that traded), so trying many variants raises the bar;
- minimum track record length (MinTRL): sessions needed before the observed
  Sharpe ratio is significant at 95%;
- beta-adjusted alpha: OLS of daily returns on the benchmark, with the t
  statistic of the intercept.

Benchmarks are defined before the fact (`performance.benchmarks`): each has an
id that includes a hash of its definition, so changing a definition creates a
new benchmark and the old one keeps being reported, never replaced. Their
returns are total returns from the journal's price marks plus the dividends and
splits recorded for them (`benchmark_action`); without those records they are
price returns and say so. Returns come in two kinds: the account's (after its
own fees and slippage, which the equity already contains) and the system's
(also after the model and data costs). A cost that is not known leaves the
system return unknown, never zero.

Statistics also include the Sortino ratio, maximum drawdown, alpha t
statistics with Newey-West (HAC) standard errors, and the Sharpe ratio adjusted
for return autocorrelation (Lo 2002), each with its sample length.

`capital_stage` turns these into a staged-capital readiness verdict. It is a
report only: this application has no live-trading mode, and any real-money step
needs the operator's explicit authorization.
"""
import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from statistics import NormalDist

from .risk import RiskError

EULER = 0.5772156649
Z95 = NormalDist().inv_cdf(0.95)


def moments(returns):
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return mean, 0.0, 0.0, 3.0
    skew = sum(((r - mean) / std) ** 3 for r in returns) / n
    kurt = sum(((r - mean) / std) ** 4 for r in returns) / n
    return mean, std, skew, kurt


def _sr_variance_factor(sr, skew, kurt):
    return max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr * sr)


def probabilistic_sharpe(returns, threshold=0.0):
    """P(true per-session Sharpe > threshold); None with fewer than 3 returns or no variation."""
    if len(returns) < 3:
        return None
    mean, std, skew, kurt = moments(returns)
    if std == 0:
        return None
    sr = mean / std
    return NormalDist().cdf((sr - threshold) * math.sqrt(len(returns) - 1) / math.sqrt(
        _sr_variance_factor(sr, skew, kurt)))


def expected_max_sharpe(returns, trials):
    """Per-session Sharpe expected from the best of `trials` skill-less configurations."""
    if trials <= 1 or len(returns) < 3:
        return 0.0
    mean, std, skew, kurt = moments(returns)
    sr = mean / std if std else 0.0
    sigma = math.sqrt(_sr_variance_factor(sr, skew, kurt) / (len(returns) - 1))
    normal = NormalDist()
    return sigma * ((1 - EULER) * normal.inv_cdf(1 - 1 / trials) + EULER * normal.inv_cdf(1 - 1 / (trials * math.e)))


def deflated_sharpe(returns, trials):
    return probabilistic_sharpe(returns, expected_max_sharpe(returns, trials))


def min_track_record(returns, threshold=0.0, confidence=0.95):
    """Sessions needed for the observed Sharpe to beat `threshold` at `confidence`; None if it does not exceed it."""
    if len(returns) < 3:
        return None
    mean, std, skew, kurt = moments(returns)
    if std == 0 or mean / std <= threshold:
        return None
    sr = mean / std
    z = NormalDist().inv_cdf(confidence)
    return math.ceil(1 + _sr_variance_factor(sr, skew, kurt) * (z / (sr - threshold)) ** 2)


def alpha_beta(returns, market):
    """OLS returns = alpha + beta x market; alpha per session, its t statistic and beta. None if unidentifiable."""
    n = len(returns)
    if n < 5 or len(market) != n:
        return None
    mx, my = sum(market) / n, sum(returns) / n
    sxx = sum((m - mx) ** 2 for m in market)
    if sxx == 0:
        return None
    beta = sum((m - mx) * (r - my) for m, r in zip(market, returns)) / sxx
    alpha = my - beta * mx
    residual = sum((r - alpha - beta * m) ** 2 for m, r in zip(market, returns)) / (n - 2)
    se = math.sqrt(residual * (1 / n + mx * mx / sxx)) if residual > 0 else 0.0
    return {"alpha_per_session": alpha, "alpha_annualized": alpha * 252, "beta": beta,
            "alpha_t": alpha / se if se > 0 else None, "observations": n}


def evidence(returns, market, trials):
    """Everything the acceptance report shows about whether the record shows skill."""
    if len(returns) < 3:
        return {"sessions": len(returns), "status": "insufficient_sample"}
    mean, std, skew, kurt = moments(returns)
    sr = mean / std if std else None
    regression = regression_hac(returns, market) if market is not None and len(market) == len(returns) else None
    path = path_statistics(returns)
    return {"sessions": len(returns), "mean_return": mean, "volatility_annualized": std * math.sqrt(252),
            **{key: path[key] for key in ("annual_return", "max_drawdown", "sharpe_lo_adjusted", "sortino_annualized",
                                          "autocorrelation_lag1")},
            "sharpe_annualized": sr * math.sqrt(252) if sr is not None else None,
            "skewness": skew, "kurtosis": kurt, "configurations_tried": trials,
            "probabilistic_sharpe": probabilistic_sharpe(returns),
            "deflated_sharpe": deflated_sharpe(returns, trials),
            "min_track_record_sessions": min_track_record(returns),
            "benchmark_regression": regression, "status": "computed",
            "meaning": "probabilistic_sharpe: chance the true Sharpe ratio is above 0; deflated_sharpe: the same "
                       "after the best-of-N luck of the configurations tried; min_track_record_sessions: sessions "
                       "needed for 95% confidence at the observed Sharpe."}


# ----- Predefined benchmarks and net returns ----------------------------------------------------

DEFAULT_BENCHMARKS = {"spy": {"SPY": "1"}, "risk_matched": {"SPY": "0.6", "SGOV": "0.4"}}
SESSIONS_PER_MONTH = 252 / 12


def benchmark_id(name, weights):
    """The name plus a hash of the definition: a changed definition is a different benchmark."""
    canonical = json.dumps({symbol: format(Decimal(str(weight)).normalize(), "f")
                            for symbol, weight in sorted(weights.items())}, sort_keys=True)
    return f"{name}-{hashlib.sha256(canonical.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class PerformanceSettings:
    """benchmarks: {name: {symbol: weight}}, weights summing to 1, rebalanced every session.
    data_costs_usd_per_month: paid data subscriptions charged to the system return; null = unknown."""
    benchmarks: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_BENCHMARKS.items()})
    data_costs_usd_per_month: str | None = "0"

    def __post_init__(self):
        if not isinstance(self.benchmarks, dict) or not 1 <= len(self.benchmarks) <= 6:
            raise RiskError("performance.benchmarks must define 1..6 benchmarks")
        clean = {}
        for name, weights in self.benchmarks.items():
            if (not isinstance(name, str) or not 1 <= len(name) <= 30 or
                    not all(c.islower() or c.isdigit() or c == "_" for c in name)):
                raise RiskError("performance.benchmarks names must be lower-case letters, digits or _")
            if not isinstance(weights, dict) or not 1 <= len(weights) <= 5:
                raise RiskError(f"performance.benchmarks.{name} must map 1..5 symbols to weights")
            try:
                parsed = {symbol: Decimal(str(weight)) for symbol, weight in weights.items()}
            except InvalidOperation:
                raise RiskError(f"performance.benchmarks.{name} weights must be numbers") from None
            if any(not isinstance(symbol, str) or not symbol or len(symbol) > 10 or symbol != symbol.strip().upper()
                   for symbol in parsed) or any(not weight.is_finite() or weight <= 0 for weight in parsed.values()):
                raise RiskError(f"performance.benchmarks.{name} needs upper-case symbols with positive weights")
            if sum(parsed.values()) != 1:
                raise RiskError(f"performance.benchmarks.{name} weights must sum to 1")
            clean[name] = {symbol: format(weight.normalize(), "f") for symbol, weight in parsed.items()}
        object.__setattr__(self, "benchmarks", clean)
        cost = self.data_costs_usd_per_month
        if cost is not None:
            try:
                value = Decimal(str(cost))
            except InvalidOperation:
                value = Decimal(-1)
            if not value.is_finite() or value < 0:
                raise RiskError("performance.data_costs_usd_per_month must be a non-negative number or null")
            object.__setattr__(self, "data_costs_usd_per_month", format(value.normalize(), "f"))

    def definitions(self):
        return [{"id": benchmark_id(name, weights), "name": name, "weights": weights}
                for name, weights in self.benchmarks.items()]

    def symbols(self):
        return sorted({symbol for weights in self.benchmarks.values() for symbol in weights})

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown performance settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def component_return(prices, actions, a, b):
    """Total return of one symbol from session a's mark to session b's: dividends with an ex-date in (a, b]
    are added, a split in (a, b] rescales the earlier price. None without both marks."""
    if a not in prices or b not in prices or prices[a] <= 0:
        return None
    start, paid = prices[a], 0.0
    for action in actions:
        if a < action["date"] <= b:
            if action.get("split"):
                start /= float(action["split"])
            paid += float(action.get("dividend") or 0)
    return (prices[b] + paid) / start - 1


def benchmark_series(definition, pairs, marks, actions):
    """Per-pair returns of a daily rebalanced benchmark; None where a component mark is missing."""
    out = []
    for a, b in pairs:
        parts = [(float(w), component_return(marks.get(s, {}), actions.get(s, []), a, b))
                 for s, w in definition["weights"].items()]
        out.append(None if any(r is None for _, r in parts) else sum(w * r for w, r in parts))
    return out


def newey_west_lags(n):
    return max(1, int(4 * (n / 100) ** (2 / 9)))


def regression_hac(returns, market, lags=None):
    """OLS returns = alpha + beta x market with ordinary and Newey-West (Bartlett) alpha t statistics."""
    n = len(returns)
    base = alpha_beta(returns, market)
    if base is None:
        return None
    lags = newey_west_lags(n) if lags is None else lags
    alpha, beta = base["alpha_per_session"], base["beta"]
    errors = [r - alpha - beta * m for r, m in zip(returns, market)]
    xtx = [[n, sum(market)], [sum(market), sum(m * m for m in market)]]
    meat = [[0.0, 0.0], [0.0, 0.0]]
    rows = [(1.0, m) for m in market]
    for lag in range(0, lags + 1):
        weight = 1.0 if lag == 0 else 1 - lag / (lags + 1)  # Bartlett kernel.
        for t in range(lag, n):
            u = errors[t] * errors[t - lag]
            x, y = rows[t], rows[t - lag]
            for i in range(2):
                for j in range(2):
                    # Lag 0: e^2 x x'; later lags: e_t e_{t-l} (x_t x_{t-l}' + x_{t-l} x_t').
                    meat[i][j] += weight * u * (x[i] * y[j] + (x[j] * y[i] if lag else 0))
    det = xtx[0][0] * xtx[1][1] - xtx[0][1] * xtx[1][0]
    if det <= 0:
        return {**base, "alpha_t_newey_west": None, "newey_west_lags": lags}
    inv = [[xtx[1][1] / det, -xtx[0][1] / det], [-xtx[1][0] / det, xtx[0][0] / det]]
    left = [[sum(inv[i][k] * meat[k][j] for k in range(2)) for j in range(2)] for i in range(2)]
    cov = [[sum(left[i][k] * inv[k][j] for k in range(2)) for j in range(2)] for i in range(2)]
    variance = cov[0][0]
    return {**base, "alpha_t_newey_west": alpha / math.sqrt(variance) if variance > 0 else None,
            "newey_west_lags": lags}


def autocorrelation(returns, lag):
    n = len(returns)
    mean = sum(returns) / n
    denominator = sum((r - mean) ** 2 for r in returns)
    if lag >= n or denominator == 0:
        return 0.0
    return sum((returns[t] - mean) * (returns[t - lag] - mean) for t in range(lag, n)) / denominator


def lo_sharpe(returns, periods=252, lags=None):
    """Annualized Sharpe ratio with Lo's (2002) autocorrelation correction instead of x sqrt(periods).

    Autocorrelations through `lags` (default: the Newey-West lag) are estimated; higher
    ones are taken as zero. Positive autocorrelation lowers the ratio.
    """
    if len(returns) < 3:
        return None
    mean, std, _, _ = moments(returns)
    if std == 0:
        return None
    lags = min(periods - 1, newey_west_lags(len(returns)) if lags is None else lags)
    inflation = periods + 2 * sum((periods - k) * autocorrelation(returns, k) for k in range(1, lags + 1))
    if inflation <= 0:
        return None
    return mean / std * periods / math.sqrt(inflation)


def path_statistics(returns):
    """Annualized return and volatility, Sharpe (naive and Lo-adjusted), Sortino and maximum drawdown."""
    n = len(returns)
    if n < 3:
        return {"sessions": n, "status": "insufficient_sample"}
    mean, std, _, _ = moments(returns)
    growth, peak, worst, level = 1.0, 1.0, 0.0, 1.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
        growth *= 1 + r
    downside = math.sqrt(sum(min(r, 0) ** 2 for r in returns) / n)
    return {"sessions": n, "status": "computed", "annual_return": growth ** (252 / n) - 1 if growth > 0 else -1.0,
            "annual_volatility": std * math.sqrt(252), "max_drawdown": worst,
            "sharpe_annualized": mean / std * math.sqrt(252) if std else None,
            "sharpe_lo_adjusted": lo_sharpe(returns),
            "sortino_annualized": mean / downside * math.sqrt(252) if downside else None,
            "autocorrelation_lag1": autocorrelation(returns, 1)}


def net_returns(pairs, model_costs, data_cost_per_session):
    """Account and system returns for each session pair.

    pairs: [{"return", "equity_start", ...}]; model_costs: per pair, USD or None when a
    call in the interval was unpriced; data_cost_per_session: USD or None (unknown).
    A system return with any unknown cost is None, never the account return.
    """
    account = [p["return"] for p in pairs]
    system, unknown = [], 0
    for pair, cost in zip(pairs, model_costs):
        if cost is None or data_cost_per_session is None:
            system.append(None)
            unknown += 1
        else:
            system.append(pair["return"] - (cost + data_cost_per_session) / pair["equity_start"])
    known = [r for r in system if r is not None]
    return {"account": {**path_statistics(account), "meaning": "after the account's own fees and slippage"},
            "system": ({**path_statistics(known), "meaning": "after fees, slippage, model and data costs"}
                       if not unknown else {"status": "unknown_costs", "sessions_with_unknown_costs": unknown,
                                            "meaning": "a model call was unpriced or data costs are unknown; "
                                                       "the system return is not estimated"}),
            "costs_usd": {"model": sum(c for c in model_costs if c is not None),
                          "data": data_cost_per_session * len(pairs) if data_cost_per_session is not None else None,
                          "complete": not unknown}}


def benchmark_comparison(definition, returns, series, current):
    """Excess return, tracking error, information ratio and alpha (OLS and Newey-West t) against one benchmark."""
    both = [(r, b) for r, b in zip(returns, series) if b is not None]
    result = {"id": definition["id"], "name": definition["name"], "weights": definition["weights"],
              "current": current, "sessions": len(both), "sessions_missing_marks": len(series) - len(both)}
    if len(both) < 3:
        return {**result, "status": "insufficient_sample"}
    mine, theirs = [r for r, _ in both], [b for _, b in both]
    excess = [r - b for r, b in both]
    _, tracking, _, _ = moments(excess)
    own, base = path_statistics(mine), path_statistics(theirs)
    return {**result, "status": "computed", "benchmark": base,
            "annual_excess_return": own["annual_return"] - base["annual_return"],
            "tracking_error": tracking * math.sqrt(252),
            "information_ratio": (sum(excess) / len(excess)) / tracking * math.sqrt(252) if tracking else None,
            "regression": regression_hac(mine, theirs)}


def capital_stage(operational_verdict, stats, max_drawdown, drawdown_limit):
    """Staged-capital readiness. A report only: no live-trading path exists in this application."""
    reasons = []
    if operational_verdict != "ready":
        reasons.append("operational acceptance not passed")
    if stats.get("status") != "computed":
        reasons.append("too few sessions for statistics")
    else:
        sessions, needed = stats["sessions"], stats["min_track_record_sessions"]
        if sessions < 60:
            reasons.append(f"{sessions} sessions observed; at least 60 required")
        if needed is None:
            reasons.append("the observed Sharpe ratio is not positive")
        elif sessions < needed:
            reasons.append(f"minimum track record is {needed} sessions; {sessions} observed")
        if (stats["probabilistic_sharpe"] or 0) < 0.95:
            reasons.append("probabilistic Sharpe below 0.95")
        if (stats["deflated_sharpe"] or 0) < 0.90:
            reasons.append("deflated Sharpe below 0.90 after the configurations tried")
        regression = stats.get("benchmark_regression") or {}
        # The smaller of the ordinary and the autocorrelation-robust t: the bar is never loosened.
        values = [t for t in (regression.get("alpha_t"), regression.get("alpha_t_newey_west")) if t is not None]
        if (min(values) if values else 0) < Z95:
            reasons.append(f"beta-adjusted alpha t below {Z95:.2f} (one-sided 95%, ordinary and Newey-West)")
    if max_drawdown is not None and drawdown_limit is not None and max_drawdown >= drawdown_limit:
        reasons.append("the drawdown limit was reached")
    stage = "paper_only" if reasons else "eligible_stage_1"
    return {"stage": stage, "reasons": reasons,
            "plan": ["paper_only: keep paper trading and collecting evidence",
                     "eligible_stage_1: at most 10% of the intended capital, after explicit operator authorization",
                     "stage_2 (25%) and stage_3 (50%) need a live record meeting the same tests; not available here"],
            "notice": "Report only. This application has no live-trading mode; real money needs explicit operator "
                      "authorization and a separate, reviewed execution path."}
