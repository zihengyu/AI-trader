"""V2 4.5: a purchase whose calibrated edge does not cover its round-trip cost is declined, once ideas are calibrated.

Synthetic idea scorecards, quotes and journals only; no network, broker or model inference.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from scripts.autonomous_fund.scorecard import score_ideas
from scripts.autonomous_fund.trade_value import judge, limit_adds, model

CALIBRATED = {"verdict": "informative", "scored": 60, "hit_rate": 0.6, "mean_abs_move": 0.03,
              "by_confidence": {"0.5-0.60": {"scored": 20, "hit_rate": 0.52}, "0.8-0.95": {"scored": 12, "hit_rate": 0.75},
                                "0.7-0.80": {"scored": 4, "hit_rate": 0.9}},
              "confidence_bounds": {"0.5-0.60": [0.5, 0.6], "0.7-0.80": [0.7, 0.8], "0.8-0.95": [0.8, 0.96]}}
QUOTE = SimpleNamespace(bid=Decimal("99.95"), ask=Decimal("100.05"))  # 10 bps spread.


def idea(symbol, confidence, direction="up"):
    return {"idea_id": f"{symbol}-{confidence}", "symbol": symbol, "direction": direction, "confidence": confidence}


def test_before_the_ideas_are_calibrated_the_manager_only_sees_the_costs():
    for ideas in (None, {"verdict": "insufficient_sample", "scored": 5}, {**CALIBRATED, "verdict": "not_informative"}):
        current = model(ideas, Decimal("0.001"), Decimal("0.0005"))
        assert current["active"] is False and current["round_trip_without_spread_bps"] == 30.0
    weights = {"ALFA": Decimal("0.08")}
    assert limit_adds(weights, {}, [idea("ALFA", 0.55)], {"ALFA": QUOTE}, model(None, 0.001, 0.0005)) == (weights, {})


def test_the_edge_uses_the_buckets_own_hit_rate_only_with_enough_scored_ideas():
    current = model(CALIBRATED, Decimal("0.001"), Decimal("0.0005"))
    assert current["active"] and {b["low"] for b in current["buckets"]} == {0.5, 0.8}  # 0.7 has only 4 scored.
    weak = judge(current, idea("ALFA", 0.55), QUOTE.bid, QUOTE.ask)
    assert weak["hit_rate"] == 0.52 and abs(weak["edge"] - 0.04 * 0.03) < 1e-9
    assert abs(weak["cost"] - 2 * (0.0005 + 0.001 + 0.0005)) < 1e-6 and not weak["covered"]
    strong = judge(current, idea("ALFA", 0.85), QUOTE.bid, QUOTE.ask)
    assert strong["hit_rate"] == 0.75 and strong["covered"]
    thin = judge(current, idea("ALFA", 0.72), QUOTE.bid, QUOTE.ask)
    assert thin["hit_rate"] == 0.6  # Too few in its own bucket: the overall hit rate.
    against = judge(current, idea("ALFA", 0.85, "down"), QUOTE.bid, QUOTE.ask)
    assert against["edge"] < 0 and not against["covered"]  # Buying what the research expects to fall.


def test_only_uncovered_purchases_with_an_idea_are_held_and_never_below_the_holding():
    current = model(CALIBRATED, Decimal("0.001"), Decimal("0.0005"))
    weights = {"ALFA": Decimal("0.08"), "BETA": Decimal("0.06"), "GAMMA": Decimal("0.02"), "DELTA": Decimal("0.05")}
    holdings = {"ALFA": Decimal("0.03"), "GAMMA": Decimal("0.05")}
    ideas = [idea("ALFA", 0.85), idea("ALFA", 0.55), idea("BETA", 0.85), idea("GAMMA", 0.55)]
    quotes = {s: QUOTE for s in weights}
    limited, held = limit_adds(weights, holdings, ideas, quotes, current)
    assert limited["ALFA"] == Decimal("0.03") and set(held) == {"ALFA"}  # The latest ALFA idea is the weak one.
    assert limited["BETA"] == Decimal("0.06")  # A covered purchase.
    assert limited["GAMMA"] == Decimal("0.02")  # A reduction is never limited.
    assert limited["DELTA"] == Decimal("0.05")  # No idea today: nothing to judge it by.


def test_scored_ideas_report_their_mean_move_and_bucket_bounds():
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
    closes = {"ALFA": list(zip(days, [100, 100, 105, 110, 110])), "BETA": list(zip(days, [100, 100, 102, 105, 105])),
              "SPY": list(zip(days, [100, 100, 101, 102, 102]))}
    formed = "2026-06-02T13:00:00+00:00"  # Before the open: measured from that day's close.
    ideas = [{"idea_id": "a", "symbol": "ALFA", "direction": "up", "horizon_sessions": 2, "confidence": 0.6,
              "formed_at": formed},
             {"idea_id": "b", "symbol": "BETA", "direction": "outperform", "horizon_sessions": 2, "confidence": 0.7,
              "formed_at": formed, "benchmark": "SPY"}]
    result = score_ideas(ideas, closes)
    assert result["scored"] == 2 and abs(result["mean_abs_move"] - (0.10 + 0.03) / 2) < 1e-9  # Relative beyond SPY.
    assert result["confidence_bounds"]["0.6-0.70"] == [0.6, 0.7] and result["confidence_bounds"]["0.8-0.95"][1] == 0.96
    assert score_ideas([], {})["mean_abs_move"] is None


def strategy_with_ideas(tmp_path, active):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    opens = (NOW - timedelta(hours=1)).isoformat()
    strategy.premarket_research = {"session_open": opens}
    from scripts.autonomous_fund.strategy import trading_day
    from scripts.autonomous_fund.risk import instant
    strategy.journal.event(NOW, "research_idea", {**idea("ALFA", 0.55), "session_date": trading_day(instant(opens)),
                                                  "horizon_sessions": 5})
    strategy.journal.set_meta("trade_value_model", model(CALIBRATED if active else None, Decimal("0.001"),
                                                         Decimal("0.0005")))
    return strategy, NOW


def test_the_allocation_holds_an_uncovered_add_and_records_it_once(tmp_path):
    strategy, now = strategy_with_ideas(tmp_path, active=True)
    overlay = {"binding": []}
    active = {"id": "decision-1"}
    weights = {"ALFA": Decimal("0.08")}
    for _ in range(2):
        limited = strategy._limit_uncovered_adds(active, weights, {"ALFA": Decimal("0.02")}, {"ALFA": QUOTE}, now, overlay)
        assert limited == {"ALFA": Decimal("0.02")}
    held = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "trade_value_held"]
    assert len(held) == 1 and held[0]["symbol"] == "ALFA" and held[0]["held_at"] == "0.02"
    assert overlay["binding"][0] == "trade_value:ALFA"


def test_without_calibrated_ideas_the_allocation_is_unchanged(tmp_path):
    strategy, now = strategy_with_ideas(tmp_path, active=False)
    weights = {"ALFA": Decimal("0.08")}
    assert strategy._limit_uncovered_adds({"id": "d"}, weights, {}, {"ALFA": QUOTE}, now, {"binding": []}) == weights


def test_the_decision_context_carries_the_costs_and_the_local_books_own_fees(tmp_path):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    strategy.broker = object.__new__(LocalPaperBroker)
    strategy.broker.fee_rate, strategy.broker.slippage = Decimal("0.001"), Decimal("0.0005")
    current = strategy._trade_value_model(NOW)
    assert current["active"] is False and current["fee_bps"] == 10.0 and current["slippage_bps"] == 5.0
    assert strategy.journal.get_meta("trade_value_model") == current
    strategy.broker = SimpleNamespace(slippage="not a number")  # Another broker: no modeled fee.
    assert strategy._trade_value_model(NOW)["fee_bps"] == 0.0
    strategy._scorecards = lambda now: (_ for _ in ()).throw(RuntimeError("synthetic"))
    assert strategy._trade_value_model(NOW) == {"active": False, "reason": "unavailable: RuntimeError"}
