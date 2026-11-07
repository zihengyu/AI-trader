"""Research output compatibility and actionable failures without model inference."""
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from lumibot.components.agents.structured_output import parse_structured_output
from scripts.autonomous_fund.risk import RiskError, validate_evidence
from scripts.autonomous_fund.strategy import Critique, Decision, Research
from .test_autonomous_fund_review import CLOSE, events, review_harness


def research_body():
    return {"summary": "Synthetic research, not an investment recommendation.", "evidence": [{
        "source": "Synthetic source", "available_at": (CLOSE - timedelta(days=1)).isoformat(),
        "observed_at": CLOSE.isoformat(), "estimated": False, "estimate_reason": ""}]}


def result_for(body, schema=Research, *, fenced=False, payload=None):
    text = json.dumps(body)
    if fenced:
        text = "```json\n" + text + "\n```"
    parsed, error = parse_structured_output(text, schema.model_json_schema(), schema)
    return SimpleNamespace(model="fixture/research", cache_hit=False,
                           usage={"input_tokens": 11, "output_tokens": 7}, summary=text,
                           parsed=parsed, parse_error=error, payload=payload or {})


@pytest.mark.parametrize("reason,note", [("", ""), ("Known timestamp", ""),
                                         ("Estimated from the reporting period", "Estimated from the reporting period"),
                                         ("", "Estimated from the reporting period"),
                                         (None, "Estimated from the reporting period")])
def test_explanation_aliases_keep_one_canonical_reason_without_mutating_input(reason, note):
    body = research_body()
    item = body["evidence"][0]
    item["estimated_note"] = note
    if reason is None:
        del item["estimate_reason"]
    else:
        item["estimate_reason"] = reason
    original = deepcopy(body)
    result = result_for(body)
    assert result.parse_error is None
    saved = result.parsed.model_dump()["evidence"][0]
    assert saved["estimate_reason"] == (reason or note)
    assert "estimated_note" not in saved
    assert body == original
    assert saved["available_at"] == item["available_at"]
    assert saved["observed_at"] == item["observed_at"]


@pytest.mark.parametrize("changes", [
    {"estimate_reason": "First explanation", "estimated_note": "Conflicting explanation"},
    {"estimated_note": 7}, {"estimated_note": None}, {"estimated_note": {"text": "untyped"}},
    {"unknown_field": ""}, {"estimated": "false"}, {"source": ""},
])
def test_alias_compatibility_preserves_strict_types_and_rejects_conflicts_or_other_extras(changes):
    body = research_body()
    body["evidence"][0].update(changes)
    assert result_for(body).parse_error


@pytest.mark.parametrize("field", ["source", "available_at", "observed_at", "estimated", "estimate_reason"])
def test_evidence_still_requires_every_canonical_field_unless_its_explicit_alias_is_present(field):
    body = research_body()
    del body["evidence"][0][field]
    assert result_for(body).parse_error


def test_alias_does_not_bypass_historical_cutoffs_or_require_dates_for_live_sources():
    body = research_body()
    item = body["evidence"][0]
    item.pop("estimate_reason")
    item.update(estimated=True, estimated_note="Estimated from the reporting period")
    evidence = result_for(body).parsed.model_dump()["evidence"]
    validate_evidence(evidence, CLOSE, True)
    for field in ("available_at", "observed_at"):
        future = deepcopy(evidence)
        future[0][field] = (CLOSE + timedelta(seconds=1)).isoformat()
        with pytest.raises(RiskError, match="after the simulated cutoff"):
            validate_evidence(future, CLOSE, True)
    item["available_at"] = None
    evidence = result_for(body).parsed.model_dump()["evidence"]
    validate_evidence(evidence, CLOSE, False)
    with pytest.raises(RiskError, match="readable"):
        validate_evidence(evidence, CLOSE, True)
    item.update(available_at=(CLOSE - timedelta(days=1)).isoformat(), estimated_note="")
    with pytest.raises(RiskError, match="require an explanation"):
        validate_evidence(result_for(body).parsed.model_dump()["evidence"], CLOSE, True)


def test_postmarket_review_completes_and_reuses_saved_research_with_redundant_note(tmp_path):
    strategy, calls = review_harness(tmp_path)
    original = strategy.agents["researcher"].run

    def researcher(**kwargs):
        body = json.loads(original(**kwargs).summary)
        body["evidence"][0]["estimated_note"] = ""
        return result_for(body)

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    strategy.after_market_closes()
    assert [role for role, _ in calls] == ["researcher", "manager"]
    assert len(events(strategy, "postmarket_review")) == 1
    saved = events(strategy, "postmarket_research")[0]["research"]["evidence"][0]
    assert "estimated_note" not in saved
    assert not events(strategy, "notification")
    assert not strategy.journal.pending()


@pytest.mark.parametrize("role,schema,body,path,code", [
    ("researcher", Research, {**research_body(), "evidence": [{
        **research_body()["evidence"][0], "observed_at": 7}]}, "evidence[0].observed_at", "string_type"),
    ("critic", Critique, {"summary": 7}, "summary", "string_type"),
    ("manager", Decision, {"rationale": "Synthetic incomplete decision"}, "as_of", "missing"),
])
def test_invalid_answers_report_role_field_and_code_without_echoing_input(tmp_path, role, schema, body, path, code):
    strategy, _ = review_harness(tmp_path)
    result = result_for(body, schema, fenced=True)
    with pytest.raises(RiskError) as raised:
        strategy._record_agent(role, result)
    assert role in str(raised.value)
    assert path in str(raised.value) and code in str(raised.value)
    saved = events(strategy, "agent")[-1]
    assert saved["parse_error"] == result.parse_error
    assert saved["answer_diagnostic"]["kind"] == "schema_validation"
    assert saved["usage"] == result.usage


def test_postmarket_schema_failure_notifies_the_field_without_research_text_or_input_values(tmp_path):
    strategy, calls = review_harness(tmp_path)
    body = research_body()
    body["evidence"][0]["observed_at"] = {"private": "synthetic-private-token"}
    body["evidence"][0]["synthetic-private-key"] = "synthetic-private-token"
    strategy.agents["researcher"].run = lambda **kwargs: result_for(body)
    strategy.after_market_closes()
    notice = events(strategy, "notification")[-1]
    assert notice["kind"] == "postmarket_review_error"
    assert "researcher" in notice["details"] and "postmarket_review" in notice["details"]
    assert "evidence[0].observed_at" in notice["details"] and "string_type" in notice["details"]
    assert "<extra>" in notice["details"] and "extra_forbidden" in notice["details"]
    assert "synthetic-private" not in notice["details"]
    assert not calls and not events(strategy, "postmarket_review")
    assert not strategy.journal.pending()


def test_runtime_timeout_is_distinguished_from_a_schema_error_without_echoing_provider_message(tmp_path):
    strategy, _ = review_harness(tmp_path)
    result = result_for(research_body())
    result.parsed, result.parse_error = None, None
    result.summary = "Provider failure: synthetic-private-token"
    result.payload = {"runtime_error": True, "error_class": "TimeoutError",
                      "error_message": "request URL contained synthetic-private-token"}
    with pytest.raises(RiskError) as raised:
        strategy._record_agent("manager", result, phase="postmarket_review")
    assert "TimeoutError" in str(raised.value) and "runtime_error" in str(raised.value)
    assert "synthetic-private" not in str(raised.value)


def test_postmarket_manager_diagnostics_use_the_review_schema_and_bound_the_notice(tmp_path):
    from scripts.autonomous_fund.review import PostmarketReview

    strategy, _ = review_harness(tmp_path)
    result = result_for({"summary": "Synthetic incomplete review"}, PostmarketReview)
    with pytest.raises(RiskError) as raised:
        strategy._record_agent("manager", result, phase="postmarket_review")
    diagnostic = events(strategy, "agent")[-1]["answer_diagnostic"]
    assert diagnostic["count"] > len(diagnostic["errors"]) == 5
    assert "session_date (missing)" in str(raised.value)
    assert "targets" not in str(raised.value)
    assert len(str(raised.value)) < 500


def test_critic_accepts_only_the_empty_placeholder_without_discarding_the_critique():
    body = {"summary": "Synthetic counterargument", "concerns": [],
            "missing_evidence": ["Independent confirmation"], "missing_evidence_placeholder": None}
    original = deepcopy(body)
    result = result_for(body, Critique)
    assert result.parse_error is None
    assert result.parsed.model_dump() == {key: value for key, value in body.items()
                                         if key != "missing_evidence_placeholder"}
    assert body == original


@pytest.mark.parametrize("placeholder", ["Unverified claim", "", [], {}, False])
def test_critic_never_discards_content_in_the_placeholder(placeholder):
    assert result_for({"summary": "Synthetic counterargument",
                       "missing_evidence_placeholder": placeholder}, Critique).parse_error


@pytest.mark.parametrize("text,kind", [("", "missing_answer"), ("No JSON was returned", "invalid_json")])
def test_empty_and_non_json_answers_have_safe_diagnostic_categories(tmp_path, text, kind):
    strategy, _ = review_harness(tmp_path)
    result = result_for(research_body())
    result.parsed, result.parse_error, result.summary = None, "untrusted parse error", text
    with pytest.raises(RiskError, match=kind):
        strategy._record_agent("researcher", result)


@pytest.mark.parametrize("mode", ["local", "paper", "backtest"])
@pytest.mark.parametrize("bad_answer", ["{}", '{"ideas": []}', '{"summary": "Incomplete research"}',
                                       "Synthetic prose without JSON"])
def test_postmarket_research_gets_one_audited_format_correction(tmp_path, mode, bad_answer):
    from scripts.autonomous_fund.review import strategy_memory

    strategy, calls = review_harness(tmp_path)
    strategy.mode = mode
    strategy.model_budget = SimpleNamespace(max_agent_runs_per_day=3, max_daily_cost_usd=None)
    original = strategy.agents["researcher"].run
    attempts = []

    def researcher(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            result = result_for({})
            result.summary = bad_answer
            result.parsed, result.parse_error = parse_structured_output(
                bad_answer, Research.model_json_schema(), Research)
            return result
        result = original(**kwargs)
        if mode != "backtest":
            # Live undated sources remain usable after correction.
            result.parsed.evidence[0].available_at = None
            result.summary = result.parsed.model_dump_json()
        return result

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    strategy.after_market_closes()
    assert len(attempts) == 2  # A completed review never repays either stage.
    assert all(attempt["output_schema"] is Research for attempt in attempts)
    first, second = (attempt["context"] for attempt in attempts)
    correction = second["structured_answer_correction"]
    assert correction["previous_answer"] == bad_answer
    assert {key: value for key, value in second.items() if key != "structured_answer_correction"} == first
    assert first["orders_enabled"] is False
    if bad_answer == "{}":
        assert correction["diagnostic"]["errors"] == [
            {"path": "summary", "type": "missing"}, {"path": "evidence", "type": "missing"}]
    [retry] = events(strategy, "postmarket_research_retry")
    assert retry["answer_diagnostic"] == correction["diagnostic"]
    agents = events(strategy, "agent")
    assert [agent["name"] for agent in agents] == ["researcher", "researcher", "manager"]
    assert agents[0]["summary"] == bad_answer and agents[0]["parse_error"]
    assert sum(agent["usage"]["input_tokens"] for agent in agents) == 33
    assert sum(agent["usage"]["output_tokens"] for agent in agents) == 21
    assert strategy.journal.counter("agent_runs", CLOSE) == 3
    [saved] = events(strategy, "postmarket_research")
    # Compare the persisted snapshot after the journal's Decimal serialization.
    assert saved["context"] == json.loads(json.dumps(first, default=str))
    assert "structured_answer_correction" not in saved["context"]
    assert len(events(strategy, "postmarket_review")) == 1
    assert strategy_memory(strategy.journal, CLOSE)["version"] == 1
    assert [name for name, _ in calls] == ["researcher", "manager"]
    assert not events(strategy, "notification") and not strategy.journal.pending()


def test_failed_postmarket_correction_keeps_the_previous_memo_and_both_invalid_answers(tmp_path):
    from scripts.autonomous_fund.review import strategy_memory

    strategy, calls = review_harness(tmp_path)
    strategy.after_market_closes()
    tomorrow = CLOSE + timedelta(days=1)
    strategy.get_datetime = lambda: tomorrow
    strategy.journal.event(tomorrow - timedelta(hours=4), "account", {
        "cash": "9100", "equity": "10000", "positions": {"ALFA": 9}})
    attempts = []

    def researcher(**kwargs):
        attempts.append(kwargs)
        return result_for({"summary": "Synthetic private research text"})

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    assert len(attempts) == 2
    assert len(events(strategy, "postmarket_research_retry")) == 1
    assert len(events(strategy, "postmarket_research")) == len(events(strategy, "postmarket_review")) == 1
    assert strategy_memory(strategy.journal, tomorrow)["version"] == 1
    assert [name for name, _ in calls] == ["researcher", "manager"]
    assert [agent["parse_error"] is not None for agent in events(strategy, "agent")[-2:]] == [True, True]
    notice = events(strategy, "notification")[-1]
    assert "researcher/postmarket_review" in notice["details"] and "evidence (missing)" in notice["details"]
    assert "private research" not in json.dumps(events(strategy, "postmarket_research_retry"))
    assert "private research" not in notice["details"]
    assert not strategy.journal.pending()


@pytest.mark.parametrize("blocked", ["stop", "cost", "runs"])
def test_postmarket_research_correction_respects_stop_cost_and_room_for_the_manager(tmp_path, blocked):
    strategy, calls = review_harness(tmp_path)
    strategy.model_budget = SimpleNamespace(max_agent_runs_per_day=2 if blocked == "runs" else 60,
                                            max_daily_cost_usd=None)
    cost_checks, attempts = [], []

    def cost_exhausted(now, step):
        cost_checks.append(step)
        return blocked == "cost"

    def researcher(**kwargs):
        attempts.append(kwargs)
        if blocked == "stop":
            strategy.halt_path.touch()
        return result_for({})

    strategy._cost_exhausted = cost_exhausted
    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    assert len(attempts) == strategy.journal.counter("agent_runs", CLOSE) == 1
    assert not calls and not events(strategy, "postmarket_research_retry")
    assert not events(strategy, "postmarket_research") and not events(strategy, "postmarket_review")
    if blocked == "cost":
        assert "postmarket_research_correction" in cost_checks
    assert not strategy.journal.pending()


@pytest.mark.parametrize("failure", ["runtime", "empty", "transport"])
def test_postmarket_non_format_failures_do_not_start_a_correction(tmp_path, failure):
    strategy, calls = review_harness(tmp_path)
    attempts = []

    def researcher(**kwargs):
        attempts.append(kwargs)
        if failure == "transport":
            raise ConnectionError("Synthetic private provider message")
        result = result_for({})
        if failure == "runtime":
            result.payload = {"runtime_error": True, "error_class": "TimeoutError"}
        else:
            result.summary = ""
        return result

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    assert len(attempts) == 1 and not calls
    assert not events(strategy, "postmarket_research_retry")
    assert not events(strategy, "postmarket_research") and not events(strategy, "postmarket_review")
    assert len(events(strategy, "agent")) == 1
    assert "private provider" not in events(strategy, "notification")[-1]["details"]


@pytest.mark.parametrize("corrected", [False, True])
def test_postmarket_format_correction_never_bypasses_historical_evidence_cutoffs(tmp_path, corrected):
    strategy, calls = review_harness(tmp_path)
    strategy.mode = "backtest"
    attempts = []

    def researcher(**kwargs):
        attempts.append(kwargs)
        if corrected and len(attempts) == 1:
            return result_for({})
        body = research_body()
        body["evidence"][0]["available_at"] = (CLOSE + timedelta(days=1)).isoformat()
        return result_for(body)

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    assert len(attempts) == (2 if corrected else 1)
    assert len(events(strategy, "postmarket_research_retry")) == int(corrected)
    assert "after the simulated cutoff" in events(strategy, "notification")[-1]["details"]
    assert not calls and not events(strategy, "postmarket_review") and not events(strategy, "postmarket_research")
    assert not strategy.journal.pending()


def test_interrupted_correction_retains_unknown_usage_without_another_retry(tmp_path):
    strategy, calls = review_harness(tmp_path)
    attempts = []

    def researcher(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 2:
            raise ConnectionError("Synthetic private provider message")
        return result_for({})

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    assert len(attempts) == 2 and not calls
    agents = events(strategy, "agent")
    assert agents[0]["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert agents[1]["usage"] is None and agents[1]["parse_error"] == "ConnectionError"
    assert strategy.journal.counter("agent_runs", CLOSE) == 2
    assert len(events(strategy, "postmarket_research_retry")) == 1
    assert not events(strategy, "postmarket_research") and not events(strategy, "postmarket_review")
    assert events(strategy, "notification")[-1]["details"] == "ConnectionError"


def test_correction_costs_are_counted_with_the_original_failed_call(tmp_path):
    strategy, _ = review_harness(tmp_path)
    strategy.scripted = False  # Synthetic responses exercise real cost accounting without inference.
    strategy.config["model_prices_per_million"] = {
        model: {"input": 1, "output": 2} for model in ("fixture/research", "fixture/flash", "fixture/pro")}
    original = strategy.agents["researcher"].run
    attempts = []

    def researcher(**kwargs):
        attempts.append(kwargs)
        return result_for({}) if len(attempts) == 1 else original(**kwargs)

    strategy.agents["researcher"].run = researcher
    strategy.after_market_closes()
    assert len(attempts) == 2 and len(events(strategy, "postmarket_review")) == 1
    assert [agent["cost_usd"] for agent in events(strategy, "agent")] == [0.000025] * 3
    assert strategy.journal.counter("model_cost_microusd", CLOSE) == 75


def test_postmarket_correction_through_native_agent_handles_and_local_broker(tmp_path, monkeypatch):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.risk import Limits
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
    from .test_autonomous_fund_local import Clock

    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("LUMIBOT_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "universe": {"ALFA": "stock"}}
    journal = Journal(tmp_path, account_digest("local", "synthetic-format-account"))
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    requests = []

    class FormatRuntime(ScriptedRuntime):
        def run(self, request):
            requests.append(request)
            result = super().run(request)  # Also checks that trading mutation tools are absent.
            if len(requests) == 1:
                result.summary = "{}"
            return result

    strategy = AutonomousFund(broker=broker, name="FormatRecovery", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local",
        "account_binding": "synthetic-format-account", "runtime": FormatRuntime()})
    try:
        strategy._executor._initialize()
        strategy._snapshot()
        clock.value = CLOSE
        source._cache.clear()
        strategy.after_market_closes()
        strategy.after_market_closes()
        assert [request.agent_name for request in requests] == ["researcher", "researcher", "manager"]
        assert requests[1].context["structured_answer_correction"]["previous_answer"] == "{}"
        assert len(events(strategy, "postmarket_review")) == 1
        assert [agent["parse_error"] is not None for agent in events(strategy, "agent")] == [True, False, False]
        assert not journal.pending() and not events(strategy, "order_submitted")
    finally:
        broker._close_connection()

