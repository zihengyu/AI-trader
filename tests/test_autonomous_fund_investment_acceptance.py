"""V2 4.4: investment-capability acceptance, separate from the operational verdict.

Synthetic return series, benchmarks and costs only; no network, broker or model inference.
"""
import math
import random

import pytest

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


COSTS = {"usd": 120.0, "annual_rate_on_equity": 0.01, "fills_without_arrival": 0, "sessions": 250}


def verdict(returns, market, **kwargs):
    stats = evidence(returns, market, 1)
    result = investment_acceptance(stats, returns, market, kwargs.pop("net", net()), LEARNING,
                                   {"verdict": "insufficient_sample"}, {"by_purpose": {}, "calls_saved": {}},
                                   costs=kwargs.pop("costs", COSTS))
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


def test_the_edge_must_survive_twice_the_realized_trading_costs():
    returns, market = series(250, 0.002)
    result, criteria = verdict(returns, market)
    stressed = criteria["risk_matched_excess_annual_2x_trading_costs"]
    assert stressed["required"] and stressed["passed"] and math.isclose(stressed["value"], 0.04)  # 5% - 1x 1%.
    assert result["cost_sensitivity"]["risk_matched_excess_annual"] == {
        "1x": 0.05, "2x": 0.04, "3x": pytest.approx(0.03)}
    result, criteria = verdict(returns, market, costs={**COSTS, "annual_rate_on_equity": 0.06})
    assert not criteria["risk_matched_excess_annual_2x_trading_costs"]["passed"]
    assert result["verdict"] == "edge_not_shown"  # A 5% edge on 6% of costs does not survive doubling them.
    result, criteria = verdict(returns, market, costs={**COSTS, "fills_without_arrival": 2})
    assert criteria["risk_matched_excess_annual_2x_trading_costs"]["value"] == "unknown_costs"
    assert result["verdict"] == "edge_not_shown"
    result, criteria = verdict(returns, market, costs=None)
    assert criteria["risk_matched_excess_annual_2x_trading_costs"]["value"] is None and result["verdict"] == "edge_not_shown"


def test_trading_costs_take_fees_and_absolute_slippage_once_per_order():
    from scripts.autonomous_fund.validation import trading_costs
    events = [("t1", "fill", {"broker_id": "b1", "price": "101", "quantity": "10", "trade_cost": "1.01"}),
              ("t2", "fill", {"broker_id": "b2", "price": "49", "quantity": "4", "trade_cost": "0.2"}),
              ("t3", "fill", {"broker_id": "b2", "price": "49", "quantity": "4", "trade_cost": "0.2"}),
              # The reconciled record replaces b2's two callbacks (cumulative 8 shares).
              ("t4", "fill_reconciled", {"broker_id": "b2", "price": "49", "quantity": "8", "trade_cost": "0.4"}),
              ("t5", "fill", {"broker_id": "b3", "price": "20", "quantity": "5", "trade_cost": "0.1"})]
    by_broker = {"b1": {"intent_id": "i1", "side": "buy"}, "b2": {"intent_id": "i2", "side": "sell"},
                 "b3": {"intent_id": "i3", "side": "buy"}}
    references = {"i1": 100.0, "i2": 50.0}  # b3 has no arrival price.
    pairs = [{"equity_start": 10_000.0}] * 126
    costs = trading_costs(events, by_broker, references, pairs)
    # b1: 1.01 + |101-100| x 10; b2: 0.4 + |49-50| x 8 (a worse sale); b3: its fee only.
    assert math.isclose(costs["usd"], 1.01 + 10 + 0.4 + 8 + 0.1)
    assert costs["fills_without_arrival"] == 1 and costs["sessions"] == 126
    assert math.isclose(costs["annual_rate_on_equity"], costs["usd"] / 10_000 * 2)
    assert trading_costs([], {}, {}, [])["annual_rate_on_equity"] is None
