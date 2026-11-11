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


def journal_with_costs(tmp_path):
    from scripts.autonomous_fund.journal import Journal
    journal = Journal(tmp_path, "synthetic-cost-account")
    journal.event(close(DAYS[0]) - timedelta(hours=8), "startup", {"performance": {"data_costs_usd_per_month": "21"}})
    for day in DAYS[:3]:
        for minutes in (0, 30):  # Marked about every 30 seconds in practice: days count, not marks.
            journal.event(close(day) - timedelta(hours=6, minutes=minutes), "benchmark", {"SPY": "700", "SGOV": "100"})
    journal.event(close(DAYS[1]), "agent", {"name": "manager", "model": "deepseek/x", "cost_usd": 2.5, "mock": False})
    journal.event(close(DAYS[1]), "agent", {"name": "manager", "model": "deepseek/x", "cost_usd": None, "mock": False})
    journal.event(close(DAYS[1]), "agent", {"name": "manager", "model": "scripted/no-inference", "cost_usd": 9, "mock": True})
    journal.event(close(DAYS[2]), "agent", {"name": "critic", "model": "deepseek/x", "cost_usd": 9, "cache_hit": True})
    return journal


def test_accrued_costs_count_paid_calls_and_data_per_recorded_session(tmp_path):
    from scripts.autonomous_fund.scoreboard import accrued_costs
    journal = journal_with_costs(tmp_path)
    with journal.connection() as db:
        costs = accrued_costs(db, (close(DAYS[2]) + timedelta(hours=1)).isoformat())
        early = accrued_costs(db, (close(DAYS[0]) + timedelta(hours=1)).isoformat())
    assert costs["model_usd"] == 2.5 and costs["unpriced_calls"] == 1  # Scripted and cached calls cost nothing.
    assert costs["data_usd"] == pytest.approx(21 / 21 * 3)  # $21 a month, three recorded sessions.
    assert early["model_usd"] == 0 and early["data_usd"] == pytest.approx(1.0)


def test_every_account_update_shows_the_fund_value_after_its_costs():
    from scripts.autonomous_fund.notifications import readable
    from scripts.autonomous_fund.status import account_lines
    account = {"as_of": "2026-11-10T14:35:00+00:00", "equity": "9974.19", "cash": "9974.19", "positions": {}}
    costs = {"model_usd": 123.45, "data_usd": 0.0, "unpriced_calls": 2}
    lines = account_lines(account, now=datetime(2026, 11, 10, 14, 36, tzinfo=timezone.utc), costs=costs)
    [line] = [text for text in lines if text.startswith("扣除 AI 费用后的基金净值")]
    assert "$9,850.74" in line and "模型费用 $123.45" in line and "另有 2 次调用未定价" in line
    assert not [text for text in account_lines(account, now=datetime(2026, 11, 10, 14, 36, tzinfo=timezone.utc))
                if text.startswith("扣除 AI 费用")]
    text = readable("execution_update", {"at": "2026-11-10T14:36:00+00:00", "account": account, "recent_trades": [],
                                         "fund_costs": costs})
    assert "扣除 AI 费用后的基金净值 $9,850.74" in text


def test_the_daily_attribution_and_the_weekly_report_show_returns_after_ai_costs(tmp_path):
    from scripts.autonomous_fund.notifications import attribution_lines, weekly_document
    from tests.test_autonomous_fund_quant import harness
    from tests.test_autonomous_fund_weekly_report import ledger
    strategy = harness(tmp_path / "attribution", mode="local")
    strategy.journal = journal_with_costs(tmp_path / "costs")
    result = {"status": "complete", "start_equity": 10_000.0, "observed_return": 0.001, "previous_session": "a",
              "session_date": "b", "benchmark": "SPY", "benchmark_return": 0.0, "components": {}, "unexplained": 0.0}
    strategy._attribute_fund_costs(result, close(DAYS[0]) + timedelta(hours=1), close(DAYS[2]) + timedelta(hours=1))
    assert result["fund_costs"]["model_usd"] == 2.5 and result["fund_costs"]["data_usd"] == pytest.approx(2.0)
    assert result["net_return"] == pytest.approx(0.001 - 4.5 / 10_000)
    [line] = [text for text in attribution_lines(result) if "AI 和数据费用" in text]
    assert "-0.04%" in line and "扣费后当日收益 0.06%" in line and "另有 1 次调用未定价" in line
    _, sequence = ledger(tmp_path / "weekly")
    _, text = weekly_document(tmp_path / "weekly", sequence)
    assert "本周 AI 费用 $0.25（2 次调用），扣除后本周收益" in text and "另有 1 次调用未定价、未计入" in text


def test_the_cost_reminder_fires_once_per_threshold_and_never_stops_trading(tmp_path):
    from scripts.autonomous_fund.risk import RiskError
    from scripts.autonomous_fund.strategy import ModelBudget
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    board = {"status": "computed", "since": "2026-10-28", "costs_share_of_starting_equity": 0.021,
             "model_costs_usd": 210.0, "data_costs_usd": 0.0, "unpriced_calls": 0, "account_return": -0.0026,
             "net_return": -0.0236, "cost_pace": {"share_of_equity_per_year": 0.12}}
    strategy._fund_scoreboard = lambda now: board
    assert strategy._check_fund_cost_alert(NOW)["share"] == 0.021
    assert strategy._check_fund_cost_alert(NOW) is None  # Same 2% step: said once.
    board["costs_share_of_starting_equity"] = 0.041
    strategy._check_fund_cost_alert(NOW)
    assert [kind for kind, _ in strategy.notices] == ["fund_cost_alert", "fund_cost_alert"]
    assert not strategy.halt_path.exists()  # Only a reminder.
    strategy.model_budget = ModelBudget(cost_alert_share=None)
    board["costs_share_of_starting_equity"] = 0.5
    assert strategy._check_fund_cost_alert(NOW) is None
    with pytest.raises(RiskError, match="cost_alert_share"):
        ModelBudget(cost_alert_share="0")
