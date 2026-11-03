"""V2 3.1: an open session without a valid investment decision is reported once a day, unless something explains it."""
from datetime import datetime, timedelta, timezone

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.notifications import readable
from tests.test_autonomous_fund_monitor import heartbeat

OPEN = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)  # 09:30 New York, a regular session (Tuesday).


def fund(tmp_path, observe_only=False):
    journal = Journal(tmp_path, "synthetic-drought-account")
    journal.event(OPEN - timedelta(hours=2), "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION,
                                                          "observe_only": observe_only})
    return journal, Monitor(tmp_path)


def poll(journal, monitor, root, at):
    """A healthy, iterating runner observed at `at`."""
    heartbeat(root, at)
    journal.event(at, "iteration_finished", {})
    monitor.poll(at)


def droughts(journal):
    return [r["body"]["details"] for r in journal.all_events()
            if r["kind"] == "notification" and r["body"]["kind"] == "decision_drought"]


def test_no_decision_after_the_grace_period_alerts_once_a_day(tmp_path):
    journal, monitor = fund(tmp_path)
    journal.event(OPEN + timedelta(minutes=20), "decision_backoff", {"role": "manager"})
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=60))
    assert droughts(journal) == []
    for minutes in (95, 150, 300):
        poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=minutes))
    [details] = droughts(journal)
    assert details["last_decision_at"] is None and details["minutes_without_decision"] == 95
    assert details["decision_failures_today"] == 1
    text = readable("decision_drought", details)
    assert "95 分钟没有有效的投资决策" in text and "还没有" in text and "重试 1 次" in text


def test_a_decision_and_its_own_next_review_defer_the_alert(tmp_path):
    journal, monitor = fund(tmp_path)
    decided = OPEN + timedelta(minutes=30)
    journal.event(decided, "decision_validated", {"targets": [], "next_review_at": (decided + timedelta(hours=1)).isoformat()})
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=95))  # Past the grace, but a decision exists.
    poll(journal, monitor, tmp_path, decided + timedelta(hours=1, minutes=170))  # Due + 2h50.
    assert droughts(journal) == []
    poll(journal, monitor, tmp_path, decided + timedelta(hours=4, minutes=5))  # Due + 3h05.
    [details] = droughts(journal)
    assert details["last_decision_at"] == decided.isoformat() and details["minutes_without_decision"] == 245


def test_yesterdays_decision_does_not_count_for_today(tmp_path):
    journal, monitor = fund(tmp_path)
    journal.event(OPEN - timedelta(hours=20), "decision_validated", {"targets": [], "next_review_at": OPEN.isoformat()})
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=95))
    assert len(droughts(journal)) == 1


def test_a_stop_holds_the_alert_and_its_lifting_restarts_the_clock(tmp_path):
    journal, monitor = fund(tmp_path)
    stops.write(tmp_path, "operator", "synthetic operator hold", OPEN)
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=120))
    stops.clear(tmp_path)
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=125))
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=205))  # 85 minutes after the hold ended.
    assert droughts(journal) == []
    poll(journal, monitor, tmp_path, OPEN + timedelta(minutes=215))
    assert len(droughts(journal)) == 1


def test_a_spent_model_budget_observe_only_or_a_closed_market_is_not_a_drought(tmp_path):
    journal, monitor = fund(tmp_path / "budget")
    journal.event(OPEN + timedelta(minutes=40), "notification", {"kind": "model_cost_budget_exhausted", "details": {}})
    poll(journal, monitor, tmp_path / "budget", OPEN + timedelta(minutes=120))
    assert droughts(journal) == []
    journal, monitor = fund(tmp_path / "observe", observe_only=True)
    poll(journal, monitor, tmp_path / "observe", OPEN + timedelta(minutes=120))
    assert droughts(journal) == []
    journal, monitor = fund(tmp_path / "closed")
    poll(journal, monitor, tmp_path / "closed", OPEN + timedelta(hours=8))  # 17:30 New York.
    assert droughts(journal) == []


def test_status_shows_todays_decisions(tmp_path):
    from scripts.autonomous_fund.status import summary
    journal, _ = fund(tmp_path)
    heartbeat(tmp_path, OPEN + timedelta(hours=2))
    journal.event(OPEN - timedelta(hours=20), "decision_validated", {"targets": []})
    journal.event(OPEN + timedelta(minutes=45), "decision_validated", {"targets": []})
    text = summary(tmp_path, "local", OPEN + timedelta(hours=2))
    assert "决策：今天 1 次有效决策，最近一次 10:15（纽约）；累计 2 次" in text
