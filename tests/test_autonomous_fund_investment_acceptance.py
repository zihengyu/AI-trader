"""V2 4.4: investment-capability acceptance, separate from the operational verdict.

Synthetic return series, benchmarks and costs only; no network, broker or model inference.
"""
import math
import random

from scripts.autonomous_fund.performance import evidence
from scripts.autonomous_fund.validation import investment_acceptance


def series(n, alpha, beta=0.8, seed=3, flip_second_half=False):
    rng = random.Random(seed)
    market = [rng.gauss(0.0004, 0.01) for _ in range(n)]
    returns = []
    for i, m in enumerate(market):
        a = -alpha if flip_second_half and i >= n // 2 else alpha
        returns.append(a + beta * m + rng.gauss(0, 0.002))
    return returns, market


def net(excess=0.05, alpha_t=3.0, system_annual=0.20, benchmark_annual=0.08, system_status="computed"):
    system = ({"status": "computed", "annual_return": system_annual} if system_status == "computed"
              else {"status": "unknown_costs", "sessions_with_unknown_costs": 2})
    return {"returns": {"account": {"status": "computed"}, "system": system},
            "benchmarks": [{"id": "risk_matched-x", "name": "risk_matched", "current": True, "status": "computed",
                            "annual_excess_return": excess, "regression": {"alpha_t": alpha_t, "alpha_t_newey_west": alpha_t},
                            "benchmark": {"annual_return": benchmark_annual}}]}


LEARNING = {"memo_minus_baseline_1d_excess": {"sufficient": False, "t_stat": 0.4, "pairs": 8}}


def verdict(returns, market, **kwargs):
    stats = evidence(returns, market, 1)
    result = investment_acceptance(stats, returns, market, kwargs.pop("net", net()), LEARNING,
                                   {"verdict": "insufficient_sample"}, {"by_purpose": {}, "calls_saved": {}})
    return result, {c["criterion"]: c for c in result["criteria"]}


def test_fewer_than_sixty_sessions_are_never_judged():
    result, criteria = verdict(*series(40, 0.002))
    assert result["verdict"] == "insufficient_evidence" and not criteria["min_sessions"]["passed"]
    assert "运行验收通过不代表投资能力验收通过" in result["notice"]


def test_a_steady_beta_adjusted_edge_that_survives_costs_is_only_a_preliminary_edge():
    result, criteria = verdict(*series(250, 0.002))
    assert result["verdict"] == "preliminary_edge", [c for c in criteria.values() if c["required"] and not c["passed"]]
    assert criteria["alpha_t_vs_spy"]["passed"] and criteria["both_halves_beta_adjusted_positive"]["passed"]
    assert math.isclose(criteria["system_net_excess_annual"]["value"], 0.12)
    # Gate 4 and the ideas' calibration are reported, not required.
    assert not criteria["learning_beats_baseline_t"]["passed"] and not criteria["learning_beats_baseline_t"]["required"]
    assert not criteria["research_ideas_informative"]["required"]


def test_an_edge_from_one_half_of_the_record_is_not_stable():
    result, criteria = verdict(*series(120, 0.004, flip_second_half=True))
    halves = criteria["both_halves_beta_adjusted_positive"]
    assert not halves["passed"] and halves["value"][0] > 0 > halves["value"][1]
    assert result["verdict"] == "edge_not_shown"


def test_unknown_costs_or_a_lost_benchmark_comparison_show_no_edge():
    returns, market = series(250, 0.002)
    result, criteria = verdict(returns, market, net=net(system_status="unknown"))
    assert criteria["system_net_excess_annual"]["value"] == "unknown_costs" and result["verdict"] == "edge_not_shown"
    result, criteria = verdict(returns, market, net=net(system_annual=0.07))  # Costs ate the excess.
    assert not criteria["system_net_excess_annual"]["passed"] and result["verdict"] == "edge_not_shown"
    result, criteria = verdict(returns, market, net=net(alpha_t=1.2))
    assert not criteria["risk_matched_excess_annual"]["passed"] and result["verdict"] == "edge_not_shown"
