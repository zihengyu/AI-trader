"""Is today's market outside the history the fund's estimates were made from?

The risk model's expected shortfall and correlations come from the last
`es_lookback_days` sessions; the scorecards and factor tests from the history
the fund holds (about two years). When the market moves past everything in
that history, those estimates describe a calmer market than today's and likely
understate risk. This compares the latest completed session with that history:

- benchmark_vol_20d: annualized volatility of the benchmark's last 20 daily returns
  (higher than every earlier 20-session stretch = beyond);
- benchmark_return_1d: the benchmark's latest daily return (a larger fall or rise
  than any earlier day = beyond);
- benchmark_drawdown: the benchmark's decline from its highest close in the history
  (deeper than any earlier point = beyond);
- stock_mean_correlation_20d: mean pairwise correlation of the tradable stocks'
  last 20 daily returns, among names with all 20 (higher than every earlier
  stretch = beyond). Correlations near their highs mean diversification is failing.

Each metric is compared with the earlier values inside the risk model's window
(`beyond_risk_window`) and with all earlier values in the history (`beyond_history`).
Context and a notice for the operator and the AI: it never changes a limit or an order.
"""
from __future__ import annotations

from math import isfinite, sqrt

WINDOW = 20
MIN_PRIOR = 60
MIN_NAMES = 10
DIRECTIONS = {"benchmark_vol_20d": "high", "benchmark_return_1d": "both", "benchmark_drawdown": "low",
              "stock_mean_correlation_20d": "high"}


def _returns(closes):
    return [b / a - 1 for a, b in zip(closes, closes[1:])]


def _std(values):
    mean = sum(values) / len(values)
    return sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _benchmark_series(rows):
    """{metric: [(day, value)]} from ascending completed (day, close) rows."""
    days = [str(d) for d, _ in rows]
    closes = [float(c) for _, c in rows]
    returns = _returns(closes)  # returns[i] is the move into days[i + 1].
    vol = [(days[i + 1], _std(returns[i - WINDOW + 1:i + 1]) * sqrt(252))
           for i in range(WINDOW - 1, len(returns))]
    peak, drawdown = 0.0, []
    for day, close in zip(days, closes):
        peak = max(peak, close)
        drawdown.append((day, close / peak - 1))
    return {"benchmark_vol_20d": vol, "benchmark_return_1d": list(zip(days[1:], returns)),
            "benchmark_drawdown": drawdown}


def _correlation_series(stocks, days):
    """Mean pairwise correlation over each 20-return stretch ending on a benchmark day.

    For returns standardized within the stretch, var(sum) = n + sum of the off-diagonal
    correlations, so the mean pairwise correlation is (var(sum) - n) / (n (n - 1)):
    exact, and linear in the number of names.
    """
    closes = {s: {str(d): float(c) for d, c in rows} for s, rows in (stocks or {}).items()}
    series = []
    for end in range(WINDOW, len(days)):
        stretch = days[end - WINDOW:end + 1]
        standardized = []
        for by_day in closes.values():
            if not all(day in by_day for day in stretch):
                continue
            moves = _returns([by_day[day] for day in stretch])
            spread = _std(moves)
            if not spread:
                continue
            mean = sum(moves) / len(moves)
            standardized.append([(m - mean) / spread for m in moves])
        n = len(standardized)
        if n < MIN_NAMES:
            continue
        total = [sum(column) for column in zip(*standardized)]
        series.append((days[end], (_std(total) ** 2 - n) / (n * (n - 1))))
    return series


def _compare(value, prior, direction):
    if len(prior) < MIN_PRIOR:
        return None
    low, high = min(prior), max(prior)
    beyond = {"high": value > high, "low": value < low, "both": value > high or value < low}[direction]
    return {"sessions": len(prior), "min": low, "max": high,
            "percentile": round(100 * sum(v <= value for v in prior) / len(prior), 2), "beyond": beyond}


def assess(benchmark, stocks=None, *, risk_window=252):
    """benchmark: ascending completed (day, close); stocks: {symbol: [(day, close)]} on the same calendar."""
    rows = [(str(d), c) for d, c in benchmark or [] if type(c) in (int, float) and isfinite(c) and c > 0]
    if len(rows) < WINDOW + MIN_PRIOR + 1:
        return {"status": "unavailable", "reason": f"needs at least {WINDOW + MIN_PRIOR + 1} completed sessions",
                "history_sessions": len(rows)}
    days = [d for d, _ in rows]
    series = _benchmark_series(rows)
    series["stock_mean_correlation_20d"] = _correlation_series(stocks, days)
    first_in_window = days[max(0, len(days) - 1 - risk_window)]
    metrics, beyond_window, beyond_history = {}, [], []
    for name, values in series.items():
        if not values or values[-1][0] != days[-1]:
            metrics[name] = {"status": "unavailable"}
            continue
        value, prior = values[-1][1], values[:-1]
        history = _compare(value, [v for _, v in prior], DIRECTIONS[name])
        window = _compare(value, [v for d, v in prior if d >= first_in_window], DIRECTIONS[name])
        metrics[name] = {"value": value, "risk_window": window, "history": history}
        if window and window["beyond"]:
            beyond_window.append(name)
        if history and history["beyond"]:
            beyond_history.append(name)
    compared = any(m.get("risk_window") or m.get("history") for m in metrics.values())
    status = ("beyond_history" if beyond_history else "beyond_risk_window" if beyond_window
              else "normal" if compared else "unavailable")
    return {"as_of_close": days[-1], "status": status, "beyond_risk_window": beyond_window,
            "beyond_history": beyond_history, "risk_window_sessions": risk_window, "history_sessions": len(days),
            "first_session": days[0], "metrics": metrics,
            "meaning": "context, not a rule: no limit or order changes. beyond_risk_window = today's market is "
                       "outside every earlier value in the window the risk model's expected shortfall and "
                       "correlations were estimated on, so those estimates likely understate risk now (its "
                       "historical stress scenarios still apply); beyond_history = outside everything in the "
                       "history the fund holds, so its scorecards and factor tests have not seen such a market"}
