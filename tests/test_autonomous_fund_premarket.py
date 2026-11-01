"""Closed-market research and native lifecycle handoff; no paid inference."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.risk import Account, Intent, Limits, RiskError, number
from scripts.autonomous_fund.strategy import AutonomousFund, Research, account_digest


SUNDAY = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def harness(tmp_path):
    strategy = object.__new__(AutonomousFund)
    strategy.mode = "local"
    strategy.observe_only = False
    strategy.scripted = True
    strategy.run_dir = tmp_path
    strategy.config = {}
    strategy.limits = Limits()
    strategy.universe = frozenset({"ALFA", "BETA"})
    strategy.journal = Journal(tmp_path, "premarket-unit")
    strategy.halt_path = tmp_path / "STOP"
    strategy.minutes_before_opening = 60
    strategy.premarket_research = None
    strategy.get_datetime = lambda: SUNDAY
    strategy.broker = SimpleNamespace(is_market_open=lambda: False)
    strategy._snapshot = lambda: Account(number(10000), number(10000), {}, strategy.get_datetime())
    strategy.log_message = lambda *args, **kwargs: None
    strategy.notify = lambda kind, details: strategy.journal.event(strategy.get_datetime(), "notification", {"kind": kind, "details": details})
    calls = []

    def research(**kwargs):
        calls.append(kwargs)
        body = Research(summary="SCRIPTED research; not an investment prediction.", evidence=[{
            "source": "Synthetic fixture", "available_at": None,
            "observed_at": strategy.get_datetime().isoformat(), "estimated": False, "estimate_reason": ""}])
        return SimpleNamespace(model="scripted/no-inference", cache_hit=False, usage={"input_tokens": 10, "output_tokens": 20},
                               summary=body.model_dump_json(), parsed=body, parse_error=None)

    strategy.agents = {"researcher": SimpleNamespace(run=research)}
    return strategy, calls


def test_weekend_research_is_recorded_without_order_or_manager(tmp_path):
    strategy, calls = harness(tmp_path)
    strategy.before_market_opens()
    assert len(calls) == 1
    context = calls[0]["context"]
    assert context["historical"] is False
    assert context["orders_enabled"] is False
    assert context["market"]["next_open"] == "2026-11-02T14:30:00+00:00"
    assert context["phase"] == "closed_startup"
    assert not strategy.journal.pending()
    result = report(tmp_path, write_files=False)
    assert result["premarket_research_runs"] == 1
    assert result["validated_decisions"] == result["fills_observed"] == 0
    assert result["model_usage"]["input_tokens"] == 10
    assert json.loads((tmp_path / "premarket_research.json").read_text())["research"]["summary"].startswith("SCRIPTED")


def test_research_deduplicates_session_phase_but_refreshes_before_open(tmp_path):
    strategy, calls = harness(tmp_path)
    strategy.before_market_opens()
    strategy.before_market_opens()
    assert len(calls) == 1
    strategy.get_datetime = lambda: datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc)
    strategy.before_market_opens()
    strategy.before_market_opens()
    assert len(calls) == 2
    assert calls[-1]["context"]["phase"] == "before_market_opens"
    assert calls[-1]["context"]["previous_premarket_research"]["phase"] == "closed_startup"
    strategy.get_datetime = lambda: datetime(2026, 11, 3, 13, 30, tzinfo=timezone.utc)
    strategy.before_market_opens()
    assert len(calls) == 3


@pytest.mark.parametrize("control", ["observe_only", "stop", "historical", "open"])
def test_research_respects_observation_stop_and_historical_boundaries(tmp_path, control):
    strategy, calls = harness(tmp_path)
    if control == "observe_only":
        strategy.observe_only = True
    elif control == "stop":
        strategy.halt_path.touch()
    elif control == "historical":
        strategy.mode = "backtest"
    else:
        strategy.get_datetime = lambda: datetime(2026, 11, 2, 16, tzinfo=timezone.utc)
    strategy.before_market_opens()
    assert not calls
    assert not strategy.journal.pending()


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_closed_market_order_guard_precedes_reservation(tmp_path, mode):
    strategy, _ = harness(tmp_path)
    strategy.mode = mode
    with pytest.raises(RiskError, match="open regular market"):
        strategy._execute(Intent("ALFA", "buy", 1, number(100)), SUNDAY)
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 0


def test_unavailable_account_is_disclosed_without_blocking_ordinary_research(tmp_path):
    strategy, calls = harness(tmp_path)
    strategy._snapshot = lambda: (_ for _ in ()).throw(ConnectionError("private provider response"))
    strategy.before_market_opens()
    assert calls[0]["context"]["account"] == {"unavailable": "ConnectionError"}
    assert strategy.premarket_research
    assert not strategy.journal.pending()


def test_research_failure_is_reported_without_creating_a_decision(tmp_path):
    strategy, _ = harness(tmp_path)
    strategy.agents["researcher"].run = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("private provider response"))
    strategy.before_market_opens()
    assert strategy.premarket_research is None
    result = report(tmp_path, write_files=False)
    assert result["premarket_research_runs"] == result["validated_decisions"] == 0
    assert not strategy.journal.pending()
    notifications = json.loads((tmp_path / "notifications.json").read_text())
    assert notifications[-1]["kind"] == "premarket_research_error"
    assert notifications[-1]["details"] == "RuntimeError"


def test_native_startup_restart_preopen_and_open_execution(tmp_path, monkeypatch):
    from lumibot.strategies.strategy_executor import StrategyExecutor
    from lumibot.traders import Trader
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from .test_autonomous_fund_local import Clock

    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setattr(StrategyExecutor, "_publish_run_once_final_cloud_state", lambda *args, **kwargs: None)
    clock = Clock()
    clock.value = SUNDAY
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "reasoning_effort": None, "limits": {},
              "agent_models": {"researcher": "deepseek/deepseek-flash", "manager": "deepseek/deepseek-v4-pro"},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    binding = account_digest("local", "local-virtual-account", config)
    journal = Journal(tmp_path, binding)
    calls = []
    model_calls = []

    class RecordingRuntime(ScriptedRuntime):
        def run(self, request):
            calls.append((request.agent_name, request.context))
            model_calls.append((request.agent_name, request.model))
            result = super().run(request)
            if (request.agent_name == "manager" and request.context.get("strategy_memory") and
                    request.context.get("phase") != "postmarket_review"):
                # A synthetic model changes its target after considering the
                # memo. The production Python code does not choose this symbol.
                body = json.loads(result.summary)
                body["targets"] = [{"symbol": "QQQ", "weight": .05}]
                result.summary = json.dumps(body)
            return result

    def startup(run_once=True):
        broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
        strategy = AutonomousFund(broker=broker, name="ResearchSession", parameters={
            "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
            "runtime": RecordingRuntime()})
        trader = Trader()
        trader.add_strategy(strategy)
        if run_once:
            trader.run_all(run_once=True)
        else:
            # Enter the native continuous-session setup without a run_once exit
            # closing the connection before the simulated session begins.
            strategy._executor._initialize()
            strategy._executor._initialize_live_market_calendars(broker.market)
        return strategy

    startup()
    strategy = startup(run_once=False)
    assert [name for name, _ in calls] == ["researcher"]
    assert not journal.pending()
    assert strategy.premarket_research["phase"] == "closed_startup"

    def advance_to_open(timedelta=None):
        clock.value = datetime(2026, 11, 2, 13 if timedelta is None else 14, 30, tzinfo=timezone.utc)
        source._cache.clear()

    monkeypatch.setattr(strategy, "await_market_to_open", advance_to_open)
    monkeypatch.setattr(strategy._executor, "_send_startup_cloud_update", lambda: None)
    assert strategy._executor._setup_market_session(has_data_source=True)
    assert [name for name, _ in calls] == ["researcher", "researcher"]
    assert strategy.premarket_research["phase"] == "before_market_opens"
    assert not journal.pending()
    clock.value += timedelta(minutes=5)
    source._cache.clear()
    strategy.on_trading_iteration()
    assert [name for name, _ in calls] == ["researcher", "researcher", "researcher", "manager"]
    assert model_calls == [("researcher", "deepseek/deepseek-flash")] * 3 + [("manager", "deepseek/deepseek-v4-pro")]
    assert strategy.agents["researcher"].allow_trading is False
    assert strategy.agents["manager"].allow_trading is False
    assert calls[-1][1]["premarket_research"]["phase"] == "before_market_opens"
    assert len(journal.pending()) == 1
    identifier = journal.pending()[0]["broker_id"]
    clock.value += timedelta(minutes=2)
    source._cache.clear()
    strategy.broker.do_polling()
    assert strategy.broker._pull_order(identifier, strategy.name).status == "fill"
    assert strategy.broker._get_balances_at_broker(None, strategy)[0] < 10000
    strategy._snapshot()

    # Exercise the native close callback, schema override on the SAME manager,
    # and next-session feedback through a new independently validated target.
    submitted_before = len([row for row in journal.export() if row["kind"] == "order_submitted"])
    clock.value = datetime(2026, 11, 2, 21, 5, tzinfo=timezone.utc)
    source._cache.clear()
    strategy._executor._after_market_closes()
    postmarket = [context for _, context in calls if context.get("phase") == "postmarket_review"]
    assert len(postmarket) == 2
    assert postmarket[-1]["orders_enabled"] is False
    assert postmarket[-1]["session"]["counts"]["fill"] >= 1
    assert model_calls[-2:] == [("researcher", "deepseek/deepseek-flash"), ("manager", "deepseek/deepseek-v4-pro")]
    assert report(tmp_path, write_files=False)["strategy_memory_version"] == 1
    assert len([row for row in journal.export() if row["kind"] == "order_submitted"]) == submitted_before
    clock.value = datetime(2026, 11, 3, 13, 30, tzinfo=timezone.utc)
    source._cache.clear()
    strategy.before_market_opens()
    assert calls[-1][1]["strategy_memory"]["version"] == 1
    clock.value = datetime(2026, 11, 3, 15, 30, 5, tzinfo=timezone.utc)
    source._cache.clear()
    strategy.on_trading_iteration()
    assert calls[-2][1]["strategy_memory"]["version"] == 1
    assert calls[-1][1]["strategy_memory"]["version"] == 1
    assert strategy.last_decision["targets"] == [{"symbol": "QQQ", "weight": .05}]
    # The executor sells the old ALFA holding first, rather than interpreting
    # the memo or replacement target as an immediate unchecked QQQ order.
    last = [row for row in journal.export() if row["kind"] == "order_submitted"][-1]
    assert last["body"]["symbol"] == "ALFA" and last["body"]["side"] == "sell"
    strategy.broker._close_connection()
