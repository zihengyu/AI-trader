"""Risk-budget construction and its paired evaluation against the AI's own weights.

Synthetic, seeded histories and neutral symbols only; no network or model.
"""
from datetime import date, timedelta

import numpy as np
import pytest

from scripts.autonomous_fund.construction import ConstructionSettings, evaluate, pairs
from scripts.autonomous_fund.quant import QuantPolicy, RiskEstimate, risk_budget, risk_contributions
from scripts.autonomous_fund.risk import Limits, RiskError, number
from tests.test_autonomous_fund_risk_model import DAYS, rows

LIMITS = Limits()


def estimate(vols=(0.02, 0.01, 0.005), corr=0.3, seed=4):
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 1, len(DAYS))
    frames = {}
    for i, vol in enumerate(vols):
        own = np.sqrt(corr) * common + np.sqrt(1 - corr) * rng.normal(0, 1, len(DAYS))
        frames[f"N{i}"] = rows(vol * own)
    if corr < 0:
        frames["N1"] = rows(-vols[1] * common)
    universe = {s: "stock" for s in frames}
    policy = QuantPolicy.from_config({"correlation_shrinkage": "0"}, universe, LIMITS)
    return RiskEstimate(frames, policy)


def test_risk_contributions_become_proportional_to_the_ai_weights_at_the_same_gross():
    est = estimate()
    ai = {"N0": number("0.10"), "N1": number("0.06"), "N2": number("0.04")}
    budgeted = risk_budget(ai, est)
    assert float(sum(budgeted.values())) == pytest.approx(0.20, abs=1e-5)
    shares = risk_contributions(budgeted, est)
    assert shares["N0"] == pytest.approx(0.5, abs=1e-3) and shares["N2"] == pytest.approx(0.2, abs=1e-3)
    assert budgeted["N2"] > ai["N2"] and budgeted["N0"] < ai["N0"]  # The low-volatility name gets more weight.
    assert risk_contributions(ai, est)["N0"] > 0.7  # The AI's raw weights concentrate risk in the volatile name.


def test_risk_budgeting_handles_negative_correlation_and_names_without_history():
    est = estimate(corr=-0.5)
    budgeted = risk_budget({"N0": number("0.05"), "N1": number("0.05"), "NEWCO": number("0.03")}, est)
    assert all(w > 0 for w in budgeted.values()) and budgeted["NEWCO"] == number("0.03")
    assert risk_budget({"N0": number("0.05")}, est) == {"N0": number("0.05")}


def variant(day, ai, budgeted):
    return (f"{day.isoformat()}T15:00:00+00:00", {"ai": ai, "risk_budget": budgeted})


def test_auto_mode_switches_only_on_sufficient_significant_evidence():
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(60)]
    # LOW quietly outperforms; the risk-budget variant holds more of it.
    closes = {"SPY": [(d.isoformat(), 100 + 0.1 * i + (0.3 if i % 2 else 0)) for i, d in enumerate(days)],
              "HIGH": [(d.isoformat(), 100 + 0.1 * i + (0.6 if i % 2 else 0)) for i, d in enumerate(days)],
              "LOW": [(d.isoformat(), 100 * 1.002 ** i) for i, d in enumerate(days)]}
    variants = [variant(d, {"HIGH": "0.08", "LOW": "0.02"}, {"HIGH": "0.03", "LOW": "0.07"}) for d in days[30:58]]
    settings = ConstructionSettings(min_pairs=20)
    result = evaluate(variants, closes, settings, "ai")
    assert result["test"]["pairs"] >= 20 and result["test"]["mean"] > 0 and result["champion"] == "risk_budget"
    assert evaluate(variants[:5], closes, settings, "ai")["champion"] == "ai"  # Too few sessions.
    assert evaluate(variants, closes, ConstructionSettings(mode="ai"), "risk_budget")["champion"] == "ai"
    assert len(pairs(variants + variants[-1:], closes)) == len(pairs(variants, closes))  # One pair per session.


def test_construction_settings_validate():
    with pytest.raises(RiskError):
        ConstructionSettings.from_config({"mode": "optimizer"})
    with pytest.raises(RiskError):
        ConstructionSettings.from_config({"min_pairs": 1})


def test_strategy_records_both_variants_and_trades_the_champion(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, accept, account, decision, default_frames, harness, quote
    strategy = harness(tmp_path, default_frames(ALFA=.02, BETA=.004))
    strategy.construction = ConstructionSettings()
    strategy._record_construction_variant(decision([("ALFA", .05), ("BETA", .05)]), NOW)
    (recorded,) = [r["body"] for r in strategy.journal.all_events() if r["kind"] == "construction_variant"]
    assert recorded["trading"] == "ai" and number(recorded["risk_budget"]["BETA"]) > number(recorded["ai"]["BETA"])
    strategy.journal.observe_session(NOW, account())
    active = accept(strategy, [("ALFA", .05), ("BETA", .05)])
    prices = {"ALFA": quote(), "BETA": quote()}
    assert strategy._candidates(active, account(), prices, NOW)["overlay"]["construction"] == "ai"
    strategy.journal.set_meta("construction_champion", "risk_budget")
    strategy._estimates = {}
    result = strategy._candidates(active, account(), prices, NOW)
    assert result["overlay"]["construction"] == "risk_budget"
    requested = {s: number(w) for s, w in result["overlay"]["requested"].items()}
    assert requested["BETA"] > requested["ALFA"]  # The calmer name carries more weight under risk budgets.


# ----- Devil's-advocate critic ----------------------------------------------------------------

def critic_strategy(tmp_path, run=None, runs_used=0):
    from types import SimpleNamespace
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.strategy import AutonomousFund, CriticSettings, Critique, ModelBudget
    from tests.test_autonomous_fund_quant import NOW
    journal = Journal(tmp_path, "synthetic-critic")
    journal.counter("agent_runs", NOW, runs_used)
    answer = Critique(summary="The momentum thesis is crowded.", concerns=[
        {"symbol": "ALFA", "concern": "Valuation already prices the growth.", "evidence": "synthetic"}])
    agent = SimpleNamespace(run=run or (lambda **kw: SimpleNamespace(parsed=answer, parse_error=None, model="m/x",
                                                                      cache_hit=False, usage={}, summary="")))
    fake = SimpleNamespace(critic=CriticSettings(), _critic_ready=True, journal=journal, agents={"critic": agent},
                           get_datetime=lambda: NOW, scripted=True, agent_models={"critic": "m/x"})
    fake._record_agent = lambda name, result, phase=None: AutonomousFund._record_agent(fake, name, result, phase)
    fake._agent_call = lambda name, **kw: AutonomousFund._agent_call(fake, name, **kw)
    fake._cost_exhausted = lambda now, step: AutonomousFund._cost_exhausted(fake, now, step)
    return fake, ModelBudget(), NOW


def test_the_critique_reaches_the_manager_context(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    fake, budget, now = critic_strategy(tmp_path)
    critique = AutonomousFund._critique(fake, {"as_of": now.isoformat()}, {"summary": "r", "evidence": []}, budget, now)
    assert critique["concerns"][0]["symbol"] == "ALFA" and critique["summary"].startswith("The momentum")


def test_a_failed_or_unaffordable_critique_never_blocks_the_decision(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund, CriticSettings
    def broken(**kwargs):
        raise ConnectionError("model down")
    fake, budget, now = critic_strategy(tmp_path / "broken", run=broken)
    assert AutonomousFund._critique(fake, {}, {}, budget, now) is None
    assert any(r["kind"] == "critic_unavailable" for r in fake.journal.all_events())
    poor, budget, now = critic_strategy(tmp_path / "poor", runs_used=budget.max_agent_runs_per_day - 5)
    assert AutonomousFund._critique(poor, {}, {}, budget, now) is None
    assert any(r["kind"] == "critic_skipped" for r in poor.journal.all_events())
    off, budget, now = critic_strategy(tmp_path / "off")
    off.critic = CriticSettings(enabled=False)
    assert AutonomousFund._critique(off, {}, {}, budget, now) is None


def test_critic_settings_validate():
    from scripts.autonomous_fund.strategy import CriticSettings
    assert CriticSettings.from_config({"model": "deepseek/deepseek-flash"}).model == "deepseek/deepseek-flash"
    with pytest.raises(RiskError):
        CriticSettings.from_config({"model": "no-prefix"})
    with pytest.raises(RiskError):
        CriticSettings.from_config({"enabled": "yes"})
