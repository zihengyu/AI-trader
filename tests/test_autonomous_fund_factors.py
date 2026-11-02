"""Factor lab: safe AI factor expressions, look-ahead-free evaluation and live retirement.

Synthetic price panels only (seeded), plus the repository's recorded ETF closes for
the command-line check. No network, broker or model inference.
"""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.factors import (GRAMMAR, FactorLab, FactorSettings, compute, critical_t, evaluate,
                                             parse)
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import RiskError

NOW = datetime(2026, 11, 2, 21, 5, tzinfo=timezone.utc)
SETTINGS = FactorSettings()


def panel(seed=1, drift=0.003, names=20, days=600, flip_at=None):
    """Persistent per-name drifts make 60-session momentum genuinely predictive; drift=0 is pure noise."""
    rng = np.random.default_rng(seed)
    mu = rng.permutation(np.linspace(-drift, drift, names))
    returns = rng.normal(0, 0.012, (days, names)) + mu
    if flip_at is not None:
        returns[flip_at:] -= 2 * mu  # The regime reverses: yesterday's winners now lose.
    index = pd.bdate_range("2023-01-02", periods=days)
    close = pd.DataFrame(100 * np.exp(np.cumsum(returns, axis=0)), index=index,
                         columns=[f"S{i:02d}" for i in range(names)])
    volume = pd.DataFrame(rng.integers(1_000_000, 5_000_000, (days, names)).astype(float), index=index,
                          columns=close.columns)
    return close, volume


@pytest.mark.parametrize("expression", [
    "__import__('os').system('x')", "close.__class__", "open('secrets')", "(lambda: 1)()", "ret(n=5)",
    "ret(0)", "ret(5000)", "ret(5.5)", "ret(-3)", "rank(5)", "1 + 2", "ts_std(close, 1)", "price + 1",
    "close[0]", "close if close else volume", "close ** 2", "rank(close, close)", "", "x" * 301,
    "rank(" * 40 + "close" + ")" * 40,
])
def test_factor_language_refuses_anything_but_whitelisted_backward_operators(expression):
    with pytest.raises(RiskError):
        parse(expression)


def test_grammar_examples_and_every_operator_only_look_backwards():
    close, volume = panel()
    cut = close.index[400]
    altered_close, altered_volume = close.copy(), volume.copy()
    future = close.index > cut
    rng = np.random.default_rng(9)
    altered_close.loc[future] *= rng.uniform(0.5, 1.5, altered_close.loc[future].shape)
    altered_volume.loc[future] *= 3
    expressions = GRAMMAR["examples"] + [
        "ts_rank(close, 20)", "delta(log(close), 5)", "zscore(ts_max(close, 10) / ts_min(close, 10))",
        "demean(abs(ret(3)))", "sign(delay(ret(1), 2)) * ts_std(ret(1), 20)", "rank(close / volume)"]
    for expression in expressions:
        before = compute(expression, close, volume).loc[:cut]
        after = compute(expression, altered_close, altered_volume).loc[:cut]
        pd.testing.assert_frame_equal(before, after, check_exact=False, rtol=1e-12)  # Changing the future changes nothing earlier.


def test_a_genuine_signal_is_validated_and_noise_is_rejected():
    close, volume = panel(seed=1)
    real = evaluate("rank(ret(60))", 5, close, volume, SETTINGS, trials=1)
    assert real["status"] == "validated" and real["direction"] == 1
    assert real["in_sample"]["t"] > real["critical_t"] and real["holdout"]["t"] >= SETTINGS.holdout_min_t
    flipped = evaluate("-rank(ret(60))", 5, close, volume, SETTINGS, trials=1)
    assert flipped["status"] == "validated" and flipped["direction"] == -1  # Same evidence, opposite sign.
    for seed in range(5):
        noise = evaluate("rank(ret(60))", 5, *panel(seed=seed, drift=0), SETTINGS, trials=1)
        assert noise["status"] == "rejected"


def test_the_evidence_bar_rises_with_every_expression_tried():
    assert critical_t(1, 0.05) == 1.96
    assert critical_t(10, 0.05) < critical_t(50, 0.05) < critical_t(500, 0.05)
    close, volume = panel(seed=3, drift=0.0012)  # A modest edge: enough for one honest test, not for 200.
    weak = evaluate("rank(ret(60))", 5, close, volume, SETTINGS, trials=1)
    mined = evaluate("rank(ret(60))", 5, close, volume, SETTINGS, trials=200)
    assert abs(weak["in_sample"]["t"]) >= weak["critical_t"]
    assert mined["status"] == "rejected" and "trials-adjusted" in mined["reason"]


def test_too_few_names_or_too_little_history_is_insufficient_not_a_verdict():
    close, volume = panel(names=5)
    assert evaluate("rank(ret(60))", 5, close, volume, SETTINGS, trials=1)["status"] == "insufficient_data"
    close, volume = panel(days=150)
    assert evaluate("rank(ret(60))", 5, close, volume, SETTINGS, trials=1)["status"] == "insufficient_data"
    with pytest.raises(RiskError):
        evaluate("rank(ret(60))", 21, close, volume, SETTINGS, trials=1)


def proposal(expression, name="momentum", horizon=5):
    return {"name": name, "expression": expression, "horizon_sessions": horizon, "hypothesis": "synthetic"}


def test_registry_counts_each_distinct_expression_once_and_caps_a_review(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-factors"), SETTINGS)
    close, volume = panel()
    first = lab.test([proposal("rank(ret(60))"), proposal("not valid((")], NOW, "2026-11-02", close, volume)
    assert [r["status"] for r in first] == ["validated", "invalid"] and lab.trials() == 1
    again = lab.test([proposal("rank( ret(60) )", name="renamed")], NOW, "2026-11-03", close, volume)
    assert again[0]["repeat"] and lab.trials() == 1  # Re-proposing is not a fresh chance to pass.
    many = lab.test([proposal(f"rank(ret({n}))", name=f"m{n}") for n in range(20, 30)], NOW, "2026-11-04",
                    close, volume)
    assert len(many) == SETTINGS.max_proposals_per_review and lab.trials() == 1 + SETTINGS.max_proposals_per_review
    short, short_volume = panel(days=150)
    lab.test([proposal("-vol(20)", name="lowvol")], NOW, "2026-11-05", short, short_volume)
    trials = lab.trials()
    retest = lab.test([proposal("-vol(20)", name="lowvol")], NOW, "2026-11-06", close, volume)
    assert retest[0]["status"] in {"validated", "rejected"} and lab.trials() == trials  # More data, same trial.
    assert retest[0]["trials"] == trials  # Its evidence bar is the one it was first tried under.


def test_live_record_retires_a_factor_whose_edge_reverses(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-live"), FactorSettings(live_min_observations=3))
    close, volume = panel(seed=3, days=626, flip_at=600)
    validation_day = str(close.index[599].date())
    assert lab.test([proposal("rank(ret(60))")], NOW, validation_day, close.iloc[:600], volume.iloc[:600])[0][
        "status"] == "validated"
    context = lab.context(NOW, close.iloc[:600], volume.iloc[:600])
    assert context["validated"][0]["live"]["n"] == 0
    favored = {row["symbol"] for row in context["validated"][0]["live"]["favored"]}
    leaders = set(close.iloc[599].div(close.iloc[539]).sort_values().tail(5).index)
    assert favored == leaders  # Current ranks come from the latest completed close, direction-adjusted.
    later = lab.context(NOW, close, volume)
    assert later["validated"] == [] and later["recently_rejected"][-1]["status"] == "retired"
    assert any(row["kind"] == "factor_retired" for row in lab.journal.all_events())


def test_context_tells_the_ai_the_grammar_the_standard_and_its_track_record(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-context"), SETTINGS)
    close, volume = panel()
    lab.test([proposal("rank(ret(60))"), proposal("rank(volume)", name="busy")], NOW, "2026-11-02", close, volume)
    context = lab.context(NOW, close, volume)
    assert context["grammar"] is GRAMMAR and context["trials"] == 2
    assert context["next_critical_t"] == critical_t(3, SETTINGS.significance)
    assert [row["expression"] for row in context["validated"]] == ["rank(ret(60))"]
    assert context["recently_rejected"][0]["expression"] == "rank(volume)"
    offline = lab.context(NOW, None, None)  # Missing history never hides what was validated.
    assert offline["validated"][0]["live"]["unavailable"] == "history"


@pytest.mark.parametrize("section, message", [
    ({"min_names": 2}, "min_names"), ({"holdout_fraction": 0.9}, "holdout_fraction"),
    ({"enabled": "yes"}, "enabled"), ({"surprise": 1}, "Unknown factor_lab"),
])
def test_factor_settings_are_validated(section, message):
    with pytest.raises(RiskError, match=message):
        FactorSettings.from_config(section)


# ----- Integration: the review proposes, the code tests, decisions see the evidence ----------

def test_review_factor_proposals_are_tested_and_reach_the_next_decision(tmp_path):
    from scripts.autonomous_fund.review import PostmarketReview
    from scripts.autonomous_fund.strategy import AutonomousFund
    from .test_autonomous_fund_review import answer, events, review_harness
    strategy, calls = review_harness(tmp_path)
    close, volume = panel()
    strategy.factor_lab = FactorLab(strategy.journal, SETTINGS)
    strategy._factor_panel = lambda now: (close, volume)
    original = strategy.agents["manager"].run

    def manager(**kwargs):
        if kwargs.get("output_schema") is not PostmarketReview:
            return original(**kwargs)
        context = kwargs["context"]
        assert context["factor_lab"]["grammar"] is GRAMMAR  # The reviewer learns the language and its record.
        body = original(**kwargs).parsed.model_dump()
        body["factor_proposals"] = [{"name": "中期动量", "expression": "rank(ret(60))", "horizon_sessions": 5,
                                     "hypothesis": "60 日相对强势在 5 日内延续"}]
        return answer(PostmarketReview.model_validate(body), "fixture/pro")
    strategy.agents["manager"].run = manager
    strategy.after_market_closes()
    tested = events(strategy, "factor_tests")[0]
    assert tested["results"][0]["status"] == "validated" and tested["session_date"] == "2026-11-02"
    context = strategy._factor_context(NOW)
    assert context["validated"][0]["name"] == "中期动量"
    assert AutonomousFund._arm_context({"factor_lab": context}, "baseline")["factor_lab"] is None  # Control arm.
    assert AutonomousFund._arm_context({"factor_lab": context}, "memo")["factor_lab"] is context


def test_factor_lab_failures_never_break_the_review(tmp_path):
    from scripts.autonomous_fund.review import PostmarketReview
    from .test_autonomous_fund_review import answer, events, review_harness
    strategy, _ = review_harness(tmp_path)
    strategy.factor_lab = FactorLab(strategy.journal, SETTINGS)
    strategy._factor_panel = lambda now: (_ for _ in ()).throw(ConnectionError("history down"))
    original = strategy.agents["manager"].run

    def manager(**kwargs):
        body = original(**kwargs).parsed.model_dump()
        body["factor_proposals"] = [{"name": "m", "expression": "rank(ret(60))", "horizon_sessions": 5,
                                     "hypothesis": "h"}]
        return answer(PostmarketReview.model_validate(body), "fixture/pro")
    strategy.agents["manager"].run = manager
    strategy.after_market_closes()
    assert events(strategy, "postmarket_review") and events(strategy, "factor_lab_unavailable")


def test_factor_lab_command_runs_on_recorded_market_data(tmp_path, capsys):
    from pathlib import Path
    from scripts.autonomous_fund import run
    root = Path(__file__).resolve().parents[1]
    run.main(["factor-lab", "--run-dir", str(tmp_path), "--data-dir", str(root / "data"),
              "--symbols", "SPY,TLT,GLD,IEF,DJP", "--expression", "rank(ret(20))", "--horizon", "5",
              "--min-names", "5"])
    out = capsys.readouterr().out
    result = json.loads(out[out.index("{"):])
    assert result["first_close"] == "2019-01-02" and result["names"] == 5
    assert result["status"] in {"validated", "rejected", "insufficient_data"}
    assert result["in_sample"]["n"] > 0


def signal_panel(flip_at=None, names=20, days=600, seed=4):
    """Volume carries a signal for the next day's return; after flip_at the relation reverses."""
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 1, (days, names))
    sign = np.ones(days)
    if flip_at is not None:
        sign[flip_at:] = -1
    returns = rng.normal(0, 0.012, (days, names))
    returns[1:] += 0.004 * sign[1:, None] * z[:-1]
    index = pd.bdate_range("2023-01-02", periods=days)
    columns = [f"S{i:02d}" for i in range(names)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(returns, axis=0)), index=index, columns=columns)
    return close, pd.DataFrame(1e6 * np.exp(0.5 * z), index=index, columns=columns)


def test_dates_are_sampled_without_overlapping_forward_returns():
    from scripts.autonomous_fund.factors import daily_ic
    close, volume = panel()
    for horizon in (1, 5, 20):
        valid = len(daily_ic(compute("rank(ret(60))", close, volume), close, horizon, SETTINGS.min_names))
        result = evaluate("rank(ret(60))", horizon, close, volume, SETTINGS, trials=1)
        assert result["in_sample"]["n"] + result["holdout"]["n"] == -(-valid // horizon)


def test_an_in_sample_edge_that_the_holdout_contradicts_is_rejected():
    close, volume = signal_panel()
    assert evaluate("rank(volume)", 1, close, volume, SETTINGS, trials=1)["status"] == "validated"
    close, volume = signal_panel(flip_at=440)  # The holdout starts near session 420.
    result = evaluate("rank(volume)", 1, close, volume, SETTINGS, trials=1)
    assert result["in_sample"]["t"] > result["critical_t"]
    assert result["status"] == "rejected" and "holdout" in result["reason"]


def test_favored_names_follow_the_validated_direction(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-direction"), SETTINGS)
    close, volume = panel()
    lab.test([proposal("-rank(ret(60))", name="inverted")], NOW, "2026-11-02", close, volume)
    validated = lab.context(NOW, close, volume)["validated"][0]
    assert validated["direction"] == -1
    leaders = set(close.iloc[-1].div(close.iloc[-61]).sort_values().tail(5).index)
    assert {row["symbol"] for row in validated["live"]["favored"]} == leaders


def test_the_strategy_panel_never_includes_todays_forming_bar(tmp_path):
    from types import SimpleNamespace
    from scripts.autonomous_fund.strategy import AutonomousFund
    days = pd.date_range("2026-10-26 16:00", periods=6, freq="D", tz="America/New_York")  # Last one is "today".
    frame = pd.DataFrame({"close": np.arange(100.0, 106.0), "volume": np.full(6, 1e6)}, index=days)
    journal = Journal(tmp_path, "synthetic-panel")
    fake = SimpleNamespace(universe={"ALFA", "BETA"}, _factor_cache=None, journal=journal,
                           factor_lab=FactorLab(journal, SETTINGS),
                           get_historical_prices=lambda asset, length, timestep: SimpleNamespace(df=frame))
    close, volume = AutonomousFund._factor_panel(fake, days[-1].to_pydatetime())
    assert list(close.columns) == ["ALFA", "BETA"] and len(close) == 5 and close.iloc[-1, 0] == 104.0
    assert volume.iloc[-1, 0] == 1e6
