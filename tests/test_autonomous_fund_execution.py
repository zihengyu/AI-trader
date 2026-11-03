"""Durable allocation and exception-based plan approval, using synthetic owned accounts."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.execution import (
    EXECUTION_REVISION, ApprovalPending, ApprovalPolicy, ExecutionState, resolve_approval,
)
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import Account, Intent, Limits, Price, RiskError, number, plan


NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
UNIVERSE = {"ALFA", "BETA", "QQQ"}
LIMITS = Limits()


def setup(tmp_path, weights=None):
    journal = Journal(tmp_path, "synthetic-local-binding")
    journal.event(NOW, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    state = ExecutionState(journal)
    body = {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            "next_review_at": (NOW + timedelta(hours=1)).isoformat(), "rationale": "Synthetic allocation",
            "targets": [{"symbol": s, "weight": w} for s, w in (weights if weights is not None else [("ALFA", .09)])]}
    state.accept(body, NOW, UNIVERSE, LIMITS)
    account = Account(number(10000), number(10000), {}, NOW)
    active = state.active(NOW, UNIVERSE, LIMITS)
    return journal, state, account, active


def envelope(reasons=("exposure_ramp",), buys=None, **changes):
    body = {"reasons": list(reasons),
            "buys": buys or {"ALFA": {"max_weight": "0.09", "reference_limit": "100.10"}},
            "max_exposure_after": "0.50", "max_buy_turnover_usd": "10000", "observed_equity": "10000",
            "start_exposure": "0", "current_exposure": "0", "cash_spent": "0",
            "auto_envelope": {"exposure_increase": "0.25", "buy_turnover": "0.40", "remaining_usd": "0"},
            "stress": {"stressed": False}, "risk": {"annual_vol": 0.08, "es_975_1d": 0.012},
            "binding_constraints": []}
    body.update(changes)
    return body


def need(weight="0.09", exposure="0.09", turnover="900", reasons=("exposure_ramp",)):
    return {"reasons": list(reasons), "weight_after": number(weight), "exposure_after": number(exposure),
            "buy_turnover_after": number(turnover)}


def order(quantity=8, limit="100.10", symbol="ALFA", side="buy"):
    return Intent(symbol, side, quantity, number(limit))


def authorize(root, request, now=NOW):
    return resolve_approval(root, request["id"], request["digest"], True, now)


def reserve(journal, state, active, intent, requirement, now=NOW):
    return journal.reserve(intent, now, LIMITS, guard=state.guard(active, intent, now, requirement, UNIVERSE, LIMITS))


def test_one_plan_approval_covers_several_purchases_within_its_envelope(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    assert request["state"] == "pending"
    with pytest.raises(ApprovalPending):
        reserve(journal, state, active, order(), need())
    assert not journal.pending()
    assert authorize(tmp_path, request) == "approved"
    first = reserve(journal, state, active, order(4), need("0.04", "0.04", "400"))
    journal.update(first, "synthetic-1", True, NOW)
    # Not single-use: the approved envelope, not one order, is the unit of review.
    second = reserve(journal, state, active, order(4), need("0.08", "0.08", "800"))
    journal.update(second, "synthetic-2", True, NOW)
    with journal.connection() as db:
        assert db.execute("SELECT state FROM fund_approvals WHERE id=?", (request["id"],)).fetchone()[0] == "approved"
        assert db.execute("SELECT count(*) FROM events WHERE kind='approval_used'").fetchone()[0] == 2


def test_automatic_purchase_needs_no_request_and_sales_never_wait(tmp_path):
    journal, state, account, active = setup(tmp_path, [])
    key = reserve(journal, state, active, order(10, "99.00", side="sell"), None)
    assert journal.pending()[0]["id"] == key
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0


def test_reservation_failure_keeps_the_approval_and_records_no_use(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    authorize(tmp_path, request)
    journal.reserve(Intent("BETA", "buy", 1, number(100)), NOW, LIMITS)  # Unresolved intent blocks.
    with pytest.raises(RiskError, match="Unresolved"):
        reserve(journal, state, active, order(), need())
    with journal.connection() as db:
        assert db.execute("SELECT state FROM fund_approvals WHERE id=?", (request["id"],)).fetchone()[0] == "approved"
        assert db.execute("SELECT count(*) FROM events WHERE kind='approval_used'").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["price", "weight", "exposure", "turnover", "symbol", "side", "reason",
                                    "day", "expiry", "digest", "binding", "stop"])
def test_plan_approval_cannot_expand_to_another_order_scope_or_context(tmp_path, change):
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    if change == "digest":
        with pytest.raises(RiskError):
            resolve_approval(tmp_path, request["id"], "wrong", True, NOW)
        return
    if change == "binding":
        with journal.connection() as db:
            db.execute("UPDATE meta SET value='other-account' WHERE key='binding'")
        with pytest.raises(RiskError):
            authorize(tmp_path, request)
        return
    if change == "stop":
        (tmp_path / "STOP").touch()
        with pytest.raises(RiskError):
            authorize(tmp_path, request)
        return
    authorize(tmp_path, request)
    intent, requirement, now = order(), need(), NOW
    if change == "price":
        intent = order(limit="102.20")  # Above the 2% collar around the reviewed reference.
    elif change == "weight":
        requirement = need(weight="0.12")
    elif change == "exposure":
        requirement = need(exposure="0.55")
    elif change == "turnover":
        requirement = need(turnover="10001")
    elif change == "symbol":
        intent = order(symbol="BETA")
    elif change == "side":
        intent = order(side="sell")
    elif change == "reason":
        requirement = need(reasons=("exposure_ramp", "market_stress"))
    elif change == "day":
        now += timedelta(days=1)
    else:
        now += timedelta(minutes=61)
    with pytest.raises(RiskError):
        reserve(journal, state, active, intent, requirement, now)
    assert not journal.pending()


def test_request_is_reused_within_scope_and_replaced_when_scope_grows(tmp_path):
    journal, state, account, active = setup(tmp_path, [("ALFA", .09), ("BETA", .05)])
    one = state.request(active, envelope(), NOW)
    within = envelope(buys={"ALFA": {"max_weight": "0.09", "reference_limit": "101.50"}})
    assert state.request(active, within, NOW + timedelta(minutes=1))["id"] == one["id"]
    grown = envelope(buys={"ALFA": {"max_weight": "0.09", "reference_limit": "100.10"},
                           "BETA": {"max_weight": "0.05", "reference_limit": "50.05"}})
    two = state.request(active, grown, NOW + timedelta(minutes=2))
    assert two["id"] != one["id"]
    three = state.request(active, envelope(reasons=("exposure_ramp", "market_stress"), buys=grown["buys"]), NOW)
    assert three["id"] != two["id"]
    with journal.connection() as db:
        states = dict(db.execute("SELECT id,state FROM fund_approvals"))
    assert states == {one["id"]: "superseded", two["id"]: "superseded", three["id"]: "pending"}
    with journal.connection() as db:
        notices = db.execute("SELECT count(*) FROM events WHERE kind='notification'").fetchone()[0]
    assert notices == 3  # One actionable alert per distinct plan scope, none for repeated polls.


def test_restart_restores_target_but_expired_or_rejected_targets_do_not_execute(tmp_path):
    journal, state, account, active = setup(tmp_path)
    restored = ExecutionState(Journal(tmp_path, "synthetic-local-binding"))
    assert restored.active(NOW + timedelta(minutes=10), UNIVERSE, LIMITS)["id"] == active["id"]
    assert restored.active(NOW - timedelta(seconds=1), UNIVERSE, LIMITS) is None
    assert restored.active(NOW + timedelta(hours=1), UNIVERSE, LIMITS) is None
    request = restored.request(active, envelope(), NOW)
    resolve_approval(tmp_path, request["id"], request["digest"], False, NOW)
    assert restored.active(NOW, UNIVERSE, LIMITS) is None
    assert restored.latest(NOW)["state"] == "rejected"


def test_superseded_decision_changed_policy_or_risk_model_invalidates_old_approval(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    authorize(tmp_path, request)
    assert ExecutionState(journal, risk_digest="different-risk-model").active(NOW, UNIVERSE, LIMITS) is None
    assert ExecutionState(journal, ApprovalPolicy(valid_minutes=30)).active(NOW, UNIVERSE, LIMITS) is None
    state.accept(active["decision"], NOW, UNIVERSE, LIMITS)
    with pytest.raises(RiskError):
        reserve(journal, state, active, order(), need())


def test_accepted_target_respects_minimum_review_interval(tmp_path):
    journal = Journal(tmp_path, "synthetic-local-binding")
    state = ExecutionState(journal)
    body = {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            "next_review_at": (NOW + timedelta(minutes=1)).isoformat(), "rationale": "Synthetic",
            "targets": [{"symbol": "ALFA", "weight": .05}]}
    state.accept(body, NOW, UNIVERSE, LIMITS, min_review=timedelta(minutes=15))
    assert state.active(NOW + timedelta(minutes=10), UNIVERSE, LIMITS) is not None
    assert state.active(NOW + timedelta(minutes=15), UNIVERSE, LIMITS) is None


@pytest.mark.parametrize("settings", [{"auto_daily_exposure_increase": "0.6", "max_daily_exposure_increase": "0.5"},
                                      {"max_daily_buy_turnover": "4"}, {"valid_minutes": 0},
                                      {"price_collar": "0.2"}, {"stress_requires_approval": "yes"}, {"unknown": 1}])
def test_invalid_approval_policy_fails_explicitly(settings):
    with pytest.raises(RiskError):
        ApprovalPolicy.from_config(settings)


def test_operator_can_revoke_unused_approval_while_stop_is_active(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    authorize(tmp_path, request)
    (tmp_path / "STOP").touch()
    assert resolve_approval(tmp_path, request["id"], request["digest"], False, NOW) == "rejected"
    assert state.active(NOW, UNIVERSE, LIMITS) is None


def test_operator_cli_works_while_runner_lock_is_held_and_requires_no_model_key(tmp_path, monkeypatch, capsys):
    from filelock import FileLock
    from scripts.autonomous_fund import run
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    monkeypatch.setattr(run, "datetime", type("Clock", (), {"now": staticmethod(lambda tz: NOW)}))
    monkeypatch.setattr(run, "models_ready", lambda *args: (_ for _ in ()).throw(AssertionError("No inference allowed")))
    with FileLock(str(tmp_path / "runner.lock"), timeout=0):
        run.main(["approve", "--run-dir", str(tmp_path), "--approval-id", request["id"], "--approval-digest", request["digest"]])
    assert "approved" in capsys.readouterr().out
    assert not journal.pending()  # Approving never submits the order itself.


def test_unchecked_execute_is_refused_before_a_reservation(tmp_path):
    from .test_autonomous_fund import harness
    strategy = harness(tmp_path)
    with pytest.raises(RiskError, match="independent current"):
        strategy._execute(Intent("ALFA", "buy", 1, number(100)), strategy.get_datetime())
    assert not strategy.journal.pending()


def test_hard_daily_order_limit_does_not_generate_a_useless_approval_request(tmp_path):
    from .test_autonomous_fund import harness, decision, quote, account
    strategy = harness(tmp_path)
    strategy.universe = UNIVERSE
    strategy.execution = ExecutionState(strategy.journal)
    now = strategy.get_datetime()
    strategy.execution.accept(decision([("ALFA", .09)]), now, UNIVERSE, LIMITS)
    for i in range(5):
        key = strategy.journal.reserve(Intent("BETA", "buy", 1, number(100)), now, LIMITS)
        strategy.journal.update(key, str(i), True, now)
    prices = {"ALFA": quote()}
    intent = plan({"ALFA": number(".09")}, account(), prices, UNIVERSE, LIMITS, now)[0]
    with pytest.raises(RiskError, match="daily submission limit"):
        strategy._execute(intent, now, account(), prices)
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0


def test_native_restart_continues_multisymbol_target_without_recalling_models(tmp_path, monkeypatch):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
    from .test_autonomous_fund_local import Clock
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "universe": {
        "ALFA": "stock", "BETA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    calls = []

    class TwoTargets(ScriptedRuntime):
        def run(self, request):
            calls.append(request.agent_name)
            result = super().run(request)
            if request.agent_name == "manager":
                body = json.loads(result.summary)
                body["targets"] = [{"symbol": "ALFA", "weight": .05}, {"symbol": "BETA", "weight": .05}]
                result.summary = json.dumps(body)
            return result

    def startup():
        broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False)
        strategy = AutonomousFund(broker=broker, name="AllocationRecovery", parameters={"config": config,
            "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account", "runtime": TwoTargets()})
        strategy._executor._initialize()
        return strategy

    first = startup()
    first.on_trading_iteration()
    first_order = journal.pending()[0]["broker_id"]
    first.broker._close_connection()
    clock.value += timedelta(minutes=10)
    source._cache.clear()
    restored = startup()
    restored.broker.do_polling()
    assert restored.broker._pull_order(first_order, restored.name).status == "fill"
    restored.on_trading_iteration()
    second_order = restored.broker._pull_order(journal.pending()[0]["broker_id"], restored.name)
    assert second_order.asset.symbol == "BETA"
    # Session decisions use saved research, with an optional critic before the manager.
    # Restarted allocation must still continue without any additional AI run.
    assert calls == ["critic", "manager"]
    clock.value += timedelta(minutes=2)
    source._cache.clear()
    restored.broker.do_polling()
    restored.on_trading_iteration()
    assert not journal.pending()
    assert calls == ["critic", "manager"]
    assert restored.execution.latest(clock())["state"] == "complete"
    assert {p.asset.symbol for p in restored.broker._pull_positions(restored.name)} == {"ALFA", "BETA"}
    with journal.connection() as db:
        # Ordinary purchases inside the automatic envelope never involve the operator.
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0
    restored.broker._close_connection()


def test_native_purchase_beyond_envelope_waits_for_plan_approval_then_rechecks_and_fills(tmp_path, monkeypatch):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
    from .test_autonomous_fund_local import Clock
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    # An operator who wants every purchase reviewed sets the automatic envelope to zero.
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "approval": {"auto_daily_exposure_increase": "0"},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    calls = []

    class MajorTarget(ScriptedRuntime):
        def run(self, request):
            calls.append(request.agent_name)
            result = super().run(request)
            if request.agent_name == "manager":
                body = json.loads(result.summary)
                body["targets"] = [{"symbol": "ALFA", "weight": .09}]
                result.summary = json.dumps(body)
            return result

    broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="OperatorApproval", parameters={"config": config,
        "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account", "runtime": MajorTarget()})
    strategy._executor._initialize()
    strategy.on_trading_iteration()
    assert not journal.pending()
    with journal.connection() as db:
        request = dict(db.execute("SELECT * FROM fund_approvals").fetchone())
    assert request["state"] == "pending"
    assert json.loads(request["body"])["reasons"] == ["exposure_ramp"]
    assert set(json.loads(request["body"])["buys"]) == {"ALFA"}
    assert resolve_approval(tmp_path, request["id"], request["digest"], True, clock()) == "approved"
    clock.value += timedelta(seconds=20)
    source._cache.clear()
    strategy.on_trading_iteration()
    assert len(journal.pending()) == 1
    # Approval and execution resume the original manager target without research.
    assert calls == ["critic", "manager"]
    identifier = journal.pending()[0]["broker_id"]
    clock.value += timedelta(minutes=2)
    source._cache.clear()
    broker.do_polling()
    assert broker._pull_order(identifier, strategy.name).status == "fill"
    with journal.connection() as db:
        assert db.execute("SELECT state FROM fund_approvals WHERE id=?", (request["id"],)).fetchone()[0] == "approved"
        assert db.execute("SELECT count(*) FROM events WHERE kind='approval_used'").fetchone()[0] == 1
    broker._close_connection()
