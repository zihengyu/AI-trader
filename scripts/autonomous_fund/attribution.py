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
from .risk import ObservedPrice, RiskError, instant, number
from .scorecard import beta as estimate_beta

COMPONENTS = ("market", "selection", "cash_equivalent", "timing", "slippage", "fees")


def observation_only(packet):
    """Whether a saved review has an interim account endpoint."""
    result = packet.get("attribution") or {}
    if window := result.get("valuation_window"):
        return window.get("kind") == "account_observations"
    end = (packet.get("session") or {}).get("last_observed_account")
    if not end or not packet.get("market_close"):
        return False
    try:
        return instant(end.get("at")) < instant(packet["market_close"])
    except (TypeError, ValueError):
        return True


def _is_buy(side):
    return "buy" in str(side).lower()


def observed_prices(row, at, limits):
    """Prices belonging to one account mark, bounded by their own source clocks."""
    body = row["body"]
    values = {}
    for source in (body.get("benchmark_observations"), (body.get("valuation") or {}).get("quotes")):
        for symbol, mark in (source or {}).items():
            try:
                price = ObservedPrice(number(mark["price"]), instant(mark["as_of"])).validate(at, limits)
            except (KeyError, TypeError, RiskError):
                continue
            values[symbol] = float(price.price)
    return values


def attribute(start, end, fills, closes, previous_day, day, groups=None, benchmark="SPY", *, window_prices=None):
    """Attribution of one session.

    start/end: account marks {"equity", "positions": {symbol: qty}, "cash_equivalents": {symbol: qty}};
    fills: [{"symbol", "side", "quantity", "price", "fee", "arrival"}] executed during the session;
    closes: {symbol: [(day, close), ...]} completed daily closes for historical beta.
    window_prices: (start prices, end prices) when either account anchor is intraday.
    """
    equity = float(start["equity"]) if start else 0.0
    if equity <= 0 or not end:
        return {"status": "unavailable", "session_date": day,
                "reason": "no positive starting account mark" if equity <= 0 else "no account mark for the session"}
    series = {symbol: {str(d): float(c) for d, c in rows or []} for symbol, rows in closes.items()}

    def close(symbol, when):
        if window_prices is not None:
            return window_prices[0 if when == previous_day else 1].get(symbol)
        return series.get(symbol, {}).get(when)

    def session_return(symbol):
        before, after = close(symbol, previous_day), close(symbol, day)
        return after / before - 1 if before and after else None

    market_return = session_return(benchmark)
    unavailable, assumed, rows, by_group = [], [], [], {}
    parts = dict.fromkeys(COMPONENTS, 0.0)
    missing_prices = ("no matched account-window prices" if window_prices is not None
                      else f"no completed closes for {previous_day} and {day}")
    if market_return is None:
        unavailable.append(f"{benchmark}: {missing_prices}")
    for symbol, quantity in sorted((start.get("positions") or {}).items()):
        quantity = float(quantity)
        if not quantity:
            continue
        own = session_return(symbol)
        if own is None or market_return is None:
            unavailable.append(f"{symbol}: {missing_prices}")
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
            unavailable.append(f"{symbol}: {missing_prices}")
            continue
        parts["cash_equivalent"] += float(quantity) * close(symbol, previous_day) / equity * own
    trades, fees_unknown, no_arrival = [], [], []
    missing_end = False
    cash_sweep = {"fills": 0, "priced_fills": 0, "notional": 0.0, "observed_friction_usd": 0.0}
    for fill in fills:
        symbol, quantity, price = fill["symbol"], float(fill["quantity"]), float(fill["price"])
        sign = 1 if _is_buy(fill["side"]) else -1
        if fill.get("fee") is None:
            fees_unknown.append(symbol)
        else:
            parts["fees"] -= float(fill["fee"]) / equity
        arrival = fill.get("arrival")
        if fill.get("order_kind") == "cash_sweep" and sign == 1:
            cash_sweep["fills"] += 1
            cash_sweep["notional"] += quantity * price
            if arrival is not None and fill.get("fee") is not None:
                cash_sweep["priced_fills"] += 1
                cash_sweep["observed_friction_usd"] += quantity * (price - float(arrival)) + float(fill["fee"])
        final = close(symbol, day)
        if final is None:
            missing_end = True
            unavailable.append(f"{symbol} trade: missing end-of-window price" if window_prices is not None
                               else f"{symbol} trade: no completed close for {day}")
        if arrival is None:
            no_arrival.append(symbol)
            trades.append({"symbol": symbol, "side": "buy" if sign > 0 else "sell", "quantity": quantity,
                           "fill": price, "arrival": None, "close": final, "timing": None, "slippage": None})
            continue
        timing = None if final is None else sign * quantity * (final - float(arrival)) / equity
        slippage = sign * quantity * (float(arrival) - price) / equity
        if timing is not None:
            parts["timing"] += timing
        parts["slippage"] += slippage
        trades.append({"symbol": symbol, "side": "buy" if sign > 0 else "sell", "quantity": quantity, "fill": price,
                       "arrival": float(arrival), "close": final, "timing": timing, "slippage": slippage})
    observed = float(end["equity"]) / equity - 1
    explained = sum(parts.values())
    if no_arrival:
        parts["timing"], parts["slippage"] = None, None
    if missing_end:
        parts["timing"] = None
    if fees_unknown:
        parts["fees"] = None
    return {"status": "partial" if unavailable or fees_unknown or no_arrival else "complete", "session_date": day,
            "previous_session": previous_day, "start_equity": equity, "observed_return": observed,
            "components": parts, "explained": explained, "unexplained": observed - explained,
            "benchmark": benchmark, "benchmark_return": market_return,
            "selection_by_group": dict(sorted(by_group.items())), "by_symbol": rows, "trades": trades,
            **({"cash_sweep_execution": cash_sweep} if cash_sweep["fills"] else {}),
            "beta_assumed": assumed, "fees_unknown": fees_unknown, "arrival_unknown": no_arrival,
            "unavailable": unavailable,
            "meaning": "Fractions of the starting equity. market = beta x benchmark move; selection = the rest of "
                       "each holding's move; timing = the session's trades against the close; slippage = fills "
                       "against the arrival price; fees = recorded trade costs. unexplained is reported, not "
                       "forced to zero (marks before the close, dividends, corporate actions)."}
