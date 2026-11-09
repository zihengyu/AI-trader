"""Scorecards grouped by market regime, and recent-vs-earlier decay checks (background, not criteria).

Synthetic closes and decisions only; no network, broker or model inference.
"""
import math
import random
from datetime import date

import pandas as pd

from scripts.autonomous_fund.notifications import regime_trend_lines
from scripts.autonomous_fund.scorecard import (MIN_TREND_SAMPLE, TREND_WINDOW, market_regimes, recent_versus_earlier,
                                               score_decisions, score_ideas)


def sessions(count, start=date(2024, 1, 2)):
    return [d.date().isoformat() for d in pd.bdate_range(start, periods=count)]


def benchmark(seed=7):
    """300 calm rising sessions, then 120 volatile falling ones."""
    rng, close, rows = random.Random(seed), 100.0, []
    days = sessions(420)
    for i, day in enumerate(days):
        close *= 1 + (0.001 + rng.gauss(0, 0.004) if i < 300 else -0.004 + rng.gauss(0, 0.025))
        rows.append((day, close))
    return rows


def test_regimes_use_only_closes_through_each_session_and_tell_calm_rises_from_volatile_falls():
    rows = benchmark()
    labels = market_regimes(rows)
    days = [day for day, _ in rows]
    assert days[TREND_WINDOW - 2] not in labels and days[TREND_WINDOW - 1] in labels  # 200 closes are needed.
    # No look-ahead: a session's label is the same whether or not later closes exist.
    prefix = market_regimes(rows[:330])
    assert all(labels[day] == label for day, label in prefix.items())
    rising = [labels[d] for d in days[220:300]]
    falling = [labels[d] for d in days[380:420]]  # The 200-session mean lags the turn by design.
    assert sum(label.startswith("up_") for label in rising) / len(rising) > 0.9
    assert sum(label == "down_volatile" for label in falling) / len(falling) > 0.9
    assert labels[days[310]] == "up_volatile"  # Volatility reacts within 20 sessions; the trend label later.
    assert market_regimes(rows[:150]) == {}  # Too short: no label rather than a guess.


def test_regime_lookbacks_do_not_slide_over_a_missing_benchmark_session():
    rows = benchmark()[:235]
    sessions = tuple(day for day, _ in rows)
    partial = rows[:210] + rows[211:]
    assert sessions[-1] in market_regimes(rows, sessions=sessions)
    assert sessions[209] in market_regimes(partial, sessions=sessions)
    assert sessions[-1] not in market_regimes(partial, sessions=sessions)


def test_recent_versus_earlier_flags_only_a_clear_change():
    assert recent_versus_earlier([1.0] * (MIN_TREND_SAMPLE - 1))["status"] == "insufficient_sample"
    flat = recent_versus_earlier([0.1] * 30)  # Float rounding must not introduce artificial variance.
    assert flat["status"] == "observed" and flat["t_stat"] is None and flat["change"] == "no_clear_change"
    rng = random.Random(3)
    fading = [rng.gauss(0.01, 0.005) for _ in range(40)] + [rng.gauss(-0.01, 0.005) for _ in range(20)]
    result = recent_versus_earlier(fading)
    assert result["change"] == "weaker_recently" and result["recent"]["n"] == 20 and result["earlier"]["n"] == 40
    noise = recent_versus_earlier([rng.gauss(0, 0.01) for _ in range(60)])
    assert noise["change"] == "no_clear_change"
    hits = recent_versus_earlier([True, False] * 15 + [True] * 15)
    assert hits["change"] == "stronger_recently" and math.isclose(hits["recent"]["mean"], 1.0)


def test_decisions_and_ideas_are_grouped_by_the_regime_of_their_start():
    rows = benchmark()
    days = [day for day, _ in rows]
    labels = market_regimes(rows)
    closes = {"SPY": rows, "ALFA": [(d, c * (1.0005 ** i)) for i, (d, c) in enumerate(rows)]}
    decisions = [(f"{day}T20:00:00+00:00", {"targets": [{"symbol": "ALFA", "weight": 0.5}]})
                 for day in days[230:400:5]]
    card = score_decisions(decisions, closes, regimes=labels)
    assert {row["regime"] for row in card["recent"]} <= set(labels.values())
    assert {"up_calm", "down_volatile"} <= set(card["by_regime"])
    total = sum(group["5"]["observed"] for group in card["by_regime"].values())
    assert total == card["by_horizon"]["5"]["observed"]
    assert card["trend_5d_net_excess"]["status"] == "observed"
    assert "by_regime" not in card["recent"][0]  # A row names its regime; groups live in the summary.
    plain = score_decisions(decisions, closes)
    assert plain["by_regime"] == {} and "regime" not in plain["recent"][0]

    ideas = [{"idea_id": f"i{n}", "symbol": "ALFA", "direction": "up", "horizon_sessions": 5, "confidence": 0.6,
              "formed_at": f"{day}T12:00:00+00:00"} for n, day in enumerate(days[230:400:3])]
    scored = score_ideas(ideas, closes, regimes=labels)
    assert sum(group["scored"] for group in scored["by_regime"].values()) == scored["scored"]
    assert scored["trend"]["status"] == "observed"
    assert score_ideas(ideas, closes)["by_regime"] == {}


def test_reports_show_regime_groups_and_a_fading_record_as_background():
    card = {"decisions": {"by_regime": {"up_calm": {"5": {"observed": 12, "mean_net_excess_return": 0.004}},
                                        "down_volatile": {"5": {"observed": 0, "mean_net_excess_return": None}}},
                          "trend_5d_net_excess": {"status": "observed", "earlier": {"n": 20, "mean": 0.006},
                                                  "recent": {"n": 10, "mean": -0.003}, "difference": -0.009,
                                                  "t_stat": -2.4, "change": "weaker_recently"}},
            "ideas": {"by_regime": {"up_calm": {"scored": 30, "hit_rate": 0.6}},
                      "trend": {"status": "insufficient_sample", "scored": 5, "minimum_sample": 20}}}
    text = "\n".join(regime_trend_lines(card))
    assert "决策 5 日净超额：上涨·平稳 12 次、平均 0.40%" in text and "下跌·波动大" not in text
    assert "研究想法：上涨·平稳 30 条、命中率 60.00%" in text and "只作背景" in text
    assert "最近 10 条平均 -0.30%，之前 20 条 0.60%（t=-2.40，最近明显变弱" in text
    assert "研究想法命中率：最近" not in text  # Too few ideas to compare.
    assert regime_trend_lines({}) == []


def test_constant_groups_with_different_hit_rates_are_reported_without_a_t_statistic():
    trend = recent_versus_earlier([True] * 20 + [False] * 10)
    assert trend["difference"] == -1 and trend["t_stat"] is None
    [line] = regime_trend_lines({"ideas": {"trend": trend}})
    assert "最近 10 条平均 0.00%，之前 20 条 100.00%" in line and "无法计算 t" in line


def test_the_managers_scorecard_summary_carries_regimes_and_the_trend(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, default_frames, harness
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy.prompt_version = "synthetic-version"
    summary = strategy._scorecard_summary(NOW)
    assert "decisions_by_regime" in summary and "decisions_trend" in summary
    assert summary["research_ideas"]["trend"]["status"] == "insufficient_sample"
    regimes = strategy._market_regimes(NOW)
    assert isinstance(regimes, dict)
    assert all(day < NOW.date().isoformat() for day in regimes)  # Completed sessions only.
