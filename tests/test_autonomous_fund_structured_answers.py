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

