"""Separate repeat assessments from executable changes and actual selected-account evidence."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.decision_activity import activity, lines
from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.status import summary

OPEN = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
NOW = OPEN + timedelta(hours=7)


def plan(book, index, *, old_format=False, targets=None):
    at = OPEN + timedelta(minutes=index * 10)
    key = f"synthetic-plan-{index}"
    body = {"as_of": at.isoformat(), "expires_at": (at + timedelta(minutes=30)).isoformat(),
            "next_review_at": (at + timedelta(minutes=10)).isoformat(), "rationale": "Synthetic assessment",
            "targets": [{"symbol": "ALFA", "weight": .05}] if targets is None else targets}
    book.event(at, "decision_accepted", {"id": key, "until_at": body["next_review_at"], "decision": body})
    book.event(at, "decision_validated", {**body, "prompt_version": "fixture",
                                          **({} if old_format else {"decision_id": key})})
    return at, key


@pytest.mark.parametrize("old_format", [False, True])
def test_twenty_assessments_and_one_notification_are_distinguished_from_one_trade(tmp_path, old_format):
    book = Journal(tmp_path, "synthetic-activity-account")
    for index in range(20):
        at, key = plan(book, index, old_format=old_format)
        if index == 0:
            book.event(at, "notification", {"kind": "decision_changed", "details": {
                "at": at.isoformat(), **({} if old_format else {"decision_id": key})}})
            book.event(at + timedelta(seconds=1), "order_submitted", {
                "decision_id": key, "broker_id": "synthetic-buy", "side": "buy", "symbol": "ALFA"})
            book.event(at + timedelta(seconds=2), "fill", {
                "broker_id": "synthetic-buy", "quantity": "1", "price": "90", "symbol": "ALFA", "side": "buy"})
        book.event(at + timedelta(seconds=3), "allocation_complete", {"decision_id": key})
    with book.connection() as db:
        seq = db.execute("SELECT sequence FROM events WHERE kind='notification'").fetchone()[0]
        db.execute("CREATE TABLE fund_deliveries(sequence INTEGER PRIMARY KEY,state TEXT)")
        db.execute("INSERT INTO fund_deliveries VALUES(?, 'sent')", (seq,))
    result = activity(tmp_path, "2026-11-02", NOW)
    assert result["validated"] == 20 and result["changes"] == {"initial": 1, "maintain": 19}
    assert result["orders"] == result["fills"] == result["notifications"] == 1
    assert result["deliveries"] == {"sent": 1}
    assert len(result["decisions"][0]["fills"]) == 1
    assert all(not item["fills"] for item in result["decisions"][1:])
    text = "\n".join(lines(result, timeline=True))
    assert "维持目标组合 19 次" in text and "已送达 1 条" in text
    assert "下单 1／成交 1" in text and "维持组合也是一次复核" in text


def test_level_changes_minor_weights_and_failures_have_separate_counts(tmp_path):
    book = Journal(tmp_path, "synthetic-activity-variations")
    target = {"symbol": "BETA", "weight": .05, "entry_limit": 100, "stop_loss": 90}
    plan(book, 0, targets=[target])
    plan(book, 1, targets=[{**target, "entry_limit": 101}])
    plan(book, 2, targets=[{**target, "entry_limit": 101, "weight": .055}])
    plan(book, 3, targets=[{**target, "entry_limit": 101, "weight": .08}])
    book.event(OPEN + timedelta(hours=1), "decision_backoff", {"role": "manager"})
    book.event(OPEN + timedelta(hours=1), "review_skipped", {"reason": "nothing changed"})
    result = activity(tmp_path, now=NOW)
    assert result["changes"] == {"initial": 1, "levels": 1, "minor": 1, "portfolio": 1}
    assert result["failures"] == result["quiet_skips"] == 1


def test_dates_use_new_york_and_future_events_are_excluded(tmp_path):
    book = Journal(tmp_path, "synthetic-offset-account")
    # These are the same New York evening, despite different UTC dates and offsets.
    for at in ("2026-11-02T23:00:00+00:00", "2026-11-03T08:00:00+08:00"):
        book.event(at, "decision_validated", {"targets": []})
    book.event("2026-11-04T10:00:00-05:00", "decision_validated", {"targets": []})
    now = datetime.fromisoformat("2026-11-03T01:00:00+00:00")
    result = activity(tmp_path, "2026-11-02", now)
    assert result["validated"] == result["cumulative_validated"] == 2


def test_status_does_not_truncate_a_busy_days_count_to_two_hundred(tmp_path):
    book = Journal(tmp_path, "synthetic-busy-account")
    with book.connection() as db:
        db.executemany("INSERT INTO events(at,kind,body) VALUES(?,?,?)", [
            ((OPEN + timedelta(minutes=index)).isoformat(), "decision_validated", encode({"targets": []}))
            for index in range(300)])
    text = summary(tmp_path, "local", NOW)
    assert "今天 300 次有效决策" in text and "累计 300 次" in text


def test_missing_accounts_are_never_created_and_invalid_dates_are_rejected(tmp_path):
    missing = tmp_path / "not-an-account"
    with pytest.raises(RiskError, match="No existing"):
        activity(missing, now=NOW)
    assert not missing.exists()
    with pytest.raises(RiskError, match="YYYY-MM-DD"):
        activity(tmp_path, "last night", NOW)


def test_decisions_command_uses_the_configured_account_and_is_read_only(tmp_path):
    from tests.test_autonomous_fund_scripts import account_book, script
    book, env = account_book(tmp_path)
    journal = Journal(book, "synthetic-script-account")
    plan(journal, 0)
    other = tmp_path / "other-account"
    other_book = Journal(other, "synthetic-other-account")
    for index in range(3):
        plan(other_book, index)
    before = (book / "state.sqlite3").read_bytes()
    result = script("fund-logs.sh", tmp_path, env, "decisions", "2026-11-02")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "通过校验 1 次" in result.stdout and "通过校验 3 次" not in result.stdout
    assert (book / "state.sqlite3").read_bytes() == before
