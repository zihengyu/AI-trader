"""Decision stability repeats the accepted decision's task and strategy context.

Synthetic decisions and fake model calls only; no network, broker or model inference.
"""
from datetime import timedelta

import pytest

from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.stability import INPUT_SCOPE, compare, score
from scripts.autonomous_fund.strategy import DECISION_CORRECTION_TASK, DECISION_TASK, Decision
from scripts.autonomous_fund.validation import ValidationSettings
from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness


def test_two_answers_are_compared_by_overlap_weight_distance_and_the_one_point_rule():
    same = compare(decision([("ALFA", .05), ("BETA", .05)]), decision([("BETA", .055), ("ALFA", .05)]))
    assert same["same_targets"] and same["symbol_overlap"] == 1.0 and same["weight_distance"] == pytest.approx(.0025)
    moved = compare(decision([("ALFA", .05)]), decision([("ALFA", .07)]))
    assert not moved["same_targets"] and moved["weight_distance"] == pytest.approx(.01)
    swapped = compare(decision([("ALFA", .05)]), decision([("BETA", .05)]))
    assert swapped["symbol_overlap"] == 0.0 and swapped["weight_distance"] == pytest.approx(.05)
    assert compare(decision([]), decision([]))["same_targets"] and compare(decision([]), decision([]))["symbol_overlap"] == 1


def test_the_score_sets_repeat_noise_against_how_much_real_decisions_change():
    checks = [{"valid": True, "weight_distance": .02, "symbol_overlap": .5, "same_targets": False, "decision_id": "a"},
              {"valid": True, "weight_distance": .0, "symbol_overlap": 1.0, "same_targets": True, "decision_id": "b"},
              {"valid": False, "error": "TimeoutError"}]
    decisions = [decision([("ALFA", .05)]), decision([("ALFA", .07)]), decision([("ALFA", .05)])]
    card = score(checks, decisions)
    assert (card["checks"], card["failed"]) == (2, 1) and card["same_rate"] == .5
    assert card["mean_weight_distance"] == pytest.approx(.01) and card["mean_consecutive_distance"] == pytest.approx(.01)
    assert card["noise_ratio"] == pytest.approx(1.0)  # Asking again changes as much as a real decision does.
    assert score([], [])["noise_ratio"] is None


def harness(tmp_path, answers):
    strategy = model_harness(tmp_path)
    strategy.mode = "local"
    strategy.validation = ValidationSettings(stability_checks_per_session=1)
    calls = []

    def call(name, purpose=None, **kwargs):
        calls.append({"name": name, "purpose": purpose, **kwargs})
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    strategy._agent_call = call
    return strategy, calls


def events(strategy, kind):
    return [row["body"] for row in strategy.journal.all_events() if row["kind"] == kind]


def test_once_a_session_the_same_input_is_asked_again_and_never_traded(tmp_path):
    strategy, calls = harness(tmp_path, [decision([("ALFA", .05), ("BETA", .03)])])
    context = {"as_of": NOW.isoformat(), "account": {"cash": "10000"}}
    accepted = decision([("ALFA", .05)])
    result = strategy._stability_check(context, accepted, "d-1", NOW, strategy.model_budget)
    assert calls[0]["context"] is context and calls[0]["task_prompt"] == DECISION_TASK
    assert calls[0]["purpose"] == "stability"
    assert result["symbol_overlap"] == .5 and result["weight_distance"] == pytest.approx(.015)  # Half of .03.
    [event] = events(strategy, "decision_stability")
    assert event["decision_id"] == "d-1" and event["valid"] and event["second_symbols"] == ["ALFA", "BETA"]
    assert strategy._stability_check(context, accepted, "d-2", NOW + timedelta(hours=1), strategy.model_budget) is None
    assert len(calls) == 1  # Once per session.
    assert not strategy.journal.pending()  # Nothing was submitted.


@pytest.mark.parametrize("blocker", ["mock", "stop", "off", "budget", "cost"])
def test_replays_stop_and_exhausted_budgets_skip_the_check(tmp_path, blocker):
    strategy, calls = harness(tmp_path, [decision([("ALFA", .05)])])
    if blocker == "mock":
        strategy.mode = "mock"  # A replay could answer the same input from its cache.
    elif blocker == "stop":
        strategy.halt_path.write_text("synthetic stop")
    elif blocker == "off":
        strategy.validation = ValidationSettings(stability_checks_per_session=0)
    elif blocker == "budget":
        budget = strategy.model_budget
        strategy.journal.counter("agent_runs", NOW, budget.max_agent_runs_per_day - budget.reserved_review_runs)
    else:
        strategy._cost_exhausted = lambda now, step: step == "stability"
    assert strategy._stability_check({}, decision([("ALFA", .05)]), "d", NOW, strategy.model_budget) is None
    assert calls == []


def test_a_failed_second_answer_is_recorded_not_raised(tmp_path):
    strategy, _ = harness(tmp_path, [RiskError("Invalid structured agent answer: manager")])
    assert strategy._stability_check({}, decision([("ALFA", .05)]), "d", NOW, strategy.model_budget) is None
    [event] = events(strategy, "decision_stability")
    assert not event["valid"] and event["error"].startswith("Invalid structured agent answer")


def test_the_decision_path_hands_the_check_the_strategy_context_the_manager_saw(tmp_path):
    strategy = model_harness(tmp_path)
    seen, checked = [], []
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]

    def call(name, **kwargs):
        seen.append((name, kwargs.get("context")))
        return {"summary": "s", "evidence": evidence} if name == "researcher" else decision([("ALFA", .05)])
    strategy._agent_call = call
    strategy._stability_check = lambda context, accepted, decision_id, now, budget, **kwargs: checked.append(
        (context, accepted, decision_id, kwargs["task_prompt"]))
    strategy._decide(account(), NOW, False)
    assert checked == []  # Not inside the decision: its call would hold up stops and order repricing.
    strategy._run_pending_stability(NOW + timedelta(minutes=1))
    manager_context = next(context for name, context in seen if name == "manager")
    [(context, accepted, decision_id, task)] = checked
    assert context is manager_context and accepted["targets"][0]["symbol"] == "ALFA" and decision_id
    assert task == DECISION_TASK
    strategy._run_pending_stability(NOW + timedelta(minutes=2))
    assert len(checked) == 1  # Asked once.


def test_stability_repeats_the_accepted_correction_and_excludes_legacy_comparisons(tmp_path):
    from tests.test_autonomous_fund_decision_correction import PROSE, answer, fund, valid

    strategy, calls = fund(tmp_path, [answer(PROSE, Decision), valid(), valid()])
    strategy.mode = "local"
    strategy.validation = ValidationSettings()
    strategy._session_research = lambda now: {"research": {}}
    strategy._decide(account(), NOW, False)
    strategy.journal.event(NOW, "decision_stability", {
        "valid": True, "decision_id": "legacy", "weight_distance": .42, "symbol_overlap": .5, "same_targets": False})

    repeat = strategy._run_pending_stability(NOW + timedelta(minutes=1))

    _, correction, repeated = [call for call in calls if call["name"] == "manager"]
    assert correction["task_prompt"] == repeated["task_prompt"] == DECISION_CORRECTION_TASK
    assert correction["context"] == repeated["context"]
    assert repeat["input_scope"] == INPUT_SCOPE
    stability = strategy._scorecards(NOW + timedelta(minutes=1))["stability"]
    assert stability["checks"] == 1 and stability["mean_weight_distance"] == 0


def test_the_repeat_waits_for_open_orders_and_never_crosses_a_session(tmp_path):
    strategy = model_harness(tmp_path)
    checked = []
    strategy._stability_check = lambda context, accepted, decision_id, now, budget, **kwargs: checked.append(decision_id)
    open_orders = [{"id": "o-1"}]
    strategy.journal.pending = lambda: open_orders
    strategy._stability_pending = ({}, DECISION_TASK, decision([("ALFA", .05)]), "d-1", NOW)
    strategy._stability_pending = ({}, DECISION_TASK, decision([("ALFA", .06)]), "d-2", NOW + timedelta(minutes=5))
    strategy._run_pending_stability(NOW + timedelta(minutes=6))
    assert checked == []  # Our order is still working.
    open_orders.clear()
    strategy._run_pending_stability(NOW + timedelta(minutes=7))
    assert checked == ["d-2"]
    strategy._stability_pending = ({}, DECISION_TASK, decision([("ALFA", .05)]), "d-3", NOW)
    strategy._run_pending_stability(NOW + timedelta(days=1))
    assert checked == ["d-2"] and strategy._stability_pending is None  # Yesterday's decision is not asked today.


def test_settings_and_report_line():
    from scripts.autonomous_fund.notifications import evidence_lines
    assert ValidationSettings().stability_checks_per_session == 1
    with pytest.raises(RiskError, match="stability_checks_per_session"):
        ValidationSettings(stability_checks_per_session=4)
    card = {"stability": {"input_scope": INPUT_SCOPE, "checks": 6, "mean_weight_distance": .04, "same_rate": .5,
                          "mean_consecutive_distance": .05, "noise_ratio": .8}}
    [line] = [text for text in evidence_lines(card) if text.startswith("决策稳定性")]
    # The ratio includes fresh runtime state, so the report now describes what was measured.
    assert "6 次" in line and "4.00% 的仓位不同" in line and "复问差异相当于真实调整的 80.00%" in line
    assert not [text for text in evidence_lines({"stability": {"checks": 0}}) if text.startswith("决策稳定性")]
