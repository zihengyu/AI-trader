"""Monitor progress and truthful status, using synthetic accounts and transport only."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.execution import EXECUTION_REVISION, ExecutionPolicy, ExecutionState
from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.monitor import Monitor, require_monitor
from scripts.autonomous_fund.status import summary


NOW = datetime(2026, 11, 4, 1, tzinfo=timezone.utc)
OPEN = datetime(2026, 11, 4, 14, 30, tzinfo=timezone.utc)


def account(root, at=NOW, interval=30):
    journal = Journal(root, "synthetic-monitor-progress-account")
    ExecutionState(journal)
    journal.event(at, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION,
                                   "execution": {"iteration_seconds": interval}})
    heartbeat(root, at)
    return journal


def heartbeat(root, at, state="running"):
    (root / "status.json").write_text(encode({"heartbeat_at": at.isoformat(), "runner_state": state}))


class SlowReplies:
    chat_id = "synthetic-recipient"
    approver_ids = {"synthetic-owner"}
    replies_enabled = True

    def __init__(self, count=12):
        self.elapsed, self.replies, self.notices = 0, [], []
        self.rows = [{"update_id": i, "message": {"message_id": i + 1,
                     "chat": {"id": self.chat_id}, "from": {"id": "synthetic-owner"},
                     "text": "STATUS", "date": int(NOW.timestamp())}} for i in range(count)]

    def updates(self, offset):
        self.elapsed += 9
        return [row for row in self.rows if row["update_id"] >= offset]

    def send(self, text, reply_to=None):
        self.elapsed += 9  # Each request succeeds within the real transport's ten-second timeout.
        (self.replies if reply_to else self.notices).append(reply_to or text)
        return {"ok": True, "message_id": len(self.replies) + len(self.notices)}


def test_slow_telegram_backlog_yields_without_skipping_commands_or_starving_notices(tmp_path):
    journal = account(tmp_path)
    transport = SlowReplies()
    monitor = Monitor(tmp_path, transport)
    # Inject only elapsed transport time; run the real poll, health checks and command cursor.
    monitor.clock = lambda: transport.elapsed
    for index in range(4):
        journal.event(NOW, "notification", {"kind": "approval_required", "details": {"id": f"synthetic-{index}"}})
    for _ in range(40):
        started = transport.elapsed
        at = NOW + timedelta(seconds=started)
        heartbeat(tmp_path, at)
        monitor.poll(at)
        duration = transport.elapsed - started
        assert duration <= 30, "notification work must yield before the independent health gate expires"
        require_monitor(tmp_path, at + timedelta(seconds=duration + 10))  # Include the normal poll sleep.
        if len(transport.replies) == 12 and len(transport.notices) == 4:
            break
    assert transport.replies == list(range(1, 13))
    assert len(transport.notices) == 4
    with journal.connection() as db:
        assert db.execute("SELECT value FROM fund_monitor_meta WHERE key='telegram_offset'").fetchone()[0] == "12"
    assert not (tmp_path / "STOP").exists()


@pytest.mark.parametrize("change", ["monitor_restart", "observation_gap", "runner_restart", "clock_rollback"])
def test_auto_resume_requires_a_continuous_observation_of_the_current_runner(tmp_path, change):
    journal = account(tmp_path)
    policy = stops.ResumePolicy(auto_resume_after_seconds=180)
    stops.write(tmp_path, "service", "service_start", NOW)
    monitor = Monitor(tmp_path, resume_policy=policy)
    for seconds in range(0, 151, 30):
        at = NOW + timedelta(seconds=seconds)
        heartbeat(tmp_path, at)
        monitor.poll(at)
    recovered = NOW + timedelta(seconds=180)
    if change == "monitor_restart":
        monitor = Monitor(tmp_path, resume_policy=policy)
    elif change == "observation_gap":
        recovered = NOW + timedelta(minutes=10)
    elif change == "runner_restart":
        journal.event(recovered, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    else:
        recovered = NOW + timedelta(seconds=120)
    for seconds in range(0, 181, 30):
        at = recovered + timedelta(seconds=seconds)
        heartbeat(tmp_path, at)
        monitor.poll(at)
        assert (tmp_path / "STOP").exists() == (seconds < 180)


@pytest.mark.parametrize("interval", [30, 60, 180, 300])
def test_an_idle_runner_is_not_stalled_before_its_configured_next_iteration(tmp_path, interval):
    journal = account(tmp_path, OPEN, ExecutionPolicy(iteration_seconds=interval).iteration_seconds)
    finished = OPEN + timedelta(minutes=10)
    journal.event(finished, "iteration_finished", {})
    at = finished + timedelta(seconds=interval - 1)
    heartbeat(tmp_path, at)
    monitor = Monitor(tmp_path)
    assert monitor.poll(at)["health"] == "healthy"
    at = finished + timedelta(seconds=max(interval + 10, 180) + 1)
    heartbeat(tmp_path, at)
    assert monitor.poll(at)["health"] == "iteration_stalled"


def test_a_running_iteration_keeps_the_original_stall_threshold(tmp_path):
    journal = account(tmp_path, OPEN, interval=300)
    started = OPEN + timedelta(minutes=10)
    journal.event(started, "iteration_started", {})
    at = started + timedelta(seconds=181)
    heartbeat(tmp_path, at)
    assert Monitor(tmp_path).poll(at)["health"] == "iteration_stalled"


@pytest.mark.parametrize("execution", [None, "invalid old record", {"iteration_seconds": True}, {"iteration_seconds": 900}])
def test_missing_or_invalid_saved_intervals_do_not_extend_stall_detection(tmp_path, execution):
    journal = account(tmp_path, OPEN)
    journal.event(OPEN, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION,
                                  "execution": execution})
    finished = OPEN + timedelta(minutes=10)
    journal.event(finished, "iteration_finished", {})
    at = finished + timedelta(seconds=181)
    heartbeat(tmp_path, at)
    assert Monitor(tmp_path).poll(at)["health"] == "iteration_stalled"


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("case", ["never_started", "exited", "stale", "future", "unknown_state", "malformed"])
def test_no_stop_file_does_not_claim_an_unconfirmed_runner_is_trading(tmp_path, case, mode):
    if case != "never_started":
        at = NOW - timedelta(hours=2) if case == "stale" else NOW
        at = NOW + timedelta(minutes=1) if case == "future" else at
        heartbeat(tmp_path, at, "exited" if case == "exited" else "starting" if case == "unknown_state" else "running")
        if case == "malformed":
            (tmp_path / "status.json").write_text('["invalid heartbeat shape"]')
        if mode == "paper":
            (tmp_path / "status.json").rename(tmp_path / "runner_health.json")
    text = summary(tmp_path, mode, NOW)
    assert "交易：运行中" not in text
    assert "没有 STOP" in text


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_fresh_running_status_and_an_operator_stop_remain_distinct(tmp_path, mode):
    heartbeat(tmp_path, NOW)
    if mode == "paper":
        (tmp_path / "status.json").rename(tmp_path / "runner_health.json")
        heartbeat(tmp_path, NOW - timedelta(hours=2), "exited")  # The other mode's file cannot override this one.
    assert "交易：运行中（没有 STOP）" in summary(tmp_path, mode, NOW)
    stops.write(tmp_path, "operator", "operator_stop", NOW)
    assert "交易：已停止（人工停止" in summary(tmp_path, mode, NOW)
