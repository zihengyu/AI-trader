"""Bounded supplemental closing receipts, independent of the frozen account NAV."""
from .risk import MarketDataUnavailable, ObservedPrice, instant, number, trading_day

MAX_ATTEMPTS = 5
RETRY_SECONDS = 60


def quote(broker, asset, closed_at):
    """Reuse successful receipts; persist retry reservations before provider I/O.

    A retry may add a source-timed quote and actions, never rewrite the account
    valuation. Reservation survives restarts and provider backoff still applies.
    """
    journal, now = broker.journal, broker.data_source.get_datetime()
    close = instant(closed_at).isoformat()
    state = journal.get_meta("comparison_closing_collection") or {}
    if state.get("close") != close:
        state = {"close": close, "symbols": {}}
    item = state["symbols"].get(asset.symbol, {})
    if item.get("receipt"):
        receipt = item["receipt"]
        return ObservedPrice(number(receipt["price"]), instant(receipt["as_of"])).validate(closed_at, broker.limits)
    if item.get("attempts", 0) >= MAX_ATTEMPTS or (item.get("next_at") and now < instant(item["next_at"])):
        raise MarketDataUnavailable("Supplemental closing quote awaits its bounded retry")
    from datetime import timedelta
    item = {"attempts": item.get("attempts", 0) + 1, "next_at": (now + timedelta(seconds=RETRY_SECONDS)).isoformat()}
    state["symbols"][asset.symbol] = item
    journal.set_meta("comparison_closing_collection", state)
    mark = broker._closing_quote(asset, closed_at).validate(closed_at, broker.limits)
    observed = broker.data_source.get_datetime()
    receipt = {"symbol": asset.symbol, "price": str(mark.price), "as_of": mark.as_of.isoformat(),
               "session_close": close}
    journal.event(observed, "comparison_closing_quote", receipt)
    source = broker._closing_sources.get(asset.symbol, {})
    for action in source.get("corporate_actions", []):
        if action["date"] <= trading_day(closed_at):
            journal.event(observed, "comparison_corporate_action", {"symbol": asset.symbol, **action})
    item["receipt"] = receipt
    journal.set_meta("comparison_closing_collection", state)
    return mark


def recover(broker, body):
    """Best-effort missing benchmark/shadow quotes; account NAV stays immutable."""
    policy, close = broker._valuation_policy, (body.get("valuation") or {}).get("session_close")
    if not policy or not close:
        return
    try:
        saved = broker.journal.get_meta("comparison_symbols") or {}
        extra = set(saved.get("symbols", [])) & set(policy["universe"])
    except Exception:
        extra = set()
    from lumibot.entities import Asset
    for symbol in sorted((set(policy["benchmark_symbols"]) | extra) - set(body.get("benchmark_observations", {}))):
        try:
            quote(broker, Asset(symbol, asset_type="stock"), instant(close))
        except Exception:
            continue  # Diagnostic failure cannot withhold an already valid NAV.
