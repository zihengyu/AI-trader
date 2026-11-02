"""Skill-or-luck statistics and the staged-capital readiness verdict.

Seeded synthetic return series and a synthetic journal only.
"""
import math
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

import numpy as np
import pytest

from scripts.autonomous_fund.performance import (alpha_beta, capital_stage, deflated_sharpe, evidence,
                                                 min_track_record, probabilistic_sharpe)


def series(mean, std, n, seed=1):
    return list(np.random.default_rng(seed).normal(mean, std, n))


def test_probabilistic_sharpe_matches_the_normal_case_and_rises_with_evidence():
    returns = series(0.001, 0.01, 252)
    mean, std = np.mean(returns), np.std(returns, ddof=1)
    sr = mean / std
    # Close to the normal-returns formula when skewness ~ 0 and kurtosis ~ 3.
    approx = NormalDist().cdf(sr * math.sqrt(251) / math.sqrt(1 + sr * sr / 2))
    assert probabilistic_sharpe(returns) == pytest.approx(approx, abs=0.03)
    assert probabilistic_sharpe(series(0.001, 0.01, 1000)) > probabilistic_sharpe(series(0.001, 0.01, 60))
    assert probabilistic_sharpe([0.01, 0.01]) is None


def test_trying_more_configurations_deflates_the_sharpe_ratio():
    returns = series(0.0012, 0.01, 300)
    assert deflated_sharpe(returns, 1) == pytest.approx(probabilistic_sharpe(returns))
    assert deflated_sharpe(returns, 50) < deflated_sharpe(returns, 5) < deflated_sharpe(returns, 1)


def test_minimum_track_record_shrinks_with_a_higher_sharpe_and_is_none_without_an_edge():
    strong = series(0.003, 0.01, 300)
    weak = [r - 0.0015 for r in strong]  # Same noise, half the edge.
    assert min_track_record(strong) < min_track_record(weak)
    assert min_track_record(series(-0.001, 0.01, 300)) is None


def test_beta_adjusted_alpha_separates_skill_from_market_exposure():
    market = series(0.0005, 0.01, 400, seed=3)
    noise = series(0, 0.002, 400, seed=4)
    beta_only = [1.5 * m + e for m, e in zip(market, noise)]
    skilled = [0.001 + 1.5 * m + e for m, e in zip(market, noise)]
    assert alpha_beta(beta_only, market)["beta"] == pytest.approx(1.5, abs=0.05)
    assert abs(alpha_beta(beta_only, market)["alpha_t"]) < 2
    assert alpha_beta(skilled, market)["alpha_t"] > 5


def test_capital_stage_requires_every_test_and_never_claims_a_live_path():
    weak = evidence(series(0.0002, 0.01, 40), None, 3)
    stage = capital_stage("ready", weak, 0.02, 0.08)
    assert stage["stage"] == "paper_only" and any("60" in r for r in stage["reasons"])
    market = series(0.0004, 0.01, 400, seed=5)
    strong_returns = [0.002 + 0.5 * m + e for m, e in zip(market, series(0, 0.004, 400, seed=6))]
    strong = evidence(strong_returns, market, 2)
    assert capital_stage("ready", strong, 0.03, 0.08)["stage"] == "eligible_stage_1"
    assert capital_stage("not_ready", strong, 0.03, 0.08)["stage"] == "paper_only"
    assert capital_stage("ready", strong, 0.08, 0.08)["stage"] == "paper_only"  # Hit the drawdown latch.
    assert "no live-trading mode" in capital_stage("ready", strong, 0.03, 0.08)["notice"]


def test_acceptance_report_includes_the_evidence_and_ignores_reserve_moves(tmp_path):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.validation import acceptance, acceptance_markdown
    journal = Journal(tmp_path, "synthetic-performance")
    start = datetime(2026, 7, 6, 19, 0, tzinfo=timezone.utc)  # 15:00 New York; July 6-31 has no NYSE holiday.
    journal.event(start, "startup", {"mode": "local", "limits": {"drawdown": "0.08"}, "risk_policy_digest": "p1",
                                     "execution": {"iteration_seconds": 60}})
    equity, spy = 10000.0, 500.0
    day = start
    for i in range(20):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        journal.event(day, "decision_validated", {"targets": [], "prompt_version": "v1" if i < 10 else "v2"})
        journal.event(day, "account", {"as_of": day.isoformat(), "cash": "0", "equity": str(equity), "positions": {}})
        journal.event(day, "benchmark", {"SPY": str(spy), "QQQ": "400"})
        if i == 10:
            journal.event(day + timedelta(minutes=1), "operator_reserve_adjusted", {})
            equity += 5000  # Cash moved into the sleeve: not a return.
        equity *= 1.002
        spy *= 1.001
        day += timedelta(days=1)
    result = acceptance(tmp_path, now=day + timedelta(days=1))
    stats = result["performance_evidence"]
    assert stats["sessions"] == 18  # 19 consecutive pairs minus the one spanning the reserve move.
    assert stats["configurations_tried"] == 2 and stats["mean_return"] == pytest.approx(0.002, abs=1e-9)
    assert result["capital_stage"]["stage"] == "paper_only"
    text = acceptance_markdown(result)
    assert "业绩证据" in text and "分阶段资金就绪度" in text
