"""Source-bound triage samples and fill-time capital-sleeve isolation; offline only."""
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.cash import CashSweep
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.risk import Intent, Limits, number
from scripts.autonomous_fund.run import ScriptedRuntime
from scripts.autonomous_fund.strategy import AutonomousFund, Mandate, account_digest
from .test_autonomous_fund_local import Clock, advance
from .test_autonomous_fund_quant import NOW, model_harness
from .test_autonomous_fund_triage import events


@pytest.fixture
def profiled_fund(tmp_path, monkeypatch):
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock, brokers = Clock(), []
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)

    def create(decision_model="deepseek/deepseek-flash", **triage):
        settings = {"mode": "auto", "start": "shadow", "model": "openai/gpt-synthetic-a", **triage}
        config = {"model": "deepseek/deepseek-flash", "limits": {}, "universe": {"ALFA": "stock"},
                  "agent_models": {"researcher": "deepseek/deepseek-flash", "manager": decision_model},
                  "mandate": {"capital_usd": None}, "critic": {"enabled": False},
                  "factor_lab": {"enabled": False}, "events": {"enabled": False}, "macro": {"enabled": False},
                  "triage": settings}
        journal = Journal(tmp_path, account_digest("local", "synthetic-profile-account", config))
        broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
        brokers.append(broker)
        strategy = AutonomousFund(broker=broker, name="ProfileIsolation", parameters={
            "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "synthetic-profile-account",
            "runtime": ScriptedRuntime()})
        strategy._executor._initialize()
        return strategy

    yield create
    for broker in brokers:
        broker._close_connection()


def scored_state(strategy):
    strategy.journal.set_meta("triage_pairs", [False] * 40)
    strategy.journal.set_meta("triage_enforcing", True)
    strategy.journal.set_meta("triage_dismissal_count", 4)
    strategy.journal.set_meta("triage_last_at", NOW.isoformat())
    strategy.journal.counter("triage_runs", NOW, 11)
    strategy.journal.counter("agent_runs", NOW, 7)


def test_same_profile_preserves_scored_samples_and_state_across_native_restart(profiled_fund):
    first = profiled_fund()
    scored_state(first)
    second = profiled_fund()
    assert second.journal.get_meta("triage_pairs") == [False] * 40
    assert second._triage_enforcing() is True
    assert second.journal.get_meta("triage_dismissal_count") == 4
    assert second.journal.counter("triage_runs", NOW) == 11
    assert second.journal.counter("agent_runs", NOW) == 7


@pytest.mark.parametrize("change", [{"model": "openai/gpt-synthetic-b"}, {"window": 10},
                                    {"max_miss_rate": .1}, {"audit_every": 3}, {"mode": "shadow"}])
def test_changed_profile_does_not_inherit_another_models_qualification(profiled_fund, change):
    first = profiled_fund()
    scored_state(first)
    old = first.journal.get_meta("triage_profile")
    second = profiled_fund(**change)
    assert second.journal.get_meta("triage_pairs") == []
    assert second.journal.get_meta("triage_dismissal_count") == 0
    assert second.journal.get_meta("triage_last_at") is None
    assert second._triage_enforcing() is False
    assert second.journal.counter("triage_runs", NOW) == 11  # A new profile grants no extra paid-call budget.
    assert second.journal.counter("agent_runs", NOW) == 7
    assert second.journal.get_meta("triage_profile") != old
    archived = events(second, "triage_profile_changed")[-1]
    assert archived["previous_pairs"] == [False] * 40 and archived["previous_profile"] == old


@pytest.mark.parametrize("change", ["instructions", "manager_model"])
def test_changed_judge_context_also_starts_a_new_triage_cohort(profiled_fund, monkeypatch, change):
    first = profiled_fund()
    scored_state(first)
    if change == "instructions":
        import scripts.autonomous_fund.triage as triage
        monkeypatch.setattr(triage, "TRIAGE_PROMPT", triage.TRIAGE_PROMPT + " Synthetic additional instruction.")
        second = profiled_fund()
    else:
        second = profiled_fund(decision_model="openai/gpt-synthetic-manager")
    assert second.journal.get_meta("triage_pairs") == [] and not second._triage_enforcing()


@pytest.mark.parametrize("enforcing", [False, True])
@pytest.mark.parametrize("legacy_profile", [None, "unbound", {}])
def test_legacy_unbound_samples_are_archived_without_reenabling_a_rolled_back_screen(profiled_fund, enforcing, legacy_profile):
    first = profiled_fund()
    scored_state(first)
    first.journal.set_meta("triage_enforcing", enforcing)
    if legacy_profile is None:
        with first.journal.connection() as db:
            db.execute("DELETE FROM meta WHERE key='triage_profile'")
    else:
        first.journal.set_meta("triage_profile", legacy_profile)
    second = profiled_fund()
    assert second.journal.get_meta("triage_pairs") == []
    assert second._triage_enforcing() is enforcing
    assert events(second, "triage_profile_changed")[-1]["reason"] == "legacy_unbound"


@pytest.mark.parametrize("same_model,explicit_rate", [(False, False), (True, False), (False, True)])
def test_triage_costs_include_its_actual_model_in_the_pricing_scope(tmp_path, same_model, explicit_rate):
    strategy = model_harness(tmp_path)
    del strategy._record_agent
    strategy.scripted = False
    strategy.agent_models = {"researcher": "fixture/base", "manager": "fixture/base"}
    strategy.triage_model = "fixture/base" if same_model else "fixture/screen"
    prices = {"input": 1, "output": 2}
    strategy.config = {"model_prices_per_million": {"fixture/screen": prices} if explicit_rate else prices}
    answer = SimpleNamespace(model=strategy.triage_model, cache_hit=False,
                             usage={"input_tokens": 1000, "output_tokens": 100}, summary="synthetic",
                             parsed={"escalate": False}, parse_error=None)
    strategy._record_agent("triage", answer)
    [record] = events(strategy, "agent")
    assert record["cost_usd"] == (pytest.approx(.0012) if same_model or explicit_rate else None)


def test_output_protocol_diagnostics_include_the_triage_model_without_changing_decision_models(tmp_path):
    strategy = model_harness(tmp_path)
    strategy.scripted, strategy.triage_model = True, "openai/gpt-synthetic-screen"
    strategy.agent_models = {"researcher": "deepseek/deepseek-flash", "manager": "deepseek/deepseek-v4-pro"}
    before = dict(strategy.agent_models)
    protocols = strategy._record_output_protocols("local")
    assert protocols["triage"] is None
    assert events(strategy, "structured_output_protocol")[0]["models"]["triage"] == strategy.triage_model
    assert strategy.agent_models == before


def test_a_pending_result_from_a_previous_profile_cannot_enter_the_new_sample(tmp_path):
    from .test_autonomous_fund_quant import decision
    from .test_autonomous_fund_triage import fund, verdict
    strategy, _ = fund(tmp_path, "auto", [])
    strategy.triage_profile = {"id": "new"}
    strategy._pending_triage = {"id": "synthetic-result", "profile_id": "old", "action": "audit", "verdict": verdict(False)}
    before = strategy.last_decision
    strategy.last_decision = decision([("BETA", .05)])
    assert strategy._score_triage(before) is None
    assert strategy.journal.get_meta("triage_pairs") is None
    assert events(strategy, "triage_score_discarded")[0]["reason"] == "profile_changed"


@pytest.fixture
def external_book(tmp_path):
    brokers = []

    def create(external_quantity=20, reserved=False, shared=False):
        clock = Clock()
        clock.price, clock.high = 20., 20.01
        cash = number(10000 if reserved else 1000)
        source = YahooRealtimeData(now=clock, fetch=clock.fetch)
        journal = Journal(tmp_path, "synthetic-sleeve-book")
        limits = Limits(daily_orders=20)
        broker = LocalPaperBroker(source, journal, limits, connect_stream=False, starting_cash=str(cash))
        brokers.append(broker)
        with journal.connection() as db:
            db.execute("INSERT INTO local_positions VALUES(?,?,?,?)", ("OWNER", external_quantity, "20", str(clock())))
        journal.event(clock(), "local_ledger_origin", {"cash": str(cash), "receivables": "0", "positions": {
            "OWNER": {"quantity": external_quantity, "average": "20"}}})
        journal.apply_mandate(clock(), "1000", cash, cash, {}, False, {"OWNER": external_quantity}, False)
        mandate = Mandate(capital_usd="1000", allow_external_holdings=True, local_account_usd=str(cash))
        universe = {"ALFA", "BILLS"} | ({"OWNER"} if shared else set())
        broker.configure_valuation(universe=universe, mandate=mandate, sweep=CashSweep("BILLS"))
        assert broker.ledger_audit()["status"] == "verified"
        return clock, source, journal, broker, limits

    yield create
    for broker in brokers:
        broker._close_connection()


def submit_raw(book, symbol, quantity, side="buy"):
    from lumibot.entities import Asset, Order
    from scripts.autonomous_fund.execution import ExecutionState
    from scripts.autonomous_fund.risk import Price
    clock, _, journal, broker, limits = book
    intent = Intent(symbol, side, quantity, number(21 if side == "buy" else 19))
    key = journal.reserve(intent, clock(), limits)
    ExecutionState(journal).record_submission(key, "synthetic-decision", intent, clock(), quote=Price(
        number("19.99"), number("20.01"), clock(), number(50_000_000)))
    order = Order("SleeveIsolation", Asset(symbol), quantity, side, order_type="limit",
                  limit_price=21 if side == "buy" else 19, custom_params={"local_intent": key})
    broker.submit_order(order)
    return order


@pytest.mark.parametrize("external_quantity", [20, 150])
@pytest.mark.parametrize("quantity", [4, 6])
@pytest.mark.parametrize("reserved", [False, True])
def test_external_holdings_neither_expand_nor_consume_the_sleeves_risk_limits(external_book, external_quantity, quantity, reserved):
    book = external_book(external_quantity, reserved)
    clock, source, journal, broker, _ = book
    order = submit_raw(book, "ALFA", quantity)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "SleeveIsolation").status == ("fill" if quantity == 4 else "canceled")
    if quantity == 6:
        [canceled] = journal.events(("local_risk_canceled",))
        assert canceled["body"]["reason"] == "position_limit"
    assert broker.ledger_audit()["status"] == "verified"


@pytest.mark.parametrize("reserved", [False, True])
def test_parking_buffer_uses_sleeve_equity_not_external_investments(external_book, reserved):
    book = external_book(150, reserved)
    clock, source, _, broker, _ = book
    order = submit_raw(book, "BILLS", 49)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "SleeveIsolation").status == "fill"
    assert broker.ledger_audit()["status"] == "verified"


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_frozen_operator_lots_cannot_be_traded_by_a_pending_sleeve_order(external_book, side):
    book = external_book(20, shared=True)
    clock, source, journal, broker, _ = book
    order = submit_raw(book, "OWNER", 1, side)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "SleeveIsolation").status == "canceled"
    [canceled] = journal.events(("local_risk_canceled",))
    assert canceled["body"]["reason"] == "external_holding"
    with journal.connection() as db:
        assert db.execute("SELECT quantity FROM local_positions WHERE symbol='OWNER'").fetchone()[0] == 20


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_an_unadmitted_pending_order_cannot_enter_the_sleeve(external_book, side):
    book = external_book()
    clock, source, journal, broker, _ = book
    order = submit_raw(book, "UNLISTED", 1, side)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "SleeveIsolation").status == "canceled"
    [canceled] = journal.events(("local_risk_canceled",))
    assert canceled["body"]["reason"] == "ineligible_asset"
    assert journal.events(("fill",)) == []
