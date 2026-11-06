"""Deterministic daily attribution: where a session's return came from, computed by code, not estimated by the AI.

For a session D the starting book is the last account mark before D: its
positions, its cash and its cash-equivalent ETF. With the weight
w_i = q_i x close_i(D-1) / equity(D-1) and the completed close-to-close return r_i:

- market: w_i x beta_i x r_SPY, with beta from completed closes through D-1 only
  (1.0 and listed in `beta_assumed` when too few closes exist);
- selection: w_i x (r_i - beta_i x r_SPY), also summed by risk group;
- cash equivalent: the sweep ETF's weight x its price return (its distributions
  are not in the price, so they land in `unexplained`);
- timing of the session's trades against the close: buys q x (close - arrival),
  sells q x (arrival - close);
- execution slippage against the arrival price: buys q x (arrival - fill),
  sells q x (fill - arrival); missing arrivals leave timing and slippage unknown;
- fees: minus the recorded trade costs (unknown fees are listed, not taken as 0).

Each part is divided by equity(D-1). The observed return comes from the account
marks, and its difference from the sum (marks taken before the close,
dividends, corporate actions, rounding) is reported as `unexplained`, never
forced to zero. A part whose inputs are missing is listed in `unavailable`.
"""
from .scorecard import beta as estimate_beta

COMPONENTS = ("market", "selection", "cash_equivalent", "timing", "slippage", "fees")


def _is_buy(side):
    return "buy" in str(side).lower()


def attribute(start, end, fills, closes, previous_day, day, groups=None, benchmark="SPY"):
    """Attribution of one session.

    start/end: account marks {"equity", "positions": {symbol: qty}, "cash_equivalents": {symbol: qty}};
    fills: [{"symbol", "side", "quantity", "price", "fee", "arrival"}] executed during the session;
    closes: {symbol: [(day, close), ...]} completed daily closes; previous_day/day: ISO session dates.
    """
    equity = float(start["equity"]) if start else 0.0
    if equity <= 0 or not end:
        return {"status": "unavailable", "session_date": day,
                "reason": "no positive starting account mark" if equity <= 0 else "no account mark for the session"}
    series = {symbol: {str(d): float(c) for d, c in rows or []} for symbol, rows in closes.items()}

    def close(symbol, when):
        return series.get(symbol, {}).get(when)

    def session_return(symbol):
        before, after = close(symbol, previous_day), close(symbol, day)
        return after / before - 1 if before and after else None

    market_return = session_return(benchmark)
    unavailable, assumed, rows, by_group = [], [], [], {}
    parts = dict.fromkeys(COMPONENTS, 0.0)
    if market_return is None:
        unavailable.append(f"{benchmark}: no completed closes for {previous_day} and {day}")
    for symbol, quantity in sorted((start.get("positions") or {}).items()):
        quantity = float(quantity)
        if not quantity:
            continue
        own = session_return(symbol)
        if own is None or market_return is None:
            unavailable.append(f"{symbol}: no completed closes for {previous_day} and {day}")
            continue
        weight = quantity * close(symbol, previous_day) / equity
        exposure = estimate_beta(closes.get(symbol), closes.get(benchmark), previous_day)
        if exposure is None:
            exposure = 1.0
            assumed.append(symbol)
        market, selection = weight * exposure * market_return, weight * (own - exposure * market_return)
        parts["market"] += market
        parts["selection"] += selection
        group = (groups or {}).get(symbol, "other")
        by_group[group] = by_group.get(group, 0.0) + selection
        rows.append({"symbol": symbol, "weight": weight, "return": own, "beta": exposure, "market": market,
                     "selection": selection, "group": group})
    for symbol, quantity in (start.get("cash_equivalents") or {}).items():
        own = session_return(symbol)
        if own is None:
            unavailable.append(f"{symbol}: no completed closes for {previous_day} and {day}")
            continue
        parts["cash_equivalent"] += float(quantity) * close(symbol, previous_day) / equity * own
    trades, fees_unknown, no_arrival = [], [], []
    for fill in fills:
        symbol, quantity, price = fill["symbol"], float(fill["quantity"]), float(fill["price"])
        sign = 1 if _is_buy(fill["side"]) else -1
        if fill.get("fee") is None:
            fees_unknown.append(symbol)
        else:
            parts["fees"] -= float(fill["fee"]) / equity
        final = close(symbol, day)
        if final is None:
            unavailable.append(f"{symbol} trade: no completed close for {day}")
            continue
        arrival = fill.get("arrival")
        if arrival is None:
            no_arrival.append(symbol)
            trades.append({"symbol": symbol, "side": "buy" if sign > 0 else "sell", "quantity": quantity,
                           "fill": price, "arrival": None, "close": final, "timing": None, "slippage": None})
            continue
        timing, slippage = sign * quantity * (final - float(arrival)), sign * quantity * (float(arrival) - price)
        parts["timing"] += timing / equity
        parts["slippage"] += slippage / equity
        trades.append({"symbol": symbol, "side": "buy" if sign > 0 else "sell", "quantity": quantity, "fill": price,
                       "arrival": float(arrival), "close": final, "timing": timing / equity,
                       "slippage": slippage / equity})
    observed = float(end["equity"]) / equity - 1
    explained = sum(parts.values())
    if no_arrival:
        parts["timing"], parts["slippage"] = None, None
    if fees_unknown:
        parts["fees"] = None
    return {"status": "partial" if unavailable or fees_unknown or no_arrival else "complete", "session_date": day,
            "previous_session": previous_day, "start_equity": equity, "observed_return": observed,
            "components": parts, "explained": explained, "unexplained": observed - explained,
            "benchmark": benchmark, "benchmark_return": market_return,
            "selection_by_group": dict(sorted(by_group.items())), "by_symbol": rows, "trades": trades,
            "beta_assumed": assumed, "fees_unknown": fees_unknown, "arrival_unknown": no_arrival,
            "unavailable": unavailable,
            "meaning": "Fractions of the starting equity. market = beta x benchmark move; selection = the rest of "
                       "each holding's move; timing = the session's trades against the close; slippage = fills "
                       "against the arrival price; fees = recorded trade costs. unexplained is reported, not "
                       "forced to zero (marks before the close, dividends, corporate actions)."}
