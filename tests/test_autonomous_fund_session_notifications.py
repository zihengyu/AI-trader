"""Real app entrypoints and execution/watch owners, with synthetic clocks and no I/O providers."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.notifications import readable
from scripts.autonomous_fund.risk import MarketDataUnavailable, ObservedPrice, number
from .test_autonomous_fund_alert_recovery import alert_strategy, notices
from .test_autonomous_fund_autonomy import accept
from .test_autonomous_fund_decision_correction import events
from .test_autonomous_fund_execution_policy_boundaries import native_fund, accept_targets  # noqa: F401
from .test_autonomous_fund_local import Clock, advance
from .test_autonomous_fund_quant import NOW, account
from .test_autonomous_fund_review import CLOSE, review_harness


def lag(strategy, clock, *, age=65, reason="stale"):
    def failed(symbol):
        raise MarketDataUnavailable("Stale, future or nonpositive observation", diagnostic={
            "reason": reason, "age_seconds": age, "max_age_seconds": 60,
            "source_as_of": (clock() - timedelta(seconds=age)).isoformat(),
            "checked_at": clock().isoformat(), "price_kind": "completed_minute_close_with_spread_proxy"})
    strategy._alert_price = failed


@pytest.mark.parametrize("symbol", ["BETA", "CHAR"])
def test_short_publication_gap_survives_restart_without_telegram_flapping(tmp_path, symbol):
    clock = Clock()
    clock.value = NOW
    strategy = alert_strategy(tmp_path)
    strategy.get_datetime = clock
    accept(strategy, [], [{"symbol": symbol, "above": 110, "reason": "Synthetic level"}])
    lag(strategy, clock)
    strategy._watch(account(), clock())
    clock.value += timedelta(seconds=30)
    restarted = alert_strategy(tmp_path)
    restarted.get_datetime = clock
    lag(restarted, clock, age=82)
    restarted._watch(account(), clock())
    assert restarted.journal.get_meta("price_alert_health")[symbol]["failed_checks"] == 2
    assert not notices(restarted, "price_alert_unavailable")
    # A fresh crossing acts immediately; the grace only changes notice delivery.
    restarted._alert_price = lambda _: ObservedPrice(number(111), clock())
    restarted._watch(account(), clock())
    assert len(events(restarted, "price_alert")) == 1
    assert not notices(restarted, "price_alert_recovered")
    assert events(restarted, "price_alert_recovery_observed")[0]["failed_checks"] == 2
    assert len(events(restarted, "price_observation_rejected")) == 1
    assert not restarted.journal.pending()


def test_persistent_publication_lag_notifies_once_then_recovers(tmp_path):
    strategy, clock = alert_strategy(tmp_path), Clock()
    clock.value = NOW
    strategy.get_datetime = clock
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    lag(strategy, clock)
    for seconds in (0, 30, 60, 90, 120):
        clock.value = NOW + timedelta(seconds=seconds)
        strategy._watch(account(), clock())
    assert len(notices(strategy, "price_alert_unavailable")) == 1
    assert not events(strategy, "price_alert")
    assert strategy.journal.get_meta("price_alert_health")["BETA"]["failed_checks"] == 5
    strategy._alert_price = lambda _: ObservedPrice(number(105), clock())
    strategy._watch(account(), clock())
    assert notices(strategy, "price_alert_recovered")[0]["failed_checks"] == 5


@pytest.mark.parametrize("age,reason", [(121, "stale"), (-1, "future"), (65, "invalid_price")])
def test_older_or_malformed_quote_notifies_immediately(tmp_path, age, reason):
    strategy, clock = alert_strategy(tmp_path), Clock()
    clock.value = NOW
    strategy.get_datetime = clock
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    lag(strategy, clock, age=age, reason=reason)
    strategy._watch(account(), clock())
    assert notices(strategy, "price_alert_unavailable")[0]["reason"] == reason
    assert not events(strategy, "price_alert") and not strategy.journal.pending()


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("closing", ["2026-11-16T21:00:00+00:00", "2026-11-27T18:00:00+00:00"])
def test_closed_session_defers_before_valuation_quotes_or_approvals(native_fund, mode, closing):
    strategy, clock, _, journal, _ = native_fund()
    strategy.mode = mode
    clock.value = datetime.fromisoformat(closing)
    active, key = accept_targets(strategy, clock)
    def forbidden(*a, **kw):
        raise AssertionError("Closed-session plan attempted execution I/O")
    strategy._snapshot = strategy._price = strategy._candidates = forbidden
    strategy._continue_allocation(active)
    strategy._continue_allocation(active)
    [detail] = notices(strategy, "allocation_session_pending")
    assert detail["decision_id"] == key and detail["valid_at_next_open"] is False
    assert strategy.execution.latest(clock())["state"] == "active"
    assert not events(strategy, "order_submitted") and not journal.pending()
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0
    assert "过期计划须重新决策" in readable("allocation_session_pending", detail)
    # Expired intent does not revive when the next session opens.
    clock.value = datetime.fromisoformat(detail["next_open"])
    assert strategy.execution.active(clock(), strategy.universe, strategy.limits) is None


@pytest.mark.parametrize("fails", [False, True])
def test_quote_read_crossing_the_close_defers_without_order_or_generic_block(native_fund, fails):
    strategy, clock, _, journal, _ = native_fund()
    clock.value = datetime(2026, 11, 16, 20, 59, 59, tzinfo=timezone.utc)
    active, _ = accept_targets(strategy, clock, [("ALFA", .05)])
    held = strategy._snapshot()
    original = strategy._price
    def quote(symbol):
        result = original(symbol)
        clock.value += timedelta(seconds=2)
        if fails:
            raise MarketDataUnavailable("Stale, future, crossed or excessively wide quote")
        return result
    strategy._price = quote
    strategy._continue_allocation(active, held)
    assert len(notices(strategy, "allocation_session_pending")) == 1
    assert not journal.pending() and not events(strategy, "allocation_complete")


def test_open_session_keeps_real_quote_failure_and_does_not_claim_calendar_deferral(native_fund):
    strategy, clock, _, journal, _ = native_fund()
    active, _ = accept_targets(strategy, clock, [("ALFA", .05)])
    held = strategy._snapshot()
    def unavailable(symbol):
        raise MarketDataUnavailable("Synthetic missing source quote")
    strategy._price = unavailable
    with pytest.raises(MarketDataUnavailable, match="Synthetic missing"):
        strategy._continue_allocation(active, held)
    assert not notices(strategy, "allocation_session_pending") and not journal.pending()


def test_postmarket_review_sees_after_close_plan_separately_from_real_fills(tmp_path):
    strategy, calls = review_harness(tmp_path)
    from scripts.autonomous_fund.execution import ExecutionState
    strategy.execution = ExecutionState(strategy.journal)
    strategy.get_datetime = lambda: CLOSE + timedelta(seconds=2)
    from .test_autonomous_fund_quant import decision
    body = {**decision([]), "as_of": CLOSE.isoformat(), "expires_at": (CLOSE + timedelta(hours=2)).isoformat(),
            "next_review_at": (CLOSE + timedelta(hours=1)).isoformat()}
    key = strategy.execution.accept(body, CLOSE, strategy.universe, strategy.limits)
    strategy.after_market_closes()
    for _, call in calls:
        pending = call["context"]["post_close_plan"]
        assert pending["decision_id"] == key and pending["targets"] == []
        assert pending["is_fill_evidence"] is False
    assert "context:post_close_plan" in calls[-1][1]["context"]["allowed_evidence_refs"]
    assert events(strategy, "postmarket_review")[0]["post_close_plan"]["decision_id"] == key
    assert not events(strategy, "order_submitted") and not strategy.journal.pending()


def test_reused_postmarket_research_does_not_hide_a_newer_pending_plan(tmp_path):
    from scripts.autonomous_fund.execution import ExecutionState
    from .test_autonomous_fund_quant import decision
    strategy, calls = review_harness(tmp_path)
    strategy.execution = ExecutionState(strategy.journal)
    strategy.get_datetime = lambda: CLOSE
    manager = strategy.agents["manager"].run
    strategy.agents["manager"].run = lambda **kw: (_ for _ in ()).throw(ConnectionError("synthetic retry"))
    strategy.after_market_closes()
    assert events(strategy, "postmarket_research")[0]["context"]["post_close_plan"] is None
    when = CLOSE + timedelta(seconds=2)
    body = {**decision([]), "as_of": when.isoformat(), "expires_at": (when + timedelta(hours=2)).isoformat(),
            "next_review_at": (when + timedelta(hours=1)).isoformat()}
    key = strategy.execution.accept(body, when, strategy.universe, strategy.limits)
    strategy.get_datetime = lambda: when + timedelta(seconds=1)
    strategy.agents["manager"].run = manager
    strategy.after_market_closes()
    context = calls[-1][1]["context"]
    assert context["post_close_plan"]["decision_id"] == key
    assert "context:post_close_plan" in context["allowed_evidence_refs"]
    assert any(row["ref"] == "context:post_close_plan" for row in context["evidence_catalog"])
    assert len([name for name, _ in calls if name == "researcher"]) == 1
    assert events(strategy, "postmarket_review")[0]["post_close_plan"]["decision_id"] == key


def test_datadesk_cli_does_not_construct_an_implicit_alpaca_broker(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"model": "deepseek/deepseek-flash", "limits": {}, "universe": {"ALFA": "stock"}}))
    code = r'''
import os, sys
from unittest.mock import patch
import lumibot.brokers as brokers
assert 'lumibot.credentials' not in sys.modules
def forbidden(*a, **kw):
    raise AssertionError('Read-only desk constructed an implicit trading broker')
with patch.object(brokers, 'Alpaca', forbidden):
    from scripts.autonomous_fund import run, datadesk
    def desk(root, config, mode):
        assert os.environ['LUMIBOT_LAZY_CREDENTIALS'] == 'true'
        assert mode == 'local'
        # Selected broker constructors remain callable: only implicit import
        # construction was deferred, not the explicit paper trading broker.
        from lumibot.credentials import _DEFAULT_CREDENTIALS_CACHE
        assert _DEFAULT_CREDENTIALS_CACHE is None
        print('desk-started-without-trading-broker')
        return 0
    datadesk.run = desk
    run.main(['datadesk', '--config', sys.argv[1], '--run-dir', sys.argv[2]])
'''
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("FUND_", "ALPACA_", "TRADIER_", "LUMIBOT_"))}
    env.update(ALPACA_API_KEY="synthetic-key", ALPACA_API_SECRET="synthetic-secret",
               LUMIBOT_DISABLE_DOTENV="true", LUMIBOT_LAZY_CREDENTIALS="false")
    result = subprocess.run([sys.executable, "-B", "-c", code, str(config), str(tmp_path / "desk")],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr[-6000:]
    assert "desk-started-without-trading-broker" in result.stdout


def test_lazy_default_credentials_do_not_disable_explicit_paper_broker_streams(monkeypatch):
    from lumibot.brokers.alpaca import Alpaca
    from lumibot.brokers.broker import Broker
    monkeypatch.setenv("LUMIBOT_LAZY_CREDENTIALS", "true")
    for key in ("LUMIBOT_CONNECT_STREAM", "LUMIBOT_START_ORDERS_THREAD", "LUMIBOT_SCHEDULED_EXECUTION"):
        monkeypatch.delenv(key, raising=False)
    calls = []
    monkeypatch.setattr(Broker, "__init__", lambda self, **kw: calls.append(kw))
    # Caller-owned client/data isolate authentication and network; retain the
    # adapter's real constructor and default stream/thread option selection.
    Alpaca({"PAPER": True}, trading_client=SimpleNamespace(), data_source=SimpleNamespace())
    assert calls[0]["connect_stream"] is True and calls[0]["start_orders_thread"] is True


def test_native_manager_finishing_after_close_retains_unfilled_liquidation_plan(native_fund):
    from scripts.autonomous_fund.strategy import Decision
    from .test_autonomous_fund_autonomy import decision_body
    strategy, clock, source, journal, broker = native_fund()
    clock.value = datetime(2026, 11, 16, 20, 55, 5, tzinfo=timezone.utc)
    active, _ = accept_targets(strategy, clock, [("ALFA", .05)])
    strategy._continue_allocation(active, strategy._snapshot())
    advance(clock, source)
    broker.do_polling()
    strategy._reconcile()
    assert strategy._snapshot().positions.get("ALFA", 0) > 0
    clock.value = datetime(2026, 11, 16, 20, 59, 20, tzinfo=timezone.utc)
    submitted_before = len(events(strategy, "order_submitted"))
    strategy._shadow = lambda *a, **kw: None
    def manager(name, **kwargs):
        assert name == "manager"
        assert kwargs["context"]["holding_math"]["risk_positions_usd"] > 0
        clock.value = datetime(2026, 11, 16, 21, 0, 2, tzinfo=timezone.utc)
        body = Decision.model_validate(decision_body([], when=clock()))
        return SimpleNamespace(model="deepseek/deepseek-flash", parsed=body,
            parse_error=None, usage={"input_tokens": 1, "output_tokens": 1}, cache_hit=False,
            summary=body.model_dump_json())
    strategy._agent_call = manager
    strategy._decide(strategy._snapshot(), clock(), False)
    [pending] = notices(strategy, "allocation_session_pending")
    assert strategy.last_decision["targets"] == []
    assert journal.get_meta("allocation_session_pending")["decision_id"] == pending["decision_id"]
    assert len(events(strategy, "order_submitted")) == submitted_before
    assert strategy._snapshot().positions.get("ALFA", 0) > 0 and not journal.pending()
    assert not notices(strategy, "blocked")
    from scripts.autonomous_fund.decision_activity import activity, lines
    timeline = activity(journal.root, "2026-11-16", clock())
    assert "等待常规交易时段" in "\n".join(lines(timeline, timeline=True))


def test_holding_math_scales_costs_to_risk_notional_and_does_not_double_count_cash():
    from dataclasses import replace
    from scripts.autonomous_fund.trade_value import holding_math
    held = replace(account(cash=8500, positions={"ALFA": 1, "BETA": 2}, equity=10000),
                   cash_equivalent_value=number(8300), cash_equivalents={"BILLS": 83},
                   position_prices={"ALFA": number(500), "BETA": number(500)})
    result = holding_math(held, local_costs={"fee_rate": ".001", "slippage": ".0004", "spread": ".0002"})
    assert result["free_cash_usd"] == 200 and result["cash_equivalents_usd"] == 8300
    assert result["risk_positions_usd"] == 1500
    assert sum(row["notional_usd"] for row in result["positions"].values()) == 1500
    assert result["estimated_exit_cost_usd"] == 2.25
    assert result["estimated_round_trip_cost_usd"] == 4.5
    assert result["estimated_exit_cost_nav_bps"] == 2.25
    assert not held.open_orders  # No account or execution mutation.


def test_missing_marks_and_broker_costs_remain_unknown_in_holding_math():
    from scripts.autonomous_fund.trade_value import holding_math
    result = holding_math(account(cash=9500, positions={"ALFA": 5}, equity=10000))
    assert result["positions"]["ALFA"]["notional_usd"] is None
    assert result["estimated_exit_cost_usd"] is None
    assert result["cost_assumptions"] is None
    assert result["risk_positions_usd"] == 500


@pytest.mark.parametrize("mode,phrase", [("full", "独立风控 AI 审批"), ("semi", "等待人工审批"),
                                        (None, "按运行配置进入审批")])
def test_decision_notice_describes_the_selected_approval_owner(mode, phrase):
    text = readable("decision_changed", {"automation_mode": mode})
    assert phrase in text and "审批不放宽硬性风控" in text


def test_action_notice_is_bounded_without_discarding_unconfirmed_source_records(tmp_path):
    from scripts.autonomous_fund.action_quality import KEY
    from scripts.autonomous_fund.datastore import DailyStore
    from scripts.autonomous_fund.risk import trading_day
    from .test_autonomous_fund_quant import harness
    strategy = harness(tmp_path, mode="local")
    store = strategy.daily_store = DailyStore(tmp_path / "daily")
    day = trading_day(NOW)
    rows = [{"kind": "dividend", "date": (NOW - timedelta(days=i)).date().isoformat(),
             "yahoo": "0.50", "alpaca": None, "reason": "one_source_only"} for i in range(67)]
    source = {"ALFA": {"status": "unconfirmed", "unconfirmed": rows}}
    store.put(day, KEY, source)
    details = strategy._report_bar_quality(NOW)
    assert details["corporate_actions"]["unconfirmed"] == {"ALFA": 67}
    assert len(details["corporate_actions"]["unconfirmed_examples"]) == 5
    assert details["corporate_actions"]["unconfirmed_reasons"] == {"one_source_only": 67}
    text = readable("bar_quality_issues", details)
    assert "另有 62 条" in text and "单源记录 67 条" in text
    assert store.get(day, KEY) == source and details["withheld"] == []
