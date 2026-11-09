"""Attribute broker positions to the AI capital sleeve or to the operator.

A brokerage account may already hold the operator's own investments (stocks,
ETFs, money-market sweep funds). With `allow_external_holdings`, every position
present when the mandate is first initialized becomes the operator's frozen
baseline. The AI sleeve is only what the AI itself bought afterwards.

Symbols the operator holds are SHARED with the eligible universe only by name:
the AI must never trade them, because a sale in a shared position would let the
broker's tax-lot method (often first-in-first-out) sell the operator's lots and
would make attribution ambiguous. They are removed from the tradable universe.
"""
from datetime import timedelta

from .risk import RiskError, instant, number


def market_value(position):
    payload = getattr(position, "raw_broker_payload", None) or getattr(position, "_raw", None)
    if isinstance(payload, dict) and payload.get("marketValue") is not None:
        return number(payload["marketValue"])
    return None


def average_prices(positions, holdings):
    """Observed weighted entry prices for fully attributed sleeve positions only."""
    quantities, costs = {}, {}
    for position in positions:
        symbol = position.asset.symbol
        if symbol not in holdings or str(position.asset.asset_type) != "stock":
            continue
        try:
            quantity = number(position.quantity)
            price = number(getattr(position, "avg_fill_price", None))
        except RiskError:
            continue
        if quantity > 0 and price > 0:
            quantities[symbol] = quantities.get(symbol, number(0)) + quantity
            costs[symbol] = costs.get(symbol, number(0)) + quantity * price
    # A missing lot price must not silently become a partial, misleading average.
    return {symbol: costs[symbol] / quantity for symbol, quantity in quantities.items()
            if quantity == number(holdings[symbol])}


def recent_trades(events, as_of, limit=6):
    """Confirmed fills visible at the account snapshot, without double-counting reconciliation."""
    cutoff = instant(as_of)
    callbacks, reconciled = [], {}
    for index, (at, kind, body) in enumerate(events):
        if kind not in {"fill", "fill_reconciled"} or instant(at) > cutoff:
            continue
        row = {**body, "at": at, "evidence_kind": (
            "broker_cumulative_observation" if kind == "fill_reconciled" else
            body.get("evidence_kind", "native_fill_callback"))}
        if kind == "fill_reconciled":
            reconciled[str(body["broker_id"])] = (index, row)
        else:
            callbacks.append((index, row))
    chosen = [(index, row) for index, row in callbacks if str(row["broker_id"]) not in reconciled]
    chosen += list(reconciled.values())
    chosen.sort(key=lambda item: (instant(item[1]["at"]), item[0]))
    return [row for _, row in chosen[-limit:]]


def split_holdings(positions, universe, *, quote_asset=None, allow_external=False, baseline=None,
                   initialized=False, price=None):
    """Return (sleeve quantities, external quantities, external value, shared symbols).

    baseline: the operator's frozen external quantities (after initialization).
    price(symbol) values external stocks without a broker market value.
    Raises RiskError for anything that cannot be attributed safely.
    """
    baseline = {k: number(v) for k, v in (baseline or {}).items()}
    sleeve, external, value = {}, {}, number(0)
    for position in positions:
        if quote_asset is not None and position.asset == quote_asset:
            continue
        symbol = position.asset.symbol
        stock = str(position.asset.asset_type) == "stock"
        quantity = number(position.quantity)
        operator_owned = allow_external and (not initialized or symbol in baseline or not (stock and symbol in universe))
        if operator_owned:
            external[symbol] = external.get(symbol, number(0)) + quantity
            worth = market_value(position)
            if worth is None:
                quote = price(symbol) if (stock and price is not None) else None
                if quote is None or not number(quote) > 0:
                    raise RiskError(f"External holding {symbol} cannot be valued; mandate sleeve unavailable")
                worth = quantity * number(quote)
            value += worth
        elif stock and symbol in universe:
            sleeve[symbol] = sleeve.get(symbol, number(0)) + quantity
        else:
            raise RiskError("Dedicated account contains an unsupported asset")
    shared = frozenset((set(external) if not initialized else set(baseline)) & set(universe))
    return sleeve, external, value, shared


DAY_TRADE_WINDOW = 5
LEGACY_PDT_COUNT = 3


def day_trades(fills, now):
    """Same-day round trips per New York date and over the last five NYSE sessions (an estimate).

    A day trade is a buy followed by a sale of the same symbol on the same New York date;
    each buy-then-sell sequence counts once. fills: (at, kind, body) rows of fill and
    fill_reconciled; a broker's cumulative observation replaces that order's callbacks.
    The legacy PDT count is informational; this does not determine a brokerage account's
    classification, buying power or margin requirements.
    """
    from zoneinfo import ZoneInfo
    import pandas_market_calendars as mcal
    zone, now = ZoneInfo("America/New_York"), instant(now)
    rows = recent_trades(fills, now, limit=max(1, len(fills)))
    per_symbol_day = {}
    for row in sorted(rows, key=lambda r: instant(r["at"])):
        day = instant(row["at"]).astimezone(zone).date().isoformat()
        per_symbol_day.setdefault((day, row.get("symbol")), []).append(str(row.get("side")))
    by_day = {}
    for (day, _), sides in per_symbol_day.items():
        opened, count = False, 0
        for side in sides:
            if side == "buy":
                opened = True
            elif side == "sell" and opened:
                count, opened = count + 1, False
        if count:
            by_day[day] = by_day.get(day, 0) + count
    today = now.astimezone(zone).date()
    schedule = mcal.get_calendar("NYSE").schedule(start_date=today - timedelta(days=14), end_date=today)
    sessions = [index.date().isoformat() for index in schedule.index][-DAY_TRADE_WINDOW:]
    recent = sum(by_day.get(day, 0) for day in sessions)
    return {"last_sessions": recent, "sessions": sessions, "by_day": dict(sorted(by_day.items())),
            "legacy_count_threshold": LEGACY_PDT_COUNT, "window": DAY_TRADE_WINDOW,
            "above_legacy_count": recent > LEGACY_PDT_COUNT}
