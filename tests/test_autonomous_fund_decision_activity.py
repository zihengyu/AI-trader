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


def test_timeline_shows_why_the_manager_was_woken_and_what_it_said(tmp_path):
    book = Journal(tmp_path, "synthetic-activity-account")
    plan(book, 0)
    wake = OPEN + timedelta(minutes=5)
    book.event(wake, "review_triggered", {"reason": "news", "title": "Synthetic headline about ALFA guidance",
                                          "review_at": wake.isoformat()})
    book.event(wake, "review_triggered", {"reason": "price_move", "symbol": "ALFA", "review_at": wake.isoformat()})
    book.event(wake, "event_triage", {"id": "t1", "action": "audit", "reasons": ["news", "price_move"],
                                      "verdict": {"escalate": False, "importance": "low",
                                                  "affected_symbols": [], "reason": "Routine coverage"}})
    at, key = plan(book, 1, targets=[{"symbol": "ALFA", "weight": .02}])
    book.event(at, "triage_scored", {"id": "t1", "miss": True, "manager_changed": True})
    text = "\n".join(lines(activity(tmp_path, "2026-11-02", now=NOW), timeline=True))
    assert "触发：资讯 1 次、价格异动 1 次" in text and "Synthetic headline about ALFA" in text
    assert "快速模型判断不需要，抽查仍叫了基金经理（抽查结果：基金经理改了组合，算漏判）｜Routine coverage" in text
    assert text.count("理由：Synthetic assessment") == 2


def test_notification_id_opens_only_its_linked_order_timeline(tmp_path):
    book = Journal(tmp_path, "synthetic-timeline-account")
    at, key = plan(book, 0)
    book.event(at, "notification", {"kind": "decision_changed", "details": {"decision_id": key}})
    book.event(at + timedelta(seconds=50), "order_submitted", {
        "decision_id": key, "broker_id": "order-a", "symbol": "ALFA", "side": "sell",
        "quantity": "3", "limit": "90.12", "send_at": (at + timedelta(seconds=43)).isoformat()})
    book.event(at + timedelta(minutes=2), "fill", {
        "broker_id": "order-a", "symbol": "ALFA", "side": "sell", "quantity": "1", "price": "90.31"})
    book.event(at + timedelta(minutes=3), "order_reprice_cancel", {"broker_id": "order-a"})
    book.event(at + timedelta(minutes=4), "local_order_canceled", {
        "broker_id": "order-a", "reason": "order_expired"})
    book.event(at + timedelta(minutes=5), "local_order_canceled", {"broker_id": "other-order"})
    plan(book, 1)

    data = activity(tmp_path, "2026-11-02", now=NOW)
    item = data["decisions"][0]
    notice = item["notifications"][0]["sequence"]
    text = "\n".join(lines(data, timeline=True, event_sequence=notice))
    assert f"事件 #{item['sequence']}" in text
    assert "计划接纳 11-02 09:30:00 ｜ 有效至 11-02 09:40:00" in text
    assert "限价 $90.12 ｜ 送单 11-02 09:30:43，回执 11-02 09:30:50" in text
    assert "09:32:00 成交观测 1 股 @ $90.31" in text
    assert "09:33:00 申请撤单重报" in text
    assert "09:34:00 模拟券商确认撤单（order_expired）" in text
    assert "09:35:00" not in text and "09:40:00 事件" not in text


def test_target_notice_reports_only_visible_orders_and_fills_from_its_plan(tmp_path):
    from scripts.autonomous_fund.execution import ExecutionState
    from scripts.autonomous_fund.monitor import Monitor
    from .test_autonomous_fund_monitor import FakeTelegram

    book = Journal(tmp_path, "synthetic-notice-account")
    ExecutionState(book)
    expiry = OPEN + timedelta(minutes=10)
    at, key = plan(book, 0)
    with book.connection() as db:
        db.execute("INSERT INTO fund_decisions VALUES(?,?,?,?,?,?)",
                   (key, at.isoformat(), expiry.isoformat(), "{}", "test", "active"))
    for seconds, kind, item in (
        (10, "order_submitted", {"decision_id": "unrelated", "broker_id": "other", "kind": "allocation", "quantity": "3"}),
        (20, "order_submitted", {"decision_id": key, "broker_id": "mine", "kind": "allocation", "quantity": "3"}),
        (40, "fill", {"broker_id": "mine", "symbol": "ALFA", "side": "sell", "quantity": "1", "price": "10"}),
        (50, "fill_reconciled", {"broker_id": "mine", "symbol": "ALFA", "side": "sell", "quantity": "3", "price": "10"}),
    ):
        book.event(at + timedelta(seconds=seconds), kind, item)
    monitor = Monitor(tmp_path, FakeTelegram())
    notice = {"kind": "decision_changed", "details": {"at": at.isoformat(), "decision_id": key}}
    for seconds, expected in ((15, "尚未记录送单"), (25, "已送单 1 笔"), (45, "部分成交"),
                              (55, "已提交订单均已成交"), (601, "计划已过期")):
        outgoing = monitor._outgoing(notice, 1, "", at + timedelta(seconds=seconds))
        assert outgoing[0] == "text" and expected in outgoing[1]
