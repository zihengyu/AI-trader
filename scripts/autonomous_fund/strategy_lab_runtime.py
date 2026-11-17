"""Thin fund-owned bridge to the lab; cached reads only, no research order path."""
import json
from datetime import timedelta

from .risk import instant, trading_day


def cached_snapshot(strategy, now):
    """Reuse the existing data desk; never refetch history on the trading thread.

    This cache has no original publication receipts, delisting completeness or
    unadjusted-price guarantee. It is useful exploratory data, not admission proof.
    """
    lab, store = getattr(strategy, "strategy_lab", None), getattr(strategy, "daily_store", None)
    if lab is None or store is None:
        return None
    requested = set()
    for row in lab.rows():
        if row["task_state"] != "open":
            continue
        proposal = json.loads(row["proposal"])
        requested.update(proposal["instruments"])
        requested.update(proposal.get("data_symbols", []))
        requested.add(proposal["validation"]["benchmark"])
    if requested and lab.policy.get("cash_symbol"):
        requested.add(lab.policy["cash_symbol"])
    raw_bars = {}
    for symbol in sorted(requested):
        # Only operator-selected cache names, never a path from a model answer.
        if (symbol not in strategy.universe and symbol != lab.policy.get("cash_symbol")) or any(
                c in symbol for c in ("/", "\\", "\x00")):
            continue
        cached = store.get(trading_day(now), "bars/" + symbol)
        if cached and cached.get("rows"):
            raw_bars[symbol] = cached["rows"]
    if not raw_bars:
        return None
    import pandas_market_calendars as mcal
    days = [r[0] for values in raw_bars.values() for r in values]
    schedule = mcal.get_calendar("NYSE").schedule(start_date=min(days), end_date=max(days))
    bounds = {str(day.date()): (row.market_open, row.market_close) for day, row in schedule.iterrows()}
    bars = {}
    for symbol, values in raw_bars.items():
        rows = []
        for raw in values:
            day, close, volume, opened, high, low = raw
            open_at, close_at = bounds.get(day, (None, None))
            if open_at is None or instant(close_at) > instant(now):
                continue
            rows.append({"day": day, "at": close_at.isoformat(), "open_at": open_at.isoformat(),
                         "available_at": close_at.isoformat(), "open": opened, "high": high, "low": low,
                         "close": close, "volume": volume, "availability_estimated": True})
        if rows:
            bars[symbol] = rows
    if not bars:
        return None
    return {"bars": bars, "actions": [],
            "coverage": {"raw_prices": False, "corporate_actions_complete": False,
                         "point_in_time_membership": False, "publication_times_complete": False},
            "source": "existing_daily_store_exploratory",
            "availability_estimate_reason": "Completed-session boundaries; original provider publication times are unavailable."}


def tick(strategy):
    lab = getattr(strategy, "strategy_lab", None)
    if lab is None or strategy.observe_only or strategy.halt_path.exists() or strategy.mode not in {"local", "paper"}:
        return
    now = strategy.get_datetime()
    from .session_calendar import regular_session_open
    if regular_session_open(now):
        return
    previous = getattr(strategy, "_strategy_lab_tick_at", None)
    if previous is not None and instant(now) - instant(previous) < timedelta(seconds=30):
        return
    strategy._strategy_lab_tick_at = now
    data = cached_snapshot(strategy, now)
    if data is not None:
        lab.schedule(data, now)
    lab.kick(now)
