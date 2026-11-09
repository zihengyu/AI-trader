"""Synthetic regressions for comparable scorecard dates and executable share counts."""
from decimal import Decimal

import pytest

from scripts.autonomous_fund.risk import Limits, desired_shares
from scripts.autonomous_fund.scorecard import (beta, completed_sessions, score_decisions, score_ideas,
                                               score_predictions, score_timing)
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


def test_fixed_exchange_calendar_does_not_skip_a_missing_benchmark_close():
    closes = {"ALFA": [("2026-11-24", 100), ("2026-11-27", 120)],
              "SPY": [("2026-11-24", 100), ("2026-11-27", 110)]}
    sessions = completed_sessions(closes, "2026-11-28")
    assert sessions == ("2026-11-24", "2026-11-25", "2026-11-27")  # Thanksgiving is not a session.
    at = "2026-11-24T15:00:00+00:00"
    prediction = {"session_date": "2026-11-24", "review": {"predictions": [{
        "symbol": "ALFA", "direction": "outperform", "horizon_sessions": 1}]}}
    idea = {**prediction["review"]["predictions"][0], "formed_at": at, "confidence": .7}
    portfolio = [(at, {"targets": [{"symbol": "ALFA", "weight": .1}]})]
    trigger = [(at, "protective_trigger", {"symbol": "ALFA", "trigger": "stop_loss", "bid": 100})]
    assert score_predictions([prediction], closes, sessions=sessions)["unavailable"] == 1
    assert score_ideas([idea], closes, sessions=sessions)["unavailable"] == 1
    assert score_decisions(portfolio, closes, sessions=sessions)["recent"][0]["horizons"]["1"]["status"] == "unavailable"
    assert score_timing(trigger, closes, sessions=sessions)["recent"][0]["horizons"]["1"]["status"] == "unavailable"
    assert score_predictions([prediction], closes, sessions=sessions[:1])["pending"] == 1
    # An intermediate hole does not block a return when both scheduled endpoints exist.
    prediction["review"]["predictions"][0]["horizon_sessions"] = 2
    assert score_predictions([prediction], closes, sessions=sessions)["correct"] == 1


def test_late_decision_starts_at_the_next_close_after_an_early_close():
    sessions = ("2026-11-27", "2026-11-30", "2026-12-01")
    closes = {"SPY": list(zip(sessions, [100, 110, 110])), "ALFA": list(zip(sessions, [100, 160, 176]))}
    late = [("2026-11-27T19:00:00+00:00", {"targets": [{"symbol": "ALFA", "weight": 1}]})]
    row = score_decisions(late, closes, sessions=sessions)["recent"][0]
    assert row["start_day"] == "2026-11-30"  # The Friday NYSE session closed at 18:00 UTC.
    assert row["horizons"]["1"]["end_day"] == "2026-12-01"
    assert row["horizons"]["1"]["portfolio_return"] == pytest.approx(.1)


def test_ab_comparisons_count_each_return_window_once():
    from scripts.autonomous_fund.construction import pairs
    from scripts.autonomous_fund.validation import shadow_pairs

    sessions = ("2026-11-27", "2026-11-30", "2026-12-01")
    closes = {"SPY": list(zip(sessions, [100, 100, 100])), "ALFA": list(zip(sessions, [100, 110, 132]))}
    early = "2026-11-27T17:00:00+00:00"
    late = "2026-11-27T19:00:00+00:00"
    next_session = "2026-11-30T15:00:00+00:00"
    records = [(early, .1), (late, .2), (next_session, .4)]
    variants = [(at, {"ai": {"SPY": weight}, "risk_budget": {"ALFA": weight}}) for at, weight in records]
    shadows = [{"decided_at": at, "valid": True, "arm": "baseline", "trading_arm": "memo",
                "decision": {"targets": [{"symbol": "SPY", "weight": weight}]},
                "trading_decision": {"targets": [{"symbol": "ALFA", "weight": weight}]}}
               for at, weight in records]
    construction = pairs(reversed(variants), closes, sessions=sessions)
    assert [day for day, _ in construction] == list(sessions[:2])
    assert [value for _, value in construction] == pytest.approx([.01, .08])
    assert shadow_pairs(reversed(shadows), closes, sessions=sessions) == pytest.approx([.01, .08])


def test_beta_does_not_turn_a_missing_session_into_a_daily_return():
    import pandas as pd
    days = [d.date().isoformat() for d in pd.bdate_range("2026-11-02", periods=22)]
    rows = [(day, 100 + i) for i, day in enumerate(days) if i != 10]
    assert beta(rows, rows, days[-1]) == pytest.approx(1)
    assert beta(rows, rows, days[-1], sessions=days) is None  # Only 19 genuinely consecutive pairs.


def test_runner_scorecard_uses_the_calendar_even_when_the_benchmark_endpoint_is_missing(tmp_path):
    from datetime import datetime, timezone
    from tests.test_autonomous_fund_quant import harness

    now = datetime(2026, 11, 27, 19, tzinfo=timezone.utc)
    strategy = harness(tmp_path, mode="local")
    rows = {"ALFA": [("2026-11-24", 100), ("2026-11-25", 115), ("2026-11-27", 120)],
            "SPY": [("2026-11-24", 100), ("2026-11-27", 110)]}
    strategy._daily_closes = lambda symbol, now: rows.get(symbol, [])
    strategy.journal.event("2026-11-24T15:00:00+00:00", "decision_validated",
                           {"targets": [{"symbol": "ALFA", "weight": .1}]})
    horizons = strategy._scorecards(now)["decisions"]["recent"][0]["horizons"]
    assert horizons["1"]["status"] == "unavailable" and horizons["5"]["status"] == "pending"


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
