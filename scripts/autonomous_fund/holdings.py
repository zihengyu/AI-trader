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
from .risk import RiskError, number


def market_value(position):
    payload = getattr(position, "raw_broker_payload", None) or getattr(position, "_raw", None)
    if isinstance(payload, dict) and payload.get("marketValue") is not None:
        return number(payload["marketValue"])
    return None


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
