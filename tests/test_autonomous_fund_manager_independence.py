"""The regular-session manager does not wait for a new researcher call."""
from datetime import timedelta

import pytest

from .test_autonomous_fund_quant import NOW, account, decision, model_harness


def live_manager(tmp_path, mode="local"):
    strategy = model_harness(tmp_path)
    strategy.mode = mode
    strategy.premarket_research = None
    strategy.log_message = lambda *args, **kwargs: None
    calls = []

    def run(name, **kwargs):
        calls.append((name, kwargs["context"]))
        if name == "researcher":
            raise OSError("synthetic unavailable researcher")
        return decision([])

    strategy._agent_call = run
    return strategy, calls


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_open_manager_runs_without_calling_the_unavailable_researcher(tmp_path, mode):
    strategy, calls = live_manager(tmp_path, mode)
    strategy._decide(account(), NOW, False)
    assert [name for name, _ in calls] == ["manager"]
    assert calls[0][1]["research"] is None
    assert calls[0][1]["research_status"]["state"] == "unavailable"
    assert strategy.last_decision["targets"] == []
    assert any(row["kind"] == "decision_validated" for row in strategy.journal.all_events())
    assert not strategy.journal.get_meta("decision_failures")


@pytest.mark.parametrize("previous_session", [False, True])
def test_saved_research_keeps_its_original_times_and_session(tmp_path, previous_session):
    strategy, calls = live_manager(tmp_path)
    opens = NOW - timedelta(minutes=30, days=3 if previous_session else 0)
    made = opens - timedelta(hours=1)
    research = {"summary": "Synthetic dated research", "evidence": [{
        "source": "Synthetic fixture", "available_at": None, "observed_at": made.isoformat(),
        "estimated": False, "estimate_reason": ""}]}
    packet = {"as_of": made.isoformat(), "completed_at": made.isoformat(),
              "session_open": opens.isoformat(), "phase": "before_market_opens", "research": research}
    strategy.premarket_research = packet
    strategy._decide(account(), NOW, False)
    context = calls[0][1]
    assert context["research"] == research
    assert context["research_status"]["state"] == ("previous_session" if previous_session else "available")
    assert context["research_status"]["completed_at"] == made.isoformat()
    assert context["premarket_research"] == packet
    assert strategy.premarket_research == packet


def test_unusable_saved_research_is_disclosed_without_blocking_manager(tmp_path):
    strategy, calls = live_manager(tmp_path)
    strategy.premarket_research = {"research": {"summary": "Missing source metadata"}}
    strategy._decide(account(), NOW, False)
    assert [name for name, _ in calls] == ["manager"]
    assert calls[0][1]["research_status"]["state"] == "invalid"
    assert calls[0][1]["research"] is None
    assert calls[0][1]["premarket_research"] is None


def test_one_remaining_decision_run_is_available_to_manager(tmp_path):
    strategy, calls = live_manager(tmp_path)
    budget = strategy.model_budget
    strategy.journal.counter("agent_runs", NOW, budget.max_agent_runs_per_day - budget.reserved_review_runs - 1)
    strategy._decide(account(), NOW, False)
    assert [name for name, _ in calls] == ["manager"]
    strategy.journal.counter("agent_runs", NOW, 1)
    strategy._decide(account(), NOW, False)
    assert len(calls) == 1
    assert strategy.notices[-1][0] == "model_budget_exhausted"


def test_manager_failure_still_backs_off_and_success_clears_its_state(tmp_path):
    strategy, calls = live_manager(tmp_path)
    good_run = strategy._agent_call

    def fail(name, **kwargs):
        assert name == "manager"
        raise OSError("synthetic manager failure")

    strategy._agent_call = fail
    for delay in (5, 15, 60):
        with pytest.raises(OSError):
            strategy._decide(account(), NOW, False)
        assert strategy.next_review == NOW + timedelta(minutes=delay)
        assert strategy.journal.get_meta("decision_failure_role") == "manager"
    strategy._agent_call = good_run
    strategy._decide(account(), NOW, False)
    assert strategy.journal.get_meta("decision_failures") == 0
    assert strategy.journal.get_meta("decision_retry_at") is None
    assert strategy.journal.get_meta("decision_failure_role") is None


def test_failed_optional_critic_still_reaches_manager_without_research(tmp_path):
    from scripts.autonomous_fund.strategy import CriticSettings
    strategy, calls = live_manager(tmp_path)
    strategy.critic, strategy._critic_ready = CriticSettings(), True
    good_run = strategy._agent_call

    def run(name, **kwargs):
        if name == "critic":
            calls.append((name, kwargs["context"]))
            raise OSError("synthetic optional critic outage")
        return good_run(name, **kwargs)

    strategy._agent_call = run
    strategy._decide(account(), NOW, False)
    assert [name for name, _ in calls] == ["critic", "manager"]
    assert calls[-1][1]["critique"] is None
    assert calls[-1][1]["research_status"]["state"] == "unavailable"
    assert strategy.last_decision is not None
    assert not strategy.journal.get_meta("decision_retry_at")


@pytest.mark.parametrize("role", ["researcher", "manager", None])
def test_restart_migrates_only_a_proven_legacy_researcher_backoff(tmp_path, role):
    strategy, _ = live_manager(tmp_path)
    retry = NOW + timedelta(hours=1)
    strategy.journal.set_meta("decision_failures", 3)
    strategy.journal.set_meta("decision_retry_at", retry.isoformat())
    if role:
        strategy.journal.event(NOW, "agent_call_started", {"name": role})
        strategy.journal.event(NOW, "agent", {"name": role, "parse_error": "OSError"})
        strategy.journal.event(NOW, "agent_call_finished", {"name": role})
    strategy.journal.event(NOW, "decision_backoff", {"failures": 3, "retry_at": retry.isoformat()})
    # Later pre/post-market research must not overwrite the failed cycle's role.
    strategy.journal.event(NOW, "agent_call_finished", {"name": "researcher"})
    strategy.halt_path.touch()
    strategy.journal.counter("agent_runs", NOW, 7)
    restored = strategy._restore_decision_retry()
    assert restored == (None if role == "researcher" else retry)
    assert strategy.halt_path.exists()
    assert strategy.journal.counter("agent_runs", NOW) == 7
    migrations = [r for r in strategy.journal.all_events() if r["kind"] == "research_backoff_detached"]
    assert len(migrations) == (1 if role == "researcher" else 0)
    strategy._restore_decision_retry()
    assert len([r for r in strategy.journal.all_events() if r["kind"] == "research_backoff_detached"]) == len(migrations)


def test_mismatched_legacy_backoff_evidence_is_preserved(tmp_path):
    strategy, _ = live_manager(tmp_path)
    retry = NOW + timedelta(hours=1)
    strategy.journal.set_meta("decision_retry_at", retry.isoformat())
    strategy.journal.event(NOW, "agent_call_finished", {"name": "researcher"})
    strategy.journal.event(NOW, "decision_backoff", {"retry_at": (retry - timedelta(minutes=5)).isoformat()})
    assert strategy._restore_decision_retry() == retry


def test_historical_research_failure_keeps_the_existing_evidence_gate(tmp_path):
    strategy, calls = live_manager(tmp_path, "mock")
    with pytest.raises(OSError):
        strategy._decide(account(), NOW, False)
    assert [name for name, _ in calls] == ["researcher"]
    assert not any(row["kind"] == "decision_validated" for row in strategy.journal.all_events())


@pytest.mark.parametrize("control", ["stop", "observe_only", "budget", "manager_retry"])
def test_live_iteration_keeps_existing_controls(tmp_path, control):
    from types import SimpleNamespace
    strategy, calls = live_manager(tmp_path)
    strategy.config, strategy.run_dir, strategy.observe_only = {}, tmp_path, control == "observe_only"
    strategy.broker = SimpleNamespace(do_polling=lambda: None, data_source=SimpleNamespace(
        session=SimpleNamespace(status=lambda: {"open": True})))
    strategy._reconcile = lambda: None
    strategy._snapshot = account
    strategy._announce_session_open = lambda *args: None
    strategy._watch = lambda *args: False
    strategy._report_if_due = lambda *args: None
    if control == "stop":
        strategy.halt_path.touch()
    elif control == "budget":
        budget = strategy.model_budget
        strategy.journal.counter("agent_runs", NOW, budget.max_agent_runs_per_day - budget.reserved_review_runs)
    elif control == "manager_retry":
        strategy._decision_failed(strategy.model_budget)
    strategy.on_trading_iteration()
    assert not calls
    assert not strategy.journal.pending()
    assert any(r["kind"] == "iteration_finished" for r in strategy.journal.all_events())


def test_native_open_and_restart_after_failed_research(tmp_path, monkeypatch):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.risk import Limits
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
    from .test_autonomous_fund_local import Clock

    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    clock = Clock()
    clock.value = NOW - timedelta(hours=1)
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "reasoning_effort": None, "limits": {}, "critic": {"enabled": False},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    calls = []

    class UnavailableResearch(ScriptedRuntime):
        def run(self, request):
            calls.append((request.agent_name, request.context))
            if request.agent_name == "researcher":
                raise OSError("synthetic research outage")
            return super().run(request)

    def startup():
        broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
        strategy = AutonomousFund(broker=broker, name="IndependentSession", parameters={
            "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
            "runtime": UnavailableResearch()})
        strategy._executor._initialize()
        strategy._executor._initialize_live_market_calendars(broker.market)
        return strategy

    strategy = startup()
    strategy.before_market_opens()
    assert calls and all(name == "researcher" for name, _ in calls)
    assert strategy.premarket_research is None
    assert not journal.pending()
    # Reproduce an old shared retry, then restart after the opening bell.
    retry = NOW + timedelta(hours=1)
    journal.set_meta("decision_failures", 3)
    journal.set_meta("decision_retry_at", retry.isoformat())
    journal.event(clock(), "agent_call_finished", {"name": "researcher"})
    journal.event(clock(), "decision_backoff", {"failures": 3, "retry_at": retry.isoformat()})
    strategy.broker._close_connection()
    clock.value = NOW
    source._cache.clear()
    before = len(calls)
    restored = startup()
    try:
        assert restored.next_review is None
        restored.on_trading_iteration()
        assert [name for name, _ in calls[before:]] == ["manager"]
        assert calls[-1][1]["research_status"]["state"] == "unavailable"
        assert journal.get_meta("decision_retry_at") is None
        assert any(r["kind"] == "decision_validated" for r in journal.all_events())
        assert len(journal.pending()) == 1  # A validated manager target reaches the real local executor.
        assert restored.agents["manager"].allow_trading is False
    finally:
        restored.broker._close_connection()
