"""The fund's net-of-cost scoreboard: costs are paid whatever the manager decides, so cash is not free.

Synthetic marks, costs and benchmarks only; no network, broker or model inference.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.scoreboard import compute

DAYS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9)]
CASH_DAY = 0.00016  # About 4% a year.
BENCHMARKS = [{"id": "spy", "name": "spy", "weights": {"SPY": "1"}},
              {"id": "rm", "name": "risk_matched", "weights": {"SPY": "0.6", "SGOV": "0.4"}}]


def close(day):
    return datetime(day.year, day.month, day.day, 21, tzinfo=timezone.utc)


def synthetic(cost=6.0, spy_day=0.01):
    equities = [10_000 * (1 + CASH_DAY) ** i for i in range(len(DAYS))]
    pairs = [{"start_day": a.isoformat(), "end_day": b.isoformat(), "start": close(a), "end": close(b),
              "equity_start": ea, "return": eb / ea - 1}
             for (a, ea), (b, eb) in zip(zip(DAYS, equities), zip(DAYS[1:], equities[1:]))]
    calls = [(close(day) - timedelta(hours=5), cost) for day in DAYS[1:]]
    marks = {"SPY": {d.isoformat(): 700 * (1 + spy_day) ** i for i, d in enumerate(DAYS)},
             "SGOV": {d.isoformat(): 100 * (1 + CASH_DAY) ** i for i, d in enumerate(DAYS)}}
    return pairs, calls, marks


def test_an_all_cash_fund_loses_money_once_costs_exceed_the_bill_rate():
    pairs, calls, marks = synthetic()
    board = compute(pairs, calls, 0.0, BENCHMARKS, marks, {}, sweep="SGOV")
    assert board["status"] == "computed" and board["sessions"] == 5
    assert board["account_return"] == pytest.approx((1 + CASH_DAY) ** 5 - 1)  # Interest only.
    assert board["model_costs_usd"] == 30 and board["costs_share_of_starting_equity"] == pytest.approx(0.003)
    assert board["net_return"] < 0 < board["account_return"]  # The costs exceed the interest.
    spy = board["benchmarks"]["spy"]
    assert spy["return"] == pytest.approx(1.01 ** 5 - 1) and spy["net_excess"] == pytest.approx(board["net_return"] - spy["return"])
    assert board["cost_pace"]["share_of_equity_per_year"] == pytest.approx(6 * 252 / 10_000, rel=0.01)
    cash = board["cash"]
    assert cash["annual_yield"] == pytest.approx((1 + CASH_DAY) ** 252 - 1)
    assert cash["annual_net_of_cost_pace"] < -0.1  # About 4% earned, about 15% spent.
    assert not board["net_is_upper_bound"] and board["unpriced_calls"] == 0


def test_unpriced_calls_and_unknown_data_costs_are_never_free():
    pairs, calls, marks = synthetic()
    board = compute(pairs, calls + [(close(DAYS[2]) - timedelta(hours=1), None)], None, BENCHMARKS, marks, {})
    assert board["unpriced_calls"] == 1 and board["net_is_upper_bound"] and board["data_costs_usd"] is None
    assert board["cash"] is None  # No sweep configured.


def test_missing_marks_and_empty_history_are_unavailable_never_zero():
    pairs, calls, marks = synthetic()
    del marks["SPY"][DAYS[3].isoformat()]
    board = compute(pairs, calls, 0.0, BENCHMARKS, marks, {}, sweep="SGOV")
    assert board["benchmarks"]["spy"]["return"] is None and board["benchmarks"]["spy"]["net_excess"] is None
    assert compute([], [], 0.0, BENCHMARKS, {}, {})["status"] == "insufficient_sample"
    assert compute(pairs[:2], calls, 0.0, BENCHMARKS, marks, {}, sweep="SGOV")["cash"] is None  # Too short to annualize.


def test_the_strategy_reads_closing_marks_benchmarks_and_paid_calls_from_the_journal(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep
    from tests.test_autonomous_fund_quant import default_frames, harness
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy.cash_sweep = CashSweep("SGOV")
    pairs, calls, marks = synthetic()
    journal = strategy.journal
    for i, day in enumerate(DAYS):
        journal.event(close(day) - timedelta(hours=6), "benchmark",
                      {"SPY": str(marks["SPY"][day.isoformat()]), "SGOV": str(marks["SGOV"][day.isoformat()])})
        journal.event(close(day) - timedelta(hours=2), "account", {"as_of": "x", "cash": "1", "equity": "1"})  # Intraday.
        journal.event(close(day), "account", {"as_of": "x", "cash": "0", "equity": str(10_000 * (1 + CASH_DAY) ** i)})
        if i:
            journal.event(close(day) - timedelta(hours=5), "agent", {"name": "manager", "model": "deepseek/x",
                                                                       "cost_usd": 6.0, "cache_hit": False, "mock": False})
            journal.event(close(day) - timedelta(hours=4), "agent", {"name": "manager", "model": "scripted/no-inference",
                                                                       "cost_usd": 100.0, "cache_hit": False, "mock": True})
            journal.event(close(day) - timedelta(hours=3), "agent", {"name": "manager", "model": "deepseek/x",
                                                                       "cost_usd": 100.0, "cache_hit": True, "mock": False})
    now = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
    board = strategy._fund_scoreboard(now)
    assert board["status"] == "computed" and board["sessions"] == 5 and board["model_costs_usd"] == 30
    assert board["account_return"] == pytest.approx((1 + CASH_DAY) ** 5 - 1)  # Closing marks, not intraday ones.
    assert board["benchmarks"]["risk_matched"]["return"] is not None and board["cash"]["symbol"] == "SGOV"
    assert strategy._fund_scoreboard(now) is board  # Once per completed session.


def test_the_manager_and_the_critic_see_the_scoreboard_in_the_decision_context(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness
    strategy = model_harness(tmp_path)
    seen = {}
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]

    def call(name, **kwargs):
        seen[name] = kwargs.get("context")
        return {"summary": "s", "evidence": evidence} if name == "researcher" else decision([])
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert seen["manager"]["fund_scoreboard"]["status"] == "insufficient_sample"  # A fresh journal: no sessions yet.


def test_reports_and_prompts_say_cash_is_not_free():
    from scripts.autonomous_fund.notifications import evidence_lines
    from scripts.autonomous_fund.strategy import CRITIC_PROMPT, MANAGER_PROMPT
    pairs, calls, marks = synthetic()
    board = compute(pairs, calls, 0.0, BENCHMARKS, marks, {}, sweep="SGOV")
    [line] = [text for text in evidence_lines({"fund": board}) if text.startswith("扣除全部成本后的基金收益")]
    assert "净收益 -" in line and "风险匹配基准（60% SPY + 40% SGOV）" in line
    assert "要跑赢基准至少得多赚这么多" in line and "全拿现金（SGOV" in line
    assert not [text for text in evidence_lines({"fund": {"status": "insufficient_sample"}})
                if text.startswith("扣除全部成本")]
    assert "net of every cost" in MANAGER_PROMPT and "active bet against the market" in MANAGER_PROMPT
    assert "Choose cash freely" not in MANAGER_PROMPT and "win back costs" in MANAGER_PROMPT
    assert "against staying in cash" in CRITIC_PROMPT
