"""V2 3.5: deterministic daily attribution of a session's return, computed by code from journal facts.

Synthetic closes, fills and account marks only; no network, broker or model inference.
"""
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.attribution import attribute
from scripts.autonomous_fund.notifications import attribution_lines


def sessions(count, start=date(2026, 6, 1)):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


DAYS = sessions(40)
PREVIOUS, DAY = DAYS[-2], DAYS[-1]


def market():
    rng, spy = random.Random(1), [400.0]
    for _ in DAYS[1:]:
        spy.append(spy[-1] * (1 + rng.gauss(0.0005, 0.01)))
    alfa = [100.0]
    for i in range(1, len(DAYS)):
        move = spy[i] / spy[i - 1] - 1
        alfa.append(alfa[-1] * (1 + 1.5 * move + (0.01 if i == len(DAYS) - 1 else 0)))  # Beta 1.5; +1% stock pick today.
    closes = {"SPY": list(zip(DAYS, spy)), "ALFA": list(zip(DAYS, alfa)),
              "BETA": [(PREVIOUS, 50.0), (DAY, 51.0)], "SGOV": [(PREVIOUS, 100.0), (DAY, 100.02)]}
    return closes, spy, alfa


def test_the_parts_and_the_unexplained_rest_add_up_to_the_observed_return():
    closes, spy, alfa = market()
    start = {"equity": "10000", "positions": {"ALFA": "20"}, "cash_equivalents": {"SGOV": "30"}}
    arrival = alfa[-2] * 1.001
    fills = [{"symbol": "BETA", "side": "buy", "quantity": "10", "price": "50.2", "fee": "1", "arrival": 50.0},
             {"symbol": "ALFA", "side": "OrderSide.SELL", "quantity": "5", "price": str(arrival - 0.05), "fee": "0.5",
              "arrival": arrival}]
    weight, r_spy = 20 * alfa[-2] / 10000, spy[-1] / spy[-2] - 1
    expected = {"market": weight * 1.5 * r_spy, "selection": weight * 0.01, "cash_equivalent": 0.3 * 0.0002,
                "timing": (10 * (51 - 50) - 5 * (alfa[-1] - arrival)) / 10000,
                "slippage": (10 * (50 - 50.2) - 5 * 0.05) / 10000, "fees": -1.5 / 10000}
    explained = sum(expected.values())
    end = {"equity": str(10000 * (1 + explained + 0.0004)), "positions": {}}  # e.g. a dividend accrued.
    result = attribute(start, end, fills, closes, PREVIOUS, DAY, groups={"ALFA": "technology"})
    assert result["status"] == "complete"
    for key, value in expected.items():
        assert result["components"][key] == pytest.approx(value, abs=1e-12), key
    assert result["explained"] == pytest.approx(explained, abs=1e-12)
    assert result["unexplained"] == pytest.approx(0.0004, abs=1e-9)  # Reported, never forced to zero.
    assert result["observed_return"] == pytest.approx(result["explained"] + result["unexplained"])
    assert result["selection_by_group"] == {"technology": pytest.approx(weight * 0.01)}
    assert result["by_symbol"][0]["beta"] == pytest.approx(1.5) and result["benchmark_return"] == pytest.approx(r_spy)
    lines = attribution_lines(result)
    assert any(line.startswith("- 选股：") for line in lines) and any("未解释" in line for line in lines)


def test_missing_inputs_are_named_not_estimated():
    closes, _, _ = market()
    del closes["SGOV"]
    start = {"equity": "10000", "positions": {"ALFA": "20", "GHOST": "3"}, "cash_equivalents": {"SGOV": "30"}}
    fills = [{"symbol": "BETA", "side": "buy", "quantity": "10", "price": "50.2", "fee": None, "arrival": None}]
    result = attribute(start, {"equity": "10100"}, fills, closes, PREVIOUS, DAY)
    assert result["status"] == "partial"
    assert any(item.startswith("GHOST") for item in result["unavailable"])
    assert any(item.startswith("SGOV") for item in result["unavailable"]) and result["components"]["cash_equivalent"] == 0
    # Without an arrival price the whole difference is timing, and the fill is listed.
    assert result["components"]["slippage"] == 0 and result["components"]["timing"] == pytest.approx(10 * (51 - 50.2) / 1e4)
    assert result["arrival_unknown"] == ["BETA"] and result["fees_unknown"] == ["BETA"] and result["components"]["fees"] == 0
    short = {"SPY": closes["SPY"][-3:], "ALFA": closes["ALFA"][-3:]}
    assert attribute({"equity": "10000", "positions": {"ALFA": "20"}}, {"equity": "10000"}, [], short, PREVIOUS,
                     DAY)["beta_assumed"] == ["ALFA"]
    assert attribute(None, {"equity": "1"}, [], closes, PREVIOUS, DAY)["status"] == "unavailable"
    assert attribute({"equity": "10000"}, None, [], closes, PREVIOUS, DAY)["status"] == "unavailable"
    assert attribution_lines(None) == ["无数据（这次复盘没有归因）。"]
    assert attribution_lines({"status": "unavailable", "reason": "x"}) == ["不可用：x"]


def test_the_strategy_attributes_from_the_journal_without_double_counting_fills(tmp_path):
    from tests.test_autonomous_fund_quant import DAYS as HISTORY, NOW, default_frames, harness
    strategy = harness(tmp_path, default_frames(), mode="local")
    previous, day = HISTORY[-2], HISTORY[-1]
    at = lambda d, hour: datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)  # noqa: E731
    journal = strategy.journal
    journal.event(at(previous, 20), "account", {"as_of": "x", "cash": "8000", "equity": "10000",
                                                "positions": {"ALFA": "20"}})
    journal.event(at(day, 15), "order_submitted", {"intent_id": "intent-1", "broker_id": "broker-1", "symbol": "BETA",
                                                   "side": "buy", "quantity": 10, "limit": "120"})
    with journal.connection() as db:
        db.execute("INSERT INTO fund_orders VALUES(?,?,?,?,?,?)", ("intent-1", "decision-1", "BETA", "buy",
                                                                  at(day, 15).isoformat(), "99.5"))
    for quantity in ("4", "6"):  # Callbacks for partial fills, then the reconciled whole order.
        journal.event(at(day, 16), "fill", {"broker_id": "broker-1", "symbol": "BETA", "side": "buy",
                                            "quantity": quantity, "price": "100", "trade_cost": None})
    journal.event(at(day, 17), "fill_reconciled", {"broker_id": "broker-1", "symbol": "BETA", "side": "buy",
                                                   "quantity": "10", "price": "100", "trade_cost": "0.4"})
    journal.event(at(day, 20), "account", {"as_of": "x", "cash": "7000", "equity": "10050",
                                           "positions": {"ALFA": "20", "BETA": "10"}})
    result = strategy._attribution({"session_date": str(day.date())}, NOW)
    assert result["status"] == "complete" and result["previous_session"] == str(previous.date())
    [trade] = result["trades"]
    assert trade["quantity"] == 10 and trade["arrival"] == 99.5 and trade["fill"] == 100
    assert result["components"]["fees"] == pytest.approx(-0.4 / 10000)
    assert result["components"]["slippage"] == pytest.approx(10 * (99.5 - 100) / 10000)
    assert result["by_symbol"][0]["symbol"] == "ALFA" and result["by_symbol"][0]["beta"] is not None
    assert strategy._attribution({"session_date": "2026-01-02"}, NOW)["status"] == "unavailable"


def test_the_review_receives_the_attribution_and_keeps_it(tmp_path):
    from tests.test_autonomous_fund_review import events, review_harness
    strategy, calls = review_harness(tmp_path)
    strategy.after_market_closes()
    assert all("attribution" in call["context"] for _, call in calls)
    packet = events(strategy, "postmarket_review")[0]
    # This harness has no risk history: the part is named as unavailable, not estimated.
    assert packet["attribution"] == {"status": "unavailable", "session_date": "2026-11-02",
                                     "reason": "quantitative risk history is off"}
    from scripts.autonomous_fund.notifications import daily_document
    with strategy.journal.connection() as db:
        sequence = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review'").fetchone()[0]
    text = daily_document(tmp_path, sequence)
    text = text[1] if isinstance(text, tuple) else text
    assert "## 当日归因（代码按收盘价计算）\n\n不可用：quantitative risk history is off" in text
