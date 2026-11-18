"""Thin fund-owned bridge to the lab; cached reads only, no research order path."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

from .risk import instant, trading_day
from .session_calendar import completed_daily_cutoff, regular_session_open
from .strategy_lab import has_source_artifact_receipt, load_snapshot
from .strategy_validation import digest, snapshot_limitations


def _cache_symbols(strategy, rows):
    cash = strategy.strategy_lab.policy.get("cash_symbol")
    requested = set()
    for row in rows:
        if row["task_state"] != "open":
            continue
        proposal = json.loads(row["proposal"])
        requested.update(proposal["instruments"])
        requested.update(proposal.get("data_symbols", []))
        requested.add(proposal["validation"]["benchmark"])
    if requested and cash:
        requested.add(cash)
    # A model-supplied feature name is never a path to arbitrary local data.
    return tuple(sorted(symbol for symbol in requested if (symbol in strategy.universe or symbol == cash)
                        and not any(char in symbol for char in ("/", "\\", "\x00"))))


def cached_snapshot(strategy, now, *, symbols=None):
    """The existing daily cache is exploratory and carries no original provider receipts."""
    lab, store = getattr(strategy, "strategy_lab", None), getattr(strategy, "daily_store", None)
    if lab is None or store is None:
        return None
    symbols = _cache_symbols(strategy, lab.rows()) if symbols is None else symbols
    raw_bars = {}
    day = trading_day(now)
    for symbol in symbols:
        cached = store.get(day, "bars/" + symbol)
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


def prepare_inputs(strategy, now):
    lab = strategy.strategy_lab
    if strategy.halt_path.exists() or regular_session_open(now):
        return
    rows = lab.rows()
    candidates = tuple((row["id"], row["version"], row["task_state"], row["state"] in {"shadow", "paper_active"})
                       for row in rows if row["task_state"] == "open" or row["state"] in {"shadow", "paper_active"})
    policy = lab.admission_policy["policy_hash"]
    inbox = lab.journal.root / "strategy_lab_snapshot.json"
    try:
        stat = inbox.stat()
    except FileNotFoundError:
        stat = None
    if stat is not None:
        revision = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        loaded = getattr(strategy, "_strategy_lab_inbox", None)
        if loaded is None or loaded[0] != revision:
            data = load_snapshot(inbox)
            loaded = strategy._strategy_lab_inbox = revision, data, digest(data)
        _, data, fingerprint = loaded
        with lab.journal.connection() as db:
            verified = lab.settings.require_source_artifacts and has_source_artifact_receipt(db, fingerprint, now)
        receipt = revision, candidates, verified
        if receipt != getattr(strategy, "_strategy_lab_inbox_receipt", None):
            key = digest([fingerprint, candidates, verified, policy])
            previous = lab.journal.get_meta("strategy_lab_last_intake", {})
            if previous.get("schedule_key") != key:
                lab.schedule(data, now)
                gaps = snapshot_limitations(data, require_receipts=True)
                if lab.settings.require_source_artifacts and not verified and not gaps:
                    gaps = ["source_artifacts_unchecked"]
                lab.journal.set_meta("strategy_lab_last_intake", {
                    "at": instant(now).isoformat(), "coverage_gaps": gaps, "schedule_key": key})
            strategy._strategy_lab_inbox_receipt = receipt

    store = getattr(strategy, "daily_store", None)
    if store is None:
        return
    symbols = _cache_symbols(strategy, rows)
    day = trading_day(now)
    cache = tuple((symbol, store.version(day, "bars/" + symbol)) for symbol in symbols)
    receipt = day, completed_daily_cutoff(now), candidates, cache
    if receipt != getattr(strategy, "_strategy_lab_cache_receipt", None):
        data = cached_snapshot(strategy, now, symbols=symbols)
        if data is not None:
            content = digest([digest(data), candidates, policy])
            if content != lab.journal.get_meta("strategy_lab_cache_content"):
                lab.schedule(data, now)
                lab.journal.set_meta("strategy_lab_cache_content", content)
        strategy._strategy_lab_cache_receipt = receipt


def tick(strategy, *, now=None):
    lab = getattr(strategy, "strategy_lab", None)
    if lab is None or strategy.observe_only or strategy.halt_path.exists() or strategy.mode not in {"local", "paper"}:
        return
    lock = strategy.__dict__.setdefault("_strategy_lab_tick_lock", Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        now = instant(now if now is not None else strategy.get_datetime())
        if regular_session_open(now):
            return
        previous = getattr(strategy, "_strategy_lab_tick_at", None)
        if previous is not None and now - previous < timedelta(seconds=30):
            return
        if lab.kick(now, prepare=lambda checked: prepare_inputs(strategy, checked)):
            strategy._strategy_lab_tick_at = now
    finally:
        lock.release()


@contextmanager
def poll_live_inputs(strategy, *, enabled, interval=30):
    """Wake the existing lab worker even while the broker waits for its next session."""
    if not enabled:
        yield
        return
    stopped = Event()

    def poll():
        while not stopped.wait(interval):
            strategy._strategy_lab_tick(now=datetime.now(timezone.utc))

    thread = Thread(target=poll, name="strategy-lab-input-poll", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)
