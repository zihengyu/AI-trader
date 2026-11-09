"""V2 4.2: degradation of what is in effect, detected by thresholds fixed in advance, rolls back or slows down.

Synthetic closes, shadow decisions, construction variants, fills and NAV marks only; no network or models.
"""
import random
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from scripts.autonomous_fund.construction import ConstructionSettings, evaluate
from scripts.autonomous_fund.degradation import daily_turnover, rolling_rollback, turnover_check
from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.notifications import readable
from scripts.autonomous_fund.validation import ValidationSettings, paired_test


def noisy(n, drift, seed):
    rng, level, out = random.Random(seed), 100.0, []
    for _ in range(n):
        out.append(level)
        level *= 1 + drift + rng.gauss(0, 0.003)
    return out


def test_a_full_window_turned_against_the_trading_arm_rolls_back_to_the_other():
    worse = [-0.001 + 0.0002 * ((-1) ** i) for i in range(20)]
    assert rolling_rollback(worse, "memo", "memo", "baseline", 2.0)[0] == "baseline"
    assert rolling_rollback(worse, "baseline", "memo", "baseline", 2.0)[0] is None  # Already the better arm.
    assert rolling_rollback([-x for x in worse], "baseline", "memo", "baseline", 2.0)[0] == "memo"
    assert rolling_rollback(worse[:19], "memo", "memo", "baseline", 2.0)[0] is None  # Not a full window.
    mixed = [0.001 if i % 2 else -0.001 for i in range(20)]
    assert rolling_rollback(mixed, "memo", "memo", "baseline", 2.0)[0] is None  # No evidence either way.


def learning(tmp_path, days):
    from tests.test_autonomous_fund_validation import learning_harness, shadow_rows
    data = {"UP": list(zip([str(d.date()) for d in days], noisy(len(days), 0.002, 1))),
            "DOWN": list(zip([str(d.date()) for d in days], noisy(len(days), -0.002, 2))),
            "SPY": [(str(d.date()), 100.0) for d in days]}
    strategy = learning_harness(tmp_path, days, data)
    rows = shadow_rows(days[:50], "UP", "DOWN") + shadow_rows(days[50:70], "DOWN", "UP")  # Memo trades.
    for row in rows:
        strategy.journal.event(row["decided_at"], "shadow_decision", row)
    return strategy


def test_the_learning_arm_rolls_back_when_its_recent_record_turns_although_the_full_record_does_not(tmp_path):
    days = pd.bdate_range("2025-01-02", periods=75, tz="America/New_York")
    strategy = learning(tmp_path, days)
    now = datetime(2025, 4, 30, 21, tzinfo=timezone.utc)
    strategy.get_datetime = lambda: now
    evaluation = strategy._evaluate_learning(now)
    assert evaluation["memo_minus_baseline_1d_excess"]["t_stat"] > -2  # The full-record rule would keep memo.
    assert evaluation["recent"]["pairs"] == 20 and evaluation["recent"]["t_stat"] <= -2
    assert evaluation["champion"] == "baseline" and evaluation["reason"].startswith("degradation: last 20 sessions")
    kind, details = strategy.notices[-1]
    assert kind == "learning_champion_switched" and details["reason"].startswith("degradation")
    assert "退化回滚" in readable(kind, details)


def test_a_construction_rolls_back_when_the_last_twenty_sessions_turn_against_it():
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(80)]
    rng = random.Random(5)
    low, high, spy = [], [], []
    a = b = c = 100.0
    for i in range(80):
        low.append((days[i].isoformat(), a)), high.append((days[i].isoformat(), b)), spy.append((days[i].isoformat(), c))
        early = i < 55
        a *= 1 + (0.002 if early else -0.003) + rng.gauss(0, 0.001)
        b *= 1 + (-0.001 if early else 0.003) + rng.gauss(0, 0.001)
        c *= 1 + rng.gauss(0, 0.0005)
    closes = {"LOW": low, "HIGH": high, "SPY": spy}
    variants = [(f"{d.isoformat()}T15:00:00+00:00", {"ai": {"HIGH": "0.08", "LOW": "0.02"},
                                                     "risk_budget": {"HIGH": "0.02", "LOW": "0.08"}}) for d in days[:78]]
    settings = ConstructionSettings(min_pairs=20)
    result = evaluate(variants, closes, settings, "risk_budget")
    assert result["test"]["t_stat"] > -2  # The full record still favours risk budgets.
    assert result["recent"]["t_stat"] <= -2 and result["champion"] == "ai"
    assert result["reason"].startswith("degradation") and result["trigger"] is result["recent"]
    assert evaluate(variants, closes, ConstructionSettings(mode="risk_budget"), "risk_budget")["champion"] == "risk_budget"
    text = readable("construction_switched", {"from": "risk_budget", "to": "ai", "test": result["trigger"],
                                              "reason": result["reason"]})
    assert "退化回滚" in text and "t = " in text


def test_turnover_dedupes_reconciled_fills_and_counts_quiet_sessions_as_zero():
    fills = [("2026-11-02T15:00:00+00:00", "fill", {"broker_id": "a", "quantity": "4", "price": "100"}),
             ("2026-11-02T15:01:00+00:00", "fill_reconciled", {"broker_id": "a", "quantity": "10", "price": "100"}),
             ("2026-11-02T16:00:00+00:00", "fill", {"broker_id": "b", "quantity": "5", "price": "100"})]
    assert daily_turnover(fills, {"2026-11-02": 10000.0, "2026-11-03": 10000.0}) == [("2026-11-02", 0.15),
                                                                                 ("2026-11-03", 0.0)]


def series(values):
    return [(f"day-{i:03d}", value) for i, value in enumerate(values)]


def test_the_turnover_guard_needs_history_turns_on_far_above_it_and_off_back_at_the_median():
    calm = [0.02 + 0.01 * (i % 5) for i in range(40)]
    assert turnover_check(series(calm), False)["status"] == "insufficient_history"
    assert turnover_check(series(calm + [0.03] * 5), False)["active"] is False
    busy = turnover_check(series(calm + [0.2] * 5), False)
    assert busy["status"] == "elevated" and busy["active"] and busy["recent_mean"] > busy["p95"]
    still = turnover_check(series(calm + [0.2] * 5 + [0.05] * 5), True)  # Below p95 but above the median.
    assert still["active"] and still["status"] == "elevated"
    assert turnover_check(series(calm + [0.2] * 5 + [0.02] * 5), True)["status"] == "recovered"


def marks_and_fills(strategy, days, turnover):
    journal = strategy.journal
    journal.event(datetime(2026, 6, 1, 12, tzinfo=timezone.utc), "startup",
                  {"mode": "local", "execution_revision": EXECUTION_REVISION})
    for i, (day, value) in enumerate(zip(days, turnover)):
        close = datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc)
        if value:
            journal.event(close - timedelta(hours=2), "fill", {"broker_id": f"o{i}", "symbol": "ALFA", "side": "buy",
                                                                "quantity": str(value * 100), "price": "100"})
        journal.event(close, "account", {"as_of": close.isoformat(), "cash": "0", "equity": "10000", "positions": {}})


def test_the_strategy_slows_reviews_while_turnover_is_far_above_its_own_history(tmp_path):
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path)
    days = [d.date() for d in pd.bdate_range("2026-06-01", periods=60) if d.date() != date(2026, 6, 19)
            and d.date() != date(2026, 7, 3)][:55]
    turnover = [0.02 + 0.01 * (i % 4) for i in range(45)] + [0.3] * 5 + [0.01] * 5
    marks_and_fills(strategy, days, turnover)
    end = lambda n: datetime(days[n - 1].year, days[n - 1].month, days[n - 1].day, 21, tzinfo=timezone.utc)  # noqa: E731
    base = strategy.model_budget.min_review_minutes
    assert strategy._check_degradation(end(45))["status"] == "normal" and strategy._min_review_minutes(
        strategy.model_budget) == base
    result = strategy._check_degradation(end(50))
    assert result["status"] == "elevated" and strategy._min_review_minutes(strategy.model_budget) == 2 * base
    kind, details = strategy.notices[-1]
    assert kind == "degradation_detected" and details["min_review_minutes"] == 2 * base
    assert "复核间隔加倍" in readable(kind, details) and "止损、止盈照常" in readable(kind, details)
    assert strategy._check_degradation(end(55))["status"] == "recovered"
    assert strategy._min_review_minutes(strategy.model_budget) == base
    assert strategy.notices[-1][0] == "degradation_cleared"
    guards = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "degradation"]
    assert [g["status"] for g in guards] == ["elevated", "recovered"]


def test_paired_statistics_are_unchanged_for_the_full_record_rule():
    stats = paired_test([0.001, 0.002, 0.0015] * 7, 20)
    assert stats["sufficient"] and stats["t_stat"] > 2
    assert ValidationSettings().switch_t_stat == 2.0
