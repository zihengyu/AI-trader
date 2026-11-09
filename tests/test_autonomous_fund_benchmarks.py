"""V2 3.4: predefined benchmarks, account and system net returns, Newey-West alpha t and Lo-adjusted Sharpe.

Synthetic journals, prices and costs only; no network, broker or model inference.
"""
import math
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.performance import (PerformanceSettings, alpha_beta, benchmark_id, benchmark_series,
                                                 component_return, lo_sharpe, net_returns, path_statistics,
                                                 regression_hac)
from scripts.autonomous_fund.risk import RiskError


def test_benchmark_ids_follow_their_definition():
    defaults = {d["name"]: d for d in PerformanceSettings().definitions()}
    assert defaults["spy"]["weights"] == {"SPY": "1"} and defaults["risk_matched"]["weights"] == {"SPY": "0.6", "SGOV": "0.4"}
    assert benchmark_id("spy", {"SPY": "1"}) == benchmark_id("spy", {"SPY": "1.00"}) == defaults["spy"]["id"]
    changed = PerformanceSettings(benchmarks={"risk_matched": {"SPY": "0.7", "SGOV": "0.3"}}).definitions()[0]
    assert changed["name"] == "risk_matched" and changed["id"] != defaults["risk_matched"]["id"]
    assert PerformanceSettings().symbols() == ["SGOV", "SPY"]
    assert PerformanceSettings(data_costs_usd_per_month=None).data_costs_usd_per_month is None
    for bad in ({"benchmarks": {"x": {"SPY": "0.5"}}}, {"benchmarks": {"x": {"spy": "1"}}},
                {"benchmarks": {"x": {"SPY": "1.5", "SGOV": "-0.5"}}}, {"benchmarks": {"Bad Name": {"SPY": "1"}}},
                {"benchmarks": {}}, {"data_costs_usd_per_month": "-5"}, {"data_costs_usd_per_month": "lots"}):
        with pytest.raises(RiskError):
            PerformanceSettings(**bad)
    with pytest.raises(RiskError):
        PerformanceSettings.from_config({"benchmark": {}})


def test_benchmarks_are_total_returns_and_missing_marks_stay_missing():
    prices = {"2026-07-01": 100.0, "2026-07-02": 99.6, "2026-07-03": 50.0}
    dividend = [{"date": "2026-07-02", "dividend": "0.4"}]
    assert component_return(prices, dividend, "2026-07-01", "2026-07-02") == pytest.approx(0.0)  # Price fell by the payout.
    split = [{"date": "2026-07-03", "split": "2"}]
    assert component_return(prices, split, "2026-07-02", "2026-07-03") == pytest.approx(50.0 / 49.8 - 1)
    assert component_return(prices, [], "2026-07-03", "2026-07-06") is None
    marks = {"SPY": {"2026-07-01": 500.0, "2026-07-02": 505.0}, "SGOV": {"2026-07-01": 100.0, "2026-07-02": 99.6}}
    definition = {"weights": {"SPY": "0.6", "SGOV": "0.4"}}
    series = benchmark_series(definition, [("2026-07-01", "2026-07-02"), ("2026-07-02", "2026-07-03")], marks,
                              {"SGOV": dividend})
    assert series[0] == pytest.approx(0.6 * 0.01) and series[1] is None


def ar1(n, phi, seed, scale=0.01):
    rng, value, out = random.Random(seed), 0.0, []
    for _ in range(n):
        value = phi * value + rng.gauss(0, scale)
        out.append(value)
    return out


def test_newey_west_alpha_t_is_smaller_with_autocorrelated_residuals():
    rng = random.Random(3)
    market = [rng.gauss(0.0004, 0.01) for _ in range(500)]
    noise = ar1(500, 0.6, 4, 0.004)
    returns = [0.0006 + 0.8 * m + e for m, e in zip(market, noise)]
    result = regression_hac(returns, market)
    assert result["alpha_t"] == pytest.approx(alpha_beta(returns, market)["alpha_t"])
    assert result["newey_west_lags"] == 5 and abs(result["alpha_t_newey_west"]) < abs(result["alpha_t"])
    independent = [0.0006 + 0.8 * m + e for m, e in zip(market, ar1(500, 0.0, 5, 0.004))]
    plain = regression_hac(independent, market)
    assert plain["alpha_t_newey_west"] == pytest.approx(plain["alpha_t"], rel=0.2)


def test_lo_sharpe_discounts_positive_autocorrelation():
    trending = [0.0005 + r for r in ar1(750, 0.5, 6)]
    naive = path_statistics(trending)["sharpe_annualized"]
    assert lo_sharpe(trending) < naive
    independent = [0.0005 + r for r in ar1(750, 0.0, 7)]
    assert lo_sharpe(independent) == pytest.approx(path_statistics(independent)["sharpe_annualized"], rel=0.15)
    assert lo_sharpe([0.01, 0.01]) is None


def test_path_statistics_drawdown_and_sortino():
    stats = path_statistics([0.1, -0.5, 0.2])
    assert stats["max_drawdown"] == pytest.approx(0.5)
    downside = math.sqrt(0.25 / 3)
    assert stats["sortino_annualized"] == pytest.approx((sum([0.1, -0.5, 0.2]) / 3) / downside * math.sqrt(252))
    assert path_statistics([0.01, 0.02])["status"] == "insufficient_sample"


def test_unknown_costs_leave_the_system_return_unknown_never_zero():
    pairs = [{"return": 0.01, "equity_start": 10000.0} for _ in range(4)]
    known = net_returns(pairs, [1.0, 2.0, 0.0, 1.0], 0.5)
    assert known["costs_usd"] == {"model": 4.0, "data": 2.0, "complete": True}
    assert known["system"]["annual_return"] < known["account"]["annual_return"]
    unpriced = net_returns(pairs, [1.0, None, 0.0, 1.0], 0.5)
    assert unpriced["system"] == {"status": "unknown_costs", "sessions_with_unknown_costs": 1,
                                  "meaning": unpriced["system"]["meaning"]}
    assert net_returns(pairs, [0.0] * 4, None)["system"]["status"] == "unknown_costs"  # Data costs unknown.
    assert unpriced["account"] == known["account"]


def test_acceptance_reports_net_returns_and_every_benchmark_definition(tmp_path):
    from scripts.autonomous_fund.validation import acceptance, investment_markdown
    journal = Journal(tmp_path, "synthetic-benchmark-account")
    start = datetime(2026, 7, 6, 19, 0, tzinfo=timezone.utc)
    old = {"benchmarks": [{"id": "risk_matched-old00000", "name": "risk_matched", "weights": {"SPY": "0.5", "SGOV": "0.5"}}],
           "data_costs_usd_per_month": "21"}
    new = {"benchmarks": PerformanceSettings().definitions(), "data_costs_usd_per_month": "21"}
    for performance, at in ((old, start - timedelta(days=1)), (new, start)):
        journal.event(at, "startup", {"mode": "local", "limits": {"drawdown": "0.08"}, "risk_policy_digest": "p1",
                                      "execution": {"iteration_seconds": 60}, "performance": performance})
    equity, spy, sgov, day = 10000.0, 500.0, 100.0, start
    for i in range(12):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        journal.event(day - timedelta(hours=1), "agent", {"name": "manager", "model": "synthetic/model",
                                                         "usage": {"prompt_tokens": 10}, "cost_usd": 0.5})
        journal.event(day, "account", {"as_of": day.isoformat(), "cash": "0", "equity": str(equity), "positions": {}})
        journal.event(day, "benchmark", {"SPY": str(spy), "QQQ": "400", "SGOV": str(sgov)})
        if i == 5:
            journal.event(day, "benchmark_action", {"symbol": "SGOV", "date": day.date().isoformat(), "dividend": "0.4"})
        equity *= 1.002
        spy *= 1.001
        day += timedelta(days=1)
    journal.event(day, "benchmark_actions_checked", {"symbols": ["SGOV", "SPY"]})
    result = acceptance(tmp_path, now=day + timedelta(days=1))
    net = result["net_returns"]
    assert net["account"]["sessions"] == 11 and net["costs_usd"]["model"] == pytest.approx(5.5)
    assert net["costs_usd"]["data"] == pytest.approx(11 / (252 / 12) * 21)
    assert net["system"]["annual_return"] < net["account"]["annual_return"]
    rows = {r["id"]: r for r in result["benchmarks"]}
    assert set(rows) == {"risk_matched-old00000"} | {d["id"] for d in new["benchmarks"]}
    assert rows["risk_matched-old00000"]["current"] is False and all(rows[d["id"]]["current"] for d in new["benchmarks"])
    matched = rows[[d["id"] for d in new["benchmarks"] if d["name"] == "risk_matched"][0]]
    assert matched["status"] == "computed" and matched["dividends"] == "included"
    spy_row = rows[[d["id"] for d in new["benchmarks"] if d["name"] == "spy"][0]]
    assert spy_row["benchmark"]["annual_return"] == pytest.approx(1.001 ** 252 - 1)
    # 60% SPY + 40% SGOV, whose only return is the recorded 0.4% distribution.
    growth = 1.0006 ** 10 * (1.0006 + 0.4 * 0.004)
    assert matched["benchmark"]["annual_return"] == pytest.approx(growth ** (252 / 11) - 1)
    text = investment_markdown(result)  # The investment report (V2 4.4) holds the return evidence.
    assert "## 净收益口径" in text and "## 事先定义的基准" in text and "旧定义" in text
    # One unpriced call makes the system return unknown rather than ignoring the cost.
    journal.event(day + timedelta(hours=1), "agent", {"name": "manager", "model": "unpriced", "usage": {"prompt_tokens": 9},
                                                       "cost_usd": None})
    journal.event(day + timedelta(hours=2), "account", {"as_of": "x", "cash": "0", "equity": str(equity), "positions": {}})
    later = acceptance(tmp_path, now=day + timedelta(days=2))
    assert later["net_returns"]["system"]["status"] == "unknown_costs" and "未知" in investment_markdown(later)


def test_the_runner_records_benchmark_dividends_once_a_day(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = object.__new__(AutonomousFund)
    strategy.journal = Journal(tmp_path, "synthetic-benchmark-account")
    strategy.performance = PerformanceSettings()
    calls = []

    def actions(asset, since=None):
        calls.append(asset.symbol)
        return [{"date": "2026-07-01", "dividend": 0.4 if asset.symbol == "SGOV" else 0, "split": 0}]
    strategy.broker = SimpleNamespace(data_source=SimpleNamespace(corporate_actions=actions))
    now = datetime(2026, 7, 6, 19, tzinfo=timezone.utc)
    strategy._record_benchmark_actions(now)
    strategy._record_benchmark_actions(now + timedelta(hours=1))  # Same trading day: no second fetch.
    strategy._benchmark_actions_day = None
    strategy._record_benchmark_actions(now + timedelta(days=1))  # Already recorded: not repeated.
    rows = [r["body"] for r in strategy.journal.all_events() if r["kind"] == "benchmark_action"]
    assert rows == [{"symbol": "SGOV", "date": "2026-07-01", "dividend": "0.4"}] and calls.count("SGOV") == 2
    assert [r["body"] for r in strategy.journal.all_events() if r["kind"] == "benchmark_actions_checked"][0] == \
        {"symbols": ["SGOV", "SPY"]}
    strategy.broker = SimpleNamespace(data_source=SimpleNamespace())  # Alpaca: no corporate actions here.
    strategy._benchmark_actions_day = None
    strategy._record_benchmark_actions(now + timedelta(days=2))
    assert sum(r["kind"] == "benchmark_actions_checked" for r in strategy.journal.all_events()) == 2
