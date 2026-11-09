"""V2 4.3: what each AI call bought: used or not, whether it changed the portfolio, and its cost.

Synthetic agents, decisions, prices and journals only; no network, broker or model inference.
"""
from datetime import timedelta
from types import SimpleNamespace

from scripts.autonomous_fund.call_value import from_events, lines, summarize
from scripts.autonomous_fund.strategy import AutonomousFund, Decision, Research, targets_changed
from scripts.autonomous_fund.validation import ValidationSettings
from tests.test_autonomous_fund_quant import NOW, account, model_harness

PRICES = {"synthetic-model": {"input": 1.0, "output": 2.0, "cached_input": 0.1}}
EVIDENCE = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
             "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]


def decision(weight):
    return {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            "next_review_at": (NOW + timedelta(hours=1)).isoformat(), "rationale": "r",
            "targets": [{"symbol": "ALFA", "weight": weight}]}


def test_a_change_is_a_symbol_added_or_removed_or_a_weight_moved_a_full_point():
    assert targets_changed(None, decision(.05))
    assert not targets_changed(decision(.05), decision(.055))
    assert targets_changed(decision(.05), decision(.06))
    assert targets_changed(decision(.05), {**decision(.05), "targets": [{"symbol": "BETA", "weight": .05}]})
    assert not targets_changed(decision(.05), {**decision(.05), "targets": [
        {"symbol": "ALFA", "weight": .05}, {"symbol": "BETA", "weight": 0}]})  # A zero weight is no holding.


def call(identifier, purpose, cost=0.01, **extra):
    return {"call_id": identifier, "purpose": purpose, "cost_usd": cost, "usage": {"x": 1}, **extra}


def test_value_counts_used_and_changing_calls_and_never_prices_unknown_costs_at_zero():
    agents = [call("a", "decision"), call("b", "decision"), call("c", "decision", parse_error="TimeoutError", cost=None,
              usage=None), call("d", "premarket_research", cost=0.02), {"purpose": "decision", "cost_usd": 9.0},
              call("e", "critique", cost=None, cache_hit=True)]
    outcomes = [{"call_id": "a", "valid": True, "changed_targets": True},
                {"call_id": "b", "valid": True, "changed_targets": False},
                {"call_id": "d", "valid": True, "new_ideas": 3}]
    result = summarize(agents, outcomes, quiet_skips=4, critiques_reused=2)
    decisions = result["by_purpose"]["decision"]
    assert (decisions["calls"], decisions["used"], decisions["changed"], decisions["failed"]) == (3, 2, 1, 1)
    assert decisions["unpriced"] == 1 and decisions["cost_per_used_call"] is None  # One failure's cost is unknown.
    assert decisions["changed_share"] == 0.5
    research = result["by_purpose"]["premarket_research"]
    assert research["ideas"] == 3 and research["cost_per_used_call"] == 0.02
    assert result["by_purpose"]["critique"]["cache_hits"] == 1  # No paid request.
    text = "\n".join(lines(result))
    assert "决策复核：3 次，被采用 2 次，其中改变目标组合 1 次（50%）" in text and "费用未知（1 次未定价" in text
    assert "盘前研究：1 次，被采用 1 次，登记可验证想法 3 条；费用 0.0200 美元，每次被采用的调用 0.0200 美元" in text
    assert "跳过的复核 4 次，复用的反方审查 2 次" in text
    assert lines(summarize([{"purpose": "decision"}], [])) == []  # Calls before call ids are not counted.


class Agent:
    def __init__(self, answers):
        self.answers = answers

    def run(self, **kwargs):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(parsed=answer, usage={"input_tokens": 1000, "output_tokens": 100},
                               model="synthetic-model", cache_hit=False, summary="s", parse_error=None)


def instrumented(tmp_path, manager_answers):
    strategy = model_harness(tmp_path)
    strategy._agent_call = AutonomousFund._agent_call.__get__(strategy)
    strategy._record_agent = AutonomousFund._record_agent.__get__(strategy)
    strategy.scripted, strategy.agent_models = False, {"manager": "synthetic-model", "researcher": "synthetic-model"}
    strategy.config = {"model_prices_per_million": PRICES}
    strategy.validation = ValidationSettings(shadow_enabled=True)
    strategy.memory_mode = "rolling"
    research = Research.model_validate({"summary": "s", "evidence": EVIDENCE})
    strategy.agents = {"researcher": Agent([research] * 6), "manager": Agent(manager_answers)}
    return strategy


def test_each_decision_call_records_its_purpose_and_whether_it_changed_the_portfolio(tmp_path):
    answers = [Decision.model_validate(d) for d in (decision(.05), decision(.05), decision(.05), decision(.08))]
    strategy = instrumented(tmp_path, answers)
    strategy._decide(account(), NOW, False)  # Decision, then the baseline shadow.
    strategy.next_review = None
    strategy._decide(account(), NOW, False)  # Same targets: used, no change. The day's shadow is done.
    strategy.next_review = None
    strategy._decide(account(), NOW, False)  # ALFA 5% -> 8%: a change.
    rows = strategy.journal.all_events()
    agents = [r["body"] for r in rows if r["kind"] == "agent"]
    assert [a["purpose"] for a in agents] == ["research", "decision", "shadow", "research", "decision",
                                              "research", "decision"]
    assert all(a["call_id"] for a in agents) and len({a["call_id"] for a in agents}) == 7
    outcomes = [r["body"] for r in rows if r["kind"] == "agent_outcome"]
    decided = [o for o in outcomes if o["purpose"] == "decision"]
    assert [o["changed_targets"] for o in decided] == [True, False, True]
    assert {o["call_id"] for o in outcomes} <= {a["call_id"] for a in agents}
    value = from_events([(r["at"], r["kind"], r["body"]) for r in rows])["by_purpose"]
    assert value["decision"]["used"] == 3 and value["decision"]["changed"] == 2 and value["shadow"]["used"] == 1
    assert value["decision"]["cost_usd"] == round(3 * (1000 * 1.0 + 100 * 2.0) / 1e6, 6)
    assert value["research"]["used"] == 0  # The replay researcher's answer has no outcome of its own.


def test_a_failed_or_rejected_call_is_counted_but_never_used(tmp_path):
    rejected = Decision.model_validate({**decision(.05), "targets": [{"symbol": "NOT_IN_UNIVERSE", "weight": .05}]})
    strategy = instrumented(tmp_path, [TimeoutError("synthetic"), rejected])
    strategy.validation = ValidationSettings(shadow_enabled=False)
    for _ in range(2):
        try:
            strategy._decide(account(), NOW, False)
        except Exception:
            pass
        strategy.next_review = None
        strategy.journal.set_meta("decision_retry_at", None)
    rows = strategy.journal.all_events()
    value = from_events([(r["at"], r["kind"], r["body"]) for r in rows])["by_purpose"]["decision"]
    assert value["calls"] == 2 and value["used"] == 0 and value["failed"] == 1
    assert value["unused_cost_usd"] > 0  # The rejected answer was paid for.
    assert not [r for r in rows if r["kind"] == "agent_outcome" and r["body"]["purpose"] == "decision"]


def test_the_weekly_report_shows_what_the_weeks_calls_bought(tmp_path):
    from scripts.autonomous_fund.notifications import weekly_document
    from tests.test_autonomous_fund_weekly_report import close_of, ledger
    journal, sequence = ledger(tmp_path)
    for day, changed in (("2026-11-04", True), ("2026-11-05", False)):
        identifier = f"call-{day}"
        journal.event(close_of(day, -200), "agent", call(identifier, "decision", cost=0.05))
        journal.event(close_of(day, -199), "agent_outcome", {"call_id": identifier, "purpose": "decision",
                                                              "valid": True, "changed_targets": changed})
    journal.event(close_of("2026-11-05", -100), "review_skipped", {})
    journal.event(close_of("2026-10-28", -100), "agent", call("last-week", "decision"))  # Another week.
    _, text = weekly_document(tmp_path, sequence)
    assert "AI 调用价值" in text and "决策复核：2 次，被采用 2 次，其中改变目标组合 1 次（50%）" in text
    assert "每次改变组合 0.1000 美元" in text and "跳过的复核 1 次" in text
