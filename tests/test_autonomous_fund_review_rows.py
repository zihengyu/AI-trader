"""The post-market review reads only the events it uses, with the same result as reading the whole journal.

The oracle is the former path: _session/_inputs over every event from
_events(journal, now). Synthetic history on a real local test book; no
network, broker or model inference.
"""
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund import review
from scripts.autonomous_fund.review import _available, _events, _inputs, _review_rows, _session
from scripts.autonomous_fund.risk import instant
from .test_autonomous_fund_closed_ledger import traded_book
from .test_autonomous_fund_report_memory import history


def more_history(root, seed):
    """Fills, decisions, notices, research and saved review stages around the NAV history."""
    rng, db = random.Random(seed * 7), sqlite3.connect(root / "state.sqlite3")
    day, rows = datetime(2026, 11, 2, tzinfo=timezone.utc), []
    for _ in range(30):
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        noon = day.replace(hour=17)
        if rng.random() < 0.5:
            rows.append((noon.isoformat(), "fill", {"broker_id": f"b{rng.random()}", "symbol": "ALFA", "side": "buy",
                                                     "quantity": "1", "price": "100", "trade_cost": "0.1"}))
        if rng.random() < 0.5:
            rows.append((noon.isoformat(), "decision_validated", {"targets": []}))
        rows.append((noon.isoformat(), "notification", {"kind": "synthetic", "details": {}}))
        rows.append((noon.isoformat(), "agent", {"name": "researcher", "phase": "intraday", "summary": "s"}))
        rows.append((day.replace(hour=13).isoformat(), "premarket_research", {
            "session_open": day.replace(hour=14, minute=30).isoformat(), "research": {"summary": "pre"}}))
        if rng.random() < 0.3:  # A saved research stage, sometimes the next morning.
            done = day.replace(hour=22) + timedelta(hours=rng.choice([0, 14]))
            rows.append((done.isoformat(), "postmarket_research", {
                "session_date": str(day.date()), "completed_at": done.isoformat(),
                "market_close": day.replace(hour=21).isoformat(), "research": {"summary": "post"}}))
        rows.append((day.replace(hour=15).isoformat(), "iteration_finished", {}))
    if seed % 2:
        rng.shuffle(rows)
    db.executemany("INSERT INTO events(at,kind,body) VALUES(?,?,?)", [(a, k, json.dumps(b)) for a, k, b in rows])
    db.commit()
    db.close()


def former(journal, now):
    rows = _events(journal, now)
    session = _session(rows, now)
    return session, rows


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_review_inputs_equal_those_from_the_whole_journal(tmp_path, seed):
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, seed)
    more_history(tmp_path, seed)
    start, compared, smaller = datetime(2026, 11, 2, 20, tzinfo=timezone.utc), 0, 0
    for hours in range(0, 24 * 34, 29):  # Clocks at shifting times of day: before, during and after sessions.
        now = start + timedelta(hours=hours)
        session, rows = former(journal, now)
        light_session, light_rows = _review_rows(journal, now)
        assert light_session == session, now
        if not session:
            continue
        day = session["session_date"]
        reviewed = any(r["kind"] == "postmarket_review" and r["body"]["session_date"] == day for r in rows)
        assert any(r["kind"] == "postmarket_review" and r["body"]["session_date"] == day for r in light_rows) == reviewed
        if reviewed:
            continue
        assert _inputs(light_rows, session, now) == _inputs(rows, session, now), now
        saved = [r["body"] for r in rows if r["kind"] == "postmarket_research" and
                 r["body"]["session_date"] == day and _available(r["body"], now)]
        light = [r["body"] for r in light_rows if r["kind"] == "postmarket_research" and
                 r["body"]["session_date"] == day and _available(r["body"], now)]
        assert light[-1:] == saved[-1:]
        assert len(light_rows) <= len(rows)
        compared, smaller = compared + 1, smaller + (len(light_rows) < len(rows))
    assert compared >= 5 and smaller >= 4  # Unreviewed sessions with baselines, benchmarks and research.


def test_a_reviewed_day_returns_before_reading_the_session(tmp_path, monkeypatch):
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, 1)
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)
    session, rows = former(journal, now)
    day = session["session_date"]
    journal.event(now - timedelta(hours=1), "postmarket_review", {"session_date": day, "completed_at": str(now)})
    reads = []
    original = review.__dict__.get("_rows_at")
    monkeypatch.setattr(review, "_rows_at", lambda db, sequences: reads.append(len(sequences)) or original(db, sequences))
    light_session, light_rows = _review_rows(journal, now)
    assert light_session["session_date"] == day
    assert [r["kind"] for r in light_rows if r["kind"] == "postmarket_review"] == ["postmarket_review"]
    assert reads and max(reads) <= 40  # The reviewed day's review plus one event per observed day, not the session.


def test_a_failed_reconciliation_hides_account_marks_as_before(tmp_path):
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, 2)
    more_history(tmp_path, 2)
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash='1'")  # The ledger no longer reconciles.
    compared = 0
    for day in range(3, 30, 3):
        now = datetime(2026, 11, day, 23, tzinfo=timezone.utc)
        session, rows = former(journal, now)
        light_session, light_rows = _review_rows(journal, now)
        assert light_session == session and not any(r["kind"] == "account" for r in light_rows)
        if session and not any(r["kind"] == "postmarket_review" and r["body"]["session_date"] == session["session_date"]
                               for r in rows):
            assert _inputs(light_rows, session, now) == _inputs(rows, session, now)
            compared += 1
    assert compared >= 3


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_weekly_facts_equal_those_from_every_mark_and_benchmark(tmp_path, seed):
    from zoneinfo import ZoneInfo
    from scripts.autonomous_fund.notifications import WEEKLY_KINDS, _weekly_events, _weekly_facts, week_of
    from scripts.autonomous_fund.risk import instant
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, seed)
    more_history(tmp_path, seed)
    rng = random.Random(seed)
    with journal.connection() as db:  # Later benchmarks with missing, zero or broken prices keep earlier valid ones.
        for at, in db.execute("SELECT at FROM events WHERE kind='benchmark'").fetchall():
            if rng.random() < 0.2:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (at, "benchmark", json.dumps(
                    rng.choice([{"SPY": "0"}, {"SPY": "x", "QQQ": None}, {"QQQ": "-1"}, {"IWM": "200"}]))))
    zone, compared = ZoneInfo("America/New_York"), 0
    for day in ("2026-11-06", "2026-11-13", "2026-11-20", "2026-12-04"):
        cutoff = datetime.fromisoformat(day + "T23:00:00+00:00")
        monday, sunday, _ = week_of(day)
        with journal.connection() as db:
            every = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
                "SELECT at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(WEEKLY_KINDS)),
                WEEKLY_KINDS) if instant(at) <= cutoff]
            reduced = _weekly_events(db, cutoff, zone)
        expected = _weekly_facts(every, monday, sunday, zone)
        assert _weekly_facts(reduced, monday, sunday, zone) == expected
        assert len(reduced) < len(every)
        compared += expected.get("week_return") is not None
    assert compared >= 3


def test_bounded_journal_reads_match_filtering_every_event(tmp_path):
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, 3)
    every = journal.all_events()
    after = every[len(every) // 2]["sequence"]
    assert journal.events(after=after) == [r for r in every if r["sequence"] > after]
    assert journal.events(("benchmark", "fill")) == [r for r in every if r["kind"] in {"benchmark", "fill"}]
    assert journal.events(("account",), latest=3) == [r for r in every if r["kind"] == "account"][-3:]
    assert journal.events(("no_such_kind",), latest=1) == []


def test_the_review_and_weekly_loads_do_not_hold_every_mark(tmp_path):
    import tracemalloc
    from zoneinfo import ZoneInfo
    from scripts.autonomous_fund.notifications import WEEKLY_KINDS, _weekly_events
    from scripts.autonomous_fund.risk import instant
    _, _, journal, _, _ = traded_book(tmp_path)
    with journal.connection() as db:  # 20 sessions of marks every 30 s with a benchmark each, 2 KB per mark.
        _, raw = db.execute("SELECT at,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        body, rows, day = {**json.loads(raw), "padding": "x" * 2000}, [], datetime(2026, 11, 3, tzinfo=timezone.utc)
        for _ in range(28):
            day += timedelta(days=1)
            if day.weekday() < 5:
                for i in range(0, 390 * 60, 30 * 6):  # Every 3 minutes keeps the test quick; the shape is the same.
                    at = (day.replace(hour=14, minute=30) + timedelta(seconds=i)).isoformat()
                    rows += [(at, "account", json.dumps({**body, "as_of": at})), (at, "benchmark", '{"SPY": "600"}')]
        db.executemany("INSERT INTO events(at,kind,body) VALUES(?,?,?)", rows)
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)

    def peak(step):
        tracemalloc.start()
        try:
            step()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def weekly_every():
        with journal.connection() as db:
            return [(at, kind, json.loads(b)) for at, kind, b in db.execute(
                "SELECT at,kind,body FROM events WHERE kind IN (%s)" % ",".join("?" * len(WEEKLY_KINDS)), WEEKLY_KINDS)
                if instant(at) <= now]

    def weekly_light():
        with journal.connection() as db:
            return _weekly_events(db, now, ZoneInfo("America/New_York"))
    before, after = peak(lambda: _events(journal, now)), peak(lambda: _review_rows(journal, now))
    assert after * 5 < before, (after, before)  # The marks hold about 6 MB of text.
    before, after = peak(weekly_every), peak(weekly_light)
    assert after * 5 < before, (after, before)


def test_event_reads_without_account_marks_skip_the_nav_audit(tmp_path, monkeypatch):
    from scripts.autonomous_fund import ledger_audit
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, 1)
    more_history(tmp_path, 1)
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)
    kinds = ("postmarket_review", "decision_validated", "order_submitted", "research_idea")
    expected = [r for r in _events(journal, now) if r["kind"] in kinds]
    calls = []
    original = ledger_audit.audit_connection
    monkeypatch.setattr(ledger_audit, "audit_connection", lambda db, **k: calls.append(1) or original(db, **k))
    assert _events(journal, now, kinds=kinds) == expected and calls == []  # It only withholds account marks.
    _events(journal, now, kinds=("account",))
    assert calls == [1]


@pytest.mark.parametrize("seed", [1, 2])
def test_attribution_marks_equal_those_from_every_account_event(tmp_path, seed):
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from scripts.autonomous_fund.strategy import AutonomousFund
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, seed)
    zone, fund = ZoneInfo("America/New_York"), SimpleNamespace(journal=journal)

    def local_day(row):
        closed = (row["body"].get("valuation") or {}).get("session_close")
        return instant(closed or row["at"]).astimezone(zone).date().isoformat()
    compared = 0
    for offset in range(1, 30, 3):
        now = datetime(2026, 11, 3, 23, tzinfo=timezone.utc) + timedelta(days=offset)
        day = (now - timedelta(days=offset % 2)).astimezone(zone).date().isoformat()
        accounts = [r for r in _events(journal, now, kinds=("account",))]
        before = [r for r in accounts if local_day(r) < day]
        ending = [r for r in accounts if local_day(r) == day]
        light = AutonomousFund._attribution_marks(fund, day, now, zone)
        assert light == ((before[-1] if before else None), (ending[-1] if ending else None)), now
        compared += bool(before and ending)
    assert compared >= 3

