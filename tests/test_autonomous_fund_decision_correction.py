"""The intraday manager decision gets one schema-guided correction when its answer's format fails.

2026-11 server incident (event #19744): "Invalid structured agent answer: manager; schema_validation:
$ (model_type)" — the answer was not one JSON object, and the round was dropped without a correction.
Synthetic answers only; no network, broker or model inference.
"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from lumibot.components.agents.structured_output import parse_structured_output
from scripts.autonomous_fund.diagnostics import answer_shape, structured_answer_diagnostic
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.strategy import (DECISION_CORRECTION_TASK, DECISION_TASK, AutonomousFund, Decision,
                                              Research)
from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness

PROSE = "I reviewed the snapshot [1] and will keep the current targets."  # A bracket a lenient parser picks up.


def answer(text, schema, payload=None):
    parsed, error = parse_structured_output(text, schema.model_json_schema(), schema)
    return SimpleNamespace(model="deepseek/deepseek-v4-pro", cache_hit=False, usage={"input_tokens": 10,
                           "output_tokens": 5}, summary=text, parsed=parsed, parse_error=error, payload=payload or {})


def research():
    return answer(json.dumps({"summary": "synthetic", "evidence": [{
        "source": "fixture", "available_at": (NOW - timedelta(days=1)).isoformat(), "observed_at": NOW.isoformat(),
        "estimated": False, "estimate_reason": ""}]}), Research)


def fund(tmp_path, manager_answers):
    strategy = model_harness(tmp_path)
    del strategy._record_agent  # The real validation, exactly as on the server.
    strategy.scripted, strategy.agent_models = False, {"researcher": "deepseek/deepseek-flash",
                                                       "manager": "deepseek/deepseek-v4-pro"}
    calls, answers = [], list(manager_answers)

    def call(name, purpose=None, **kwargs):
        calls.append({"name": name, "purpose": purpose, **kwargs})
        return research() if name == "researcher" else answers.pop(0)
    strategy._agent_call = call
    return strategy, calls


def valid():
    return answer(json.dumps(decision([("ALFA", .05)])), Decision)


def events(strategy, kind):
    return [row["body"] for row in strategy.journal.all_events() if row["kind"] == kind]


def test_the_incident_answer_is_no_longer_read_as_a_list():
    # Before: the lenient scan returned "[1]" from the prose, so "schema_validation: $ (model_type)".
    assert structured_answer_diagnostic(answer(PROSE, Decision), Decision) == {"kind": "invalid_json",
                                                                              "answer_shape": "prose"}
    # Prose around a real decision now parses on the first call, the bracket skipped.
    wrapped = answer(PROSE + " Final: " + json.dumps(decision([("ALFA", .05)])), Decision)
    assert wrapped.parse_error is None and wrapped.parsed.targets[0].symbol == "ALFA"
    # A whole answer of the wrong type is still reported precisely.
    assert structured_answer_diagnostic(answer("[1]", Decision), Decision) == {
        "kind": "schema_validation", "count": 1, "answer_shape": "array", "errors": [{"path": "$", "type": "model_type"}]}
    assert [answer_shape(t) for t in ('{"a": 1}', "[1]", '"x"', "3", "true", "null", "```json\n{}\n```",
                                      "note: {}")] == ["object", "array", "string", "number", "boolean", "null",
                                                       "fenced", "prose"]


def test_a_malformed_decision_gets_one_correction_and_the_round_continues(tmp_path):
    strategy, calls = fund(tmp_path, [answer(PROSE, Decision), valid()])
    strategy._decide(account(), NOW, False)
    managers = [c for c in calls if c["name"] == "manager"]
    assert [c["task_prompt"] for c in managers] == [DECISION_TASK, DECISION_CORRECTION_TASK]
    assert managers[1]["purpose"] == "decision_correction"
    correction = managers[1]["context"]["structured_answer_correction"]
    assert correction["previous_answer"] == PROSE and not correction["previous_answer_truncated"]
    assert correction["diagnostic"] == {"kind": "invalid_json", "answer_shape": "prose"}
    assert "ALFA" not in json.dumps(correction["diagnostic"])  # Locations and types only, never input values.
    [retry] = events(strategy, "decision_format_retry")
    assert retry["answer_diagnostic"]["answer_shape"] == "prose"
    agents = [e for e in events(strategy, "agent") if e["name"] == "manager"]
    assert len(agents) == 2 and "answer_diagnostic" in agents[0] and "answer_diagnostic" not in agents[1]
    assert events(strategy, "decision_validated") and not events(strategy, "decision_backoff")
    assert strategy.journal.get_meta("decision_failures") in (0, None)


def test_a_rationale_continuation_requires_a_complete_model_correction(tmp_path):
    body = decision([("ALFA", .05)])
    continuation = "Synthetic comparison with the previous plan."
    malformed = answer(json.dumps({**body, "rationale_continue": continuation}), Decision)
    corrected = {**body, "rationale": body["rationale"] + " " + continuation}
    strategy, calls = fund(tmp_path, [malformed, answer(json.dumps(corrected), Decision)])

    strategy._decide(account(), NOW, False)

    [correction] = [call for call in calls if call["purpose"] == "decision_correction"]
    previous = correction["context"]["structured_answer_correction"]
    assert json.loads(previous["previous_answer"])["rationale_continue"] == continuation
    assert previous["diagnostic"]["errors"] == [{"path": "<extra>", "type": "extra_forbidden"}]
    agents = [event for event in events(strategy, "agent") if event["name"] == "manager"]
    assert len(agents) == 2 and agents[0]["parse_error"] and not agents[1]["parse_error"]
    # Only the model's complete corrected object is adopted; no text is silently discarded.
    [accepted] = events(strategy, "decision_validated")
    assert accepted["rationale"] == corrected["rationale"] and "rationale_continue" not in accepted
    assert not events(strategy, "decision_backoff")


@pytest.mark.parametrize("bound", ["minimum", "maximum"])
def test_overflowing_claim_bound_is_corrected_before_the_decision_is_written(tmp_path, bound):
    body = {**decision([("ALFA", .05)]),
            "factual_claims": [{"symbol": "ALFA", "metric": "ret_20d", bound: 9.876}]}
    overflow = answer(json.dumps(body).replace("9.876", "1e309"), Decision)
    strategy, calls = fund(tmp_path, [overflow, valid()])

    strategy._decide(account(), NOW, False)

    [correction] = [call for call in calls if call["purpose"] == "decision_correction"]
    errors = correction["context"]["structured_answer_correction"]["diagnostic"]["errors"]
    assert errors == [{"path": f"factual_claims[0].{bound}", "type": "finite_number"}]
    assert len(events(strategy, "decision_validated")) == 1


def test_a_second_malformed_answer_backs_off_as_before_and_says_it_was_corrected(tmp_path):
    strategy, calls = fund(tmp_path, [answer(PROSE, Decision), answer('{"rationale": "half"}', Decision)])
    with pytest.raises(RiskError, match=r"also after one format correction") as raised:
        strategy._decide(account(), NOW, False)
    assert "schema_validation" in str(raised.value)
    assert len([c for c in calls if c["name"] == "manager"]) == 2  # Never a third paid call.
    assert strategy.journal.get_meta("decision_failures") == 1 and events(strategy, "decision_backoff")


def test_long_previous_answers_are_bounded(tmp_path):
    strategy, calls = fund(tmp_path, [answer("x" * 9000, Decision), valid()])
    strategy._decide(account(), NOW, False)
    correction = [c for c in calls if c["name"] == "manager"][1]["context"]["structured_answer_correction"]
    assert len(correction["previous_answer"]) == 6000 and correction["previous_answer_truncated"]


@pytest.mark.parametrize("blocker", ["runtime_error", "empty", "stop", "no_room", "cost"])
def test_no_correction_for_provider_failures_a_stop_or_an_exhausted_budget(tmp_path, blocker):
    first = (answer("", Decision, payload={"runtime_error": True, "error_class": "TimeoutError"})
             if blocker == "runtime_error" else answer("", Decision) if blocker == "empty" else answer(PROSE, Decision))
    strategy, calls = fund(tmp_path, [first, valid()])
    if blocker == "stop":
        original = strategy._agent_call

        def stop_during_answer(name, **kwargs):
            result = original(name, **kwargs)
            if name == "manager":
                strategy.halt_path.write_text("synthetic stop")
            return result
        # Exercise correction gating after STOP arrives in flight; an existing
        # STOP now prevents the initial decision call altogether.
        strategy._agent_call = stop_during_answer
    elif blocker == "no_room":
        budget = strategy.model_budget
        original = strategy.journal.counter

        def counter(name, at, add=0):  # Room for the decision, none for a correction.
            value = original(name, at, add)
            return budget.max_agent_runs_per_day - budget.reserved_review_runs if name == "agent_runs" and \
                any(c["name"] == "manager" for c in calls) else value
        strategy.journal.counter = counter
    elif blocker == "cost":
        strategy._cost_exhausted = lambda now, step: step == "decision_correction"
    with pytest.raises(RiskError):
        strategy._decide(account(), NOW, False)
    assert len([c for c in calls if c["name"] == "manager"]) == 1
    assert not events(strategy, "decision_format_retry")


def test_startup_records_which_roles_get_an_enforced_schema(tmp_path):
    strategy = model_harness(tmp_path)
    strategy.scripted = False
    logged = []
    strategy.log_message = lambda message, color=None: logged.append(message)
    strategy.agent_models = {"researcher": "deepseek/deepseek-flash", "manager": "openai/gpt-synthetic"}
    protocols = AutonomousFund._record_output_protocols(strategy, "local")
    assert protocols == {"researcher": "deepseek_responses_json_schema_v2", "manager": None}
    [event] = events(strategy, "structured_output_protocol")
    assert event["protocols"] == protocols and event["models"]["manager"] == "openai/gpt-synthetic"
    assert logged and "manager=openai/gpt-synthetic" in logged[0] and "researcher" not in logged[0].split(":")[1]
    strategy.agent_models = {"researcher": "deepseek/deepseek-flash", "manager": "deepseek/deepseek-v4-pro"}
    logged.clear()
    AutonomousFund._record_output_protocols(strategy, "local")
    assert logged == []  # Both enforced: nothing to say.
