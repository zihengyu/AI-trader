"""A per-symbol market snapshot from completed daily bars, computed once per trading day for the AI.

Without it the researcher and the manager spend tool calls (and minutes) deriving
the same returns, volatilities and trend levels again for every symbol on every
run. Deterministic code computes them once from the bars the risk engine already
holds; the agents use their tools for what code cannot know (news, filings,
theses). Only completed sessions are used, never today's forming bar, so the
snapshot is look-ahead-safe in historical runs. Missing data stays None: a value
is never filled, carried forward or estimated.
"""
import math

import numpy as np

RETURN_WINDOWS = (1, 5, 20, 60)
VOLATILITY_WINDOWS = (20, 60)
TREND_WINDOWS = (50, 200)
BETA_WINDOW, MIN_BETA_RETURNS = 60, 20
LIQUIDITY_WINDOW = 20
HIGH_WINDOW = 252
MEANING = ("Completed daily closes only (never today's bar), as of as_of_close. ret_Nd: simple return over N "
           "sessions; vol_Nd: annualized standard deviation of daily returns; beta_60d/corr_60d: to the benchmark "
           "over up to 60 shared sessions; pct_from_maN: last close versus its N-session mean; pct_from_high_252d: "
           "versus the 252-session high close; adv_20d_usd: mean close x volume. Fractions, not percent. "
           "null = not enough history or data (never filled); no_history lists symbols without completed bars.")


def _rounded(value, digits=4):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


def _beta(rows, benchmark_rows):
    """(beta, correlation) of daily returns on the sessions both histories share, or (None, None)."""
    if benchmark_rows is None:
        return None, None
    benchmark = {day: close for day, close, _ in benchmark_rows}
    shared = [(close, benchmark[day]) for day, close, _ in rows if day in benchmark][-(BETA_WINDOW + 1):]
    if len(shared) <= MIN_BETA_RETURNS:
        return None, None
    prices = np.array(shared, dtype=float)
    returns = prices[1:] / prices[:-1] - 1
    own, market = returns[:, 0], returns[:, 1]
    variance = float(np.var(market, ddof=1))
    if not variance > 0:
        return None, None
    beta = float(np.cov(own, market, ddof=1)[0, 1]) / variance
    spread = float(np.std(own, ddof=1))
    correlation = float(np.corrcoef(own, market)[0, 1]) if spread > 0 else None
    return beta, correlation


def summarize(rows, benchmark_rows=None):
    """One symbol's snapshot from [(date, close, volume)] completed sessions (oldest first)."""
    close = np.array([c for _, c, _ in rows], dtype=float)
    returns = close[1:] / close[:-1] - 1
    item = {"last_close": _rounded(close[-1]), "as_of_close": rows[-1][0], "sessions": len(close)}
    for n in RETURN_WINDOWS:
        item[f"ret_{n}d"] = _rounded(close[-1] / close[-1 - n] - 1) if len(close) > n else None
    for n in VOLATILITY_WINDOWS:
        item[f"vol_{n}d"] = _rounded(np.std(returns[-n:], ddof=1) * math.sqrt(252)) if len(returns) >= n else None
    item["beta_60d"], item["corr_60d"] = (_rounded(v) for v in _beta(rows, benchmark_rows))
    for n in TREND_WINDOWS:
        item[f"pct_from_ma{n}"] = _rounded(close[-1] / close[-n:].mean() - 1) if len(close) >= n else None
    item["pct_from_high_252d"] = _rounded(close[-1] / close[-HIGH_WINDOW:].max() - 1)
    dollars = [c * v for _, c, v in rows[-LIQUIDITY_WINDOW:] if v is not None and math.isfinite(v) and v > 0]
    # A window with any missing volume is reported as unknown, not averaged over fewer sessions.
    item["adv_20d_usd"] = round(sum(dollars) / len(dollars)) if len(dollars) == LIQUIDITY_WINDOW else None
    return item


def snapshot(histories, benchmark):
    """{"symbols": {symbol: summary}, "no_history": [...], ...} from {symbol: completed rows}."""
    reference = histories.get(benchmark) or None
    symbols, no_history = {}, []
    for symbol in sorted(histories):
        rows = histories[symbol]
        if not rows:
            no_history.append(symbol)
            continue
        symbols[symbol] = summarize(rows, reference)
    return {"benchmark": benchmark, "as_of_close": reference[-1][0] if reference else None,
            "meaning": MEANING, "symbols": symbols, "no_history": no_history}
