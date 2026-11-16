"""Forward-declared scenarios use raw receipts, separate money and visible evidence.

Synthetic prices and native application doubles; no provider, model or broker I/O.
"""
from copy import deepcopy
from dataclasses import asdict

import pytest

from scripts.autonomous_fund.comparison import Book, declaration, evaluate, project
from scripts.autonomous_fund.quant import QuantPolicy
from scripts.autonomous_fund.risk import Limits, number

DAYS = ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05")


def protocol(**kwargs):
    limits = Limits(max_position="0.5", max_exposure="0.7", daily_orders=10)
    universe = {"ALFA": "stock", "BETA": "stock", "GEAR": "leveraged_etf", "BILLS": "cash_equivalent"}
    quant = QuantPolicy.from_config(None, set(universe), limits)
    return declaration(asdict(limits), asdict(quant), universe,
        [{"id": "fixed", "name": "fixed", "weights": {"ALFA": "0.5"}}],
        cash_symbol="BILLS", fee_rate=kwargs.get("fee", "0.001"), slippage=kwargs.get("slip", "0"))


def target(symbol=None, weight="0.5"):
    return {"targets": [{"symbol": symbol, "weight": weight}] if symbol else []}


def event(seq, day, kind, body, clock="10:00:00"):
    return seq, f"{day}T{clock}-05:00", kind, body


def pair(policy, **overrides):
    return {"valid": True, "comparison_protocol_id": policy["id"], "comparison_prompt_version": "model-v1",
        "comparison_equity": "1000", "arm": "baseline", "trading_arm": "memo",
        "decision": target(), "trading_decision": target("ALFA"), **overrides}


def test_declaration_is_stable_but_limits_costs_and_reference_changes_start_new_profiles():
    saved = protocol()
    assert saved == protocol()
    assert saved["id"] != protocol(fee="0.002")["id"]
    changed = deepcopy(saved)
    changed["limits"]["max_position"] = "0.4"
    other = declaration(changed["limits"], {}, changed["universe"], changed["references"], fee_rate="0.001", slippage="0")
    assert other["id"] != saved["id"]
    assert protocol(fee=None)["fee_rate"] is None


def test_projection_reduces_only_and_respects_overlapping_groups_and_leveraged_caps():
    policy = protocol()
    policy["groups"] = [("one", "0.2", ["ALFA", "BETA"]), ("two", "0.15", ["BETA", "GEAR"])]
    weights = {s: number("0.4") for s in ("ALFA", "BETA", "GEAR", "BILLS", "OUTSIDE")}
    result = project(weights, policy)
    assert set(result) == {"ALFA", "BETA", "GEAR"}
    assert all(0 <= v <= weights[s] for s, v in result.items())
    assert result["ALFA"] + result["BETA"] <= number("0.2")
    assert result["BETA"] + result["GEAR"] <= number("0.15")
    assert result["GEAR"] <= number(policy["max_leveraged_position"])
    assert sum(result.values()) <= number("0.7")


@pytest.mark.parametrize("bad", [True, float("nan"), "-0.1", "1.1"])
def test_bad_weights_cannot_become_a_valid_virtual_position(bad):
    with pytest.raises(ValueError):
        project({"ALFA": bad}, protocol())


def test_books_keep_separate_shares_cash_costs_and_never_receive_actual_fund_flows():
    policy = protocol()
    events = [event(1, DAYS[0], "startup", {"comparison_protocol": policy}),
              event(2, DAYS[0], "shadow_decision", pair(policy))]
    accounts = {DAYS[0]: {"equity": "1000"}, DAYS[1]: {"equity": "20000"}}
    report = evaluate(events, {"ALFA": {DAYS[0]: 100, DAYS[1]: 110}}, accounts, DAYS[:2])
    memo, baseline = (report["shadow"][0]["arms"][a] for a in ("memo", "baseline"))
    assert memo["last"]["positions"] == {"ALFA": 4}
    assert memo["fees"] == "0.400"
    assert memo["return_after_trading_costs"] == pytest.approx(.0396)
    assert baseline["last"]["positions"] == {} and baseline["fees"] == "0"
    assert baseline["return_after_trading_costs"] == 0
    assert report["shadow"][0]["memo_minus_baseline"] == pytest.approx(.0396)


def test_fee_inclusive_caps_and_sells_before_buys_preserve_cash_without_margin():
    policy = protocol(fee="0.01", slip="0.01")
    book = Book(1000, policy)
    book.step(DAYS[0], {"ALFA": number("0.5")}, {"ALFA": 10})
    assert not book.error and book.last["positions"]["ALFA"] * 10 / number(book.last["equity"]) <= number("0.5")
    book.step(DAYS[1], {"BETA": number("0.5")}, {"ALFA": 10, "BETA": 10})
    assert not book.error and book.cash >= 0 and "ALFA" not in book.positions
    assert book.orders == 3 and book.fees > 0 and book.friction > 0


def test_supported_combined_local_slippage_and_half_spread_is_not_rejected():
    book = Book(1000, protocol(fee="0.01", slip="0.015"))
    book.step(DAYS[0], {"ALFA": number("0.5")}, {"ALFA": 10})
    assert book.error is None and book.cash >= 0 and book.positions["ALFA"] > 0


def test_minimum_buy_and_drawdown_latch_allow_sales_but_no_new_risk():
    policy = protocol(fee="0", slip="0")
    policy["min_trade_notional"] = "100"
    book = Book(1000, policy)
    book.step(DAYS[0], {"ALFA": number("0.05")}, {"ALFA": 10})
    assert not book.positions
    book.step(DAYS[1], {"ALFA": number("0.5")}, {"ALFA": 10})
    book.step(DAYS[2], {"BETA": number("0.5")}, {"ALFA": 5, "BETA": 10})
    assert book.paused and not book.positions and book.cash == 750
    book.step(DAYS[3], {"BETA": number("0.5")}, {"BETA": 10})
    assert not book.positions and book.paused


@pytest.mark.parametrize("fault", ["price", "allowance", "fractional_action", "bad_dividend", "missing_fee"])
def test_incomplete_step_rolls_back_and_withholds_returns(fault):
    policy = protocol(fee="0", slip="0")
    book = Book(1000, policy)
    book.step(DAYS[0], {"ALFA": number("0.5")}, {"ALFA": 100})
    previous = deepcopy(book.summary())
    marks, actions = {"ALFA": 100, "BETA": 100}, []
    if fault == "price":
        marks.pop("BETA")
    elif fault == "allowance":
        policy["limits"]["daily_orders"] = 1
    elif fault == "fractional_action":
        actions = [{"symbol": "ALFA", "date": DAYS[1], "split": "0.5"}]
    elif fault == "bad_dividend":
        actions = [{"symbol": "ALFA", "date": DAYS[1], "split": "2", "dividend": "-1"}]
    else:
        policy["fee_rate"] = None
    book.step(DAYS[1], {"BETA": number("0.5")}, marks, actions)
    after = book.summary()
    assert after["status"] == "incomplete" and after["return_after_trading_costs"] is None
    assert all(after[k] == previous[k] for k in ("last", "fees", "friction", "turnover", "orders", "observations"))


def test_recorded_split_dividend_and_duplicate_receipts_do_not_fabricate_a_gain():
    policy = protocol(fee="0", slip="0")
    events = [event(1, DAYS[0], "startup", {"comparison_protocol": policy}),
              event(2, DAYS[1], "benchmark_action", {"symbol": "ALFA", "date": DAYS[1], "split": 2}),
              event(3, DAYS[1], "benchmark_action", {"symbol": "ALFA", "date": DAYS[1], "split": "2"}),
              event(4, DAYS[1], "benchmark_action", {"symbol": "ALFA", "date": DAYS[1], "dividend": "1"})]
    accounts = {DAYS[0]: {"equity": 1000}}
    marks = {"ALFA": {DAYS[0]: 100, DAYS[1]: 50}}
    result = evaluate(events, marks, accounts, DAYS[:2])["references"][0]
    assert result["last"]["positions"] == {"ALFA": 10}
    assert result["return_after_trading_costs"] == .01
    events.append(event(5, DAYS[1], "benchmark_action", {"symbol": "ALFA", "date": DAYS[1], "dividend": 3}))
    result = evaluate(events, marks, accounts, DAYS[:2])["references"][0]
    assert result["status"] == "incomplete" and result["return_after_trading_costs"] is None


def test_native_and_supplemental_corporate_actions_are_combined_without_zero_field_conflicts():
    policy = protocol(fee="0", slip="0")
    events = [event(1, DAYS[0], "startup", {"comparison_protocol": policy}),
              event(2, DAYS[1], "local_corporate_action", {"symbol": "ALFA", "date": DAYS[1], "split": 2, "dividend": 0}),
              event(3, DAYS[1], "comparison_corporate_action", {"symbol": "ALFA", "date": DAYS[1], "split": "2.0", "dividend": 0})]
    report = evaluate(events, {"ALFA": {DAYS[0]: 100, DAYS[1]: 50}}, {DAYS[0]: {"equity": 1000}}, DAYS[:2])
    assert report["references"][0]["return_after_trading_costs"] == 0


def test_supplemental_close_retries_after_recovery_and_survives_restart_with_original_nav(tmp_path):
    from datetime import datetime, timezone, timedelta
    from scripts.autonomous_fund.risk import ObservedPrice, instant
    from scripts.autonomous_fund.learning import snapshot
    from .test_autonomous_fund_live_observations import configure
    from .test_autonomous_fund_local import setup_book
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker, universe=("ALFA", "BETA"))
    journal.set_meta("comparison_symbols", {"protocol_id": "test", "symbols": ["BETA"]})
    clock.value = datetime(2026, 11, 2, 21, 1, tzinfo=timezone.utc)
    calls = []
    def missing(asset, closed_at):
        calls.append(asset.symbol)
        raise ValueError("temporary quote gap")
    broker._closing_quote = missing
    broker._publish_valuation()
    initial = broker._last_valuation
    broker._publish_valuation()
    assert calls == ["BETA"]  # Shared bounded reservation prevents a same-poll retry storm.
    def valid(asset, closed_at):
        calls.append(asset.symbol)
        return ObservedPrice(number(100), instant(closed_at))
    broker._closing_quote = valid
    clock.value += timedelta(seconds=61)
    broker._publish_valuation()
    assert calls == ["BETA", "BETA"] and broker._last_valuation == initial
    receipts = [r for r in journal.all_events() if r["kind"] == "comparison_closing_quote"]
    assert len(receipts) == 1 and receipts[0]["body"]["symbol"] == "BETA"
    from scripts.autonomous_fund.evaluation import session_benchmarks
    account_row = next(r for r in journal.all_events() if r["kind"] == "account")
    bound = [(account_row["sequence"], instant(initial["valuation"]["session_close"]), initial)]
    with journal.connection() as db:
        assert "BETA" not in session_benchmarks(db, bound, until=clock.value - timedelta(seconds=61)).get("2026-11-02", {})
        assert session_benchmarks(db, bound, until=clock.value)["2026-11-02"]["BETA"] == 100
    broker._closing_searched, broker._last_valuation = None, None
    broker._publish_valuation()
    assert calls == ["BETA", "BETA"]  # Restart reuses supplemental evidence.
    assert sum(r["kind"] == "account" for r in journal.all_events()) == 1


def test_optional_close_failure_has_persisted_attempt_limit_and_no_nav_rewrites(tmp_path):
    from datetime import datetime, timezone, timedelta
    from .test_autonomous_fund_live_observations import configure
    from .test_autonomous_fund_local import setup_book
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker, universe=("ALFA", "BETA"))
    journal.set_meta("comparison_symbols", {"symbols": ["BETA"]})
    clock.value = datetime(2026, 11, 2, 21, 1, tzinfo=timezone.utc)
    calls = []
    def unavailable(asset, close):
        calls.append(asset.symbol)
        raise ValueError("no supplemental close")
    broker._closing_quote = unavailable
    for _ in range(10):
        broker._publish_valuation()
        broker._closing_searched, broker._last_valuation = None, None  # A process restart.
        clock.value += timedelta(seconds=61)
    assert calls == ["BETA"] * 5
    assert sum(r["kind"] == "account" for r in journal.all_events()) == 1


def test_missing_raw_closes_cannot_be_replaced_by_research_adjusted_prices_or_bridged():
    policy = protocol()
    events = [event(1, DAYS[0], "startup", {"comparison_protocol": policy})]
    result = evaluate(events, {"ALFA": {DAYS[0]: 100, DAYS[2]: 101}},
                      {DAYS[0]: {"equity": 1000}}, DAYS[:3])["references"][0]
    assert result["status"] == "incomplete" and result["unavailable"]["day"] == DAYS[1]


def test_receipt_at_close_starts_next_session_and_late_shadow_is_never_backdated():
    policy = protocol(fee="0")
    events = [event(1, DAYS[0], "startup", {"comparison_protocol": policy}, "16:00:00"),
              event(2, DAYS[1], "shadow_decision", pair(policy), "16:01:00")]
    accounts = {d: {"equity": 1000} for d in DAYS}
    prices = {"ALFA": dict(zip(DAYS, (100, 200, 300, 300)))}
    report = evaluate(events, prices, accounts, DAYS)
    assert report["references"][0]["start_day"] == DAYS[1]
    memo = report["shadow"][0]["arms"]["memo"]
    assert memo["observations"] == 2 and memo["last"]["positions"] == {"ALFA": 1}
    assert memo["return_after_trading_costs"] == 0  # The earlier 100→300 move was unavailable to this pair.


def test_restarts_preserve_books_but_changed_protocol_or_prompt_epochs_never_pool():
    policy, changed = protocol(fee="0"), protocol(fee="0.002")
    events = [event(1, DAYS[0], "startup", {"comparison_protocol": policy}),
              event(2, DAYS[0], "shadow_decision", pair(policy)),
              event(3, DAYS[1], "startup", {"comparison_protocol": policy}),
              event(4, DAYS[1], "shadow_decision", pair(policy, comparison_prompt_version="model-v2")),
              event(5, DAYS[2], "startup", {"comparison_protocol": changed}),
              event(6, DAYS[2], "shadow_decision", pair(changed))]
    report = evaluate(events, {"ALFA": {d: 100 for d in DAYS}}, {d: {"equity": 1000} for d in DAYS}, DAYS)
    assert len(report["references"]) == 2 and report["references"][0]["observations"] == 2
    assert len(report["shadow"]) == 3
    assert [s["arms"]["memo"]["observations"] for s in report["shadow"]] == [1, 1, 2]
    assert evaluate([], {}, {}, DAYS)["status"] == "untracked"


@pytest.mark.parametrize("metadata_failure", [False, True])
def test_shadow_evidence_metadata_uses_native_call_without_an_additional_model(tmp_path, metadata_failure):
    from scripts.autonomous_fund.validation import ValidationSettings
    from .test_autonomous_fund_quant import NOW, decision, model_harness
    strategy = model_harness(tmp_path)
    strategy.mode, strategy.comparison_protocol, strategy.prompt_version = "local", protocol(), "test-prompt"
    strategy.validation = ValidationSettings(shadow_enabled=True)
    if metadata_failure:
        original = strategy.journal.set_meta
        def write_meta(key, value):
            if key == "comparison_symbols":
                raise ValueError("synthetic optional capture failure")
            return original(key, value)
        strategy.journal.set_meta = write_meta
    seen = []
    strategy._agent_call = lambda name, **kwargs: seen.append(name) or decision([("BETA", .05)])
    context = {"account": {"equity": "1000", "as_of": NOW.isoformat()}, "strategy_memory": {"version": 4}}
    strategy._shadow(context, "memo", decision([("ALFA", .05)]), NOW, strategy.model_budget)
    assert seen == ["manager"]
    record = next(r["body"] for r in strategy.journal.all_events() if r["kind"] == "shadow_decision")
    assert record["valid"] and record["comparison_equity"] == "1000"
    assert record["comparison_protocol_id"] == strategy.comparison_protocol["id"]
    if metadata_failure:
        assert strategy.journal.get_meta("comparison_symbols") is None
        assert sum(r["kind"] == "shadow_decision" for r in strategy.journal.all_events()) == 1
    else:
        assert strategy.journal.get_meta("comparison_symbols")["symbols"] == ["ALFA", "BETA"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_only_closing_valuation_collects_shadow_quotes_and_failure_keeps_actual_nav(tmp_path, unavailable):
    from datetime import datetime, timezone
    from scripts.autonomous_fund.risk import ObservedPrice, instant
    from .test_autonomous_fund_live_observations import configure
    from .test_autonomous_fund_local import setup_book
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker, universe=("ALFA", "BETA"))
    journal.set_meta("comparison_symbols", {"protocol_id": "test", "symbols": ["BETA", "OUTSIDE"]})
    reads = []
    def closing_quote(asset, closed_at):
        reads.append(asset.symbol)
        if unavailable:
            raise ValueError("synthetic missing close")
        return ObservedPrice(number(100), instant(closed_at))
    broker._closing_quote = closing_quote
    broker._publish_valuation()
    assert reads == []  # No extra intraday quote polling/subscriptions.
    clock.value = datetime(2026, 11, 2, 21, 1, tzinfo=timezone.utc)
    broker._publish_valuation()
    assert reads == ["BETA"]  # A declared but ineligible symbol is never fetched.
    closed = [r["body"] for r in journal.all_events() if r["kind"] == "account"][-1]
    assert number(closed["equity"]) == 10000
    assert closed["valuation"]["session_close"]
    assert ("BETA" in closed.get("benchmark_observations", {})) is not unavailable


def test_corrupt_optional_watch_metadata_cannot_prevent_closing_account_receipt(tmp_path):
    from datetime import datetime, timezone
    from .test_autonomous_fund_live_observations import configure
    from .test_autonomous_fund_local import setup_book
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    journal.set_meta("comparison_symbols", ["invalid"])
    clock.value = datetime(2026, 11, 2, 21, 1, tzinfo=timezone.utc)
    broker._publish_valuation()
    assert any(r["kind"] == "account" and r["body"]["valuation"].get("session_close")
               for r in journal.all_events())


def test_economics_uses_raw_receipts_without_research_history_and_respects_cutoff(tmp_path):
    from scripts.autonomous_fund.economics import assess, lines
    from scripts.autonomous_fund.journal import Journal
    journal, policy = Journal(tmp_path, "synthetic-comparison"), protocol(fee="0")
    journal.event(f"{DAYS[0]}T10:00:00-05:00", "startup", {"mode": "local", "comparison_protocol": policy})
    for day, price in zip(DAYS[:2], (100, 110)):
        stamp = f"{day}T16:00:00-05:00"
        journal.event(stamp, "account", {"cash": 1000, "equity": 1000, "positions": {}, "as_of": stamp,
            "valuation": {"session_close": stamp, "book_existed_at_close": True},
            "benchmark_observations": {"ALFA": {"price": str(price), "as_of": stamp}}})
    report = assess(tmp_path, f"{DAYS[1]}T16:01:00-05:00")
    assert report["independent_books"]["references"][0]["return_after_trading_costs"] == .05
    earlier = assess(tmp_path, f"{DAYS[0]}T16:01:00-05:00")
    assert earlier["independent_books"]["references"][0]["return_after_trading_costs"] is None
    assert any("规则基准 fixed" in text for text in lines(report))
    assert any("成交校准" in text for text in lines(report))
