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


# ----- Whole shares only: symbols one share of which is above their ceiling ------------------------------

def test_a_share_above_its_ceiling_is_unaffordable_and_group_caps_count():
    from scripts.autonomous_fund.trade_value import affordability
    groups = (("leveraged:SQQQ", "0.05", ("SQQQ",)), ("technology", "0.30", ("NVDA", "SNDK")))
    result = affordability({"SNDK": 1500.0, "NVDA": 900.0, "SQQQ": 600.0, "SPY": 600.0, "BAD": None}, Decimal("10000"),
                           Decimal("0.10"), groups, held={"SNDK"})
    rows = {row["symbol"]: row for row in result["unaffordable"]}
    assert set(rows) == {"SNDK", "SQQQ"}  # NVDA (9%) and SPY (6%) fit under 10%; SQQQ's own cap is 5%.
    assert rows["SNDK"] == {"symbol": "SNDK", "share_price": 1500.0, "one_share_weight": 0.15, "max_weight": 0.1,
                            "basis": "last_close", "held": True}
    assert rows["SQQQ"]["max_weight"] == 0.05 and "Whole shares only" in result["meaning"]
    assert affordability({"SNDK": 1500.0}, 0, Decimal("0.10"), ())["unaffordable"] == []


def test_the_operator_hears_once_a_day_when_the_unaffordable_list_changes(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    prices = {"ALFA": 1500.0, "BETA": 50.0}
    strategy._market_snapshot = lambda now: {"symbols": {s: {"last_close": p} for s, p in prices.items()}}
    first = strategy._affordability(Decimal("10000"), {}, NOW)
    assert [row["symbol"] for row in first["unaffordable"]] == ["ALFA"]
    strategy._affordability(Decimal("10000"), {}, NOW + timedelta(hours=1))
    assert [kind for kind, _ in strategy.notices].count("unaffordable_symbols") == 1  # Same day, same list.
    strategy._affordability(Decimal("10000"), {}, NOW + timedelta(days=1))
    assert [kind for kind, _ in strategy.notices].count("unaffordable_symbols") == 1  # Unchanged list.
    prices["ALFA"] = 900.0
    strategy._affordability(Decimal("10000"), {}, NOW + timedelta(days=2))
    kinds = [kind for kind, _ in strategy.notices]
    assert kinds.count("unaffordable_symbols") == 2 and readable(*strategy.notices[-1]) == "ℹ️ 投资范围里的证券现在都至少买得起一股了。"
    text = readable("unaffordable_symbols", first)
    assert "ALFA：一股约 1,500.00 美元，占净值 15.0%，上限 10.0%" in text and "系统只买整股" in text
    strategy._market_snapshot = lambda now: (_ for _ in ()).throw(RuntimeError("synthetic"))
    assert strategy._affordability(Decimal("10000"), {}, NOW) == {"unavailable": "RuntimeError"}


def test_the_manager_sees_which_symbols_it_cannot_buy(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness
    strategy = model_harness(tmp_path)
    seen = {}
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]

    def call(name, **kwargs):
        seen[name] = kwargs.get("context", {})
        if name == "researcher":
            return {"summary": "s", "evidence": evidence}
        return decision([("ALFA", .05)])
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert "affordability" in seen["manager"] and "unaffordable" in seen["manager"]["affordability"]


def test_a_symbol_near_its_ceiling_is_rechecked_with_a_live_quote_and_can_become_affordable():
    from scripts.autonomous_fund.trade_value import affordability
    asked = []

    def live(symbol):
        asked.append(symbol)
        if symbol == "BROKEN":
            raise RuntimeError("synthetic quote outage")
        return {"SNDK": 980.0, "RISEN": 1060.0}[symbol]
    closes = {"SNDK": 1050.0, "RISEN": 990.0, "BROKEN": 1020.0, "BKNG": 5000.0, "SPY": 600.0}
    result = affordability(closes, 10000, Decimal("0.10"), (), live=live)
    assert sorted(asked) == ["BROKEN", "RISEN", "SNDK"]  # Far from the ceiling (BKNG 50%, SPY 6%): no quote needed.
    unaffordable = {row["symbol"]: row for row in result["unaffordable"]}
    coarse = {row["symbol"]: row for row in result["coarse"]}
    assert "SNDK" not in unaffordable and coarse["SNDK"]["basis"] == "live_quote" and coarse["SNDK"]["max_shares"] == 1
    assert unaffordable["RISEN"]["basis"] == "live_quote"  # Rose above its ceiling during the session.
    assert unaffordable["BROKEN"]["basis"] == "last_close (live quote unavailable)"
    assert unaffordable["BKNG"]["basis"] == "last_close" and coarse["SPY"]["max_shares"] == 1


def test_affordable_symbols_that_fit_only_a_few_shares_are_listed_as_coarse():
    from scripts.autonomous_fund.trade_value import affordability
    result = affordability({"NVDA": 300.0, "SPY": 60.0, "AVGO": 450.0}, 10000, Decimal("0.10"), ())
    coarse = {row["symbol"]: row["max_shares"] for row in result["coarse"]}
    assert coarse == {"NVDA": 3, "AVGO": 2} and result["unaffordable"] == []  # SPY fits 16 shares: not coarse.
    assert "rounded down to whole shares" in result["meaning"]


def test_live_rechecks_run_in_local_and_paper_mode_only(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    strategy._market_snapshot = lambda now: {"symbols": {"ALFA": {"last_close": 1020.0}}}
    quoted = []
    strategy._alert_price = lambda symbol: quoted.append(symbol) or SimpleNamespace(price=Decimal("990"))
    replay = strategy._affordability(Decimal("10000"), {}, NOW)
    assert quoted == [] and [r["symbol"] for r in replay["unaffordable"]] == ["ALFA"]  # Replays keep the close.
    strategy.mode = "local"
    live = strategy._affordability(Decimal("10000"), {}, NOW)
    assert quoted == ["ALFA"] and live["unaffordable"] == [] and live["coarse"][0]["basis"] == "live_quote"
