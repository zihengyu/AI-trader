"""Cost feedback uses recorded prices and instants, without paid inference or market I/O."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.scoreboard import accrued_costs, compute

UTC = timezone.utc
START = datetime(2026, 11, 2, 14, tzinfo=UTC)


def startup(journal, at, monthly):
    journal.event(at, "startup", {"performance": {"data_costs_usd_per_month": monthly}})


def test_subscription_changes_apply_to_new_sessions_without_repricing_history(tmp_path):
    journal = Journal(tmp_path, "synthetic-fees")
    startup(journal, START, "21")
    journal.event(START + timedelta(hours=1), "benchmark", {"ALFA": "100"})
    startup(journal, START + timedelta(days=1), "42")
    journal.event(START + timedelta(days=1, hours=1), "benchmark", {"ALFA": "101"})
    with journal.connection() as db:
        costs = accrued_costs(db, (START + timedelta(days=1, hours=2)).isoformat())
    assert costs["data_usd"] == pytest.approx(3.0)  # $1 at the old rate, $2 at the new rate.


def test_one_new_york_session_is_not_charged_twice_for_different_timestamp_offsets(tmp_path):
    journal = Journal(tmp_path, "synthetic-offsets")
    startup(journal, START, "21")
    journal.event("2026-11-02T23:30:00+00:00", "benchmark", {"ALFA": "100"})
    journal.event("2026-11-03T00:30:00+00:00", "benchmark", {"ALFA": "101"})
    with journal.connection() as db:
        costs = accrued_costs(db, "2026-11-03T02:00:00+00:00")
    assert costs["data_usd"] == 1.0  # Both observations belong to November 2 in New York.


def test_cost_cutoff_preserves_microseconds_and_excludes_future_calls(tmp_path):
    journal = Journal(tmp_path, "synthetic-cutoff")
    journal.event(START, "agent", {"model": "provider/model", "cost_usd": 2.0})
    journal.event(START + timedelta(microseconds=1), "agent", {"model": "provider/model", "cost_usd": 9.0})
    with journal.connection() as db:
        costs = accrued_costs(db, START.isoformat())
    assert costs["model_usd"] == 2.0


def test_net_return_matches_accrual_without_compounding_a_fictitious_cost_debit():
    pairs = [{"start_day": "2026-11-02", "end_day": "2026-11-03", "start": START,
              "end": START + timedelta(days=1), "return": 0.0, "equity_start": 10_000.0},
             {"start_day": "2026-11-03", "end_day": "2026-11-04", "start": START + timedelta(days=1),
              "end": START + timedelta(days=2), "return": 0.0, "equity_start": 10_000.0}]
    board = compute(pairs, [(p["end"], 100.0) for p in pairs], 0.0, [], {}, {})
    assert board["net_return"] == pytest.approx((10_000 - 200) / 10_000 - 1)


def test_fresh_cost_feedback_exists_without_completed_sessions_and_retains_unknowns(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, model_harness
    strategy = model_harness(tmp_path)
    strategy.journal.event(NOW, "agent", {"model": "provider/model", "cost_usd": 2.5})
    strategy.journal.event(NOW, "agent", {"model": "provider/model", "cost_usd": None})
    context = strategy._with_fund_cost_context({"account": {"equity": "10000", "as_of": NOW.isoformat()}}, NOW)
    costs = context["fund_costs"]
    assert costs["model_usd"] == 2.5 and costs["unpriced_calls"] == 1
    assert costs["net_fund_value_usd"] == 9997.5 and costs["net_is_upper_bound"]
    assert costs["account_as_of"] == NOW.isoformat() and costs["as_of"] == NOW.isoformat()
    assert "estimate" in costs["meaning"] and "already" in costs["meaning"]
    assert strategy._fund_scoreboard(NOW)["status"] == "insufficient_sample"


def test_unknown_past_subscription_stays_unknown_but_known_fees_are_still_deducted(tmp_path):
    from scripts.autonomous_fund.status import fund_value_line
    journal = Journal(tmp_path, "synthetic-mixed-rates")
    startup(journal, START, None)
    journal.event(START + timedelta(hours=1), "benchmark", {"BETA": "100"})
    startup(journal, START + timedelta(days=1), "42")
    journal.event(START + timedelta(days=1, hours=1), "benchmark", {"BETA": "101"})
    with journal.connection() as db:
        costs = accrued_costs(db, (START + timedelta(days=2)).isoformat())
    assert costs["data_usd"] is None and costs["unpriced_data_sessions"] == 1
    assert costs["known_data_usd"] == 2.0
    line = fund_value_line({"equity": "10000"}, costs)
    assert "$9,998.00" in line and "数据费用未知" in line


def test_manager_receives_the_costs_of_this_cycles_research_and_critique(tmp_path):
    from types import SimpleNamespace
    from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness
    strategy = model_harness(tmp_path)
    strategy.critic = SimpleNamespace(enabled=True)
    strategy._critic_ready = True
    seen = {}

    def call(name, **kwargs):
        seen[name] = kwargs["context"]
        cost = {"researcher": 1, "critic": 2, "manager": 3}[name]
        strategy.journal.event(NOW, "agent", {"model": "provider/model", "name": name, "cost_usd": cost})
        strategy.journal.counter("model_cost_microusd", NOW, cost * 1_000_000)
        strategy.journal.counter("agent_runs", NOW, 1)
        if name == "manager":
            return decision([])
        return {"summary": "Synthetic evidence", "concerns": [], "evidence": [{"source": "Synthetic",
            "available_at": NOW.isoformat(), "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]}

    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert [seen[role]["fund_costs"]["model_usd"] for role in ("researcher", "critic", "manager")] == [0, 1, 3]
    assert seen["manager"]["model_budget"]["cost_today_usd"] == 3
    assert seen["manager"]["model_budget"]["runs_today"] == 2
    assert seen["manager"]["fund_costs"]["net_fund_value_usd"] == 9997
    # A new request must not mutate the original research input or the exact
    # accepted manager input later reused by the stability measurement.
    refreshed = strategy._with_fund_cost_context(seen["manager"], NOW)
    assert refreshed["fund_costs"]["model_usd"] == 6
    assert seen["manager"]["fund_costs"]["model_usd"] == 3


def test_acceptance_net_returns_and_scoreboard_use_the_same_accrual_math():
    from scripts.autonomous_fund.performance import net_returns
    from tests.test_autonomous_fund_scoreboard import synthetic
    pairs, calls, marks = synthetic(cost=100)
    board = compute(pairs, calls, 1.0, [], marks, {})
    formal = net_returns(pairs, [cost for _, cost in calls], 1.0)
    assert formal["system"]["annual_return"] == pytest.approx((1 + board["net_return"]) ** (252 / len(pairs)) - 1)


def test_manager_does_not_assume_cash_loses_or_equities_always_win():
    from scripts.autonomous_fund.strategy import MANAGER_PROMPT
    assert "when costs exceed its income" in MANAGER_PROMPT
    assert "without assuming equities must outperform" in MANAGER_PROMPT
    assert "never cut research, tool use or the depth of your analysis" in MANAGER_PROMPT


@pytest.mark.parametrize("usage", [{"metadata": "present"}, {"input_tokens": 10},
                                  {"input_tokens": 10, "output_tokens": float("inf")},
                                  {"input_tokens": -1, "output_tokens": 2}])
def test_incomplete_or_invalid_usage_cannot_be_priced_as_a_free_call(tmp_path, usage):
    from types import SimpleNamespace
    from scripts.autonomous_fund.costs import cost_summary
    from scripts.autonomous_fund.strategy import AutonomousFund
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path, mode="local")
    strategy.scripted = False
    strategy.config = {"model_prices_per_million": {"provider/model": {"input": 1, "output": 2}}}
    strategy.agent_models = {"manager": "provider/model"}
    from scripts.autonomous_fund.costs import token_counts, usage_known
    assert not usage_known(usage)
    assert all(value >= 0 for value in token_counts(usage).values())
    result = SimpleNamespace(model="provider/model", cache_hit=False, usage=usage,
                             parsed={"ok": True}, parse_error=None, summary="Synthetic")
    AutonomousFund._record_agent(strategy, "manager", result)
    row = next(r for r in strategy.journal.all_events() if r["kind"] == "agent")
    assert row["body"]["cost_usd"] is None
    if usage.get("output_tokens") == float("inf"):
        assert row["body"]["usage"] is None and row["body"]["usage_diagnostic"]
    summary = cost_summary([row], now=NOW, prices=strategy.config["model_prices_per_million"])
    assert summary["unpriced_calls"] == 1 and not summary["complete"]


def test_explicit_zero_usage_remains_priced_and_split_input_counts_are_supported():
    from scripts.autonomous_fund.costs import call_cost, token_counts, usage_known
    for usage in ({"input_tokens": 0, "output_tokens": 0},
                  {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "completion_tokens": 0}):
        assert usage_known(usage)
        assert call_cost(token_counts(usage), {"input": 1, "output": 2}) == 0.0


def test_postmarket_manager_sees_research_cost_without_rewriting_saved_research(tmp_path):
    from scripts.autonomous_fund.review import PostmarketReview
    from tests.test_autonomous_fund_review import events, review_harness
    strategy, calls = review_harness(tmp_path)
    strategy.scripted = False
    strategy.config["model_prices_per_million"] = {
        model: {"input": 1, "output": 2} for model in ("fixture/flash", "fixture/pro")}
    original_run = strategy.agents["manager"].run

    def cite_costs(**kwargs):
        result = original_run(**kwargs)
        body = result.parsed.model_dump()
        body["selection_adjustments"][0]["evidence_refs"] = ["context:fund_costs"]
        result.parsed = PostmarketReview.model_validate(body)
        result.summary = result.parsed.model_dump_json()
        return result

    strategy.agents["manager"].run = cite_costs
    strategy.after_market_closes()
    research_costs = calls[0][1]["context"]["fund_costs"]
    manager = calls[-1][1]["context"]
    assert research_costs["model_usd"] == 0
    assert manager["fund_costs"]["model_usd"] == pytest.approx(0.000025)
    assert "context:fund_costs" in manager["allowed_evidence_refs"]
    saved = events(strategy, "postmarket_research")[0]
    assert saved["context"]["fund_costs"]["model_usd"] == 0
    packet = events(strategy, "postmarket_review")[0]
    assert packet["fund_costs"] == manager["fund_costs"]
    [entry] = [row for row in packet["evidence_catalog"] if row["ref"] == "context:fund_costs"]
    assert entry["input_as_of"] == manager["fund_costs"]["as_of"]


def test_decision_correction_refreshes_costs_of_rejected_answer(tmp_path):
    from types import SimpleNamespace
    from scripts.autonomous_fund.strategy import AutonomousFund, Decision
    from tests.test_autonomous_fund_quant import NOW, decision, model_harness
    strategy = model_harness(tmp_path)
    strategy.mode, strategy.scripted = "local", False
    strategy.config = {"model_prices_per_million": {"provider/model": {"input": 1, "output": 2}}}
    strategy.agent_models = {"manager": "provider/model"}
    strategy._record_agent = lambda name, result: AutonomousFund._record_agent(strategy, name, result)
    seen = []

    def call(name, **kwargs):
        seen.append(kwargs["context"])
        parsed = Decision.model_validate(decision([])) if len(seen) > 1 else None
        return SimpleNamespace(model="provider/model", cache_hit=False,
            usage={"input_tokens": 1_000_000, "output_tokens": 0},
            parsed=parsed, summary="[]" if parsed is None else parsed.model_dump_json(),
            parse_error="answer did not match Decision: schema_validation" if parsed is None else None)

    strategy._agent_call = call
    strategy._manager_decision({"as_of": NOW.isoformat(), "account": {"equity": "10000"}}, strategy.model_budget)
    assert len(seen) == 2
    assert [context["fund_costs"]["model_usd"] for context in seen] == [0, 1]


def test_historical_rates_match_accrued_value_scoreboard_attribution_and_acceptance(tmp_path):
    from scripts.autonomous_fund.performance import PerformanceSettings
    from scripts.autonomous_fund.validation import benchmark_report, session_pairs
    from tests.test_autonomous_fund_quant import harness
    from zoneinfo import ZoneInfo
    strategy = harness(tmp_path)
    journal = strategy.journal
    for index in range(3):
        at = START + timedelta(days=index)
        journal.event(at, "startup", {"performance": {
            "data_costs_usd_per_month": str(21 * (index + 1)), "benchmarks": PerformanceSettings().definitions()}})
        journal.event(at + timedelta(hours=1), "benchmark", {"SPY": "100", "SGOV": "100"})
        journal.event(at.replace(hour=21), "account", {"as_of": at.replace(hour=21).isoformat(),
            "equity": "10000", "cash": "10000", "positions": {}})
    now = START + timedelta(days=3)
    board = strategy._fund_scoreboard(now)
    with journal.connection() as db:
        costs = accrued_costs(db, now.isoformat())
    assert costs["data_usd"] == 6.0
    assert board["data_costs_usd"] == 5.0 and board["net_return"] == pytest.approx(-5 / 10_000)
    rows = [(row["at"], row["kind"], row["body"]) for row in journal.all_events()]
    pairs, marks = session_pairs(rows, {START.date() + timedelta(days=i) for i in range(3)}, ZoneInfo("America/New_York"))
    formal, _ = benchmark_report(rows, pairs, marks, [body for _, kind, body in rows if kind == "startup"])
    assert formal["returns"]["costs_usd"]["data"] == 5.0
    result = {"start_equity": 10000, "observed_return": 0}
    strategy._attribute_fund_costs(result, START.replace(hour=21), (START + timedelta(days=2)).replace(hour=21))
    assert result["fund_costs"]["data_usd"] == 5.0
    assert result["net_return"] == pytest.approx(board["net_return"])


def test_same_session_restart_and_future_rate_do_not_reprice_a_charge(tmp_path):
    journal = Journal(tmp_path, "synthetic-restarts")
    startup(journal, START, "21")
    journal.event(START + timedelta(hours=1), "benchmark", {"ALFA": "100"})
    startup(journal, START + timedelta(hours=2), "42")
    journal.event(START + timedelta(hours=3), "benchmark", {"ALFA": "101"})
    startup(journal, START + timedelta(days=1, microseconds=1), "2100")
    with journal.connection() as db:
        costs = accrued_costs(db, (START + timedelta(days=1)).isoformat())
    assert costs["data_usd"] == 1.0
