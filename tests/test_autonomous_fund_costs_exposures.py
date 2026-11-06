"""Model cost accounting and beta/style exposure limits.

Synthetic usage records, synthetic seeded histories and neutral symbols only; no network, no model.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.costs import call_cost, cost_summary, rate_for, token_counts
from scripts.autonomous_fund.risk import RiskError
from tests.test_autonomous_fund_quant import LIMITS, NOW, UNIVERSE, account, closes, harness, rows

PRICES = {"m/research": {"input": 0.3, "output": 1.2, "cached_input": 0.03}, "m/decide": {"input": 1.0, "output": 4.0}}


# ----- Cost accounting ------------------------------------------------------------------------------------

def test_token_counts_read_every_provider_spelling_of_cached_tokens():
    deepseek = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 900,
                "prompt_cache_miss_tokens": 100}
    litellm = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 800}}
    anthropic = {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 700}
    assert [token_counts(u)["cached_input_tokens"] for u in (deepseek, litellm, anthropic)] == [900, 800, 700]
    assert token_counts({"prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 70})["input_tokens"] == 100
    assert token_counts(None) == {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}


def test_cached_tokens_use_the_cheaper_rate_and_default_to_the_input_rate():
    counts = {"input_tokens": 1_000_000, "cached_input_tokens": 900_000, "output_tokens": 100_000}
    assert call_cost(counts, PRICES["m/research"]) == pytest.approx(0.1 * 0.3 + 0.9 * 0.03 + 0.1 * 1.2)
    assert call_cost(counts, PRICES["m/decide"]) == pytest.approx(1.0 + 0.4)  # No cached rate: priced as input.
    assert rate_for({"input": 1, "output": 2}, "m/x", models_used=2) is None  # A legacy single rate, two models.
    assert call_cost(counts, None) is None


def test_the_summary_gives_daily_cost_the_annual_pace_and_the_break_even_excess_return():
    def call(day, role, cost, **extra):
        return {"at": f"2026-11-0{day}T15:00:00+00:00", "kind": "agent",
                "body": {"name": role, "model": "m/research", "usage": {"prompt_tokens": 10}, "cost_usd": cost, **extra}}
    rows = [call(2, "researcher", 1.0), call(2, "manager", 2.0), call(3, "manager", 1.0),
            call(3, "critic", 9.0, mock=True), call(3, "critic", 9.0, cache_hit=True),  # Never billed.
            call(3, "manager", None)]  # Unpriced: counted as unpriced, never as free.
    summary = cost_summary(rows, equity=10_000, now=NOW + timedelta(days=1))
    assert summary["today_usd"] == 1.0 and summary["window_usd"] == 4.0 and summary["days_with_calls"] == 2
    assert summary["by_role_usd"] == {"manager": 3.0, "researcher": 1.0} and summary["unpriced_calls"] == 1
    assert summary["annual_pace_usd"] == 504.0 and summary["break_even_excess_return"] == pytest.approx(0.0504)
    assert not summary["complete"]


def test_each_call_records_its_tokens_and_cost_and_the_daily_counter(tmp_path):
    strategy = harness(tmp_path, mode="local")
    strategy.scripted, strategy.config = False, {"model_prices_per_million": PRICES}
    strategy.agent_models = {"researcher": "m/research", "manager": "m/decide"}
    result = SimpleNamespace(model="m/research", cache_hit=False, summary="", parse_error=None, parsed={"ok": 1},
                             usage={"prompt_tokens": 1_000_000, "completion_tokens": 0,
                                    "prompt_cache_hit_tokens": 1_000_000})
    strategy._record_agent("researcher", result)
    body = next(r["body"] for r in strategy.journal.all_events() if r["kind"] == "agent")
    assert body["cost_usd"] == pytest.approx(0.03) and body["tokens"]["cached_input_tokens"] == 1_000_000
    assert strategy.journal.counter("model_cost_microusd", NOW) == 30_000


def test_the_daily_cost_cap_stops_new_inference_but_never_the_protections(tmp_path):
    from scripts.autonomous_fund.strategy import ModelBudget
    strategy = harness(tmp_path, mode="local")
    strategy.model_budget = ModelBudget(max_daily_cost_usd="1")
    strategy.journal.counter("model_cost_microusd", NOW, 1_200_000)
    strategy._agent_call = lambda *a, **k: pytest.fail("no model call past the cost cap")
    strategy._decide(account(10_000), NOW, False)
    notices = [kind for kind, _ in strategy.notices]
    assert "model_cost_budget_exhausted" in notices
    with pytest.raises(RiskError, match="positive amount"):
        ModelBudget(max_daily_cost_usd="0")


def test_a_cost_cap_without_prices_is_refused_at_startup(tmp_path):
    from pathlib import Path
    from scripts.autonomous_fund.strategy import configuration
    path = Path(__file__).resolve().parents[1] / "scripts" / "autonomous_fund" / "config.json"
    with pytest.raises(RiskError, match="FUND_MODEL_PRICES_JSON rates for"):
        configuration(path, environ={"FUND_MAX_DAILY_COST_USD": "2"})
    prices = '{"deepseek/deepseek-flash":{"input":1,"output":1},"deepseek/deepseek-v4-pro":{"input":1,"output":1}}'
    assert configuration(path, environ={"FUND_MAX_DAILY_COST_USD": "2", "FUND_MODEL_PRICES_JSON": prices})


def test_the_status_line_states_the_break_even_in_plain_words():
    from scripts.autonomous_fund.status import cost_line
    line = cost_line({"calls": 3, "window_days": 30, "days_with_calls": 2, "today_usd": 1.0, "window_usd": 4.0,
                      "average_per_day_usd": 2.0, "annual_pace_usd": 504.0, "break_even_excess_return": 0.0504,
                      "unpriced_calls": 1})
    assert "一年约 $504" in line and "占净值 5.04%" in line and "跑赢基准" in line and "1 次调用没有单价" in line
    assert "无法估算" in cost_line({"calls": 2, "window_days": 30, "days_with_calls": 0})


# ----- Beta and style exposure limits ---------------------------------------------------------------------

def estimate_with(styles=None, betas=None, quant=None):
    from scripts.autonomous_fund.quant import QuantPolicy, RiskEstimate
    policy = QuantPolicy.from_config(quant, UNIVERSE, LIMITS)
    histories = {"SPY": rows(closes(.004, 1, beta=1.0)), "ALFA": rows(closes(.004, 2, beta=(betas or {}).get("ALFA", 2.2))),
                 "BETA": rows(closes(.004, 3, beta=(betas or {}).get("BETA", 2.0))),
                 "GAMMA": rows(closes(.004, 4, beta=(betas or {}).get("GAMMA", -0.5))),
                 "DELTA": rows(closes(.004, 5, beta=0.3))}
    return RiskEstimate(histories, policy, styles=styles), policy


def test_a_high_beta_portfolio_is_scaled_down_by_its_high_beta_names_only():
    from scripts.autonomous_fund.quant import construct
    estimate, policy = estimate_with(quant={"max_portfolio_beta": "0.2", "target_portfolio_vol": "1",
                                            "max_name_risk": "0.5", "max_portfolio_es": "1"})
    targets = {"ALFA": 0.1, "BETA": 0.1, "GAMMA": 0.1, "DELTA": 0.1}
    weights, overlay = construct(targets, {}, estimate, policy, LIMITS, 1)
    assert "portfolio_beta" in overlay["binding"] and overlay["constructed_exposures"]["beta"] <= 0.2 + 1e-6
    assert float(weights["GAMMA"]) == pytest.approx(0.1)  # A negative-beta name never pushes beta up.
    assert overlay["requested_exposures"]["beta"] > 0.2


def test_a_style_tilt_past_the_limit_scales_the_names_pushing_it_on_either_side():
    from scripts.autonomous_fund.quant import construct
    styles = {"momentum": {"ALFA": 2.0, "BETA": 1.5, "GAMMA": -1.0, "DELTA": 0.0}}
    estimate, policy = estimate_with(styles, betas={"ALFA": 0.2, "BETA": 0.2, "GAMMA": 0.2},
                                     quant={"max_style_exposure": "0.2", "target_portfolio_vol": "1",
                                            "max_name_risk": "0.5", "max_portfolio_es": "1"})
    weights, overlay = construct({"ALFA": 0.1, "BETA": 0.1, "GAMMA": 0.1, "DELTA": 0.1}, {}, estimate, policy, LIMITS, 1)
    assert overlay["requested_exposures"]["momentum"] == pytest.approx(0.25)
    assert overlay["constructed_exposures"]["momentum"] == pytest.approx(0.2, abs=1e-5)
    assert float(weights["GAMMA"]) == pytest.approx(0.1) and float(weights["DELTA"]) == pytest.approx(0.1)
    assert float(weights["ALFA"]) / float(weights["BETA"]) == pytest.approx(1.0, abs=1e-4)  # Pro rata.
    negative = {"momentum": {"ALFA": -2.0, "BETA": -1.5, "GAMMA": 1.0, "DELTA": 0.0}}
    estimate, policy = estimate_with(negative, betas={"ALFA": 0.2, "BETA": 0.2, "GAMMA": 0.2},
                                     quant={"max_style_exposure": "0.2", "target_portfolio_vol": "1",
                                            "max_name_risk": "0.5", "max_portfolio_es": "1"})
    _, overlay = construct({"ALFA": 0.1, "BETA": 0.1, "GAMMA": 0.1}, {}, estimate, policy, LIMITS, 1)
    assert overlay["constructed_exposures"]["momentum"] == pytest.approx(-0.2, abs=1e-5)
    assert "style:momentum" in overlay["binding"]


def test_style_scores_come_from_completed_sessions_and_wait_for_the_whole_universe(tmp_path):
    from scripts.autonomous_fund.quant import QuantPolicy, style_values, style_zscores
    assert style_zscores({"A": 1.0, "B": 1.0}) == {"A": 0.0, "B": 0.0}
    z = style_zscores({"A": 0.0, "B": 1.0, "C": 2.0, "D": 100.0})
    assert max(z.values()) <= 3.0 and z["A"] < z["B"] < z["C"]
    short = [(f"d{i}", 100.0 + i, 1e6) for i in range(70)]
    values = style_values(short)
    assert "momentum" not in values and values["volatility"] > 0 and "size" in values
    assert "size" not in style_values(short, stock=False)  # ETFs are neutral on size.
    from tests.test_autonomous_fund_quant import default_frames
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy._may_prepare = lambda: False
    assert strategy._style_scores(NOW) == ({}, False)  # Nothing fetched and no budget: not cached.
    strategy._may_prepare = lambda: True
    styles, complete = strategy._style_scores(NOW)
    assert complete and set(styles["volatility"]) == UNIVERSE and strategy._style_scores(NOW)[0] is styles


def test_the_risk_context_shows_exposures_limits_and_name_scores(tmp_path):
    from tests.test_autonomous_fund_quant import default_frames
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy.execution.policy = SimpleNamespace(auto_daily_exposure_increase="0.2", auto_daily_buy_turnover="0.3")
    context = strategy._risk_context(account(9_000, {"ALFA": 10}), NOW)
    exposures = context["exposures"]
    assert exposures["max_portfolio_beta"] == "1.0" and exposures["max_style_exposure"] == "0.75"
    assert set(exposures["current"]) >= {"beta", "volatility"} and "beta" in exposures["name_scores"]["ALFA"]
    assert "pro rata" in exposures["meaning"]


def test_the_critic_is_skipped_at_the_cost_cap(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund, ModelBudget
    from tests.test_autonomous_fund_construction import critic_strategy
    fake, _, now = critic_strategy(tmp_path, run=lambda **kw: pytest.fail("no critique past the cost cap"))
    fake.model_budget, notices = ModelBudget(max_daily_cost_usd="1"), []
    # Intercept the app outbox without replacing the native notify contract.
    fake._notify_fund = lambda kind, details: notices.append(kind)
    fake.journal.counter("model_cost_microusd", now, 2_000_000)
    assert AutonomousFund._critique(fake, {}, {"summary": "r", "evidence": []}, fake.model_budget, now) is None
    assert notices == ["model_cost_budget_exhausted"]
    assert any(r["kind"] == "critic_skipped" and r["body"]["reason"] == "model_cost_budget"
               for r in fake.journal.all_events())
