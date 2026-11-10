"""Large market down days scored separately from the average, from each review's attribution.

Synthetic reviews and closes only; no network, broker or model inference.
"""
import pytest

from scripts.autonomous_fund.downdays import score


def review(day, benchmark, fund, *, beta=1.0, selection=0.0, timing=0.0, status="complete"):
    return {"session_date": day, "attribution": {
        "status": status, "session_date": day, "benchmark_return": benchmark, "observed_return": fund,
        "components": {"market": beta * benchmark, "selection": selection, "cash_equivalent": 0.0, "timing": timing,
                       "slippage": 0.0, "fees": 0.0}}}


def test_down_days_report_capture_beta_and_contributions_against_the_other_days():
    reviews = [review("2026-10-01", -0.03, -0.015, beta=.5, selection=.001),
               review("2026-10-02", -0.025, -0.01, beta=.4, timing=None),
               review("2026-10-05", 0.004, 0.003, beta=.9),
               review("2026-10-06", -0.02, -0.012, beta=.6, selection=-.002),
               review("2026-10-07", 0.025, 0.02, beta=.8),
               review("2026-10-08", -0.001, 0.0, beta=1.1),
               review("2026-10-09", -0.05, None),                       # No fund return: skipped.
               review("2026-10-12", -0.04, -0.02, status="unavailable")]  # Not usable.
    card = score(reviews)
    down = card["down_days"]
    assert card["status"] == "observed" and card["sessions"] == 6
    assert down["days"] == 3 and down["dates"] == ["2026-10-01", "2026-10-02", "2026-10-06"]
    assert down["capture"] == pytest.approx(.037 / .075)  # Lost about half of the market's fall.
    assert down["mean_beta"] == pytest.approx(.5) and card["other_days"]["mean_beta"] == pytest.approx(1.0)
    assert card["beta_lower_on_down_days_by"] == pytest.approx(.5)
    assert down["selection"] == pytest.approx(-.001) and down["timing"] == 0.0 and down["timing_unknown_days"] == 1
    assert card["up_days"]["days"] == 1 and card["up_days"]["capture"] == pytest.approx(.8)


def test_few_down_days_are_marked_insufficient_and_unknown_parts_are_not_zero():
    card = score([review("2026-10-01", -0.03, -0.02, timing=None), review("2026-10-02", 0.001, 0.0)])
    assert card["status"] == "insufficient_sample" and card["down_days"]["days"] == 1
    assert card["down_days"]["timing"] is None and card["down_days"]["timing_unknown_days"] == 1
    assert score([])["down_days"] == {"days": 0, "dates": []} and score([])["beta_lower_on_down_days_by"] is None


def test_protective_exits_on_down_days_are_valued_on_the_next_session():
    reviews = [review("2026-10-01", -0.03, -0.02), review("2026-10-02", 0.001, 0.0)]
    closes = {"ALFA": [("2026-10-01", 95.0), ("2026-10-02", 90.0)]}
    triggers = [("2026-10-01T15:00:00+00:00", {"symbol": "ALFA", "trigger": "stop_loss", "bid": "96"}),
                ("2026-10-02T15:00:00+00:00", {"symbol": "ALFA", "trigger": "stop_loss", "bid": "91"})]  # Not a down day.
    exits = score(reviews, triggers, closes, sessions=["2026-10-01", "2026-10-02"])["protective_exits_on_down_days"]
    assert exits["count"] == 1 and exits["valued"] == 1
    assert exits["mean_value_added_1d"] == pytest.approx(-(90 / 96 - 1))  # Sold before a further fall: positive.


def test_down_day_scorecards_count_old_triggers_outside_the_timing_window(tmp_path):
    from scripts.autonomous_fund.journal import encode
    from scripts.autonomous_fund.notifications import evidence_lines
    from tests.test_autonomous_fund_quant import NOW, harness

    strategy = harness(tmp_path)
    day, following = "2026-10-01", "2026-10-02"
    strategy._daily_closes = lambda symbol, now: [(day, 95.0), (following, 90.0)]
    strategy.journal.event(f"{day}T21:00:00+00:00", "postmarket_review",
                           {**review(day, -0.03, -0.02), "review": {}})
    for bid in ("96", None):
        strategy.journal.event(f"{day}T15:00:00+00:00", "protective_trigger",
                               {"symbol": "ALFA", "trigger": "stop_loss", "bid": bid})
    with strategy.journal.connection() as db:
        db.executemany("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                       [(f"{following}T15:00:00+00:00", "order_submitted", encode({"side": "sell"}))] * 400)

    card = strategy._scorecards(NOW)

    exits = card["down_days"]["protective_exits_on_down_days"]
    assert exits["count"] == 2 and exits["valued"] == 1
    assert exits["mean_value_added_1d"] == pytest.approx(1 - 90 / 96)
    assert "止损/止盈触发 2 次，1 次可估值" in evidence_lines(card)[0]


def test_report_line_names_the_capture_and_the_small_sample():
    from scripts.autonomous_fund.notifications import evidence_lines
    card = score([review("2026-10-01", -0.03, -0.015, beta=.5), review("2026-10-02", 0.03, 0.024, beta=.8),
                  review("2026-10-05", 0.001, 0.0, beta=1.0)])
    [line] = [text for text in evidence_lines({"down_days": card}) if text.startswith("大跌日表现")]
    assert "1 天（不足 3 天，仅供参考）" in line and "下跌捕获 50.00%" in line and "上涨捕获 80.00%" in line
    assert "有效贝塔比平常低 0.50" in line  # Other days exclude the large up day: 1.0 - 0.5.
    assert not [text for text in evidence_lines({"down_days": score([])}) if text.startswith("大跌日表现")]
