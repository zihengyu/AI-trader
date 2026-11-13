"""Cash-equivalent execution, review-policy evidence and triage scheduling boundaries.

Synthetic accounts, neutral symbols, deterministic bars and scripted agents only.
"""
from datetime import timedelta

import pytest

from scripts.autonomous_fund.cash import CashSweep
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.risk import Intent, Limits, Price, RiskError, number
from scripts.autonomous_fund.run import ScriptedRuntime
from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
from .test_autonomous_fund_autonomy import decision_body
from .test_autonomous_fund_local import Clock, advance
from .test_autonomous_fund_quant import NOW, account
from .test_autonomous_fund_review import review_harness
from .test_autonomous_fund_triage import NEWS, events, fund, verdict


@pytest.fixture
def native_fund(tmp_path, monkeypatch):
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    brokers = []

    def create(symbol="BILLS", capital=None, limits=None, extra_symbols=()):
        clock = Clock()
        source = YahooRealtimeData(now=clock, fetch=clock.fetch)
        config = {"model": "deepseek/deepseek-flash", "limits": limits or {},
                  "universe": {"ALFA": "stock", symbol: "cash_equivalent", **{s: "stock" for s in extra_symbols}},
                  "cash_sweep": {"symbol": symbol}, "mandate": {"capital_usd": capital},
                  "factor_lab": {"enabled": False}, "events": {"enabled": False},
                  "macro": {"enabled": False}, "critic": {"enabled": False}, "triage": {"mode": "off"}}
        journal = Journal(tmp_path, account_digest("local", "synthetic-cash-account", config))
        broker = LocalPaperBroker(source, journal, Limits(**config["limits"]), connect_stream=False)
        brokers.append(broker)
        strategy = AutonomousFund(broker=broker, name="CashPolicyIntegration", parameters={
            "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "synthetic-cash-account",
            "runtime": ScriptedRuntime()})
        strategy._executor._initialize()
        return strategy, clock, source, journal, broker

    yield create
    for broker in brokers:
        broker._close_connection()


def accept_targets(strategy, clock, targets=()):
    body = decision_body([{"symbol": symbol, "weight": weight} for symbol, weight in targets], when=clock())
    key = strategy.execution.accept(body, clock(), strategy.universe, strategy.limits)
    return strategy.execution.active(clock(), strategy.universe, strategy.limits), key


def submitted(strategy, journal):
    [pending] = journal.pending()
    return strategy.session_orders[pending["id"]]


@pytest.mark.parametrize("symbol", ["BILLS", "RESERVE"])
@pytest.mark.parametrize("capital", [None, "1000"])
@pytest.mark.parametrize("paused", [False, True])
def test_native_cash_parking_fills_without_investment_caps_but_preserves_the_sleeve(native_fund, symbol, capital, paused):
    strategy, clock, source, journal, broker = native_fund(symbol, capital)
    initial = strategy._snapshot()
    if paused:
        journal.mark_equity(clock(), number(initial.equity) * 2, strategy.limits)
    active, _ = accept_targets(strategy, clock)
    strategy._continue_allocation(active, initial)
    order = submitted(strategy, journal)
    assert order.asset.symbol == symbol and number(order.quantity) * number(order.limit_price) > initial.equity * number(".7")
    advance(clock, source)
    broker.do_polling()
    actual = broker._pull_order(order.identifier, strategy.name)
    assert actual.status == "fill"
    strategy._reconcile()
    observed = strategy._snapshot()
    assert observed.positions == {} and observed.cash_equivalents == {symbol: number(order.quantity)}
    real_cash = observed.cash - observed.cash_equivalent_value
    assert real_cash >= observed.equity * strategy.cash_sweep.keep_cash
    assert journal.reserve_cash() == (number(9000) if capital else 0)
    assert broker.ledger_audit()["status"] == "verified"
    assert not events(strategy, "local_risk_canceled") and not journal.pending()


def test_native_sweep_sale_can_fund_a_normal_purchase_without_counting_remaining_sweep_as_exposure(native_fund):
    strategy, clock, source, journal, broker = native_fund()
    active, _ = accept_targets(strategy, clock)
    strategy._continue_allocation(active, strategy._snapshot())
    parked = submitted(strategy, journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(parked.identifier, strategy.name).status == "fill"
    strategy._reconcile()
    active, _ = accept_targets(strategy, clock, [("ALFA", .05)])
    strategy._continue_allocation(active, strategy._snapshot())
    funding = submitted(strategy, journal)
    assert funding.asset.symbol == "BILLS" and str(funding.side) == "sell"
    advance(clock, source)
    broker.do_polling()
    strategy._reconcile()
    strategy._continue_allocation(active, strategy._snapshot())
    purchase = submitted(strategy, journal)
    assert purchase.asset.symbol == "ALFA" and str(purchase.side) == "buy"
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(purchase.identifier, strategy.name).status == "fill"
    assert broker.ledger_audit()["status"] == "verified"


def test_an_order_kind_label_cannot_exempt_an_ordinary_stock_from_fill_time_limits(native_fund):
    strategy, clock, source, journal, broker = native_fund()
    _, key = accept_targets(strategy, clock)
    strategy._submit(Intent("ALFA", "buy", 20, number(101)), clock(), key, kind="cash_sweep")
    order = submitted(strategy, journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, strategy.name).status == "canceled"
    [canceled] = events(strategy, "local_risk_canceled")
    assert canceled["reason"] == "position_limit" and canceled["symbol"] == "ALFA"
    assert not events(strategy, "fill")


def test_disabled_cash_classification_keeps_normal_position_limits(native_fund):
    strategy, clock, source, journal, broker = native_fund()
    policy = dict(broker._valuation_policy)
    broker.configure_valuation(**{**policy, "sweep": None})
    _, key = accept_targets(strategy, clock)
    strategy._submit(Intent("BILLS", "buy", 98, number(101)), clock(), key, kind="cash_sweep")
    order = submitted(strategy, journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, strategy.name).status == "canceled"
    assert events(strategy, "local_risk_canceled")[0]["reason"] == "position_limit"


@pytest.mark.parametrize("quantity,reason", [(99, "cash_buffer"), (100, "insufficient_cash")])
def test_cash_parking_cannot_use_the_buffer_or_unfunded_cash(native_fund, quantity, reason):
    strategy, clock, source, journal, broker = native_fund()
    _, key = accept_targets(strategy, clock)
    strategy._submit(Intent("BILLS", "buy", quantity, number(101)), clock(), key, kind="cash_sweep")
    order = submitted(strategy, journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, strategy.name).status == "canceled"
    assert events(strategy, "local_risk_canceled")[0]["reason"] == reason


def test_normal_purchases_still_obey_the_portfolio_exposure_limit_with_a_configured_sweep(native_fund):
    strategy, clock, source, journal, broker = native_fund(
        limits={"max_position": ".7", "max_exposure": ".7"}, extra_symbols=("BETA",))
    _, key = accept_targets(strategy, clock)
    strategy._submit(Intent("ALFA", "buy", 60, number(101)), clock(), key)
    first = submitted(strategy, journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(first.identifier, strategy.name).status == "fill"
    strategy._reconcile()
    strategy._submit(Intent("BETA", "buy", 20, number(101)), clock(), key, kind="cash_sweep")
    second = submitted(strategy, journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(second.identifier, strategy.name).status == "canceled"
    assert events(strategy, "local_risk_canceled")[0]["reason"] == "exposure_limit"


def test_restart_preserves_cash_classification_for_an_already_submitted_sweep(native_fund):
    strategy, clock, source, journal, broker = native_fund()
    active, _ = accept_targets(strategy, clock)
    strategy._continue_allocation(active, strategy._snapshot())
    order = submitted(strategy, journal)
    restored = LocalPaperBroker(source, journal, strategy.limits, connect_stream=False)
    try:
        restored.configure_valuation(**broker._valuation_policy)
        advance(clock, source)
        restored.do_polling()
        assert restored._pull_order(order.identifier, strategy.name).status == "fill"
        restored.do_polling()
        assert len(events(strategy, "fill")) == 1 and restored.ledger_audit()["status"] == "verified"
    finally:
        restored._close_connection()


def test_partial_cash_fills_preserve_fees_and_reserved_capital(native_fund):
    strategy, clock, source, journal, broker = native_fund(capital="1000")
    clock.volume = 300  # Three shares per completed minute.
    active, _ = accept_targets(strategy, clock)
    strategy._continue_allocation(active, strategy._snapshot())
    order = submitted(strategy, journal)
    assert order.quantity == 9
    for step in range(3):
        advance(clock, source)
        broker.do_polling()
        actual = broker._pull_order(order.identifier, strategy.name)
        assert actual.status == ("partial_fill" if step < 2 else "fill")
        assert journal.reserve_cash() == number(9000)
    assert len(events(strategy, "fill")) == 3
    assert actual.trade_cost == pytest.approx(float(number(actual.avg_fill_price) * 9 * broker.fee_rate))
    assert broker.ledger_audit()["status"] == "verified"


def test_an_unadmitted_symbol_cannot_be_configured_as_the_sweep(native_fund):
    strategy, _, _, _, broker = native_fund()
    before = broker._valuation_policy
    with pytest.raises(RiskError, match="configured universe"):
        broker.configure_valuation(universe={"ALFA"}, mandate=strategy.mandate, sweep=CashSweep("RESERVE"))
    assert broker._valuation_policy is before


def test_stale_cash_parking_order_does_not_block_reviews_until_the_old_decision_expires(native_fund, monkeypatch):
    strategy, clock, source, journal, broker = native_fund()
    active, _ = accept_targets(strategy, clock)
    strategy._continue_allocation(active, strategy._snapshot())
    order = submitted(strategy, journal)
    canceled = []
    monkeypatch.setattr(strategy, "cancel_order", lambda observed: canceled.append(observed.identifier))
    clock.value += timedelta(seconds=strategy.execution_policy.reprice_after_seconds + 1)
    strategy._manage_open_orders(clock())
    assert canceled == [order.identifier]


@pytest.mark.parametrize("future", [False, True])
def test_sweep_quote_uses_the_clock_after_io_without_accepting_future_source_times(tmp_path, future):
    from .test_autonomous_fund_cash_sweep import strategy_with_sweep
    strategy = strategy_with_sweep(tmp_path)
    clock = [NOW]
    strategy.mode, strategy.get_datetime = "local", lambda: clock[0]

    def price(*args, **kwargs):
        clock[0] += timedelta(seconds=5)
        return Price(number(100), number(100), clock[0] + timedelta(seconds=1 if future else 0), number(50_000_000))

    strategy._price = price
    if future:
        with pytest.raises(RiskError):
            strategy._sweep_quote(NOW)
    else:
        assert strategy._sweep_quote(NOW).as_of == clock[0]


def test_review_agents_receive_the_configured_cash_policy_as_citable_evidence(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.cash_sweep = CashSweep("RESERVE", keep_cash="0.03", band="0.05")
    strategy._configured_universe = frozenset({"ALFA", "RESERVE"})
    strategy.after_market_closes()
    contexts = [kw["context"] for _, kw in calls]
    assert len(contexts) == 2
    for context in contexts:
        policy = context["cash_management"]
        assert policy["enabled"] and policy["symbol"] == "RESERVE"
        assert policy["keep_cash"] == "0.03" and policy["band"] == "0.05"
        assert policy["ai_target_allowed"] is False
        assert "RESERVE" not in context["eligible_universe"]
    assert "context:cash_management" in contexts[-1]["allowed_evidence_refs"]


@pytest.mark.parametrize("legacy", [False, True])
def test_review_retry_reuses_the_saved_policy_without_inventing_legacy_policy(tmp_path, legacy):
    import json
    from scripts.autonomous_fund.journal import encode
    strategy, calls = review_harness(tmp_path)
    strategy.cash_sweep = CashSweep("BILLS")
    strategy._configured_universe = frozenset({"ALFA", "BILLS", "RESERVE"})
    manager = strategy.agents["manager"].run
    strategy.agents["manager"].run = lambda **kw: (_ for _ in ()).throw(ConnectionError("synthetic failure"))
    strategy.after_market_closes()
    if legacy:
        with strategy.journal.connection() as db:
            row = db.execute("SELECT sequence,body FROM events WHERE kind='postmarket_research'").fetchone()
            body = json.loads(row["body"])
            body["context"].pop("cash_management")
            db.execute("UPDATE events SET body=? WHERE sequence=?", (encode(body), row["sequence"]))
    strategy.cash_sweep = CashSweep("RESERVE")
    strategy.agents["manager"].run = manager
    strategy.after_market_closes()
    assert [role for role, _ in calls] == ["researcher", "manager"]
    context = calls[-1][1]["context"]
    if legacy:
        assert "cash_management" not in context and "context:cash_management" not in context["allowed_evidence_refs"]
    else:
        assert context["cash_management"]["symbol"] == "BILLS"


@pytest.mark.parametrize("path", ["batch", "dismiss", "slow"])
def test_triage_never_defers_the_manager_beyond_its_maximum_quiet_deadline(tmp_path, path):
    strategy, calls = fund(tmp_path, "enforce", [verdict(False)], decided_minutes_ago=119)
    strategy._triggers = [NEWS]
    deadline = NOW + timedelta(minutes=1)
    strategy.journal.set_meta("scheduled_review_at", (NOW + timedelta(hours=1)).isoformat())
    if path == "batch":
        strategy.journal.set_meta("triage_last_at", (NOW - timedelta(minutes=2)).isoformat())
    if path == "slow":
        run = strategy.agents["triage"].run
        clock = [NOW]
        strategy.get_datetime = lambda: clock[0]

        def slow(**kwargs):
            clock[0] = NOW + timedelta(minutes=2)
            return run(**kwargs)

        strategy.agents["triage"].run = slow
    skipped = strategy._triage_gate(account(), NOW)
    if path == "slow":
        assert skipped is False  # The deadline elapsed during the screen: manager now.
        assert strategy._triggers == [NEWS]
    else:
        assert skipped and strategy.next_review <= deadline


def test_a_risk_pause_is_not_dismissed_by_the_soft_event_screen(tmp_path):
    strategy, calls = fund(tmp_path, "enforce", [verdict(False)])
    strategy._triggers = [NEWS]
    assert strategy._triage_gate(account(), NOW, paused=True) is False
    assert calls == [] and strategy._triggers == [NEWS]


def test_stop_arriving_during_triage_prevents_a_followup_manager_call(tmp_path):
    strategy, calls = fund(tmp_path, "enforce", [verdict(True)])
    strategy._triggers = [NEWS]
    original = strategy.agents["triage"].run

    def stop(**kwargs):
        strategy.halt_path.touch()
        return original(**kwargs)

    strategy.agents["triage"].run = stop
    assert strategy._triage_gate(account(), NOW) is False
    strategy._decide(account(), NOW, False)
    assert len(calls) == 1 and not events(strategy, "decision_backoff")


def test_stop_arriving_during_context_preparation_prevents_the_manager_call(tmp_path):
    from .test_autonomous_fund_manager_independence import live_manager
    strategy, calls = live_manager(tmp_path)

    # The private hook also receives the optional diagnostic call collector; STOP behavior is unchanged.
    def critique(*args, **kwargs):
        strategy.halt_path.touch()
        return None

    strategy._critique = critique
    strategy._decide(account(), NOW, False)
    assert not calls and not events(strategy, "decision_backoff")


@pytest.mark.parametrize("mode", ["local", "paper", "mock"])
def test_a_preexisting_stop_prevents_all_decision_work(tmp_path, mode):
    from .test_autonomous_fund_manager_independence import live_manager
    strategy, calls = live_manager(tmp_path, mode)
    strategy.halt_path.touch()
    strategy._decide(account(), NOW, False)
    assert not calls and not events(strategy, "decision_backoff")


@pytest.mark.parametrize("bad", ["future_decision", "future_triage", "malformed_triage", "malformed_schedule"])
def test_invalid_scheduling_state_does_not_suppress_the_manager(tmp_path, bad):
    from scripts.autonomous_fund.risk import trading_day
    strategy, calls = fund(tmp_path, "enforce", [verdict(False)])
    strategy._triggers = [NEWS]
    if bad == "future_decision":
        strategy.journal.set_meta("last_model_decision", {"at": (NOW + timedelta(minutes=1)).isoformat(),
                                                          "day": trading_day(NOW)})
    elif bad == "future_triage":
        strategy.journal.set_meta("triage_last_at", (NOW + timedelta(minutes=1)).isoformat())
    else:
        strategy.journal.set_meta("triage_last_at" if bad == "malformed_triage" else "scheduled_review_at", "invalid")
    assert strategy._triage_gate(account(), NOW) is False
    assert calls == [] and strategy._triggers == [NEWS]


@pytest.mark.parametrize("window", [10, 100])
def test_triage_status_uses_the_actual_scoring_window(tmp_path, window):
    from scripts.autonomous_fund.status import triage_line
    strategy, _ = fund(tmp_path, "shadow", [])
    pairs = [True] * 110 + [False] * 10
    strategy.journal.set_meta("triage_pairs", pairs)
    strategy.journal.event(NOW, "triage_scored", {"stats": {"window": window}})
    with strategy.journal.connection() as db:
        text = triage_line(db, NOW)
    assert f"最近 {window} 次" in text and f"漏判 {sum(pairs[-window:])} 次" in text
