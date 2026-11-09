"""Descriptive crowding proxies for the current top and bottom factor quintiles."""
from math import isfinite
from statistics import mean, median

import numpy as np

LOOKBACK = 60
MIN_SHARED = 40
MIN_BASKET = 2


def diagnose(ranks, closes, extra, short_volume=None):
    """Use only the supplied current factor ranks and closes through their last session."""
    ranks = ranks.dropna().sort_values(kind="stable")
    size = len(ranks) // 5
    if size < MIN_BASKET or ranks.iloc[size - 1] == ranks.iloc[size] or ranks.iloc[-size] == ranks.iloc[-size - 1]:
        return {"status": "unavailable", "reason": "too few distinct names for unambiguous quintiles"}
    groups = {"favored": list(ranks.index[-size:]), "disfavored": list(ranks.index[:size])}
    as_of = str(closes.index[-1].date())
    returns = closes.loc[:, list(ranks.index)].iloc[-(LOOKBACK + 1):].pct_change(fill_method=None).iloc[1:]
    extra = extra or {}
    book_to_price = None
    if extra.get("book") is not None and extra.get("shares") is not None:
        book, shares = extra["book"].iloc[-1], extra["shares"].iloc[-1]
        book_to_price = book.where(book > 0) / (closes.iloc[-1] * shares.where(shares > 0))
    short_day = (short_volume or {}).get("session_date")
    ratios = short_volume["ratios"] if short_day and short_day <= as_of else {}
    result = {"status": "observed", "as_of_close": as_of,
              "finra_session": short_day if short_day and short_day <= as_of else None,
              "correlation_lookback": LOOKBACK, "minimum_shared_returns": MIN_SHARED}
    for name, symbols in groups.items():
        matrix = returns[symbols].corr(min_periods=MIN_SHARED).to_numpy()
        correlations = matrix[np.triu_indices(len(symbols), k=1)]
        correlations = correlations[np.isfinite(correlations)]
        books = [float(v) for v in book_to_price.reindex(symbols).dropna() if isfinite(v)] if book_to_price is not None else []
        shorts = [ratios[s] for s in symbols if s in ratios and isinstance(ratios[s], (int, float)) and 0 <= ratios[s] <= 1]
        result[name] = {"names": len(symbols), "correlation_pairs": len(correlations),
                        "mean_pairwise_return_correlation": float(correlations.mean()) if correlations.size else None,
                        "valuation_names": len(books),
                        "median_book_to_price": median(books) if len(books) >= MIN_BASKET else None,
                        "finra_names": len(shorts),
                        "mean_finra_short_volume_ratio": mean(shorts) if len(shorts) >= MIN_BASKET else None}
    long_book, short_book = (result[name]["median_book_to_price"] for name in groups)
    result["book_to_price_spread"] = long_book - short_book if long_book is not None and short_book is not None else None
    return result
