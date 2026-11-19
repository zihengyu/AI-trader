"""Thin fund-owned bridge to the lab; cached reads only, no research order path."""
import json
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from stat import S_ISDIR
from threading import Event, Lock, Thread

from .risk import RiskError, instant, trading_day
from .session_calendar import completed_daily_cutoff, regular_session_open
from .strategy_lab import has_source_artifact_receipt, load_snapshot
from .strategy_evidence import load_reference
from .strategy_program import strategy_inputs
from .strategy_validation import digest, snapshot_limitations


HISTORICAL_STATES = ("candidate", "needs_data", "awaiting_new_holdout")
DAILY_FIELDS = {"open", "high", "low", "close", "volume"}


def research_cycle(journal, now, lab, mode):
    """Read the prior close and unreviewed validation milestones for scheduled research."""
    now = instant(now)
    cutoff = now.isoformat()
    with journal.connection() as db:
        stages = db.execute("SELECT at,body FROM events WHERE kind='postmarket_research' "
                            "AND julianday(at)<=julianday(?) ORDER BY sequence DESC", (cutoff,))
        previous = None
        for at, body in stages:
            packet = json.loads(body)
            if max(instant(at), instant(packet["completed_at"]), instant(packet["market_close"])) <= now:
                previous = {key: packet[key] for key in ("session_date", "market_close", "completed_at")}
                previous["summary"] = packet["research"]["summary"][:1200]
                previous["research_direction"] = packet["research"].get("research_direction", "")
                break
        progress = None
        if lab is not None:
            try:
                progress = _research_progress(db, lab.admission_policy["policy_hash"], now)
            except Exception as exc:
                progress = {"status": "unavailable", "error": type(exc).__name__}
    return {"mode": mode, "previous_postmarket": previous, "lab_progress": progress}


def _research_progress(db, policy_hash, now):
    receipts = db.execute("SELECT at,json_extract(body,'$.completed_at'),"
                          "json_extract(body,'$.strategy_lab_research_cursor') FROM events "
                          "WHERE kind IN ('premarket_research','postmarket_research') AND julianday(at)<=julianday(?) "
                          "AND json_type(body,'$.strategy_lab_research_cursor')='integer' ORDER BY sequence DESC",
                          (now.isoformat(),))
    cursor = next((seq for at, completed, seq in receipts if max(instant(at), instant(completed)) <= now), 0)
    # The researcher needs outcomes that can change the next task. Preflight
    # gaps, repeated data pulls and per-session forward marks stay in lab context.
    milestones = db.execute("""
        SELECT e.sequence,e.id,e.version,e.kind,e.at,e.body FROM fund_strategy_events e
        JOIN fund_strategy_versions v ON v.id=e.id AND v.version=e.version
        LEFT JOIN fund_strategy_jobs j ON j.id=json_extract(e.body,'$.job_id')
        WHERE e.sequence>? AND v.policy_hash=? AND (
            e.kind IN ('historical_pass','forward_qualification') OR
            (e.kind='validation' AND (j.kind='historical' OR
                (j.kind='forward' AND json_extract(e.body,'$.state') IN ('paused','forward_incomplete')))))
        ORDER BY e.sequence LIMIT 9
        """, (cursor, policy_hash))
    updates = []
    for seq, key, version, kind, at, payload in milestones:
        # Do not acknowledge later sequence numbers around an unfinished cutoff.
        if instant(at) > now:
            break
        if len(updates) == 8:
            return {"milestones": updates, "cursor": cursor, "more": True}
        body = json.loads(payload)
        updates.append({"id": key, "version": version, "kind": kind, "at": at,
                        **{field: body[field] for field in ("state", "status") if field in body}})
        cursor = seq
    return {"milestones": updates, "cursor": cursor, "more": False}


def history_symbols(proposal, cash_symbol, reference=None):
    return strategy_inputs(proposal, cash_symbol, reference).symbols


def _requested_depths(proposal, inputs):
    plan = proposal["validation"]
    sessions = sum(plan[key] for key in ("train_sessions", "validation_sessions", "final_sessions"))
    depths = dict.fromkeys(inputs.symbols, sessions + 1)  # The preceding close forms the initial signal.
    for (symbol, _), depth in inputs.known_depths.items():
        depths[symbol] = max(depths[symbol], sessions + depth)
    return depths


def _bounded_depths(depths, budget):
    bounded = {}
    for index, symbol in enumerate(sorted(depths, key=lambda key: (depths[key], key))):
        bounded[symbol] = min(depths[symbol], budget // (len(depths) - index))
        budget -= bounded[symbol]
    return bounded


def research_history_store(root):
    from .datastore import DailyStore

    return DailyStore(root / "cache" / "strategy_lab" / "daily")


def history_requests(db, universe):
    plan = history_request_plan(db, universe)
    return plan["requests"], plan["unsupported"]


def history_request_plan(db, universe):
    """Read the frozen versions the running lab can actually schedule."""
    result = {"requests": {}, "unsupported": set(), "unsupported_fields": {}, "capacity": {}}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_strategy_versions'").fetchone():
        return result
    saved = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
    policy = json.loads(saved[0]) if saved else {}
    if not policy.get("enabled"):
        return result
    requests, unsupported = result["requests"], result["unsupported"]
    for body, frozen, created_at in db.execute(
            "SELECT proposal,policy,created_at FROM fund_strategy_versions WHERE policy_hash=? AND task_state='open' "
            "AND state IN (?,?,?)", (policy["policy_hash"], *HISTORICAL_STATES)):
        proposal, frozen = json.loads(body), json.loads(frozen)
        cash = frozen.get("cash_symbol")
        try:
            reference = load_reference(db, proposal, policy["policy_hash"], created_at)
            inputs = strategy_inputs(proposal, cash, reference)
        except RiskError:
            continue  # The scheduler exposes the invalid reference receipt.
        raw_fields = {symbol for symbol, field in inputs.fields if field in DAILY_FIELDS}
        custom_fields = {symbol for symbol, field in inputs.fields if field not in DAILY_FIELDS}
        daily = inputs.prices | raw_fields | (inputs.declared - custom_fields)
        supported = {symbol for symbol in daily if symbol in universe | {cash}
                     and not any(char in symbol for char in ("/", "\\", "\x00"))}
        unsupported.update(inputs.symbols - supported)
        for symbol, field in inputs.fields:
            if symbol not in supported or field not in DAILY_FIELDS:
                result["unsupported_fields"].setdefault(symbol, set()).add(field)
        depths = _requested_depths(proposal, inputs)
        wanted = {symbol: depths[symbol] for symbol in supported}
        bounded = _bounded_depths(wanted, policy["max_history_rows"])
        short = {symbol: {"requested": wanted[symbol], "allocated": depth}
                 for symbol, depth in sorted(bounded.items()) if depth < wanted[symbol]}
        if short:
            key = f"{proposal['strategy_id']} / {proposal['version']}"
            result["capacity"][key] = {"row_limit": policy["max_history_rows"], "symbols": short}
        for symbol, depth in bounded.items():
            if depth:
                requests[symbol] = max(requests.get(symbol, 0), depth)
    result["unsupported"] = unsupported - requests.keys()
    result["unsupported_fields"] = {symbol: sorted(fields) for symbol, fields in sorted(result["unsupported_fields"].items())}
    return result


def _cache_depths(strategy, rows):
    cash = strategy.strategy_lab.policy.get("cash_symbol")
    requested = {}
    for row in rows:
        if row["task_state"] == "open" and row["state"] in HISTORICAL_STATES:
            proposal, reference = json.loads(row["proposal"]), None
            if proposal["validation"].get("reference_strategy") is not None:
                with strategy.strategy_lab.journal.connection() as db:
                    try:
                        reference = load_reference(db, proposal, row["policy_hash"], row["created_at"])
                    except RiskError:
                        continue
            try:
                depths = _requested_depths(proposal, strategy_inputs(proposal, cash, reference))
            except RiskError:
                continue
            for symbol, depth in depths.items():
                requested[symbol] = max(requested.get(symbol, 0), depth)
    # A model-supplied feature name is never a path to arbitrary local data.
    return {symbol: requested[symbol] for symbol in sorted(requested) if symbol
            and not any(char in symbol for char in ("/", "\\", "\x00"))}


def cached_snapshot(strategy, now, *, symbols=None, depths=None):
    """The existing daily cache is exploratory and carries no original provider receipts."""
    lab, store = getattr(strategy, "strategy_lab", None), getattr(strategy, "daily_store", None)
    if lab is None or store is None:
        return None
    if symbols is None:
        depths = _cache_depths(strategy, lab.rows())
        symbols = depths
    raw_bars = {}
    day = trading_day(now)
    research = research_history_store(lab.journal.root)
    # Keep each symbol on a single price series. The widest series with the
    # newest completed bar wins, so a deeper stale pull cannot hide fresh data.
    for symbol in symbols:
        histories = [(cache.get(day, "bars/" + symbol) or {}).get("rows", []) for cache in (store, research)]
        rows = max(histories, key=lambda rows: (rows[-1][0], len(rows)) if rows else ("", 0))
        if rows:
            raw_bars[symbol] = rows
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
            if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
                   for value in (opened, high, low, close, volume)) or not low <= min(opened, close) <= max(opened, close) <= high:
                continue  # A malformed row remains a gap in an exploratory snapshot.
            rows.append({"day": day, "at": close_at.isoformat(), "open_at": open_at.isoformat(),
                         "available_at": close_at.isoformat(), "open": opened, "high": high, "low": low,
                         "close": close, "volume": volume, "availability_estimated": True})
        if rows:
            bars[symbol] = rows
    budget = lab.settings.max_history_rows
    required = {symbol: min(len(rows), (depths or {}).get(symbol, len(rows))) for symbol, rows in bars.items()}
    allocated = _bounded_depths(required, budget)
    extra = _bounded_depths({symbol: len(rows) - allocated[symbol] for symbol, rows in bars.items()},
                            budget - sum(allocated.values()))
    bars = {symbol: rows[-length:] for symbol, rows in bars.items()
            if (length := allocated[symbol] + extra[symbol])}
    if not bars:
        return None
    return {"bars": bars, "actions": [],
            "coverage": {"raw_prices": False, "corporate_actions_complete": False,
                         "point_in_time_membership": False, "publication_times_complete": False},
            "source": "existing_daily_store_exploratory",
            "availability_estimate_reason": "Completed-session boundaries; original provider publication times are unavailable."}


def _prepare_inbox(strategy, lab, now, candidates, policy):
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
        artifacts = lab.journal.root / "strategy_lab_source_artifacts"
        artifact_revision = None
        if lab.settings.require_source_artifacts and not verified:
            try:
                stat = artifacts.stat()
                if S_ISDIR(stat.st_mode):
                    artifact_revision = (stat.st_ino, stat.st_mtime_ns)
            except FileNotFoundError:
                pass  # The data adapter may deliver the original files later.
        receipt = revision, candidates, verified, artifact_revision
        if receipt != getattr(strategy, "_strategy_lab_inbox_receipt", None):
            key = digest([fingerprint, candidates, verified, policy, artifact_revision])
            previous = lab.journal.get_meta("strategy_lab_last_intake", {})
            if previous.get("schedule_key") != key:
                gaps = snapshot_limitations(data, require_receipts=True)
                if artifact_revision is not None and not gaps:
                    try:
                        lab.schedule(data, now, artifacts_dir=artifacts)
                    except RiskError as exc:
                        if not str(exc).startswith("Source artifact verification failed: "):
                            raise
                        gaps = [str(exc)]
                    else:
                        verified = True
                        key = digest([fingerprint, candidates, verified, policy, None])
                        receipt = revision, candidates, verified, None
                if not verified or artifact_revision is None:
                    lab.schedule(data, now)
                if lab.settings.require_source_artifacts and not verified and not gaps:
                    gaps = ["source_artifacts_unchecked"]
                lab.journal.set_meta("strategy_lab_last_intake", {
                    "at": instant(now).isoformat(), "coverage_gaps": gaps, "schedule_key": key})
            strategy._strategy_lab_inbox_receipt = receipt


def prepare_inputs(strategy, now):
    lab = strategy.strategy_lab
    if strategy.halt_path.exists() or regular_session_open(now):
        return
    rows = lab.rows()
    candidates = tuple((row["id"], row["version"], row["task_state"], row["state"] in {"shadow", "paper_active"})
                       for row in rows if row["task_state"] == "open" or row["state"] in {"shadow", "paper_active"})
    policy = lab.admission_policy["policy_hash"]
    try:
        _prepare_inbox(strategy, lab, now, candidates, policy)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        # A malformed or temporarily unavailable provider bundle cannot block
        # independently cached data from reaching the exploratory queue.
        gap = str(exc)[:200] if isinstance(exc, RiskError) else type(exc).__name__
        previous = lab.journal.get_meta("strategy_lab_last_intake", {})
        if previous.get("coverage_gaps") != [gap]:
            lab.journal.set_meta("strategy_lab_last_intake", {"at": instant(now).isoformat(), "coverage_gaps": [gap]})
        strategy._strategy_lab_inbox_receipt = None

    store = getattr(strategy, "daily_store", None)
    if store is None:
        return
    groups = {}
    for row in rows:
        if row["policy_hash"] == policy:
            depths = tuple(_cache_depths(strategy, (row,)).items())
            if depths:
                groups.setdefault(depths, []).append((row["id"], row["version"]))

    day = trading_day(now)
    research = research_history_store(lab.journal.root)
    symbols = sorted({symbol for group in groups for symbol, _ in group})
    cache = tuple((symbol, store.version(day, "bars/" + symbol), research.version(day, "bars/" + symbol))
                  for symbol in symbols)
    receipt = day, completed_daily_cutoff(now), groups, cache
    if receipt == getattr(strategy, "_strategy_lab_cache_receipt", None):
        return

    previous = lab.journal.get_meta("strategy_lab_cache_content", {})
    previous = previous if isinstance(previous, dict) else {}  # Older ledgers stored the pooled snapshot digest.
    saved = {}
    for group, identities in groups.items():
        depths = dict(group)
        data = cached_snapshot(strategy, now, symbols=depths, depths=depths)
        if data is None:
            continue
        content = digest([digest(data), policy])
        for identity in identities:
            key = digest(identity)
            if content != previous.get(key):
                lab.schedule(data, now, candidate=identity)
            saved[key] = content
    if saved != previous:
        lab.journal.set_meta("strategy_lab_cache_content", saved)
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
