"""Decision scorecards must not mistake market beta or trading costs for selection skill.

Synthetic, neutral data only: HIGH moves exactly twice the benchmark every day,
so a portfolio holding it in a rising market has no selection skill at all.
"""
from datetime import date, timedelta

import pytest

from scripts.autonomous_fund.scorecard import score_decisions

DAYS = [date(2026, 1, 2) + timedelta(days=i) for i in range(200)]
BENCH_RETURNS = [0.004 if i % 3 else -0.002 for i in range(len(DAYS))]  # Rising, with down days.


def path(multiplier, start=100.0):
    rows, price = [], start
    for day, move in zip(DAYS, BENCH_RETURNS):
        rows.append((day.isoformat(), price))
        price *= 1 + multiplier * move
    return rows


def decided(index):
    return f"{DAYS[index].isoformat()}T15:00:00+00:00"


def test_a_pure_beta_portfolio_has_no_beta_matched_excess():
    closes = {"BENCH": path(1), "HIGH": path(2)}
    result = score_decisions([(decided(150), {"targets": [{"symbol": "HIGH", "weight": 0.5}]})], closes,
                             benchmark="BENCH")
    one = result["recent"][0]["horizons"]["5"]
    assert one["status"] == "observed"
    assert one["exposure_excess_return"] > 0.003  # The old, exposure-matched view calls this skill.
    assert result["recent"][0]["beta"] == pytest.approx(1.0, abs=0.01)  # 0.5 x beta 2.
    assert one["excess_return"] == pytest.approx(0.0, abs=0.0005)  # Beta-matched: no skill.


def test_turnover_costs_are_charged_against_excess():
    closes = {"BENCH": path(1), "HIGH": path(2), "LOW": path(0.5)}
    decisions = [(decided(150), {"targets": [{"symbol": "HIGH", "weight": 0.4}]}),
                 (decided(151), {"targets": [{"symbol": "LOW", "weight": 0.4}]})]
    result = score_decisions(decisions, closes, benchmark="BENCH", cost_bps=10)
    first, second = (row["horizons"]["1"] for row in result["recent"])
    assert result["recent"][0]["turnover"] == pytest.approx(0.4)  # From cash.
    assert result["recent"][1]["turnover"] == pytest.approx(0.8)  # Sell 0.4 HIGH, buy 0.4 LOW.
    assert first["net_excess_return"] == pytest.approx(first["excess_return"] - 0.4 * 0.001)
    assert second["net_excess_return"] == pytest.approx(second["excess_return"] - 0.8 * 0.001)
    assert result["by_horizon"]["1"]["mean_net_excess_return"] < result["by_horizon"]["1"]["mean_excess_return"]
    # Cost sensitivity: the same record at twice and three times the estimated costs.
    summary = result["by_horizon"]["1"]
    mean_cost = (0.4 + 0.8) * 0.001 / 2
    assert summary["mean_net_excess_return_2x_costs"] == pytest.approx(summary["mean_excess_return"] - 2 * mean_cost)
    assert summary["mean_net_excess_return_3x_costs"] == pytest.approx(summary["mean_excess_return"] - 3 * mean_cost)


def test_beta_uses_only_closes_through_the_decision_day():
    early = path(1)
    # After the decision HIGH stops tracking the benchmark; a later regime must not change the beta used.
    late = [(day, price) for day, price in path(2)[:151]] + [
        (d.isoformat(), path(2)[150][1]) for d in DAYS[151:]]
    full = score_decisions([(decided(150), {"targets": [{"symbol": "HIGH", "weight": 0.5}]})],
                           {"BENCH": early, "HIGH": late}, benchmark="BENCH")
    assert full["recent"][0]["beta"] == pytest.approx(1.0, abs=0.01)


def test_an_unestimable_beta_is_assumed_one_and_disclosed():
    closes = {"BENCH": path(1), "NEW": path(2)[-8:]}
    result = score_decisions([(decided(193), {"targets": [{"symbol": "NEW", "weight": 0.3}]})], closes,
                             benchmark="BENCH")
    row = result["recent"][0]
    assert row["beta_assumed"] == ["NEW"] and row["beta"] == pytest.approx(0.3)


def test_scores_are_grouped_by_prompt_version():
    closes = {"BENCH": path(1), "HIGH": path(2)}
    decisions = [(decided(140), {"targets": [{"symbol": "HIGH", "weight": 0.5}], "prompt_version": "aaa"}),
                 (decided(150), {"targets": [{"symbol": "HIGH", "weight": 0.5}], "prompt_version": "bbb"}),
                 (decided(151), {"targets": [{"symbol": "HIGH", "weight": 0.5}]})]
    result = score_decisions(decisions, closes, benchmark="BENCH")
    assert set(result["by_prompt_version"]) == {"aaa", "bbb", "unversioned"}
    assert result["by_prompt_version"]["aaa"]["5"]["observed"] == 1


def test_prompt_version_changes_with_instructions_or_models(monkeypatch):
    from scripts.autonomous_fund import strategy
    models = {"researcher": "provider/research-model", "manager": "provider/decision-model"}
    first = strategy.prompt_version(models)
    assert first == strategy.prompt_version(dict(models)) and len(first) == 12
    assert strategy.prompt_version({**models, "manager": "provider/other-model"}) != first
    monkeypatch.setattr(strategy, "MANAGER_PROMPT", strategy.MANAGER_PROMPT + " Changed.")
    assert strategy.prompt_version(models) != first
