"""Backup recovery and bounded notification reads on synthetic local journals only."""
import gc
import json
import sqlite3
import tracemalloc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.autonomous_fund import deploy
from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.risk import RiskError


NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def snapshots(tmp_path, keep=2):
    journal = Journal(tmp_path / "book", "synthetic-resilience-account")
    journal.event(NOW, "startup", {"mode": "local"})
    folder = tmp_path / "backups"
    made = [deploy.rotate(journal.root, folder, keep=keep, now=NOW + timedelta(days=i)) for i in range(keep)]
    gc.collect()  # Older runners left snapshot handles to garbage collection on Windows.
    return journal, folder, made


@pytest.mark.parametrize("failure", ["missing_source", "corrupt_source", "existing_destination"])
def test_backup_preflight_never_prunes_existing_snapshots(tmp_path, monkeypatch, failure):
    journal, folder, made = snapshots(tmp_path)
    source, now = journal.root, NOW + timedelta(days=2)
    if failure == "missing_source":
        source = tmp_path / "missing"
    elif failure == "corrupt_source":
        (source / "state.sqlite3").write_bytes(b"not a SQLite database")
    else:
        now = NOW + timedelta(days=1)
    monkeypatch.setattr("shutil.disk_usage", lambda path: SimpleNamespace(free=deploy.BACKUP_RESERVE_BYTES - 1))
    before = {path.name: (path / "state.sqlite3").read_bytes() for path in made}
    with pytest.raises((RiskError, sqlite3.DatabaseError)):
        deploy.rotate(source, folder, keep=2, now=now)
    assert {path.name: (path / "state.sqlite3").read_bytes() for path in deploy._snapshots(folder)} == before


def test_low_space_never_spends_the_last_completed_backup(tmp_path, monkeypatch):
    journal, folder, made = snapshots(tmp_path, keep=1)
    monkeypatch.setattr("shutil.disk_usage", lambda path: SimpleNamespace(free=deploy.BACKUP_RESERVE_BYTES - 1))
    with pytest.raises(deploy.BackupSpaceError):
        deploy.rotate(journal.root, folder, keep=1, now=NOW + timedelta(days=1))
    assert deploy._snapshots(folder) == made


def test_copy_failure_keeps_a_completed_backup_after_low_space_pruning(tmp_path, monkeypatch):
    journal, folder, made = snapshots(tmp_path)
    monkeypatch.setattr("shutil.disk_usage", lambda path: SimpleNamespace(free=deploy.BACKUP_RESERVE_BYTES - 1))

    def fail(*args):
        raise sqlite3.OperationalError("synthetic copy failure")

    monkeypatch.setattr(deploy, "backup", fail)
    with pytest.raises(sqlite3.OperationalError):
        deploy.rotate(journal.root, folder, keep=1, now=NOW + timedelta(days=2))
    assert deploy._snapshots(folder) == made[-1:]


def test_hard_interrupted_copies_are_not_counted_as_completed_backups(tmp_path):
    journal, folder, made = snapshots(tmp_path, keep=1)
    interrupted = folder / "20261109-120000"
    interrupted.mkdir()
    (interrupted / "state.sqlite3").write_bytes((made[0] / "state.sqlite3").read_bytes())
    (interrupted / ".incomplete").touch()
    assert deploy._snapshots(folder) == made
    completed = deploy.rotate(journal.root, folder, keep=1, now=NOW + timedelta(days=2))
    assert deploy._snapshots(folder) == [completed]
    assert (interrupted / ".incomplete").exists()  # Never claim or prune a copy that did not complete.


@pytest.mark.parametrize("fail_copy", [False, True])
def test_backup_closes_every_connection_and_removes_incomplete_copies(tmp_path, monkeypatch, fail_copy):
    journal = Journal(tmp_path / "book", "synthetic-copy-account")
    destination, opened = tmp_path / "copy", []
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            if fail_copy:
                raise sqlite3.OperationalError("synthetic interrupted copy")
            return super().backup(target, **kwargs)

    def connect(*args, **kwargs):
        db = original(*args, factory=Connection, **kwargs)
        opened.append(db)  # Retaining references makes closure independent of CPython garbage collection.
        return db

    monkeypatch.setattr(deploy.sqlite3, "connect", connect)
    try:
        if fail_copy:
            with pytest.raises(sqlite3.OperationalError):
                deploy.backup(journal.root, destination)
            assert not destination.exists()
        else:
            deploy.backup(journal.root, destination)
            assert (destination / "STOP").is_file()
        assert len(opened) >= 2
        for db in opened:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
    finally:
        for db in opened:
            db.close()


@pytest.mark.parametrize("bad_body", ["{broken", "[]", "null"])
def test_bad_notices_are_visible_in_exports_and_do_not_hide_later_events(tmp_path, bad_body):
    journal = Journal(tmp_path, "synthetic-corrupt-notice")
    with journal.connection() as db:
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,'notification',?)", (NOW.isoformat(), bad_body))
    journal.event(NOW, "notification", {"kind": "synthetic_later_notice", "details": {}})
    for rows in (journal.all_events(), journal.events(("notification",)), journal.export()):
        assert [row["body"]["kind"] for row in rows] == ["malformed_notification_body", "synthetic_later_notice"]
    exported = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert exported[0]["sequence"] < exported[1]["sequence"]
    assert journal.export() == []  # The diagnostic row must not make incremental exports replay history.
    with journal.connection() as db:
        assert db.execute("SELECT body FROM events ORDER BY sequence LIMIT 1").fetchone()[0] == bad_body


def test_corrupt_money_events_remain_strict(tmp_path):
    journal = Journal(tmp_path, "synthetic-strict-ledger")
    with journal.connection() as db:
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,'fill','{broken')", (NOW.isoformat(),))
    with pytest.raises(json.JSONDecodeError):
        journal.all_events()


def test_report_and_review_readers_survive_bad_notices(tmp_path):
    from scripts.autonomous_fund.evaluation import event_bodies
    from scripts.autonomous_fund.notifications import _weekly_events
    from scripts.autonomous_fund.report import report
    from scripts.autonomous_fund.review import _events

    journal = Journal(tmp_path, "synthetic-report-notice")
    journal.event(NOW, "startup", {"mode": "local"})
    with journal.connection() as db:
        sequence = db.execute("INSERT INTO events(at,kind,body) VALUES(?,'notification','{broken')",
                              (NOW.isoformat(),)).lastrowid
    journal.event(NOW, "notification", {"kind": "blocked", "details": "synthetic later notice"})
    snapshot = report(tmp_path, write_files=False)
    assert snapshot["blocked_iterations"] == 1
    assert snapshot["malformed_notifications"] == 1
    assert report(tmp_path, write_files=False, as_of=NOW)["blocked_iterations"] == 1
    with journal.connection() as db:
        assert event_bodies(db, [sequence])[sequence]["kind"] == "malformed_notification_body"
        weekly = _weekly_events(db, NOW, ZoneInfo("America/New_York"))
    assert any(body.get("kind") == "malformed_notification_body" for _, _, body in weekly)
    assert any(row["body"].get("kind") == "malformed_notification_body" for row in _events(journal, NOW))


def test_daily_document_keeps_corrupt_notice_diagnostics(tmp_path):
    from scripts.autonomous_fund.notifications import daily_document

    journal = Journal(tmp_path, "synthetic-daily-notice")
    with journal.connection() as db:
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,'notification','{broken')", (NOW.isoformat(),))
    packet = {
        "session_date": str(NOW.date()), "completed_at": NOW.isoformat(), "version": 1, "mock": True,
        "models": {}, "research": {"summary": "Synthetic research", "evidence": []},
        "session": {"observed_pnl_usd": None, "observed_return": None, "benchmark_observed_price_returns": {},
                    "baseline_account": None, "last_observed_account": None, "account_observation_gaps": [],
                    "limitations": "Synthetic report without account observations"},
        "review": {"summary": "Synthetic review", "selection_adjustments": [], "timing_adjustments": [],
                   "retained_lessons": [], "retired_hypotheses": [], "data_gaps": [],
                   "strategy_memo": "Synthetic memo", "confidence": 0.5}}
    journal.event(NOW, "postmarket_review", packet)
    with journal.connection() as db:
        sequence = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review'").fetchone()[0]
    filename, document = daily_document(tmp_path, sequence)
    assert filename == "daily-2026-11-08.md"
    assert "malformed_notification_body" in document
    assert "Synthetic memo" in document


class Transport:
    chat_id = "synthetic-bounded-recipient"

    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return {"ok": True, "message_id": len(self.messages)}

    def send_document(self, caption, filename, document):
        return self.send(caption + "\n" + document)


def delivery_queue(tmp_path, count):
    journal = Journal(tmp_path, "synthetic-queue-account")
    transport = Transport()
    monitor = Monitor(tmp_path, transport)
    with journal.connection() as db:
        db.executemany("INSERT INTO events(at,kind,body) VALUES(?,'notification',?)",
                       [(NOW.isoformat(), encode({"kind": "synthetic", "details": "x" * 4096}))] * count)
        db.executemany("INSERT INTO fund_deliveries VALUES(?,'pending',0,?,NULL,NULL)",
                       [(i, NOW.isoformat()) for i in range(1, count + 1)])
    return journal, monitor, transport


def test_delivery_memory_is_bounded_by_the_batch_not_the_backlog(tmp_path):
    # Warm the actual rendering path, including lazy imports, in a separate synthetic account.
    # This measures backlog growth independently of test order or first-call import overhead.
    _, warmup, _ = delivery_queue(tmp_path / "warmup", 1)
    warmup._deliver(NOW)
    journal, monitor, transport = delivery_queue(tmp_path / "measured", 2000)
    tracemalloc.start()
    try:
        monitor._deliver(NOW)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(transport.messages) == 2
    assert peak < 2 * 1024 * 1024
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_deliveries WHERE state='pending'").fetchone()[0] == 1998


def test_a_notice_corrupted_after_enqueue_does_not_stop_delivery(tmp_path):
    journal, monitor, transport = delivery_queue(tmp_path, 2)
    with journal.connection() as db:
        db.execute("UPDATE events SET body='{broken' WHERE sequence=1")
    monitor._deliver(NOW)
    with journal.connection() as db:
        states = [tuple(row) for row in db.execute("SELECT state,last_error FROM fund_deliveries ORDER BY sequence")]
    assert states == [("suppressed", "malformed_notification_body"), ("sent", None)]
    assert len(transport.messages) == 1


@pytest.mark.parametrize("only", [None, 4])
def test_due_filter_and_explicit_selection_apply_before_the_batch_limit(tmp_path, only):
    journal, monitor, transport = delivery_queue(tmp_path, 4)
    with journal.connection() as db:
        # This instant is still in the future even though its UTC-offset spelling sorts before NOW.
        future = (NOW + timedelta(hours=1)).astimezone(timezone(timedelta(hours=-5))).isoformat()
        db.execute("UPDATE fund_deliveries SET next_at=? WHERE sequence IN (1,2)", (future,))
        db.execute("UPDATE fund_deliveries SET state='sending',next_at=? WHERE sequence=2",
                   ((NOW + timedelta(microseconds=100)).isoformat(),))
        db.execute("UPDATE fund_deliveries SET next_at=? WHERE sequence=3",
                   ((NOW - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=5))).isoformat(),))
    monitor._deliver(NOW, only=only)
    with journal.connection() as db:
        sent = [row[0] for row in db.execute("SELECT sequence FROM fund_deliveries WHERE state='sent' ORDER BY sequence")]
    assert sent == ([3, 4] if only is None else [4])
    assert len(transport.messages) == len(sent)
