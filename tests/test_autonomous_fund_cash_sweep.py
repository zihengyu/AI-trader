"""Idle cash is parked in one cash-equivalent ETF without touching the AI's risk budget.

Synthetic account identities, neutral symbols and synthetic quotes only.
"""
from dataclasses import replace
from decimal import Decimal

import pytest

from scripts.autonomous_fund.cash import CashSweep, funding_sell, merge, park_buy
from scripts.autonomous_fund.risk import Account, Intent, Limits, RiskError, number, validate_decision
from tests.test_autonomous_fund_quant import NOW, Price, accept, default_frames, harness, quote

LIMITS = Limits()
UNIVERSE = {"ALFA": "stock", "SPY": "unlevered_etf", "BILLS": "cash_equivalent"}


def sweep(**settings):
    return CashSweep.from_config({"symbol": "BILLS", **settings}, UNIVERSE)


def bills(price=100):
    return Price(number(price) - number("0.01"), number(price), NOW, number(500_000_000))


def view(cash, bills_held=0, positions=None, equity=None, price=100):
    positions = dict(positions or {})
    real = Account(number(cash), number(equity if equity is not None else 10000), {**positions,
                   **({"BILLS": bills_held} if bills_held else {})}, NOW)
    return merge(real, sweep(), number(price))


def test_the_sweep_is_cash_for_every_risk_rule():
    merged = view(2000, bills_held=70, positions={"ALFA": 10})
    assert merged.positions == {"ALFA": 10} and merged.cash == number(9000)
    assert merged.cash_equivalents == {"BILLS": 70} and merged.cash_equivalent_value == number(7000)
    exposure = (merged.equity - merged.cash) / merged.equity
    assert exposure == number("0.1")  # Only ALFA counts; the T-bill ETF is cash.


def test_idle_cash_above_the_band_is_parked_keeping_a_cash_buffer():
    order = park_buy(view(9000), bills(), LIMITS, sweep(), min_trade=number(100))
    # Keep 1% ($100) in cash; buy at ask + limit buffer with a fee buffer.
    assert order.side == "buy" and order.symbol == "BILLS"
    spend = order.quantity * order.limit * (1 + LIMITS.fee_buffer)
    assert spend <= number(8900) and spend > number(8700)
    assert park_buy(view(250), bills(), LIMITS, sweep(), min_trade=number(100)) is None  # Below the 2% band.


def test_a_purchase_first_sells_enough_of_the_sweep_to_fund_it():
    merged = view(100, bills_held=99)  # $100 cash, $9,900 in the sweep.
    order = Intent("ALFA", "buy", 9, number("100.10"))
    sell = funding_sell(merged, order, bills(), LIMITS, sweep())
    needed = order.quantity * order.limit * (1 + LIMITS.fee_buffer) + number(100) - number(100)
    assert sell.side == "sell" and sell.symbol == "BILLS"
    assert sell.quantity * sell.limit >= needed and sell.quantity <= 99
    assert funding_sell(view(5000, bills_held=40), order, bills(), LIMITS, sweep()) is None  # Cash suffices.


def test_configuration_rejects_ambiguous_or_misclassified_sweeps():
    assert CashSweep.from_config({"enabled": False}, UNIVERSE) is None
    assert CashSweep.from_config({}, UNIVERSE).symbol == "BILLS"
    assert CashSweep.from_config({}, {"ALFA": "stock"}) is None  # No cash equivalent admitted.
    with pytest.raises(RiskError):
        CashSweep.from_config({"symbol": "ALFA"}, UNIVERSE)
    with pytest.raises(RiskError):
        CashSweep.from_config({}, {**UNIVERSE, "NOTES": "cash_equivalent"})
    with pytest.raises(RiskError):
        CashSweep.from_config({"keep_cash": "0.5"}, UNIVERSE)


def test_the_ai_cannot_target_the_sweep_symbol():
    from scripts.autonomous_fund.strategy import ai_universe
    eligible = ai_universe(UNIVERSE)
    assert eligible == frozenset({"ALFA", "SPY"})
    decision = {"as_of": NOW.isoformat(), "expires_at": NOW.replace(hour=20).isoformat(),
                "next_review_at": NOW.replace(hour=17).isoformat(), "rationale": "synthetic",
                "targets": [{"symbol": "BILLS", "weight": 0.1}]}
    with pytest.raises(RiskError, match="ineligible"):
        validate_decision(decision, NOW, eligible, LIMITS)


def test_shipped_configuration_admits_the_sweep_and_diversifiers():
    from pathlib import Path
    from scripts.autonomous_fund.strategy import configuration, ai_universe
    config = configuration(Path(__file__).resolve().parents[1] / "scripts/autonomous_fund/config.json")
    assert config["universe"]["SGOV"] == "cash_equivalent"
    assert {"IEF", "TLT", "GLD"} <= ai_universe(config["universe"]) and "SGOV" not in ai_universe(config["universe"])
    assert CashSweep.from_config(config.get("cash_sweep"), config["universe"]).symbol == "SGOV"


def strategy_with_sweep(tmp_path):
    strategy = harness(tmp_path, default_frames())
    strategy.cash_sweep = sweep()
    strategy.submitted = []
    strategy._submit = lambda intent, now, decision_id, **kw: strategy.submitted.append((intent, kw.get("kind")))
    strategy._execute = lambda intent, now, account=None, prices=None: strategy.submitted.append((intent, "allocation"))
    prices = {"ALFA": quote(), "BILLS": bills()}
    strategy._price = lambda symbol, record=True: prices[symbol]
    return strategy


def test_strategy_funds_a_buy_from_the_sweep_before_buying(tmp_path):
    strategy = strategy_with_sweep(tmp_path)
    merged = view(50, bills_held=99, equity=9950)
    strategy.journal.observe_session(NOW, merged)
    active = accept(strategy, [("ALFA", .09)])
    strategy._snapshot = lambda: merged
    strategy._continue_allocation(active, merged)
    (intent, kind), = strategy.submitted
    assert kind == "cash_sweep" and intent.symbol == "BILLS" and intent.side == "sell"


def test_strategy_parks_cash_only_once_the_allocation_is_complete(tmp_path):
    strategy = strategy_with_sweep(tmp_path)
    done = view(9100, positions={"ALFA": 9}, equity=10000)
    strategy.journal.observe_session(NOW, done)
    active = accept(strategy, [("ALFA", .09)])
    strategy._continue_allocation(active, done)
    (intent, kind), = strategy.submitted
    assert kind == "cash_sweep" and intent.side == "buy" and intent.symbol == "BILLS"
    strategy.submitted.clear()
    building = view(10000)
    strategy._continue_allocation(accept(strategy, [("ALFA", .09)]), building)
    (intent, kind), = strategy.submitted
    assert kind == "allocation" and intent.symbol == "ALFA"  # The AI's purchase comes first.


def test_snapshot_reports_the_sweep_as_cash_and_keeps_it_out_of_the_ai_universe(tmp_path):
    from types import SimpleNamespace
    strategy = harness(tmp_path, default_frames())
    strategy.cash_sweep = sweep()
    strategy._configured_universe = frozenset({"ALFA", "SPY", "BILLS"})
    strategy.universe = frozenset({"ALFA", "SPY", "BILLS"})  # Corrected by the snapshot.
    strategy.broker = SimpleNamespace(receivables=lambda: number(0), quote_assets=set())
    strategy.quote_asset = None
    holding = lambda symbol, quantity: SimpleNamespace(asset=SimpleNamespace(symbol=symbol, asset_type="stock"),
                                                       quantity=quantity)
    strategy.get_positions = lambda: [holding("ALFA", 10), holding("BILLS", 80)]
    strategy.get_cash, strategy.get_portfolio_value = lambda: 1000.0, lambda: 10000.0
    strategy.get_orders = lambda: []
    prices = {"ALFA": 100.0, "BILLS": 100.0, "SPY": 100.0, "QQQ": 100.0}
    strategy.get_last_price = lambda asset: prices.get(asset.symbol)
    account = strategy._snapshot()
    assert account.positions == {"ALFA": 10} and account.cash == number(9000)
    assert account.cash_equivalents == {"BILLS": 80} and "BILLS" not in strategy.universe
    session = strategy.journal.session(NOW)
    assert session["start_exposure"] == number("0.1")  # The sweep never counts as risk exposure.


def test_a_stale_sweep_quote_never_blocks_a_purchase_cash_already_funds(tmp_path):
    strategy = strategy_with_sweep(tmp_path)

    def stale(symbol, record=True):
        if symbol == "BILLS":
            raise RiskError("Stale, future, crossed or excessively wide quote")
        return quote()
    strategy._price = stale
    funded = view(10000)
    strategy.journal.observe_session(NOW, funded)
    strategy._continue_allocation(accept(strategy, [("ALFA", .09)]), funded)
    (intent, kind), = strategy.submitted
    assert kind == "allocation" and intent.symbol == "ALFA"  # No sweep quote was needed.
    strategy.submitted.clear()
    done = view(9100, positions={"ALFA": 9}, equity=10000)
    strategy._continue_allocation(accept(strategy, [("ALFA", .09)]), done)  # Parking fails quietly.
    assert strategy.submitted == []
    assert any(r["kind"] == "cash_sweep_unavailable" for r in strategy.journal.all_events())
