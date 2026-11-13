"""Accepted decisions retain their actual prior plan, critique and fill evidence.

Native application handoffs with synthetic results only; no inference or APIs.
"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.decision_activity import activity, lines
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.strategy import AutonomousFund, CriticSettings, Critique, Decision
from tests.test_autonomous_fund_quant import NOW, account, decision
from tests.test_autonomous_fund_manager_independence import live_manager
from tests.test_autonomous_fund_decision_activity import OPEN, NOW as AFTER_CLOSE, plan


def model_cycle(tmp_path, mode="local"):
    strategy, _ = live_manager(tmp_path, mode)
    strategy.scripted = False
    strategy.agent_models = {"critic": "provider/research", "manager": "provider/manager"}
    strategy.config = {"model_prices_per_million": {model: {"input": 1, "output": 2}
                                                     for model in strategy.agent_models.values()}}
    strategy.critic, strategy._critic_ready = CriticSettings(), True
    strategy._agent_call = AutonomousFund._agent_call.__get__(strategy)
    strategy._record_agent = AutonomousFund._record_agent.__get__(strategy)
    previous = decision([("ALFA", .05)])
    previous["rationale"] = "Synthetic original thesis; reassess if the evidence changes."
    strategy.last_decision = previous
    seen = {}

    def run(role, **kwargs):
        seen[role] = deepcopy(kwargs["context"])
        parsed = (Critique(summary="Synthetic counterfactual assessment", concerns=[{
            "symbol": "ALFA", "concern": "Compare holding and changing the plan", "evidence": "Synthetic source"}])
                  if role == "critic" else Decision.model_validate(decision([("BETA", .03)])))
        return SimpleNamespace(parsed=parsed, usage={"input_tokens": 20, "output_tokens": 10},
            model=strategy.agent_models[role], cache_hit=False, summary="Synthetic answer", parse_error=None)

    strategy.agents = {role: SimpleNamespace(run=lambda role=role, **kw: run(role, **kw))
                       for role in strategy.agent_models}
    fill = {"broker_id": "synthetic-buy", "symbol": "ALFA", "side": "buy", "quantity": "2", "price": "100"}
    strategy.journal.event(NOW - timedelta(minutes=4), "fill", {**fill, "quantity": "1", "trade_cost": "0.1"})
    strategy.journal.event(NOW - timedelta(minutes=3), "fill_reconciled", {**fill, "trade_cost": "0.2"})
    strategy.journal.event(NOW + timedelta(seconds=1), "fill", {**fill, "broker_id": "future-buy", "trade_cost": "0.9"})
    return strategy, seen, previous


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_both_roles_receive_prior_thesis_and_only_snapshot_visible_confirmed_fills(tmp_path, mode):
    strategy, seen, previous = model_cycle(tmp_path, mode)
    strategy._decide(account(cash=9800, positions={"ALFA": 2}), NOW, False)
    for role in ("critic", "manager"):
        assert seen[role]["previous_decision"] == previous
        trades = seen[role]["recent_trades"]
        assert len(trades) == 1 and trades[0]["broker_id"] == "synthetic-buy"
        assert trades[0]["quantity"] == "2" and trades[0]["trade_cost"] == "0.2"
        assert trades[0]["evidence_kind"] == "broker_cumulative_observation"
    events = strategy.journal.all_events()
    basis, = [row["body"] for row in events if row["kind"] == "decision_basis"]
    accepted, = [row["body"] for row in events if row["kind"] == "decision_accepted"]
    critique_call, = [row["body"] for row in events if row["kind"] == "agent" and row["body"]["name"] == "critic"]
    assert basis["decision_id"] == accepted["id"]
    assert basis["previous_decision"] == previous
    assert basis["critique"] == seen["manager"]["critique"]
    assert basis["recent_trades"] == seen["manager"]["recent_trades"]
    assert basis["critique_source"]["call_id"] == critique_call["call_id"]
    assert basis["critique_source"]["at"] == NOW.isoformat() and not basis["critique_source"]["reused"]
    selected = activity(tmp_path, "2026-11-02", now=AFTER_CLOSE)
    sequence = selected["decisions"][0]["sequence"]
    text = "\n".join(lines(selected, timeline=True, event_sequence=sequence))
    assert "关联反方原文" in text and "Synthetic counterfactual assessment" in text
    assert "Synthetic original thesis" in text and "synthetic-buy" in text and "future-buy" not in text


@pytest.mark.parametrize("changed", ["plan", "fills", "prompt_version"])
def test_critique_cache_cannot_borrow_a_different_plan_or_execution_history(tmp_path, changed):
    from tests.test_autonomous_fund_ai_requests import critic_harness
    fake, budget, now, calls = critic_harness(tmp_path)
    context = {"account": {"positions": {"ALFA": "2"}}, "previous_decision": decision([("ALFA", .05)]),
               "recent_trades": []}
    research = {"summary": "Synthetic pre-open research", "evidence": []}
    AutonomousFund._critique(fake, context, research, budget, now)
    if changed == "plan":
        context = {**context, "previous_decision": decision([("ALFA", .08)])}
    elif changed == "fills":
        context = {**context, "recent_trades": [{"broker_id": "new-confirmation", "trade_cost": None}]}
    else:
        fake.prompt_version = "synthetic-new-instructions"
    AutonomousFund._critique(fake, context, research, budget, now)
    assert len(calls) == 2


def test_reused_critique_keeps_its_original_call_and_time(tmp_path):
    from tests.test_autonomous_fund_ai_requests import critic_harness
    fake, budget, now, calls = critic_harness(tmp_path)
    context = {"account": {"positions": {}}, "recent_trades": [], "previous_decision": None}
    research = {"summary": "s", "evidence": []}
    AutonomousFund._critique(fake, context, research, budget, now)
    source = deepcopy(fake._current_critique_source)
    fake.get_datetime = lambda: now + timedelta(minutes=10)
    AutonomousFund._critique(fake, context, research, budget, fake.get_datetime())
    assert len(calls) == 1 and fake._current_critique_source == {**source, "reused": True}
    assert source["at"] == now.isoformat() and source["call_id"]


def test_missing_audit_write_does_not_block_a_valid_decision(tmp_path, monkeypatch):
    strategy, _, _ = model_cycle(tmp_path)
    original = strategy.journal.event

    def event(at, kind, body):
        if kind == "decision_basis":
            raise OSError("Synthetic audit sink failure")
        return original(at, kind, body)

    monkeypatch.setattr(strategy.journal, "event", event)
    strategy._decide(account(cash=9800, positions={"ALFA": 2}), NOW, False)
    assert strategy.last_decision["targets"] == [{"symbol": "BETA", "weight": .03}]
    assert any(kind == "decision_basis_unavailable" for kind, _ in strategy.notices)


def test_old_or_other_plans_never_supply_a_nearby_critique(tmp_path):
    journal = Journal(tmp_path, "synthetic-basis-binding")
    at, key = plan(journal, 0)
    journal.event(at + timedelta(seconds=1), "decision_basis", {
        "decision_id": "unrelated-plan", "critique": {"summary": "Wrong critique", "concerns": []}})
    selected = activity(tmp_path, "2026-11-02", now=AFTER_CLOSE)
    sequence = selected["decisions"][0]["sequence"]
    text = "\n".join(lines(selected, timeline=True, event_sequence=sequence))
    assert "未保存关联反方原文" in text and "Wrong critique" not in text
    journal.event(at + timedelta(minutes=2), "decision_basis", {
        "decision_id": key, "critique": {"summary": "Future critique", "concerns": []}})
    early = activity(tmp_path, "2026-11-02", now=at + timedelta(minutes=1))
    text = "\n".join(lines(early, timeline=True, event_sequence=sequence))
    assert "Future critique" not in text and "未保存关联反方原文" in text


def test_timing_reassessment_refreshes_fills_to_the_new_account_cutoff(tmp_path):
    from dataclasses import replace
    import json
    from tests.test_autonomous_fund_freshness_recovery import live_fund
    from tests.test_autonomous_fund_decision_correction import answer, valid
    bad = {**decision([]), "as_of": (NOW - timedelta(seconds=301)).isoformat()}
    strategy, calls = live_fund(tmp_path, [answer(json.dumps(bad), Decision), valid()])
    clock = [NOW]
    strategy.get_datetime = lambda: clock[0]
    strategy.journal.event(NOW + timedelta(seconds=1), "fill", {
        "broker_id": "during-inference", "symbol": "BETA", "side": "sell", "quantity": "1", "price": "50", "trade_cost": None})

    def snapshot():
        clock[0] = NOW + timedelta(seconds=2)
        return replace(account(), as_of=clock[0])

    strategy._snapshot = snapshot
    strategy._decide(account(), NOW, False)
    managers = [call for call in calls if call["name"] == "manager"]
    assert not managers[0]["context"]["recent_trades"]
    assert [trade["broker_id"] for trade in managers[1]["context"]["recent_trades"]] == ["during-inference"]
    basis, = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "decision_basis"]
    assert basis["recent_trades"] == managers[1]["context"]["recent_trades"]
    assert basis["recent_trades"][0]["trade_cost"] is None
