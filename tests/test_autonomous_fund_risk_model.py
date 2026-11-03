"""Correlation estimation and historical scenario stress tests of the quantitative risk engine.

Synthetic, seeded histories and neutral symbols only; no network or model.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.quant import QuantPolicy, RiskEstimate, construct, window_return
from scripts.autonomous_fund.risk import Limits, RiskError, number

LIMITS = Limits()
UNIVERSE = {"ALFA": "stock", "BETA": "stock", "GAMMA": "stock", "SPY": "unlevered_etf"}
DAYS = pd.bdate_range("2025-01-02", periods=300, tz="America/New_York")


def rows(returns):
    prices = 100 * np.cumprod(1 + np.asarray(returns))
    return [(str(day.date()), float(p)) for day, p in zip(DAYS, prices)]


def correlated(seed=1):
    rng = np.random.default_rng(seed)
    market = rng.normal(0, .01, len(DAYS))
    return {s: rows(beta * market + rng.normal(0, .008, len(DAYS)))
            for s, beta in (("ALFA", 1.2), ("BETA", .8), ("GAMMA", .3), ("SPY", 1.0))}


def policy(**settings):
    return QuantPolicy.from_config(settings, UNIVERSE, LIMITS)


def test_full_shrinkage_makes_every_defined_correlation_the_average_and_stays_psd():
    estimate = RiskEstimate(correlated(), policy(correlation_shrinkage="1"))
    corr = estimate.correlation
    off = corr[~np.eye(len(corr), dtype=bool)]
    assert np.allclose(off, off.mean())
    assert np.linalg.eigvalsh(estimate.cov).min() > -1e-12


def test_no_shrinkage_with_the_volatility_window_reproduces_the_plain_ewma_estimate():
    frames = correlated()
    plain = policy(correlation_shrinkage="0", correlation_lambda="0.94", correlation_lookback_days=63)
    estimate = RiskEstimate(frames, plain)
    symbols = list(estimate.symbols)
    returns = np.array([[b / a - 1 for (_, a), (_, b) in zip(frames[s], frames[s][1:])] for s in symbols]).T[-63:]
    w = np.array([.06 * .94 ** k for k in range(63)][::-1])
    w /= w.sum()
    cov = (returns * w[:, None]).T @ returns
    expected = cov / np.sqrt(np.outer(np.diag(cov), np.diag(cov)))
    assert np.allclose(estimate.correlation, expected)


def test_a_short_correlation_break_moves_the_default_estimate_less_than_a_fast_ewma():
    rng = np.random.default_rng(5)
    market = rng.normal(0, .01, len(DAYS))
    noise = rng.normal(0, .004, len(DAYS))
    alfa = market + noise
    beta = market + rng.normal(0, .004, len(DAYS))
    beta[-20:] = -market[-20:] + noise[-20:]  # A 20-day break in a long, strong co-movement.
    frames = {"ALFA": rows(alfa), "BETA": rows(beta)}
    fast = RiskEstimate(frames, policy(correlation_shrinkage="0", correlation_lambda="0.94",
                                       correlation_lookback_days=63)).correlation[0, 1]
    default = RiskEstimate(frames, policy()).correlation[0, 1]
    assert default > fast + 0.2


def test_flat_series_stay_perfectly_correlated_under_shrinkage():
    frames = correlated()
    frames["GAMMA"] = [(day, 100.0) for day, _ in frames["GAMMA"]]
    estimate = RiskEstimate(frames, policy(correlation_shrinkage="1"))
    g = estimate.symbols.index("GAMMA")
    assert all(estimate.correlation[g, j] == 1 for j in range(len(estimate.symbols)))


SCENARIOS = {"crash": {"benchmark": -0.30, "returns": {"ALFA": -0.50, "BETA": -0.20, "SPY": -0.30, "GAMMA": None}}}


def test_scenario_loss_scales_the_portfolio_to_its_limit():
    p = policy(max_name_risk="1", max_etf_name_risk="1", target_portfolio_vol="5", max_portfolio_es="5",
               max_scenario_loss="0.2")
    estimate = RiskEstimate(correlated(), p, scenarios=SCENARIOS)
    targets = {"ALFA": number("0.1"), "BETA": number("0.1"), "SPY": number("0.1")}  # max_position is 0.10.
    weights, report = construct(targets, {}, estimate, p, LIMITS, LIMITS.max_exposure)
    assert report["requested_metrics"]["scenario_losses"]["crash"] == pytest.approx(0.05 + 0.02 + 0.03)
    assert "scenario_stress" not in report["binding"]
    p = policy(max_name_risk="1", max_etf_name_risk="1", target_portfolio_vol="5", max_portfolio_es="5",
               max_scenario_loss="0.05")
    weights, report = construct(targets, {}, RiskEstimate(correlated(), p, scenarios=SCENARIOS), p, LIMITS,
                                LIMITS.max_exposure)
    assert "scenario_stress" in report["binding"]
    assert report["constructed_metrics"]["worst_scenario_loss"] <= 0.05 + 1e-5
    assert all(weights[s] <= targets[s] for s in weights)


def test_a_name_without_scenario_history_is_proxied_pessimistically_and_disclosed():
    p = policy()
    estimate = RiskEstimate(correlated(), p, scenarios=SCENARIOS)
    metrics = estimate.metrics({"GAMMA": .1})
    # GAMMA's beta is about 0.3, but a missing window never assumes less than the benchmark's loss.
    assert metrics["scenario_losses"]["crash"] == pytest.approx(0.03)
    assert metrics["scenario_proxied"] == {"crash": ["GAMMA"]}
    unknown = estimate.metrics({"NEWCO": .1})  # No history at all: pessimistic volatility ratio.
    assert unknown["scenario_losses"]["crash"] > 0.03


def test_a_scenario_without_benchmark_history_is_skipped_and_disclosed():
    estimate = RiskEstimate(correlated(), policy(), scenarios={"old": {"benchmark": None, "returns": {"ALFA": -0.4}}})
    metrics = estimate.metrics({"ALFA": .1})
    assert metrics["scenario_losses"] == {} and metrics["scenarios_unavailable"] == ["old"]
    assert metrics["worst_scenario_loss"] is None


def test_window_return_needs_coverage_of_the_whole_window():
    series = [(str(d.date()), 100.0 + i) for i, d in enumerate(DAYS)]
    start, end = str(DAYS[10].date()), str(DAYS[50].date())
    assert window_return(series, start, end) == pytest.approx(150 / 110 - 1)
    assert window_return(series[30:], start, end) is None  # Listed after the window began.
    assert window_return(series[:40], start, end) is None  # History stops before the window ended.


def test_scenario_policy_validates_windows():
    with pytest.raises(RiskError, match="stress_scenarios"):
        policy(stress_scenarios={"bad": ["2020-03-23", "2020-02-19"]})
    with pytest.raises(RiskError, match="stress_scenarios"):
        policy(stress_scenarios={"bad": ["not-a-date", "2020-02-19"]})
    custom = policy(stress_scenarios={"mine": ["2020-02-19", "2020-03-23"]})
    assert custom.stress_scenarios == (("mine", "2020-02-19", "2020-03-23"),)
    assert {name for name, _, _ in policy().stress_scenarios} == {"gfc_2008", "covid_2020", "rates_2022"}


def test_strategy_fetches_each_scenario_history_once_and_never_uses_an_unfinished_window(tmp_path):
    from tests.test_autonomous_fund_quant import harness
    days = pd.bdate_range("2019-12-02", "2020-06-30", tz="America/New_York")
    prices = {"ALFA": np.linspace(100, 60, len(days)), "SPY": np.linspace(100, 80, len(days))}
    frames = {s: [(str(d.date()), float(p)) for d, p in zip(days, values)] for s, values in prices.items()}
    quant = {"stress_scenarios": {"covid_2020": ["2020-02-19", "2020-03-23"], "later": ["2026-10-01", "2027-01-01"]}}
    strategy = harness(tmp_path, frames=frames, quant=quant)
    calls = []
    fetch = strategy.get_historical_prices
    strategy.get_historical_prices = lambda asset, length, timestep="day": calls.append(asset.symbol) or fetch(
        asset, length, timestep)
    now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    first = strategy._scenario_returns({"ALFA", "SPY"}, now)
    assert set(first) == {"covid_2020"}  # The 2026-27 window has not ended.
    assert first["covid_2020"]["returns"]["ALFA"] < first["covid_2020"]["benchmark"] < 0
    again = strategy._scenario_returns({"ALFA", "SPY"}, now)
    assert again == first and sorted(calls) == ["ALFA", "SPY"]  # Persisted: one fetch per symbol.
    estimate = strategy._risk_estimate({"ALFA"}, now)
    assert estimate.metrics({"ALFA": .1})["scenario_losses"]["covid_2020"] > 0


def test_an_empty_read_is_retried_and_a_stored_gap_is_rechecked_after_a_month(tmp_path):
    from datetime import timedelta
    from tests.test_autonomous_fund_quant import harness
    quant = {"stress_scenarios": {"covid_2020": ["2020-02-19", "2020-03-23"]}}
    strategy = harness(tmp_path, frames={}, quant=quant)
    calls = []
    strategy.get_historical_prices = lambda asset, length, timestep="day": calls.append(asset.symbol) or None
    now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    first = strategy._scenario_returns({"ALFA"}, now)
    assert first["covid_2020"]["returns"]["ALFA"] is None  # Unknown today...
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_scenario_returns").fetchone()[0] == 0  # ...but not stored.
    days = pd.bdate_range("2020-03-02", "2020-06-30", tz="America/New_York")  # Listed after the crash began.
    late = pd.DataFrame({"close": np.linspace(50, 60, len(days))}, index=days)
    strategy.get_historical_prices = lambda asset, length, timestep="day": calls.append(asset.symbol) or SimpleNamespace(df=late)
    strategy._scenario_failed = set()
    strategy._scenario_returns({"ALFA"}, now + timedelta(days=1))
    count = len(calls)
    strategy._scenario_returns({"ALFA"}, now + timedelta(days=2))
    assert len(calls) == count  # A real gap is stored...
    strategy._scenario_returns({"ALFA"}, now + timedelta(days=35))
    assert len(calls) > count  # ...and rechecked after a month.
