"""Synthetic regressions for comparable scorecard dates and executable share counts."""
from decimal import Decimal

import pytest

from scripts.autonomous_fund.risk import Limits, desired_shares
from scripts.autonomous_fund.scorecard import score_decisions, score_predictions
from scripts.autonomous_fund.trade_value import affordability


def cards(closes, horizon=1):
    review = {"session_date": "2026-11-02", "review": {"predictions": [{
        "symbol": "ALFA", "benchmark": "BENCH", "direction": "outperform", "horizon_sessions": horizon}]}}
    prediction = score_predictions([review], closes)["recent"][0]
    decision = score_decisions([("2026-11-02T15:00:00+00:00", {
        "targets": [{"symbol": "ALFA", "weight": .1}]})], closes, benchmark="BENCH")
    return prediction, decision["recent"][0]["horizons"][str(horizon)]


def test_a_missing_endpoint_is_not_replaced_with_a_later_symbols_close():
    closes = {"ALFA": [("2026-11-02", 100), ("2026-11-04", 120)],
              "BENCH": [("2026-11-02", 100), ("2026-11-03", 110), ("2026-11-04", 130)]}
    prediction, decision = cards(closes)
    assert prediction["status"] == decision["status"] == "unavailable"


def test_an_extra_symbol_observation_does_not_change_the_benchmark_horizon():
    closes = {"ALFA": [("2026-11-02", 100), ("2026-11-03", 150), ("2026-11-04", 90)],
              "BENCH": [("2026-11-02", 100), ("2026-11-04", 110)]}
    prediction, decision = cards(closes)
    assert prediction["status"] == "incorrect"
    assert prediction["symbol_return"] == pytest.approx(-.1)
    assert decision["portfolio_return"] == pytest.approx(-.01)


def test_missing_intermediate_closes_do_not_block_matching_endpoints():
    days = [f"2026-11-{day:02}" for day in (2, 3, 4, 5, 6, 9)]
    closes = {"ALFA": [(days[0], 100), (days[-1], 120)],
              "BENCH": [(day, 100 + i) for i, day in enumerate(days)]}
    prediction, decision = cards(closes, horizon=5)
    assert prediction["status"] == "correct"
    assert decision["status"] == "observed"
    assert decision["portfolio_return"] == pytest.approx(.02)


def test_a_benchmark_horizon_that_has_not_matured_stays_pending():
    closes = {"ALFA": [("2026-11-02", 100), ("2026-11-03", 120)],
              "BENCH": [("2026-11-02", 100)]}
    prediction, decision = cards(closes)
    assert prediction["status"] == decision["status"] == "pending"


@pytest.mark.parametrize("price, buffer, cap", [
    ("999.5", "0.001", "0.10"), ("990.10", "0.01", "0.10"),
    ("1000", "0", "0.10"), ("249.95", "0.001", "0.10"),
    ("499.9", "0.001", "0.05"), ("499.99", "0", "0.05"),
])
def test_affordability_uses_the_executors_buffer_and_cent_rounding(price, buffer, cap):
    limits = Limits(limit_buffer=buffer)
    groups = (("synthetic_group", cap, ("ALFA",)),)
    result = affordability({"ALFA": price}, 10000, limits.max_position, groups, limits=limits)
    expected = desired_shares(10000, cap, price, limits)
    if expected == 0:
        assert [row["symbol"] for row in result["unaffordable"]] == ["ALFA"]
    else:
        assert result["unaffordable"] == []
        assert result["coarse"][0]["max_shares"] == expected


def test_strategy_passes_its_configured_limit_buffer_to_affordability(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    strategy.limits = Limits(limit_buffer="0.01")
    strategy._market_snapshot = lambda now: {"symbols": {"ALFA": {"last_close": 995}}}
    result = strategy._affordability(Decimal("10000"), {}, NOW)
    assert [row["symbol"] for row in result["unaffordable"]] == ["ALFA"]


def test_buffered_affordability_retains_held_symbols_and_live_quote_fallback():
    def unavailable(symbol):
        raise RuntimeError("synthetic outage")
    result = affordability({"ALFA": 999.5, "BETA": 50}, 10000, Decimal(".1"), (),
                           held={"ALFA"}, live=unavailable)
    assert result["coarse"] == []  # BETA is affordable and fits more than four shares.
    assert result["unaffordable"][0]["held"] is True
    assert result["unaffordable"][0]["basis"] == "last_close (live quote unavailable)"


def test_live_affordability_uses_the_executors_ask_without_recording_an_order_quote(tmp_path):
    from types import SimpleNamespace
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    strategy.mode = "local"
    strategy._market_snapshot = lambda now: {"symbols": {"ALFA": {"last_close": 998}}}
    strategy._alert_price = lambda symbol: SimpleNamespace(price=Decimal("998"))
    calls = []

    def price(symbol, record=True):
        calls.append((symbol, record))
        return SimpleNamespace(ask=Decimal("999.5"))
    strategy._price = price
    result = strategy._affordability(10000, {}, NOW)
    assert calls == [("ALFA", False)]
    assert result["unaffordable"][0]["symbol"] == "ALFA"
    assert result["unaffordable"][0]["basis"] == "live_quote"


def test_shadow_selection_drops_a_pair_with_a_missing_common_endpoint():
    from scripts.autonomous_fund.validation import shadow_pairs
    closes = {"ALFA": [("2026-11-02", 100), ("2026-11-04", 120)],
              "SPY": [("2026-11-02", 100), ("2026-11-03", 110), ("2026-11-04", 130)]}
    row = {"arm": "baseline", "trading_arm": "memo", "valid": True,
           "decided_at": "2026-11-02T15:00:00+00:00",
           "decision": {"targets": [{"symbol": "SPY", "weight": .1}]},
           "trading_decision": {"targets": [{"symbol": "ALFA", "weight": .1}]}}
    assert shadow_pairs([row], closes) == []
    closes["ALFA"].insert(1, ("2026-11-03", 110))
    assert shadow_pairs([row], closes) == pytest.approx([0])
