"""Durable allocation and approval contracts, using synthetic owned accounts."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.execution import ApprovalPending, ExecutionState, resolve_approval
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import Account, Intent, Limits, Price, RiskError, number, plan


NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
UNIVERSE = {"ALFA", "BETA", "QQQ"}
LIMITS = Limits()


def setup(tmp_path, weights=None):
    journal = Journal(tmp_path, "synthetic-local-binding")
    journal.event(NOW, "startup", {"mode": "local", "execution_revision": 2})
    state = ExecutionState(journal)
    body = {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            "next_review_at": (NOW + timedelta(hours=1)).isoformat(), "rationale": "Synthetic allocation",
            "targets": [{"symbol": s, "weight": w} for s, w in (weights if weights is not None else [("ALFA", .09)])]}
    key = state.accept(body, NOW, UNIVERSE, LIMITS)
    account = Account(number(10000), number(10000), {}, NOW)
    active = state.active(NOW, UNIVERSE, LIMITS)
    return journal, state, account, active


def intent(weight=".09", account=None):
    account = account or Account(number(10000), number(10000), {}, NOW)
    quotes = {s: Price(number(100), number(100), NOW, number(50_000_000)) for s in {"ALFA"} | set(account.positions)}
    return plan({"ALFA": number(weight)}, account, quotes, UNIVERSE, LIMITS, NOW)[0]


def authorize(root, request, now=NOW):
    return resolve_approval(root, request["id"], request["digest"], True, now)


def reserve(journal, state, active, order, account, request, now=NOW):
    return journal.reserve(order, now, LIMITS, guard=state.guard(active, order, account, now, request, UNIVERSE, LIMITS))


def test_major_order_waits_then_approval_is_atomically_consumed_once(tmp_path):
    journal, state, account, active = setup(tmp_path)
    order = intent()
    request = state.prepare(active, order, account, NOW)
    assert request["state"] == "pending"
    with pytest.raises(ApprovalPending):
        reserve(journal, state, active, order, account, request)
    assert not journal.pending()
    assert authorize(tmp_path, request) == "approved"
    key = reserve(journal, state, active, order, account, request)
    with journal.connection() as db:
        assert db.execute("SELECT state FROM fund_approvals WHERE id=?", (request["id"],)).fetchone()[0] == "consumed"
    journal.update(key, "synthetic", True, NOW)
    with pytest.raises(ApprovalPending):
        reserve(journal, state, active, order, account, request)


def test_daily_limit_failure_does_not_consume_approval(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.prepare(active, intent(), account, NOW)
    authorize(tmp_path, request)
    # A reservation failure rolls approval consumption back in the same txn.
    key = journal.reserve(Intent("BETA", "buy", 1, number(100)), NOW, LIMITS)
    with pytest.raises(RiskError, match="Unresolved"):
        reserve(journal, state, active, intent(), account, request)
    with journal.connection() as db:
        assert db.execute("SELECT state FROM fund_approvals WHERE id=?", (request["id"],)).fetchone()[0] == "approved"


def test_small_orders_need_no_approval_but_splitting_turnover_cannot_bypass_it(tmp_path):
    journal, state, account, active = setup(tmp_path, [("ALFA", .07)])
    order = Intent("ALFA", "buy", 7, number(100))
    for i in range(2):
        assert state.prepare(active, order, account, NOW) is None
        key = reserve(journal, state, active, order, account, None)
        journal.update(key, str(i), True, NOW)
    request = state.prepare(active, order, account, NOW)
    assert "daily_turnover_threshold" in json.loads(request["body"])["reasons"]
    with pytest.raises(ApprovalPending):
        reserve(journal, state, active, order, account, None)


def test_full_liquidation_needs_approval_even_for_a_small_position(tmp_path):
    journal, state, account, active = setup(tmp_path, [])
    account = replace(account, cash=number(9900), positions={"ALFA": 1})
    request = state.prepare(active, Intent("ALFA", "sell", 1, number(99)), account, NOW)
    assert "liquidate_entire_portfolio" in json.loads(request["body"])["reasons"]


@pytest.mark.parametrize("change", ["quantity", "price", "symbol", "side", "day", "expiry", "digest", "binding", "stop"])
def test_grant_cannot_expand_to_another_order_or_context(tmp_path, change):
    journal, state, account, active = setup(tmp_path)
    order = intent()
    request = state.prepare(active, order, account, NOW)
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
    now = NOW
    if change == "quantity":
        order = replace(order, quantity=order.quantity + 1)
    elif change == "price":
        order = replace(order, limit=order.limit * number("1.02"))
    elif change == "symbol":
        order = replace(order, symbol="BETA")
    elif change == "side":
        order = replace(order, side="sell")
    elif change == "day":
        now += timedelta(days=1)
    else:
        now += timedelta(minutes=31)
    with pytest.raises(RiskError):
        reserve(journal, state, active, order, account, request, now)
    assert not journal.pending()


def test_repricing_within_reviewed_bounds_still_uses_the_current_order(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.prepare(active, intent(), account, NOW)
    authorize(tmp_path, request)
    updated = replace(intent(), limit=number("100.20"))
    reused = state.prepare(active, updated, account, NOW + timedelta(seconds=10))
    assert reused["id"] == request["id"]
    reserve(journal, state, active, updated, account, reused, NOW + timedelta(seconds=10))
    assert json.loads(journal.pending()[0]["body"])["limit"] == "100.20"


def test_restart_restores_target_but_expired_or_rejected_targets_do_not_execute(tmp_path):
    journal, state, account, active = setup(tmp_path)
    restored = ExecutionState(Journal(tmp_path, "synthetic-local-binding"))
    assert restored.active(NOW + timedelta(minutes=10), UNIVERSE, LIMITS)["id"] == active["id"]
    assert restored.active(NOW - timedelta(seconds=1), UNIVERSE, LIMITS) is None
    assert restored.active(NOW + timedelta(hours=1), UNIVERSE, LIMITS) is None
    request = restored.prepare(active, intent(), account, NOW)
    resolve_approval(tmp_path, request["id"], request["digest"], False, NOW)
    assert restored.active(NOW, UNIVERSE, LIMITS) is None
    assert restored.latest(NOW)["state"] == "rejected"


def test_superseded_decision_and_changed_policy_invalidate_old_approval(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.prepare(active, intent(), account, NOW)
    authorize(tmp_path, request)
    state.accept(active["decision"], NOW, UNIVERSE, LIMITS)
    with pytest.raises(RiskError):
        reserve(journal, state, active, intent(), account, request)


def test_repeated_polling_does_not_duplicate_pending_requests(tmp_path):
    journal, state, account, active = setup(tmp_path)
    one = state.prepare(active, intent(), account, NOW)
    two = state.prepare(active, intent(), account, NOW + timedelta(minutes=1))
    assert one["id"] == two["id"]
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 1


def test_new_daily_turnover_reason_requires_fresh_operator_review(tmp_path):
    journal, state, account, active = setup(tmp_path)
    original = state.prepare(active, intent(), account, NOW)
    authorize(tmp_path, original)
    prior = journal.reserve(Intent("BETA", "buy", 12, number(100)), NOW, LIMITS)
    journal.update(prior, "synthetic", True, NOW)
    with pytest.raises(ApprovalPending):
        reserve(journal, state, active, intent(), account, original)
    refreshed = state.prepare(active, intent(), account, NOW)
    assert refreshed["id"] != original["id"]
    assert "daily_turnover_threshold" in json.loads(refreshed["body"])["reasons"]


def test_operator_can_revoke_unused_approval_while_stop_is_active(tmp_path):
    journal, state, account, active = setup(tmp_path)
    request = state.prepare(active, intent(), account, NOW)
    authorize(tmp_path, request)
    (tmp_path / "STOP").touch()
    assert resolve_approval(tmp_path, request["id"], request["digest"], False, NOW) == "rejected"
    assert state.active(NOW, UNIVERSE, LIMITS) is None


def test_operator_cli_works_while_runner_lock_is_held_and_requires_no_model_key(tmp_path, monkeypatch, capsys):
    from filelock import FileLock
    from scripts.autonomous_fund import run
    journal, state, account, active = setup(tmp_path)
    request = state.prepare(active, intent(), account, NOW)
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
    order = plan({"ALFA": number(".09")}, account(), prices, UNIVERSE, LIMITS, now)[0]
    with pytest.raises(RiskError, match="daily submission limit"):
        strategy._execute(order, now, account(), prices)
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0


def test_dust_target_that_rounds_to_full_liquidation_cannot_bypass_approval(tmp_path):
    from .test_autonomous_fund import harness
    strategy = harness(tmp_path)
    now = strategy.get_datetime()
    strategy.universe = UNIVERSE
    strategy.execution = ExecutionState(strategy.journal)
    body = {"as_of": now.isoformat(), "expires_at": (now + timedelta(hours=2)).isoformat(),
            "next_review_at": (now + timedelta(hours=1)).isoformat(), "rationale": "Synthetic dust target",
            "targets": [{"symbol": "ALFA", "weight": .00001}]}
    strategy.execution.accept(body, now, UNIVERSE, LIMITS)
    account = Account(number(9900), number(10000), {"ALFA": 1}, now)
    prices = {"ALFA": Price(number(100), number(100), now, number(50_000_000))}
    order = plan({"ALFA": number(".00001")}, account, prices, UNIVERSE, LIMITS, now)[0]
    with pytest.raises(ApprovalPending):
        strategy._execute(order, now, account, prices)
    with strategy.journal.connection() as db:
        request = json.loads(db.execute("SELECT body FROM fund_approvals").fetchone()[0])
        assert "liquidate_entire_portfolio" in request["reasons"]
    assert not strategy.journal.pending()


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
    assert calls == ["researcher", "manager"]
    clock.value += timedelta(minutes=2)
    source._cache.clear()
    restored.broker.do_polling()
    restored.on_trading_iteration()
    assert not journal.pending()
    assert calls == ["researcher", "manager"]
    assert restored.execution.latest(clock())["state"] == "complete"
    assert {p.asset.symbol for p in restored.broker._pull_positions(restored.name)} == {"ALFA", "BETA"}
    restored.broker._close_connection()


def test_native_major_trade_waits_for_operator_then_rechecks_and_fills(tmp_path, monkeypatch):
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
        "ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
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
    assert resolve_approval(tmp_path, request["id"], request["digest"], True, clock()) == "approved"
    clock.value += timedelta(seconds=20)
    source._cache.clear()
    strategy.on_trading_iteration()
    assert len(journal.pending()) == 1
    assert calls == ["researcher", "manager"]
    identifier = journal.pending()[0]["broker_id"]
    clock.value += timedelta(minutes=2)
    source._cache.clear()
    broker.do_polling()
    assert broker._pull_order(identifier, strategy.name).status == "fill"
    with journal.connection() as db:
        assert db.execute("SELECT state FROM fund_approvals WHERE id=?", (request["id"],)).fetchone()[0] == "consumed"
    broker._close_connection()
