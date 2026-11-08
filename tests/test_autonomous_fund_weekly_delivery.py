"""Weekly report delivery: a report that cannot be built, a week without its last review, and what status shows.

Synthetic account, reviews and Telegram transport only; no network, broker or model inference.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.notifications import latest_weekly, week_calendar
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.status import weekly_lines
from tests.test_autonomous_fund_monitor import heartbeat
from tests.test_autonomous_fund_scripts import account_book, script
from tests.test_autonomous_fund_weekly_report import DocumentTelegram, close_of, ledger, review

ROOT = Path(__file__).resolve().parents[1]
MONDAY_OPEN = datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc)  # 09:30 New York, after the clocks changed.


def deliveries(journal):
    with journal.connection() as db:
        return {row[0]: (row[1], row[2]) for row in db.execute(
            "SELECT json_extract(e.body,'$.kind'),d.state,d.last_error FROM fund_deliveries d "
            "JOIN events e ON e.sequence=d.sequence")}


def polls(monitor, root, start, minutes):
    for minute in minutes:
        at = start + timedelta(minutes=minute)
        heartbeat(root, at)
        monitor.poll(at)


def daily_only(monkeypatch):
    import scripts.autonomous_fund.notifications as notifications
    monkeypatch.setattr(notifications, "daily_document", lambda root, sequence: ("daily.md", "daily"))
    return notifications


def test_a_report_that_cannot_be_built_does_not_hold_back_later_notices(tmp_path, monkeypatch, capsys):
    # 2026-11-08: a weekly report whose document failed retried for a day and the
    # STOP notice queued behind it was never sent: every later notice waited.
    notifications = daily_only(monkeypatch)

    def broken(root, sequence):
        raise KeyError("synthetic report bug")

    monkeypatch.setattr(notifications, "weekly_document", broken)
    journal, _ = ledger(tmp_path)
    transport = DocumentTelegram()
    monitor = Monitor(tmp_path, transport)
    start = close_of("2026-11-06", 45)
    polls(monitor, tmp_path, start, [0])
    journal.event(start + timedelta(minutes=1), "notification", {"kind": "stop_active", "details": {
        "class": "watchdog", "reason": "heartbeat_stale", "orders": "no new orders"}})
    polls(monitor, tmp_path, start, [2])
    state = deliveries(journal)
    assert state["stop_active"][0] == "sent"  # Within one poll of being queued, despite the failing report.
    assert state["weekly_report"] == ("retry", "message_unavailable_KeyError")
    polls(monitor, tmp_path, start, range(3, 15))
    assert deliveries(journal)["weekly_report"] == ("sent", "message_unavailable_KeyError")
    notice = [m for m in transport.messages if "周报" in m]
    assert len(notice) == 1 and "2026-W45 周报（2026-11-06 收盘后）无法生成（KeyError），已试 3 次" in notice[0]
    assert "bash scripts/fund-logs.sh weekly" in notice[0] and "bash scripts/fund-logs.sh errors" in notice[0]
    assert not [d for d in transport.documents if d[1].startswith("weekly-")]
    errors = capsys.readouterr().err
    assert errors.count("FUND notification_build_error: weekly_report #") == 3 and "synthetic report bug" in errors
    with journal.connection() as db:
        gate = db.execute("SELECT value FROM fund_monitor_meta WHERE key='next_delivery_at'").fetchone()
    assert gate is None  # A local build failure never pauses delivery as a Telegram outage does.


def test_a_transport_outage_still_pauses_the_whole_queue(tmp_path, monkeypatch):
    daily_only(monkeypatch)
    journal, _ = ledger(tmp_path)
    transport = DocumentTelegram()
    transport.send_document = lambda *args: {"ok": False, "error": "telegram_http_429", "retry_after": 600}
    monitor = Monitor(tmp_path, transport)
    start = close_of("2026-11-06", 45)
    polls(monitor, tmp_path, start, [0])
    journal.event(start, "notification", {"kind": "stop_active", "details": {"class": "watchdog"}})
    polls(monitor, tmp_path, start, range(1, 5))
    with journal.connection() as db:
        rows = db.execute("SELECT json_extract(e.body,'$.kind'),d.state,d.last_error FROM fund_deliveries d "
                          "JOIN events e ON e.sequence=d.sequence WHERE d.state!='suppressed' ORDER BY d.sequence").fetchall()
        gate = db.execute("SELECT value FROM fund_monitor_meta WHERE key='next_delivery_at'").fetchone()
    assert tuple(rows[0]) == ("postmarket_report", "retry", "telegram_http_429") and gate is not None
    assert {tuple(row[1:]) for row in rows[1:]} == {("pending", None)} and rows[-1][0] == "stop_active"
    assert not transport.messages  # Rate limits and outages still hold back the whole batch.


def friday_without_review(tmp_path):
    journal = Journal(tmp_path, "synthetic-weekly-account")
    journal.event(close_of("2026-11-02", -600), "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    review(journal, "2026-11-03", "周二复盘。")
    sequence = review(journal, "2026-11-04", "周三复盘。")
    return journal, sequence


def weekly_rows(journal):
    return [r["body"]["details"] for r in journal.all_events()
            if r["kind"] == "notification" and r["body"]["kind"] == "weekly_report"]


def test_a_week_without_its_last_review_is_reported_once_the_next_week_opens(tmp_path, monkeypatch):
    daily_only(monkeypatch)
    journal, wednesday = friday_without_review(tmp_path)
    transport = DocumentTelegram()
    monitor = Monitor(tmp_path, transport)
    # Friday evening, the weekend and Monday before the open: Friday's review could still be made up.
    polls(monitor, tmp_path, close_of("2026-11-06"), [60, 600, 2000, 3600])
    polls(monitor, tmp_path, MONDAY_OPEN, [-61, -1])
    assert weekly_rows(journal) == []
    polls(monitor, tmp_path, MONDAY_OPEN, [0, 1, 2])
    assert weekly_rows(journal) == [{"review_sequence": wednesday, "week": "2026-W45", "session_date": "2026-11-04",
                                     "last_session_reviewed": False}]
    weekly = [d for d in transport.documents if d[1].startswith("weekly-")]
    assert len(weekly) == 1 and "2026-W45 周报（2026-11-04 收盘后；本周最后一个交易日没有复盘）" in weekly[0][0]


def test_a_last_review_made_up_before_the_open_sends_the_ordinary_report(tmp_path, monkeypatch):
    daily_only(monkeypatch)
    journal, _ = friday_without_review(tmp_path)
    monitor = Monitor(tmp_path, DocumentTelegram())
    polls(monitor, tmp_path, close_of("2026-11-06"), [60])
    made_up = MONDAY_OPEN - timedelta(minutes=50)  # The trader reviews an hour before the open.
    journal.event(made_up, "postmarket_review", {"session_date": "2026-11-06", "completed_at": made_up.isoformat(),
                                                 "version": 1, "review": {"summary": "周五补做的复盘。"}})
    polls(monitor, tmp_path, MONDAY_OPEN, [-49, 0, 5])
    rows = weekly_rows(journal)
    assert [(row["session_date"], row.get("last_session_reviewed")) for row in rows] == [("2026-11-06", None)]


def test_the_week_calendar_names_the_next_weeks_first_open():
    assert week_calendar("2026-11-04") == (False, MONDAY_OPEN)
    assert week_calendar("2026-11-06") == (True, MONDAY_OPEN)
    ends, later = week_calendar("2026-04-01")  # Good Friday 2026: Thursday ends the week.
    assert not ends and later == datetime(2026, 4, 6, 13, 30, tzinfo=timezone.utc)


def status_lines(root, now, **kwargs):
    with sqlite3.connect(root / "state.sqlite3") as db:
        return weekly_lines(db, now, **kwargs)


def test_status_shows_where_the_weekly_report_stands(tmp_path, monkeypatch):
    daily_only(monkeypatch)
    journal, _ = friday_without_review(tmp_path)
    wednesday = close_of("2026-11-04", 60)
    assert status_lines(tmp_path, wednesday) == ["周报：还没有生成过", "  下一份 2026-W45：2026-11-06（纽约）收盘复盘完成后发送"]
    saturday = close_of("2026-11-07")
    lines = status_lines(tmp_path, saturday, stopped=True)
    assert lines[0] == "周报：还没有生成过" and lines[-1] == "  下一份 2026-W46：2026-11-13（纽约）收盘复盘完成后发送"
    assert "本周最后一个交易日 2026-11-06 还没有盘后复盘。STOP 期间不做复盘。" in lines[1]
    assert "本周有 2 次复盘，下周第一个交易日开盘后用其中最近一次发周报。" in lines[1]
    monitor = Monitor(tmp_path, DocumentTelegram())
    polls(monitor, tmp_path, MONDAY_OPEN, [0, 1, 2])
    assert status_lines(tmp_path, MONDAY_OPEN + timedelta(minutes=3)) == [
        "周报：最近一份 2026-W45（2026-11-04 收盘后，已发送）", "  下一份 2026-W46：2026-11-13（纽约）收盘复盘完成后发送"]


def test_status_explains_a_week_with_no_review_and_a_report_sent_as_a_notice(tmp_path, monkeypatch):
    notifications = daily_only(monkeypatch)
    journal = Journal(tmp_path, "synthetic-weekly-account")
    journal.event(close_of("2026-10-26", -600), "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    lines = status_lines(tmp_path, close_of("2026-11-07"))
    assert "本周没有任何复盘，这一周不会有周报。" in lines[1]
    review(journal, "2026-11-06", "周五复盘。")
    assert "2026-W45：2026-11-06 已复盘，等待独立监控生成周报" in status_lines(tmp_path, close_of("2026-11-07"))[1]
    monkeypatch.setattr(notifications, "weekly_document", lambda root, sequence: {}["missing"])
    monitor = Monitor(tmp_path, DocumentTelegram())
    polls(monitor, tmp_path, close_of("2026-11-06", 45), range(0, 12))
    assert status_lines(tmp_path, close_of("2026-11-07"))[0] == (
        "周报：最近一份 2026-W45（2026-11-06 收盘后，报告生成失败，已发提醒；原因见 bash scripts/fund-logs.sh errors）")


def test_the_weekly_command_prints_the_report_so_far(tmp_path):
    Journal(tmp_path / "empty", "synthetic-empty-account")
    with pytest.raises(RiskError, match="还没有盘后复盘"):
        latest_weekly(tmp_path / "empty")
    friday_without_review(tmp_path / "book")
    filename, text = latest_weekly(tmp_path / "book")
    assert filename == "weekly-2026-W45.md" and "最后一次复盘：2026-11-04" in text


def test_operator_commands_act_on_the_running_account(tmp_path, monkeypatch):
    # `fund-logs.sh prune` previously resolved to the earlier default directory, not the running account.
    from scripts.autonomous_fund.settings import ACCOUNT_COMMANDS, startup
    text = (ROOT / "scripts" / "fund-logs.sh").read_text(encoding="utf-8")
    body = text.split('case "${1:-status}" in', 1)[1]
    modes = set(re.findall(r"\bfund ([a-z][a-z-]*)", body)) | set(re.findall(r"args=\(([a-z-]+)\)", body))
    assert {"status", "report", "audit-ledger", "approvals", "decisions", "prune", "weekly"} <= modes
    assert modes <= ACCOUNT_COMMANDS
    monkeypatch.chdir(tmp_path)
    for mode in ("prune", "weekly"):
        assert startup(mode, None, {"FUND_MODE": "paper"})[1] == Path("artifacts/fund-paper")


def test_fund_logs_prune_and_weekly_use_the_configured_account(tmp_path):
    book, env = account_book(tmp_path)
    folder = book / "cache" / "agent_runtime" / "traces" / "manager"
    folder.mkdir(parents=True)
    old = folder / "old.json"
    old.write_text("{}")
    os.utime(old, (0, 0))
    preview = script("fund-logs.sh", tmp_path, env, "prune")
    assert preview.returncode == 0 and "模型调用追踪文件 1 个" in preview.stdout and old.exists()
    assert script("fund-logs.sh", tmp_path, env, "prune", "--apply").returncode == 0 and not old.exists()
    missing = script("fund-logs.sh", tmp_path, env, "weekly")
    assert missing.returncode != 0 and "还没有盘后复盘" in missing.stderr
    review(Journal(book, "synthetic-script-account"), "2026-11-06", "周五复盘。")
    printed = script("fund-logs.sh", tmp_path, env, "weekly")
    assert printed.returncode == 0 and printed.stdout.startswith("# 2026-W45 模拟账户周报（2026-11-02 至 2026-11-08）")
    with sqlite3.connect(book / "state.sqlite3") as db:  # Printed only: nothing queued for Telegram.
        assert db.execute("SELECT count(*) FROM events WHERE kind='notification' AND body LIKE '%weekly_report%'"
                          ).fetchone()[0] == 0


def test_review_and_research_failures_say_what_happens_next():
    # Both were sent as the raw kind and exception name, without what it means or when it is retried.
    from scripts.autonomous_fund.notifications import readable
    review_text = readable("postmarket_review_error", "TimeoutError", 41)
    assert review_text.startswith("⚠️ 盘后复盘失败：TimeoutError") and "下一个开盘前约 1 小时自动重试复盘" in review_text
    assert "下周第一个交易日开盘后" in review_text and "bash scripts/fund-logs.sh errors" in review_text
    assert "事件 #41" in review_text
    research_text = readable("premarket_research_error", "TimeoutError", 42)
    assert "盘前研究失败：TimeoutError" in research_text and "照常复核和决策" in research_text
