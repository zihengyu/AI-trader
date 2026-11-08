"""Timestamp-bounded follow-up observations and paired native replay experiments."""
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from .risk import RiskError, instant


def frozen_memory(packet, now):
    from .review import PostmarketReview
    PostmarketReview.model_validate(packet["review"])
    if (instant(packet["completed_at"]) > instant(now) or instant(packet["market_close"]) > instant(now) or
            instant(packet["review"]["as_of"]) > instant(packet["completed_at"])):
        raise RiskError("Frozen strategy memory was not available at this historical time")
    return {"version": packet["version"], "session_date": packet["session_date"],
            "available_at": packet["completed_at"], "model": packet["models"]["manager"],
            "mock": packet["mock"], "review": packet["review"], "qualification_frozen": True}


# One NAV mark is written about every 30 seconds of market time. Reports and
# decision contexts used to parse and keep every mark and benchmark body, so
# their memory grew with the fund's whole history (about 730 MB per report
# after 100,000 marks). These helpers read the few fields that ordering and
# drawdown need for all marks, and full bodies only for the marks shown.
_MARK_FIELDS = ("SELECT sequence,at,"
                "CASE WHEN json_valid(body) THEN json_extract(body,'$.equity') END,"
                "CASE WHEN json_valid(body) THEN json_extract(body,'$.valuation.session_close') END,"
                "CASE WHEN json_valid(body) THEN json_extract(body,'$.as_of') END "
                "FROM events WHERE kind='account' ORDER BY sequence")


def nav_marks(db, *, until=None, excluded=frozenset()):
    """Usable NAV marks in sequence order as (sequence, at, instant, equity, session_close, as_of).

    `equity` is the stored value (pass it to float()); `as_of` is kept only for closing marks.
    """
    marks = []
    for sequence, at, equity, close, as_of in db.execute(_MARK_FIELDS):
        if sequence in excluded:
            continue
        moment = instant(at)
        if until is None or moment <= until:
            marks.append((sequence, at, moment, equity, close, as_of if close else None))
    return marks


def event_bodies(db, sequences):
    """{sequence: parsed body} for the given event sequences, read in bounded batches."""
    sequences, bodies = sorted(set(sequences)), {}
    for start in range(0, len(sequences), 500):
        batch = sequences[start:start + 500]
        bodies.update((sequence, json.loads(body)) for sequence, body in db.execute(
            "SELECT sequence,body FROM events WHERE sequence IN (%s)" % ",".join("?" * len(batch)), batch))
    return bodies


def benchmark_events(db, instants, *, until=None):
    """The last benchmark event at each of `instants`, and at the latest instant, as (sequence, at, "benchmark", body).

    Readers key benchmarks by instant, so a later event at the same instant replaces an earlier one.
    """
    matched, latest, latest_at = {}, None, None
    for sequence, at in db.execute("SELECT sequence,at FROM events WHERE kind='benchmark' ORDER BY sequence"):
        moment = instant(at)
        if until is not None and moment > until:
            continue
        if moment in instants:
            matched[moment] = (sequence, at)
        if latest_at is None or moment >= latest_at:
            latest, latest_at = (sequence, at), moment
    wanted = sorted(set(matched.values()) | ({latest} if latest else set()))
    bodies = event_bodies(db, [sequence for sequence, _ in wanted])
    return [(sequence, at, "benchmark", bodies[sequence]) for sequence, at in wanted]


def outcome_inputs(db, marks, *, until=None):
    """The account and benchmark events `forward_outcomes` reads, as (sequence, at, kind, body).

    It uses each New York date's last mark (by time, then sequence), the date
    its session calendar starts from (the earliest local date of any mark),
    the benchmarks at those instants, and the latest benchmark, which can set
    its clock. Passing only these gives the same result as passing all of them.
    """
    zone, last, earliest = ZoneInfo("America/New_York"), {}, None
    for mark in marks:
        sequence, moment = mark[0], mark[2]
        day = moment.astimezone(zone).date()
        if day not in last or (moment, sequence) >= (last[day][2], last[day][0]):
            last[day] = mark
        if earliest is None or moment.date() < earliest[2].date():
            earliest = mark
    kept = {mark[0]: mark for mark in last.values()}
    if earliest is not None:
        kept[earliest[0]] = earliest
    accounts = [(sequence, at, "account", {"equity": equity}) for sequence, at, _, equity, _, _ in kept.values()]
    return sorted(accounts + benchmark_events(db, {mark[2] for mark in kept.values()}, until=until))


def outcome_events(journal, now):
    """`forward_outcomes` input visible at `now`, with review._events' exclusions, without every mark."""
    from .ledger_audit import audit_connection
    now = instant(now)
    with journal.connection() as db:
        db.execute("BEGIN")
        integrity = audit_connection(db)
        reviews = [(sequence, at, "postmarket_review", json.loads(body)) for sequence, at, body in db.execute(
            "SELECT sequence,at,body FROM events WHERE kind='postmarket_review' ORDER BY sequence") if instant(at) <= now]
        # A failed reconciliation withholds every account mark, as review._events does.
        marks = [] if integrity["status"] == "failed" else nav_marks(
            db, until=now, excluded=set(integrity.get("invalid_account_sequences", [])))
        rows = reviews + outcome_inputs(db, marks, until=now)
    return [(at, kind, body) for _, at, kind, body in sorted(rows, key=lambda row: row[0])]


def forward_outcomes(events):
    """Portfolio outcomes after a memo, explicitly NOT its causal treatment effect."""
    if not events:
        return []
    now = max(instant(at) for at, _, _ in events)
    reviews = [b for at, kind, b in events if kind == "postmarket_review" and instant(b["completed_at"]) <= now]
    accounts = [(instant(at), body) for at, kind, body in events if kind == "account" and instant(at) <= now]
    if not reviews or not accounts:
        return []
    benchmarks = {instant(at): b for at, kind, b in events if kind == "benchmark" and instant(at) <= now}
    zone = ZoneInfo("America/New_York")
    schedule = mcal.get_calendar("NYSE").schedule(start_date=min(at.date() for at, _ in accounts), end_date=now.date())
    completed = {day.date(): market.market_close for day, market in schedule.iterrows() if market.market_close <= now}
    # The last observation of each completed session, computed once. For a memo
    # available at time T, a session counts when its last observation is >= T.
    last_by_day = {}
    for at, body in accounts:
        day = at.astimezone(zone).date()
        if day in completed and (day not in last_by_day or at >= last_by_day[day][0]):
            last_by_day[day] = (at, body)
    sessions = sorted(last_by_day.values(), key=lambda item: item[0])
    result = []
    for packet in reviews:
        available = instant(packet["completed_at"])
        daily = [item for item in sessions if item[0] >= available]
        row = {"version": packet["version"], "available_at": packet["completed_at"], "mock": packet["mock"],
               "observed_sessions": len(daily), "horizons": {},
               "interpretation": "Following portfolio observations include later decisions and memos; no causal attribution. "
                                 "Marks may be intraday. Returns exclude model API charges and are not cash-flow adjusted."}
        for horizon in (5, 20):
            # Need a baseline PLUS horizon observed sessions, never pad missing dates.
            if len(daily) <= horizon:
                row["horizons"][str(horizon)] = {"status": "insufficient_observations", "needed": horizon + 1}
                continue
            first, last = daily[0], daily[horizon]
            baseline = float(first[1]["equity"])
            value = float(last[1]["equity"])
            comparison = {}
            for symbol in ("SPY", "QQQ"):
                begin = benchmarks.get(first[0], {}).get(symbol)
                end = benchmarks.get(last[0], {}).get(symbol)
                comparison[symbol] = float(end) / float(begin) - 1 if begin and end else None
            row["horizons"][str(horizon)] = {"status": "observed", "from": first[0].isoformat(),
                "through": last[0].isoformat(), "portfolio_return": value / baseline - 1 if baseline > 0 else None,
                "benchmark_price_returns": comparison}
        result.append(row)
    return result


def _history_digest(directory):
    paths = sorted(Path(directory).glob("*.csv"))
    if not paths:
        raise RiskError("Qualification requires a frozen CSV history directory")
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def qualify(args, config):
    """Run the same native engine/data/risk twice, with and without one frozen memo."""
    from .run import csv_data, run_backtest
    from .report import report
    if not args.data_dir or not args.memory_file:
        raise RiskError("qualify requires --data-dir and --memory-file; input history must be identical in both runs")
    packet = json.loads(args.memory_file.read_text(encoding="utf-8"))
    start = datetime.fromisoformat(args.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("America/New_York"))
    frozen_memory(packet, start)
    data = csv_data(args.data_dir)
    if set(config["universe"]) - {asset.symbol for asset in data}:
        raise RiskError("Qualification history must cover the complete configured eligible universe")
    identity = _history_digest(args.data_dir)
    results = {}
    for variant in ("baseline", "candidate"):
        child = copy.copy(args)
        child.mode, child.run_dir = "backtest", args.run_dir / variant
        child.memory_mode = "disabled" if variant == "baseline" else "frozen"
        child.frozen_memory = packet if variant == "candidate" else None
        child.run_dir.mkdir(parents=True, exist_ok=True)
        if _history_digest(args.data_dir) != identity:
            raise RiskError("Qualification input history changed")
        run_backtest(child, config)
        results[variant] = report(child.run_dir, config.get("model_prices_per_million"))
    if _history_digest(args.data_dir) != identity:
        raise RiskError("Qualification input history changed during replay")
    a, b = results["baseline"], results["candidate"]
    sufficient = min(a["observations"], b["observations"]) >= 20
    better = (sufficient and b["return"] is not None and a["return"] is not None and b["return"] > a["return"] and
              b["max_observed_drawdown"] <= a["max_observed_drawdown"] and
              not a["fill_details_incomplete"] and not b["fill_details_incomplete"])
    summary = {"memory_version": packet["version"], "memory_available_at": packet["completed_at"],
        "scripted_model": args.scripted, "history_sha256": identity, "window": {"start": args.start, "end": args.end},
        "baseline": a, "candidate": b, "status": "insufficient_sample" if not sufficient else (
            "candidate_better_in_this_replay" if better else "no_demonstrated_improvement"),
        "auto_promoted": False,
        "limitations": "One paired replay is exploratory, not proof of profitability or an independent holdout guarantee. "
                        "No human approval is assumed; major historical trades remain blocked. "
                        "Model knowledge may contain future outcomes; audit tool evidence and historical versions. "
                        "No risk, universe, live permission or running strategy is changed."}
    (args.run_dir / "qualification.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
