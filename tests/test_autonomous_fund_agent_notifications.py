"""Exercise the native notification tool against an initialized fund and a fake transport."""
from types import SimpleNamespace

import pytest

from lumibot.components.agents.builtins import _bind_notify_user
from lumibot.components.notifications.base import NotificationResult
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.risk import Limits
from scripts.autonomous_fund.run import ScriptedRuntime
from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
from .test_autonomous_fund_local import Clock
from .test_autonomous_fund_monitor import FakeTelegram, heartbeat


@pytest.fixture
def fund(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account"))
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="SyntheticNoticeFund", parameters={
        "config": {"model": "openai/fixture-model", "limits": {}, "universe": {"ALFA": "stock"}},
        "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": ScriptedRuntime()})
    strategy._executor._initialize()
    try:
        yield strategy, clock
    finally:
        broker._close_connection()


def notices(strategy):
    return [row["body"] for row in strategy.journal.all_events() if row["kind"] == "notification"]


def test_native_notify_tool_queues_to_this_account_without_forging_an_operational_alert(fund, tmp_path):
    strategy, clock = fund
    tool = _bind_notify_user(strategy, strategy.agents)
    result = tool.function(title="approval_required", message="Synthetic AI observation", severity="warning")
    assert result["ok"]
    assert result["results"][0]["provider"] == "fund_outbox"
    assert result["results"][0]["payload"]["status"] == "queued"
    assert notices(strategy) == [{"kind": "agent_notice", "details": {
        "title": "approval_required", "message": "Synthetic AI observation", "severity": "warning"}}]
    heartbeat(tmp_path, clock())
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(clock())
    assert len(transport.messages) == 1
    assert "Synthetic AI observation" in transport.messages[0]
    tool.function(title="approval_required", message="Synthetic AI observation", severity="warning")
    assert len(notices(strategy)) == 1
    assert not strategy.journal.pending()


@pytest.mark.parametrize("control", ["disabled", "backtest"])
def test_native_notification_disable_and_backtest_defaults_do_not_queue_external_messages(fund, control):
    strategy, _ = fund
    strategy.notifications.is_backtesting = control == "backtest"
    result = _bind_notify_user(strategy, strategy.agents).function(
        title="Synthetic observation", message="No external message", severity="info",
        enabled=True if control == "backtest" else False)
    assert result["ok"] and result["results"][0]["skipped"]
    assert not notices(strategy)


def test_native_notification_keeps_other_configured_providers(fund):
    strategy, _ = fund
    sent = []

    def notify(**kwargs):
        sent.append(kwargs)
        return NotificationResult(ok=True, provider="synthetic", **kwargs)

    strategy.notifications.providers.append(SimpleNamespace(notify=notify))
    result = _bind_notify_user(strategy, strategy.agents).function(
        title="Synthetic observation", message="Preserve configured fanout", severity="info")
    assert result["ok"] and len(sent) == 1
    assert {item["provider"] for item in result["results"]} == {"fund_outbox", "synthetic"}


def test_failed_outbox_write_is_retryable_and_never_acknowledged_as_queued(fund, monkeypatch):
    strategy, _ = fund
    original = strategy.journal.event

    def unavailable(*args, **kwargs):
        raise OSError("Synthetic journal outage")

    monkeypatch.setattr(strategy.journal, "event", unavailable)
    tool = _bind_notify_user(strategy, strategy.agents)
    with pytest.raises(OSError, match="Synthetic journal outage"):
        tool.function(title="Synthetic observation", message="Retry this exact notice")
    assert not notices(strategy)
    monkeypatch.setattr(strategy.journal, "event", original)
    result = tool.function(title="Synthetic observation", message="Retry this exact notice")
    assert result["ok"] and result["results"][0]["payload"]["status"] == "queued"
    assert len(notices(strategy)) == 1
