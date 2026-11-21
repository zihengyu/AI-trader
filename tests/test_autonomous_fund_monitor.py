"""Telegram HTTP doubles and independent watchdog failures; no messages sent."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.monitor import Monitor, Telegram
from scripts.autonomous_fund.risk import RiskError


NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)  # Closed market avoids scheduler assertions.


def setup(tmp_path):
    journal = Journal(tmp_path, "synthetic-notification-account")
    journal.event(NOW, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    heartbeat(tmp_path, NOW)
    return journal


def heartbeat(root, at, state="running"):
    (root / "status.json").write_text(encode({"heartbeat_at": at.isoformat(), "runner_state": state,
                                            "matching_error": None}), encoding="utf-8")


class FakeTelegram:
    chat_id = "synthetic-recipient"

    def __init__(self, outcomes=None):
        self.messages = []
        self.outcomes = list(outcomes or [])

    def send(self, text):
        self.messages.append(text)
        return self.outcomes.pop(0) if self.outcomes else {"ok": True, "message_id": len(self.messages)}


def write_schwab_token(path, issued, *, access="synthetic-private-access"):
    path.write_text(encode({"creation_timestamp": int(issued.timestamp()), "token": {
        "access_token": access, "refresh_token": "synthetic-private-refresh"}}), encoding="utf-8")


def test_schwab_reminds_at_24_and_3_hours_per_authorization_across_restarts(tmp_path):
    journal, transport = setup(tmp_path), FakeTelegram()
    path = tmp_path / "schwab.json"
    issued = NOW - timedelta(days=6)
    write_schwab_token(path, issued)
    monitor = Monitor(tmp_path, transport, schwab_token_path=path)
    heartbeat(tmp_path, NOW - timedelta(seconds=1))
    monitor.poll(NOW - timedelta(seconds=1))
    assert not transport.messages
    monitor.poll(NOW)
    assert len(transport.messages) == 1
    assert "剩余约 24 小时 0 分钟" in transport.messages[0]
    assert "bash scripts/fund-schwab-auth.sh" in transport.messages[0] and "Redirect URL>" in transport.messages[0]

    # An access-token refresh rewrites the file but preserves the original authorization timestamp.
    write_schwab_token(path, issued, access="synthetic-rewritten-access")
    restarted = Monitor(tmp_path, transport, schwab_token_path=path)
    at = NOW + timedelta(hours=21)
    heartbeat(tmp_path, at - timedelta(seconds=1))
    restarted.poll(at - timedelta(seconds=1))
    assert len(transport.messages) == 1
    restarted.poll(at)
    restarted.poll(at + timedelta(seconds=1))
    assert len(transport.messages) == 2 and "紧急" in transport.messages[1]
    assert "剩余约 3 小时 0 分钟" in transport.messages[1]
    notices = [row["body"] for row in journal.all_events()
               if row["kind"] == "notification" and row["body"].get("kind") == "schwab_token_expiry"]
    assert [row["details"]["urgency"] for row in notices] == ["day", "urgent"]
    assert "synthetic-private" not in encode(notices) + "".join(transport.messages)
    assert str(path) not in encode(notices) + "".join(transport.messages)


def test_schwab_renewal_suppresses_pending_notice_and_restarts_the_countdown(tmp_path):
    setup(tmp_path)
    transport = FakeTelegram([{"ok": False, "error": "network", "retry_after": 60}])
    path = tmp_path / "schwab.json"
    write_schwab_token(path, NOW - timedelta(days=6))
    monitor = Monitor(tmp_path, transport, schwab_token_path=path)
    monitor.poll(NOW)
    assert len(transport.messages) == 1

    new_issue = NOW + timedelta(seconds=10)
    write_schwab_token(path, new_issue)
    at = NOW + timedelta(hours=21)
    heartbeat(tmp_path, at)
    restarted = Monitor(tmp_path, transport, schwab_token_path=path)
    status = restarted.poll(at)
    assert len(transport.messages) == 1 and status["delivery_counts"].get("suppressed") == 1
    at = new_issue + timedelta(days=6)
    heartbeat(tmp_path, at)
    restarted.poll(at)
    assert len(transport.messages) == 2 and "剩余约 24 小时 0 分钟" in transport.messages[-1]


def test_schwab_late_monitor_sends_urgent_notice_only(tmp_path):
    setup(tmp_path)
    transport = FakeTelegram()
    path = tmp_path / "schwab.json"
    path.write_text('{"creation_timestamp":', encoding="utf-8")
    monitor = Monitor(tmp_path, transport, schwab_token_path=path)
    monitor.poll(NOW)
    assert not transport.messages
    write_schwab_token(path, NOW - timedelta(days=7) + timedelta(minutes=42))
    monitor.poll(NOW)
    assert len(transport.messages) == 1 and "紧急" in transport.messages[0]
    assert "剩余约 42 分钟" in transport.messages[0]


@pytest.mark.parametrize("mode,notifies", [("local", True), ("paper", False)])
def test_monitor_entry_point_uses_configured_schwab_market_data(tmp_path, monkeypatch, mode, notifies):
    from scripts.autonomous_fund import monitor as monitor_module

    setup(tmp_path)
    path, transport = tmp_path / "schwab.json", FakeTelegram()
    write_schwab_token(path, NOW - timedelta(days=6))
    monkeypatch.setenv("FUND_MODE", mode)
    monkeypatch.setenv("SCHWAB_APP_KEY", "synthetic-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "synthetic-secret")
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(path))
    monkeypatch.setattr(monitor_module, "Telegram", lambda: transport)
    poll = Monitor.poll
    monkeypatch.setattr(Monitor, "poll", lambda self: poll(self, NOW))
    config = Path(__file__).resolve().parents[1] / "scripts" / "autonomous_fund" / "config.json"
    monitor_module.run_monitor(SimpleNamespace(config=config, run_dir=tmp_path, console_only=False,
                                               heartbeat_seconds=180, agent_seconds=900, once=True))
    assert bool(transport.messages) is notifies


def test_actual_telegram_request_shape_and_redacted_transport_failure():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 7}})
    sender = Telegram("synthetic-token", "synthetic-chat", post=post)
    assert sender.send("Synthetic notification")["message_id"] == 7
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["json"]["chat_id"] == "synthetic-chat"
    sender.post = lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("token-bearing-private-url"))
    assert sender.send("test")["error"] == "transport_or_response_error"
    assert "private" not in encode(sender.send("test"))


def test_telegram_rate_limit_is_preserved():
    response = SimpleNamespace(status_code=429, json=lambda: {"ok": False, "parameters": {"retry_after": 120}})
    sender = Telegram("synthetic-token", "synthetic-chat", post=lambda *args, **kwargs: response)
    assert sender.send("test")["retry_after"] == 120


def test_missing_local_keys_is_explicit_without_reading_other_processes(monkeypatch):
    monkeypatch.delenv("FUND_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FUND_TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RiskError, match="FUND_TELEGRAM_BOT_TOKEN"):
        Telegram()


def test_delivery_is_durable_deduplicated_and_quiet_when_unchanged(tmp_path):
    journal = setup(tmp_path)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "SyntheticDataUnavailable"})
    journal.event(NOW + timedelta(seconds=1), "notification", {"kind": "blocked", "details": "SyntheticDataUnavailable"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(NOW + timedelta(seconds=2))
    monitor.poll(NOW + timedelta(seconds=3))
    restarted = Monitor(tmp_path, transport)
    result = restarted.poll(NOW + timedelta(seconds=4))
    assert len(transport.messages) == 1
    assert result["delivery_counts"] == {"sent": 1, "suppressed": 1}
    assert not (tmp_path / "STOP").exists()


def test_retry_waits_and_survives_monitor_restart(tmp_path):
    journal = setup(tmp_path)
    journal.event(NOW, "notification", {"kind": "approval_required", "details": {"id": "synthetic-id"}})
    transport = FakeTelegram([{"ok": False, "error": "telegram_http_429", "retry_after": 120}])
    Monitor(tmp_path, transport).poll(NOW)
    Monitor(tmp_path, transport).poll(NOW + timedelta(seconds=60))
    assert len(transport.messages) == 1
    Monitor(tmp_path, transport).poll(NOW + timedelta(seconds=121))
    assert len(transport.messages) == 2
    assert "synthetic-id" in transport.messages[-1]


def test_telegram_retry_after_applies_to_new_alerts_too(tmp_path):
    journal = setup(tmp_path)
    journal.event(NOW, "notification", {"kind": "approval_required", "details": {"id": "first"}})
    transport = FakeTelegram([{"ok": False, "error": "telegram_http_429", "retry_after": 120}])
    monitor = Monitor(tmp_path, transport)
    monitor.poll(NOW)
    journal.event(NOW + timedelta(seconds=1), "notification", {"kind": "approval_required", "details": {"id": "second"}})
    monitor.poll(NOW + timedelta(seconds=10))
    assert len(transport.messages) == 1
    monitor.poll(NOW + timedelta(seconds=121))
    assert len(transport.messages) == 3


@pytest.mark.parametrize("failure", ["heartbeat", "old_revision", "agent", "iteration", "exited"])
def test_watchdog_writes_stop_and_does_not_repeat_alerts_or_auto_resume(tmp_path, failure):
    journal = setup(tmp_path)
    now = NOW + timedelta(minutes=20)
    heartbeat(tmp_path, now)
    if failure == "heartbeat":
        heartbeat(tmp_path, NOW)
    elif failure == "old_revision":
        journal.event(NOW, "startup", {"mode": "local"})
    elif failure == "agent":
        journal.event(NOW, "agent_call_started", {"id": "synthetic-model-call"})
    elif failure == "iteration":
        now = datetime(2026, 11, 2, 16, tzinfo=timezone.utc)
        heartbeat(tmp_path, now)
    else:
        heartbeat(tmp_path, now, state="exited")
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(now)
    assert (tmp_path / "STOP").exists()
    (tmp_path / "STOP").unlink()
    monitor.poll(now + timedelta(seconds=1))
    assert (tmp_path / "STOP").exists()  # An unhealthy run cannot stay resumed.
    assert len(transport.messages) == 1


def test_health_recovery_keeps_stop_until_operator_action(tmp_path):
    journal = setup(tmp_path)
    monitor = Monitor(tmp_path, FakeTelegram())
    monitor.poll(NOW + timedelta(minutes=4))
    heartbeat(tmp_path, NOW + timedelta(minutes=5))
    status = monitor.poll(NOW + timedelta(minutes=5))
    assert status["health"] == "stopped"
    assert status["stop_active"] is True


def test_recurring_failure_after_recovery_is_a_new_actionable_alert(tmp_path):
    journal = setup(tmp_path)
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(NOW + timedelta(minutes=4))
    heartbeat(tmp_path, NOW + timedelta(minutes=5))
    monitor.poll(NOW + timedelta(minutes=5))
    (tmp_path / "STOP").unlink()
    monitor.poll(NOW + timedelta(minutes=9))
    assert sum("watchdog_stop" in text for text in transport.messages) == 2


def test_recipient_switch_requires_review_instead_of_cross_account_replay(tmp_path):
    journal = setup(tmp_path)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic event"})
    Monitor(tmp_path, FakeTelegram()).poll(NOW)
    other = FakeTelegram()
    other.chat_id = "other-synthetic-recipient"
    with pytest.raises(RiskError, match="recipient changed"):
        Monitor(tmp_path, other)


def test_initial_rejected_recipient_can_be_corrected_but_old_monitor_cannot_send(tmp_path):
    journal = setup(tmp_path)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic event"})
    transport = FakeTelegram([{"ok": False, "error": "telegram_http_400", "retry_after": 30}])
    original = Monitor(tmp_path, transport)
    original.poll(NOW)
    corrected = FakeTelegram()
    corrected.chat_id = "corrected-synthetic-recipient"
    Monitor(tmp_path, corrected)
    with pytest.raises(RiskError, match="stop and restart"):
        original.poll(NOW + timedelta(seconds=1))


def test_explicit_notification_test_does_not_stop_runner_or_flush_other_messages(tmp_path, monkeypatch):
    from scripts.autonomous_fund import monitor
    journal = setup(tmp_path)
    transport = FakeTelegram()
    monkeypatch.setattr(monitor, "Telegram", lambda: transport)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Old fixture"})
    result = monitor.send_test(tmp_path)
    assert result["state"] == "sent"
    assert len(transport.messages) == 1
    assert "operator_notification_test" in transport.messages[0]
    assert not (tmp_path / "STOP").exists()
    assert not journal.pending()
