"""Bounded critic format correction and executable SQL examples; no inference or network."""
import json

import duckdb
import pytest

from scripts.autonomous_fund.strategy import AutonomousFund, Critique, SQL_NOTIONAL_EXAMPLE
from .test_autonomous_fund_construction import critic_strategy
from .test_autonomous_fund_decision_correction import answer, events


def critic(tmp_path, results, *, runs_used=0, mode="local", after=None):
    pending, calls = list(results), []

    def run(**kwargs):
        calls.append(kwargs)
        result = pending.pop(0)
        if after:
            after(fake, len(calls))
        if isinstance(result, Exception):
            raise result
        return result

    fake, budget, now = critic_strategy(tmp_path, run, runs_used)
    fake.mode, fake.halt_path = mode, tmp_path / "STOP"
    fake._with_fund_cost_context = lambda context, at: {**context, "costs_refreshed": True}
    return fake, budget, now, calls


def good():
    return answer(json.dumps({"summary": "Synthetic counterargument", "concerns": [], "missing_evidence": []}), Critique)


def critique(fake, budget, now, scope=None):
    return AutonomousFund._critique(fake, {"account": {"positions": {}}}, {"summary": "Synthetic research"},
                                    budget, now, evaluation_calls=scope)


def test_one_correction_keeps_both_calls_and_caches_only_the_valid_critique(tmp_path):
    bad = answer("Synthetic prose without a JSON object", Critique)
    fake, budget, now, calls = critic(tmp_path, [bad, good()])
    scope = []
    result = critique(fake, budget, now, scope)
    assert result["summary"] == "Synthetic counterargument"
    assert len(calls) == len(scope) == 2
    correction = calls[1]["context"]["structured_answer_correction"]
    assert correction["diagnostic"]["kind"] == "invalid_json"
    assert correction["previous_answer"] == bad.summary and calls[1]["context"]["costs_refreshed"]
    recorded = events(fake, "agent")
    assert [item["parse_error"] is None for item in recorded] == [False, True]
    outcomes = events(fake, "agent_outcome")
    assert [(item["valid"], item["purpose"]) for item in outcomes] == [
        (False, "critique"), (True, "critique_correction")]
    assert outcomes[1]["corrected"] is True
    assert fake._current_critique_source["call_id"] == scope[1]
    assert events(fake, "critic_correction_requested")[0]["original_call_id"] == scope[0]
    assert critique(fake, budget, now) == result and len(calls) == 2
    assert fake._current_critique_source["reused"] is True


@pytest.mark.parametrize("cause", ["stopped", "model_budget", "model_cost_budget"])
def test_correction_respects_stop_cost_and_reserved_manager_call(tmp_path, cause):
    def after(fake, count):
        if cause == "stopped":
            fake.halt_path.touch()
        elif cause == "model_cost_budget":
            fake._cost_exhausted = lambda now, step: True

    fake, budget, now, calls = critic(tmp_path, [answer("Invalid answer", Critique)], after=after)
    if cause == "model_budget":
        # There is room for the first critic and manager, but not a second critic plus manager.
        fake.journal.counter("agent_runs", now, budget.max_agent_runs_per_day - budget.reserved_review_runs - 2)
    assert critique(fake, budget, now) is None
    assert len(calls) == 1 and not fake.journal.get_meta("critique_cache")
    assert events(fake, "critic_correction_skipped")[0]["reason"] == cause
    assert events(fake, "critic_unavailable")


@pytest.mark.parametrize("first", [answer("", Critique), answer("Invalid answer", Critique,
    {"runtime_error": True, "error_class": "TimeoutError"}), ConnectionError("synthetic provider failure")])
def test_missing_answer_or_provider_failure_is_not_retried_as_a_format_error(tmp_path, first):
    fake, budget, now, calls = critic(tmp_path, [first])
    assert critique(fake, budget, now) is None and len(calls) == 1
    assert not events(fake, "critic_correction_requested")


def test_second_invalid_answer_is_not_cached_or_retried_again(tmp_path):
    fake, budget, now, calls = critic(tmp_path, [answer("First invalid", Critique), answer("Second invalid", Critique)])
    assert critique(fake, budget, now) is None and len(calls) == 2
    assert not fake.journal.get_meta("critique_cache")
    assert [item["valid"] for item in events(fake, "agent_outcome")] == [False, False]


def test_stop_during_correction_context_refresh_prevents_the_second_call(tmp_path):
    fake, budget, now, calls = critic(tmp_path, [answer("Invalid answer", Critique)])
    refreshes = []

    def refresh(context, at):
        refreshes.append(at)
        if len(refreshes) == 2:
            fake.halt_path.touch()
        return context

    fake._with_fund_cost_context = refresh
    assert critique(fake, budget, now) is None and len(calls) == 1
    assert events(fake, "critic_correction_skipped")[0]["reason"] == "stopped"


def test_historical_critic_keeps_its_existing_single_attempt_contract(tmp_path):
    fake, budget, now, calls = critic(tmp_path, [answer("Invalid answer", Critique)], mode="backtest")
    assert critique(fake, budget, now) is None and len(calls) == 1


@pytest.mark.parametrize("definition,value,quantity", [
    ("DECIMAL(18,2)", "1234567890123456.78", 89),
    ("DECIMAL(38,20)", "1.23456789012345678901", "1.23456789012345678901"),
])
def test_sql_example_fixes_intermediate_overflow_without_changing_source_evidence(definition, value, quantity):
    with duckdb.connect() as db:
        db.execute(f"CREATE TABLE holdings(price {definition}, quantity {definition})")
        db.execute("INSERT INTO holdings VALUES (?,?)", [value, quantity])
        before = db.execute("SELECT * FROM holdings").fetchone()
        # Casting the result does not avoid the failing intermediate decimal operation.
        with pytest.raises(duckdb.OutOfRangeException):
            db.execute("SELECT CAST(price * quantity AS DOUBLE) FROM holdings").fetchall()
        [notional] = db.execute(SQL_NOTIONAL_EXAMPLE).fetchone()
        assert notional == pytest.approx(float(value) * float(quantity))
        assert db.execute("SELECT * FROM holdings").fetchone() == before
