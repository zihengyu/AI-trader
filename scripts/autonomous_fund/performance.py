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

`capital_stage` turns these into a staged-capital readiness verdict. It is a
report only: this application has no live-trading mode, and any real-money step
needs the operator's explicit authorization.
"""
import math
from statistics import NormalDist

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
    regression = alpha_beta(returns, market) if market is not None and len(market) == len(returns) else None
    return {"sessions": len(returns), "mean_return": mean, "volatility_annualized": std * math.sqrt(252),
            "sharpe_annualized": sr * math.sqrt(252) if sr is not None else None,
            "skewness": skew, "kurtosis": kurt, "configurations_tried": trials,
            "probabilistic_sharpe": probabilistic_sharpe(returns),
            "deflated_sharpe": deflated_sharpe(returns, trials),
            "min_track_record_sessions": min_track_record(returns),
            "benchmark_regression": regression, "status": "computed",
            "meaning": "probabilistic_sharpe: chance the true Sharpe ratio is above 0; deflated_sharpe: the same "
                       "after the best-of-N luck of the configurations tried; min_track_record_sessions: sessions "
                       "needed for 95% confidence at the observed Sharpe."}


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
        if (regression.get("alpha_t") or 0) < Z95:
            reasons.append(f"beta-adjusted alpha t below {Z95:.2f} (one-sided 95%)")
    if max_drawdown is not None and drawdown_limit is not None and max_drawdown >= drawdown_limit:
        reasons.append("the drawdown limit was reached")
    stage = "paper_only" if reasons else "eligible_stage_1"
    return {"stage": stage, "reasons": reasons,
            "plan": ["paper_only: keep paper trading and collecting evidence",
                     "eligible_stage_1: at most 10% of the intended capital, after explicit operator authorization",
                     "stage_2 (25%) and stage_3 (50%) need a live record meeting the same tests; not available here"],
            "notice": "Report only. This application has no live-trading mode; real money needs explicit operator "
                      "authorization and a separate, reviewed execution path."}
