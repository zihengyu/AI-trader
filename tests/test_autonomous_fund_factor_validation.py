"""V2 3.3: walk-forward factor validation, knowledge cutoffs, the random-ranking control and full trial counts.

Synthetic, seeded panels and synthetic model names only; no network and no model inference.
"""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.factors import (FactorLab, FactorSettings, evaluate, evaluate_factor, negative_control,
                                             walk_forward)
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.validation import trial_count

DAYS = pd.bdate_range("2024-01-01", periods=504)


def panel(seed, names=20, days=DAYS):
    rng = np.random.default_rng(seed)
    close = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, .015, (len(days), names)), axis=0), index=days,
                         columns=[f"S{i}" for i in range(names)])
    noise = pd.DataFrame(rng.standard_normal(close.shape), index=days, columns=close.columns)
    return close, noise, close.shift(-1) / close - 1


def test_a_genuine_signal_passes_every_walk_forward_window():
    close, noise, forward = panel(1)
    outcome = evaluate_factor(forward + 0.02 * noise, 1, close, FactorSettings(), 1)
    windows = outcome["walk_forward"]
    assert outcome["status"] == "validated" and windows["agreeing"] == 4 and windows["required"] == 3
    assert windows["embargo_sessions"] == 1 and len(windows["windows"]) == 4


@pytest.mark.parametrize("seed", [4, 5, 6])
def test_a_factor_that_worked_in_one_stretch_only_is_rejected_by_the_windows(seed):
    close, noise, forward = panel(seed)
    block, position = len(DAYS) // 5, np.arange(len(DAYS))
    strong = pd.DataFrame(np.repeat(((position < block) | (position >= 4 * block))[:, None], close.shape[1], 1),
                          index=DAYS, columns=close.columns)
    # Strong in the first and last fifth, mildly wrong in between.
    factor = (-forward + 0.1 * noise).where(~strong, forward + 0.002 * noise)
    outcome = evaluate_factor(factor, 1, close, FactorSettings(), 1)
    # The fixed split alone would have validated it.
    assert outcome["in_sample"]["t"] > outcome["critical_t"] and outcome["holdout"]["t"] > 1
    assert outcome["status"] == "rejected" and "walk-forward" in outcome["reason"]
    assert outcome["walk_forward"]["agreeing"] == 1


def test_the_embargo_keeps_every_training_return_before_its_test_window():
    close, noise, forward = panel(2)
    horizon = 5
    rows_close = close.index
    factor = (close.shift(-horizon) / close - 1) + 0.05 * noise
    outcome = evaluate_factor(factor, horizon, close, FactorSettings(), 1)
    for window in outcome["walk_forward"]["windows"]:
        last = rows_close.get_loc(pd.Timestamp(window["train_last_date"]))
        first = rows_close.get_loc(pd.Timestamp(window["first_date"]))
        # The last training return ends `horizon` sessions after it starts, strictly before the test's first close.
        assert last + horizon < first
    # Without the embargo the last training return would end exactly where the test starts.
    from scripts.autonomous_fund.factors import daily_ic
    sampled = daily_ic(factor, close, horizon, 8).iloc[::horizon]
    loose = walk_forward(sampled, 4, 1, embargo=0)
    assert any(rows_close.get_loc(pd.Timestamp(w["train_last_date"])) + horizon ==
               rows_close.get_loc(pd.Timestamp(w["first_date"])) for w in loose)


def test_knowledge_cutoffs_split_off_the_clean_record_and_unknown_models_are_flagged():
    settings = FactorSettings(model_knowledge_cutoff={"synthetic/model-a": "2025-01-31", "model-b": "2025-06-30"})
    assert settings.cutoff(["synthetic/model-a"]) == date(2025, 1, 31)
    assert settings.cutoff(["synthetic/model-a", "provider/model-b"]) == date(2025, 6, 30)  # The later one binds.
    assert settings.cutoff(["synthetic/model-a", "synthetic/unknown"]) is None and settings.cutoff([]) is None
    close, noise, forward = panel(3)
    factor = forward + 0.02 * noise
    known = evaluate_factor(factor, 1, close, settings, 1, cutoff=date(2025, 6, 30))["knowledge"]
    assert known["memory_risk"] == "partly_after_cutoff" and known["cutoff"] == "2025-06-30"
    assert known["clean_out_of_sample"]["n"] == sum(DAYS[:-1].date > date(2025, 6, 30))
    assert evaluate_factor(factor, 1, close, settings, 1, cutoff=date(2026, 6, 30))["knowledge"]["memory_risk"] == \
        "all_before_cutoff"
    unknown = evaluate_factor(factor, 1, close, settings, 1)["knowledge"]
    assert unknown["memory_risk"] == "unknown_cutoff" and unknown["clean_out_of_sample"] is None
    for bad in ({"m": "soon"}, {"": "2025-01-01"}, ["2025-01-01"]):
        with pytest.raises(RiskError):
            FactorSettings(model_knowledge_cutoff=bad)
    with pytest.raises(RiskError):
        FactorSettings(walk_forward_windows=3, min_window_agreement=4)


def test_random_rankings_pass_no_more_often_than_the_significance_level():
    days = pd.bdate_range("2024-01-01", periods=300)
    close, _, _ = panel(7, names=20, days=days)
    settings = FactorSettings()
    result = negative_control(close, settings, horizon=5, runs=60, seed=1)
    assert result["evaluated"] == 60
    assert result["validated_rate"] <= settings.significance and not result["too_loose"]
    assert result["in_sample_pass_rate"] <= 0.12  # Near the 5% level; small samples wobble.


def test_the_lab_runs_the_control_weekly_and_records_the_models_cutoff(tmp_path):
    journal = Journal(tmp_path, "synthetic-factor-account")
    lab = FactorLab(journal, FactorSettings(negative_control_runs=3, model_knowledge_cutoff={"model-a": "2025-01-31"}))
    close, _, _ = panel(8)
    volume = close * 0 + 1_000_000
    now = datetime(2026, 1, 5, 21, tzinfo=timezone.utc)
    first = lab.context(now, close, volume)["negative_control"]
    assert first["runs"] == 3 and first["as_of"] == "2026-01-05"
    assert lab.context(now + timedelta(days=6), close, volume)["negative_control"] == first  # Not rerun yet.
    assert lab.context(now + timedelta(days=7), close, volume)["negative_control"]["as_of"] == "2026-01-12"
    runs = [r for r in journal.all_events() if r["kind"] == "factor_negative_control"]
    assert len(runs) == 2
    [result] = lab.test([{"name": "momentum", "expression": "rank(ret(21))", "horizon_sessions": 5,
                          "hypothesis": "Synthetic"}], now, "2026-01-05", close, volume, models=("synthetic/model-a",))
    assert result["knowledge"]["cutoff"] == "2025-01-31" and "walk_forward" in result
    from scripts.autonomous_fund.factors import _evidence_flags
    flags = _evidence_flags(evaluate("rank(ret(21))", 5, close, volume, lab.settings, 1))
    assert flags["walk_forward_agreeing"].endswith("/4") and flags["memory_risk"] == "unknown_cutoff"
    assert _evidence_flags({"in_sample": {}}) == {"walk_forward_agreeing": None, "memory_risk": None,
                                                  "clean_out_of_sample": None}  # Validated before this existed.


def test_the_deflated_sharpe_counts_every_configuration_tried():
    base = trial_count(2, 1, 0)
    assert base == {"prompt_versions_x_risk_policies": 2, "factor_expressions": 0, "construction_alternative": 0,
                    "learning_control_arm": 0, "total": 2}
    assert trial_count(0, 0, 0)["total"] == 1  # Never zero trials.
    grown = [trial_count(2, 1, 5)["total"], trial_count(2, 1, 5, construction_runs=40)["total"],
             trial_count(2, 1, 5, construction_runs=40, shadow_runs=3)["total"], trial_count(3, 1, 5, 40, 3)["total"]]
    assert grown == sorted(grown) and len(set(grown)) == 4 and grown[-1] == 3 + 5 + 1 + 1


def test_acceptance_lists_the_trial_breakdown(tmp_path):
    from scripts.autonomous_fund.validation import acceptance, acceptance_markdown
    journal = Journal(tmp_path, "synthetic-performance")
    start = datetime(2026, 7, 6, 19, 0, tzinfo=timezone.utc)
    journal.event(start, "startup", {"mode": "local", "limits": {"drawdown": "0.08"}, "risk_policy_digest": "p1",
                                     "execution": {"iteration_seconds": 60}})
    lab = FactorLab(journal, FactorSettings())
    with journal.connection() as db:
        for n in range(4):  # Rejected factors are trials too.
            db.execute("INSERT INTO fund_factors (id, expression, horizon, name, hypothesis, proposed_at, status, "
                       "evidence, universe) VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"f{n}", f"rank(ret({n + 2}))", 5, f"f{n}", "h", "2026-07-01", "rejected", "{}", "stocks"))
    assert lab.trials() == 4
    day, equity = start, 10000.0
    for i in range(8):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        journal.event(day, "decision_validated", {"targets": [], "prompt_version": "v1"})
        journal.event(day, "construction_variant", {"ai": {}, "risk_budget": {}})
        journal.event(day, "account", {"as_of": day.isoformat(), "cash": "0", "equity": str(equity), "positions": {}})
        journal.event(day, "benchmark", {"SPY": "500", "QQQ": "400"})
        equity *= 1.001
        day += timedelta(days=1)
    result = acceptance(tmp_path, now=day + timedelta(days=1))
    stats = result["performance_evidence"]
    assert stats["configurations_breakdown"]["total"] == stats["configurations_tried"] == 1 + 4 + 1
    assert "因子表达式（含未通过） 4" in acceptance_markdown(result)
