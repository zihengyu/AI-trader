#!/usr/bin/env python3
"""Standalone DeepSeek schema/transport diagnostic; offline unless --real-models.

This checks the selected provider integration using synthetic evidence. It is
not a semantic agent eval, a strategy run or release/deployment qualification.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODELS = {"flash": "deepseek/deepseek-flash", "pro": "deepseek/deepseek-v4-pro"}
TOOL_NAME = "schema_probe_evidence"
# Every production answer schema, plus a probe of whether the provider enforces the constraints
# our local validation relies on. Requests per model for each case.
# A model may read the tool more than once before answering; production agents run several tool rounds.
MAX_TOOL_ROUNDS = 3
CASES = {"research": 1 + MAX_TOOL_ROUNDS, "decision": 1, "critique": 1, "review": 1, "constraints": 2}
REVIEW_REFS = ["event:1", "research:1", "context:session"]

_PROVIDER_CODES = {"invalid_request_error", "invalid_json_schema", "unsupported_parameter", "unsupported_value",
                   "model_not_found", "authentication_error", "insufficient_balance", "insufficient_quota",
                   "rate_limit_exceeded"}
_REQUEST_PARAMETERS = {"model", "input", "instructions", "tools", "tool_choice", "reasoning", "reasoning.effort",
                       "text", "text.format", "text.format.type", "text.format.schema", "response_format",
                       "max_output_tokens", "stream"}
_DIAGNOSTIC_TERMS = {
    "invalid", "unsupported", "unavailable", "available", "schema", "json_schema", "json_object",
    "response_format", "strict", "format", "type", "text", "tools", "tool", "input", "input_text", "output_text",
    "model", "thinking", "reasoning", "effort", "required", "properties", "additionalproperties",
    "minlength", "maxlength", "minitems", "maxitems", "default", "null", "string", "boolean", "expected",
    "missing", "must", "only", "not", "supported", "now", "parameter", "unknown", "error", "oneof", "anyof",
    "minimum", "maximum", "exclusiveminimum", "exclusivemaximum",
    "allof", "ref", "defs", "value", "in", "context", "and", "of", "for", "is", "the", "supplied", "every",
    "all", "keys", "including", "array", "object", "cannot", "together", "with", "additional", "contains",
}


class ProbeFailure(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def components():
    import httpx
    import jsonschema
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from lumibot.components.agents import deepseek_llm, runtime
    from lumibot.components.agents.structured_output import native_output_protocol, structured_output_instruction
    from scripts.autonomous_fund.review import PostmarketReview
    from scripts.autonomous_fund.strategy import Critique, Decision, Research

    return SimpleNamespace(models={"research": Research, "decision": Decision, "critique": Critique,
                                   "review": PostmarketReview},httpx=httpx, jsonschema=jsonschema, request=LlmRequest, types=types,
        adapter=deepseek_llm.DeepSeekResponsesLlm, resolve=runtime._resolve_model_for_adk,
        wire_schema=deepseek_llm.strict_output_schema,
        protocol=native_output_protocol, instruction=structured_output_instruction, schema_model=Research,
        aggregate=runtime._aggregate_usage_metadata,
        adapter_sha256=hashlib.sha256(Path(deepseek_llm.__file__).read_bytes()).hexdigest(),
        runtime_sha256=hashlib.sha256(Path(runtime.__file__).read_bytes()).hexdigest())


def provider_error_diagnostic(error):
    """Classify provider rejection without copying its message or input values.

    A bare 400 cannot distinguish unsupported schema output from a rejected
    keyword or a tool/reasoning combination. Only fixed labels and public API
    vocabulary leave this boundary; unknown detail remains local to the SDK.
    """
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    result = {}
    code = detail.get("code") or getattr(error, "code", None)
    if isinstance(code, str) and code in _PROVIDER_CODES:
        result["provider_code"] = code
    parameter = detail.get("param") or getattr(error, "param", None)
    if isinstance(parameter, str) and parameter in _REQUEST_PARAMETERS:
        result["parameter"] = parameter
    message = detail.get("message")
    if not isinstance(message, str) or not message:
        return result
    text = message.lower()
    unavailable = any(term in text for term in ("unavailable", "unsupported", "not supported", "not support"))
    schema = any(term in text for term in ("schema", "response_format", "text.format"))
    if schema and ("thinking" in text or "reasoning" in text) and unavailable:
        problem = "schema_reasoning_combination_rejected"
    elif schema and ("tools" in text or "tool calling" in text) and (
            unavailable or "together" in text or "cannot" in text):
        problem = "schema_tools_combination_rejected"
    elif schema and unavailable and any(term in text for term in ("minlength", "maxlength", "minitems", "maxitems",
                                                                 "default", "$ref", "$defs", "oneof", "anyof",
                                                                 "minimum", "maximum", "exclusiveminimum",
                                                                 "exclusivemaximum")):
        problem = "schema_keyword_rejected"
    elif schema and (unavailable or "unknown variant" in text or "supported values" in text) and (
            "json_schema" in text or "response_format" in text or "format type" in text):
        problem = "schema_format_unavailable"
    elif "required" in text and (schema or ("properties" in text and "object" in text)) and any(
            term in text for term in ("properties", "every", "all", "missing")):
        problem = "schema_required_fields_rejected"
    elif schema and ("invalid" in text or "additionalproperties" in text or "additional properties" in text):
        problem = "schema_definition_rejected"
    elif "model" in text and ("not found" in text or "does not exist" in text or "invalid model" in text):
        problem = "model_rejected"
    else:
        problem = "provider_request_rejected"
    result["provider_problem"] = problem
    result["diagnostic_terms"] = [word for word in re.findall(r"[a-z_]+", text) if word in _DIAGNOSTIC_TERMS][:40]
    result["message_sha256"] = hashlib.sha256(message.encode()).hexdigest()
    return result


def safe_error(error):
    # Provider exceptions can contain URLs, request bodies and credentials.
    result = {"type": type(error).__name__}
    if isinstance(error, ProbeFailure):
        result["code"] = str(error)
    code = getattr(error, "status_code", None)
    if type(code) is int:
        result["http_status"] = code
        result.update(provider_error_diagnostic(error))
    cause, seen = error, set()
    for _ in range(6):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        if isinstance(cause, socket.gaierror):
            result["connection_problem"] = "dns_resolution_failed"
            break
        if isinstance(cause, ssl.SSLCertVerificationError):
            result["connection_problem"] = "certificate_verification_failed"
            break
        cause = cause.__cause__ or cause.__context__
    if isinstance(error, ModuleNotFoundError):
        name = error.name or "unknown"
        result["missing_module"] = name if all(c.isalnum() or c in "_." for c in name) else "unknown"
    return result


def load_secrets(path):
    if path is None:
        return
    from dotenv import dotenv_values

    if not path.is_file():
        raise ProbeFailure("secret_file_not_found")
    if sys.platform.startswith("linux") and path.stat().st_mode & 0o077:
        raise ProbeFailure("secret_file_requires_0600")
    values = dotenv_values(path)
    # Explicit provider-only loading; existing process values take precedence.
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"):
        if name not in os.environ and values.get(name):
            os.environ[name] = values[name]


def offline_transport(c, model, schema, answer=None, tool_rounds=1):
    """Mock DeepSeek: the research tool round trip (`tool_rounds` calls before answering), or (with `answer`)
    one final JSON message."""
    wire_schema = c.wire_schema(schema)

    def handle(request):
        body = json.loads(request.content)
        if (str(request.url) != "https://api.deepseek.com/responses" or
                body["model"] != model.removeprefix("deepseek/") or
                body["text"]["format"]["schema"] != wire_schema):
            raise ProbeFailure("offline_request_contract_mismatch")
        results = [item for item in body["input"] if item["type"] == "function_call_output"]
        if answer is not None:
            output = [{"id": "msg_probe", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "annotations": [],
                                    "text": json.dumps(answer, ensure_ascii=False)}]}]
        elif len(results) < tool_rounds:
            output = [{"id": f"fc_probe_{len(results) + 1}", "type": "function_call",
                       "call_id": f"call_probe_{len(results) + 1}", "name": TOOL_NAME, "arguments": "{}"}]
        else:
            text = results[-1]["output"]
            if isinstance(text, list):
                text = "".join(part["text"] for part in text)
            evidence = json.loads(text)["evidence"]
            output = [{"id": "msg_probe", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "annotations": [], "text": json.dumps({
                           "summary": "这是接口测试使用的合成证据，没有投资结论。",
                           "evidence": [evidence], "ideas": []}, ensure_ascii=False)}]}]
        return c.httpx.Response(200, json={"id": "resp_probe", "object": "response", "created_at": 1,
            "model": body["model"], "status": "completed", "output": output,
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                      "input_tokens_details": {"cached_tokens": 5},
                      "output_tokens_details": {"reasoning_tokens": 2}}})

    return c.httpx.MockTransport(handle)


async def request_once(adapter, request, case, checkpoint, timeout):
    record = {"sequence": len(case["requests"]) + 1, "started_at": utc_now(), "status": "started", "usage": None}
    case["requests"].append(record)
    checkpoint()  # Record unknown usage before inference; interruption must not turn it into a free call.
    started = time.monotonic()
    try:
        async def collect():
            return [response async for response in adapter.generate_content_async(request)]

        responses = await asyncio.wait_for(collect(), timeout=timeout)
    except BaseException as error:
        record.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)) else "error",
                      error=safe_error(error), elapsed_seconds=round(time.monotonic() - started, 3))
        checkpoint()
        raise
    record.update(status="completed", finished_at=utc_now(), elapsed_seconds=round(time.monotonic() - started, 3))
    if len(responses) != 1:
        checkpoint()
        raise ProbeFailure("unexpected_response_count")
    response = responses[0]
    if response.usage_metadata is not None:
        record["usage"] = response.usage_metadata.model_dump(exclude_none=True)
    if response.error_code:
        record["provider_error_code"] = response.error_code
    checkpoint()
    if response.error_code:
        raise ProbeFailure("provider_response_unsuccessful")
    return response


def answer_json(response):
    text = "".join(part.text or "" for part in (response.content.parts if response.content else [])
                   if not part.thought and not part.function_call)
    try:
        return json.loads(text)  # Fences and prose fail here: test the native format, not parser leniency.
    except (ValueError, TypeError):
        raise ProbeFailure("invalid_json") from None


def validate_answer(c, response, case, schema, model=None):
    value = answer_json(response)
    try:
        c.jsonschema.validate(value, schema)
    except c.jsonschema.ValidationError as error:
        case["schema_validator"] = error.validator
        raise ProbeFailure("schema_validation") from None
    try:
        c.jsonschema.validate(value, c.wire_schema(schema))
    except c.jsonschema.ValidationError as error:
        case["schema_validator"] = error.validator
        raise ProbeFailure("wire_schema_validation") from None
    result = (model or c.schema_model).model_validate(value).model_dump()
    case["schema_valid"] = True
    return result


async def exercise(c, args, model, case, checkpoint):
    schema = c.schema_model.model_json_schema()
    adapter = c.resolve(model, output_schema=schema, reasoning_effort=args.reasoning_effort,
                        model_request_timeout_seconds=args.request_timeout_seconds)
    if not isinstance(adapter, c.adapter) or c.protocol(model) is None:
        raise ProbeFailure("native_schema_adapter_unavailable")
    if not args.real_models:
        adapter._transport = offline_transport(c, model, schema)
    case["protocol"] = c.protocol(model)
    case["schema_sha256"] = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    case["wire_schema_sha256"] = hashlib.sha256(json.dumps(c.wire_schema(schema), sort_keys=True).encode()).hexdigest()
    evidence = {"source": "probe:" + uuid4().hex, "available_at": None, "observed_at": utc_now(),
                "estimated": False, "estimate_reason": ""}
    tool = c.types.FunctionDeclaration(name=TOOL_NAME, description="Read one synthetic evidence record for this diagnostic.",
        parameters_json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False})
    request = c.request(contents=[c.types.Content(role="user", parts=[c.types.Part(text=(
        f"先调用 {TOOL_NAME} 读取合成证据，然后返回完整 Research JSON。summary 用中文简短说明这是接口测试，"
        "evidence 必须逐字复制工具返回的 evidence 对象并放进列表，ideas=[]。不要猜测或编造证据。"))])],
        config=c.types.GenerateContentConfig(system_instruction=c.instruction(schema),
            tools=[c.types.Tool(function_declarations=[tool])], max_output_tokens=args.max_output_tokens))
    response = await request_once(adapter, request, case, checkpoint, args.request_timeout_seconds)
    calls = [part.function_call for part in (response.content.parts if response.content else []) if part.function_call]
    if not calls:
        validate_answer(c, response, case, schema)
        raise ProbeFailure("tool_not_requested")
    rounds = 0
    while calls:
        # Reading the same evidence again is allowed (production agents run several tool rounds); a model
        # that never stops asking is not, and an unexpected or malformed call never is.
        if rounds == MAX_TOOL_ROUNDS:
            raise ProbeFailure("too_many_tool_rounds")
        ids = [call.id for call in calls]
        if any(call.name != TOOL_NAME or call.args for call in calls) or not all(ids) or len(set(ids)) != len(ids):
            raise ProbeFailure("unexpected_tool_call")
        rounds += 1
        case["tool_calls"] += len(calls)
        case["tool_rounds"] = rounds
        request.contents.extend([response.content, c.types.Content(role="user", parts=[
            c.types.Part(function_response=c.types.FunctionResponse(id=call.id, name=call.name,
                response={"evidence": evidence})) for call in calls])])
        response = await request_once(adapter, request, case, checkpoint, args.request_timeout_seconds)
        calls = [part.function_call for part in (response.content.parts if response.content else [])
                 if part.function_call]
    answer = validate_answer(c, response, case, schema)
    # The same record listed twice after two reads is harmless; anything else is not what the tool returned.
    if not answer["evidence"] or any(item != evidence for item in answer["evidence"]):
        raise ProbeFailure("evidence_does_not_match_tool")
    case.update(status="passed", tool_roundtrip=True, answer=answer)


def _times():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat(), (now + timedelta(hours=6)).isoformat(), (now + timedelta(hours=1)).isoformat()


def _same_instant(left, right):
    try:
        return datetime.fromisoformat(str(left).replace("Z", "+00:00")) == datetime.fromisoformat(right)
    except ValueError:
        return False


def _decision_case():
    as_of, expires, review = _times()
    prompt = ("这是接口测试，不是投资决策。可选证券只有 SPY 和 SGOV。返回完整 Decision JSON："
              f"as_of 必须是 {as_of}，expires_at 必须是 {expires}，next_review_at 必须是 {review}；"
              "rationale 用中文一句话说明这是接口测试；targets 只有一条：symbol SPY，weight 0.1，entry_limit 100，"
              "stop_loss 90，take_profit 120，take_profit_weight 0.5；alerts 一条：symbol SPY，above 130，below null，"
              "reason 说明是测试；news_watch 一条：query 为 interface test，reason 说明是测试；idea_refs 为空列表。")
    offline = {"as_of": as_of, "expires_at": expires, "next_review_at": review, "rationale": "这是接口测试。",
               "targets": [{"symbol": "SPY", "weight": 0.1, "entry_limit": 100.0, "stop_loss": 90.0,
                            "take_profit": 120.0, "take_profit_weight": 0.5}],
               "alerts": [{"symbol": "SPY", "above": 130.0, "below": None, "reason": "测试提醒"}],
               "news_watch": [{"query": "interface test", "reason": "测试"}], "idea_refs": []}

    def check(answer):
        if not all(_same_instant(answer[key], value) for key, value in (
                ("as_of", as_of), ("expires_at", expires), ("next_review_at", review))):
            raise ProbeFailure("decision_times_not_copied")
    return prompt, offline, check


def _critique_case():
    prompt = ("这是接口测试，不是真实研究。合成研究摘要：SPY 近期上涨，研究员认为趋势会延续。按 Critique schema 返回："
              "summary 用中文一句话；concerns 一条：symbol 为 SPY，concern 和 evidence 各一句话；missing_evidence 一条。")
    offline = {"summary": "这是接口测试的反方意见。",
               "concerns": [{"symbol": "SPY", "concern": "趋势可能反转。", "evidence": "仅有合成摘要。"}],
               "missing_evidence": ["没有真实证据。"]}
    return prompt, offline, None


def _review_case():
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    as_of = _times()[0]
    prompt = ("这是接口测试，不是真实复盘。按 PostmarketReview schema 用中文返回完整 JSON："
              f"session_date 必须是 {day}，as_of 必须是 {as_of}；summary 一句话；selection_adjustments 一条，"
              f"其 evidence_refs 只能从 {json.dumps(REVIEW_REFS)} 中逐字复制，例如 [\"event:1\"]；"
              "timing_adjustments 和 retired_hypotheses 为空列表；retained_lessons 和 data_gaps 各一条；confidence 为 low；"
              "strategy_memo 一句话；predictions 一条：symbol SPY、direction up、horizon_sessions 5、benchmark SPY、"
              "hypothesis_ref 为 h1；mandate_compliance 为空字符串；factor_proposals 一条：name 为 probe_momentum，"
              "expression 为 rank(ret(21))，horizon_sessions 5，hypothesis 一句话，universe 为 stocks。")
    adjustment = {"hypothesis": "接口测试假设。", "rationale": "合成依据。", "evidence_refs": ["event:1"],
                  "validation": "无。", "invalidated_when": "无。"}
    offline = {"session_date": day, "as_of": as_of, "summary": "这是接口测试。",
               "selection_adjustments": [adjustment], "timing_adjustments": [], "retained_lessons": ["测试。"],
               "retired_hypotheses": [], "data_gaps": ["没有真实数据。"], "confidence": "low",
               "strategy_memo": "接口测试，没有策略结论。",
               "predictions": [{"symbol": "SPY", "direction": "up", "horizon_sessions": 5, "benchmark": "SPY",
                                "hypothesis_ref": "h1"}], "mandate_compliance": "",
               "factor_proposals": [{"name": "probe_momentum", "expression": "rank(ret(21))", "horizon_sessions": 5,
                                     "hypothesis": "接口测试。", "universe": "stocks"}]}

    def check(answer):
        if answer["session_date"] != day or not _same_instant(answer["as_of"], as_of):
            raise ProbeFailure("review_session_not_copied")
        refs = [ref for group in ("selection_adjustments", "timing_adjustments")
                for change in answer[group] for ref in change["evidence_refs"]]
        if any(ref not in REVIEW_REFS for ref in refs):
            # The production review refuses such references too (exact match only).
            raise ProbeFailure("review_reference_outside_list")
    return prompt, offline, check


async def _single(c, args, model, case, checkpoint, schema_name, prompt, offline, validate=True):
    """One request without tools for a production schema; returns the validated (or raw) answer."""
    schema_model = c.models[schema_name]
    schema = schema_model.model_json_schema()
    adapter = c.resolve(model, output_schema=schema, reasoning_effort=args.reasoning_effort,
                        model_request_timeout_seconds=args.request_timeout_seconds)
    if not isinstance(adapter, c.adapter) or c.protocol(model) is None:
        raise ProbeFailure("native_schema_adapter_unavailable")
    if not args.real_models:
        adapter._transport = offline_transport(c, model, schema, offline)
    case["protocol"] = c.protocol(model)
    case.setdefault("schemas", {})[schema_name] = {
        "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        "wire_schema_sha256": hashlib.sha256(json.dumps(c.wire_schema(schema), sort_keys=True).encode()).hexdigest()}
    request = c.request(contents=[c.types.Content(role="user", parts=[c.types.Part(text=prompt)])],
        config=c.types.GenerateContentConfig(system_instruction=c.instruction(schema),
                                             max_output_tokens=args.max_output_tokens))
    response = await request_once(adapter, request, case, checkpoint, args.request_timeout_seconds)
    if any(part.function_call for part in (response.content.parts if response.content else [])):
        raise ProbeFailure("unexpected_tool_call")  # No tool was offered.
    if not validate:
        return answer_json(response)
    return validate_answer(c, response, case, schema, schema_model)


CONSTRAINT_NOTE = ("held: this answer kept the constraint, by the provider's enforcement or because the model ignored "
                   "the instruction; held_null: a nullable field was answered with null instead of the forbidden "
                   "value; not_enforced: the provider returned a value the schema forbids, so local validation can "
                   "still reject a production answer of that kind; untested: the field was missing or not a number.")


def _observed(value):
    """A returned value for the report: null or a finite number as is, anything else only by its type."""
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)):
        return value
    return {"type": type(value).__name__}


async def exercise_constraints(c, args, model, case, checkpoint):
    """Ask on purpose for values the schema forbids; any that come back were not enforced by the provider."""
    evidence = {"source": "probe:" + uuid4().hex, "available_at": None, "observed_at": utc_now(),
                "estimated": False, "estimate_reason": ""}
    findings = case.setdefault("constraint_findings", {})
    case["constraint_note"] = CONSTRAINT_NOTE
    research = await _single(c, args, model, case, checkpoint, "research", (
        "这是格式约束测试，用来检查接口是否强制 JSON Schema 约束。请严格照做，即使与 schema 冲突："
        "summary 必须是空字符串 \"\"，evidence 必须是空列表 []，ideas 为空列表 []。不要输出其他内容。"),
        {"summary": "约束测试。", "evidence": [evidence], "ideas": []}, validate=False)
    findings["Research.summary minLength 1"] = "not_enforced" if research.get("summary") == "" else "held"
    findings["Research.evidence minItems 1"] = "not_enforced" if research.get("evidence") == [] else "held"
    observed = case.setdefault("constraint_observed", {})
    observed["Research.summary length"] = len(research["summary"]) if isinstance(research.get("summary"), str) else None
    observed["Research.evidence items"] = len(research["evidence"]) if isinstance(research.get("evidence"), list) else None
    as_of, expires, review = _times()
    decision = await _single(c, args, model, case, checkpoint, "decision", (
        "这是格式约束测试，用来检查接口是否强制 JSON Schema 约束。请严格照做，即使与 schema 冲突："
        f"as_of 为 {as_of}，expires_at 为 {expires}，next_review_at 为 {review}，rationale 写“约束测试”；"
        "targets 只有一条：symbol SPY，weight 必须是 1.5，entry_limit 必须是 0，stop_loss、take_profit、"
        "take_profit_weight 为 null；alerts、news_watch、idea_refs 为空列表。"),
        {"as_of": as_of, "expires_at": expires, "next_review_at": review, "rationale": "约束测试",
         "targets": [{"symbol": "SPY", "weight": 1.0, "entry_limit": 0.01, "stop_loss": None, "take_profit": None,
                      "take_profit_weight": None}], "alerts": [], "news_watch": [], "idea_refs": []}, validate=False)
    targets = decision.get("targets") if isinstance(decision.get("targets"), list) else []
    first = targets[0] if targets and isinstance(targets[0], dict) else {}
    weight, entry = first.get("weight"), first.get("entry_limit")
    findings["Target.weight maximum 1"] = ("untested" if not isinstance(weight, (int, float)) else
                                           "not_enforced" if weight > 1 else "held")
    # entry_limit may be null: answering null instead of the forbidden 0 keeps the constraint ("held_null").
    findings["Target.entry_limit exclusiveMinimum 0"] = (
        "held_null" if "entry_limit" in first and entry is None else
        "untested" if not isinstance(entry, (int, float)) else "not_enforced" if entry <= 0 else "held")
    observed["Target.weight"], observed["Target.entry_limit"] = _observed(weight), _observed(entry)
    if "not_enforced" in findings.values():
        raise ProbeFailure("provider_constraint_not_enforced")
    case["schema_valid"] = True


async def exercise_case(c, args, model, case, checkpoint):
    """A production schema other than the research tool round trip (`exercise`)."""
    name = case["schema"]
    if name == "constraints":
        await exercise_constraints(c, args, model, case, checkpoint)
    else:
        prompt, offline, check = {"decision": _decision_case, "critique": _critique_case, "review": _review_case}[name]()
        answer = await _single(c, args, model, case, checkpoint, name, prompt, offline)
        if check is not None:
            check(answer)
        case["answer"] = answer
    case["status"] = "passed"


def selected_cases(value):
    names = list(CASES) if value.strip() == "all" else [n.strip() for n in value.split(",") if n.strip()]
    unknown = sorted(set(names) - set(CASES))
    if unknown or not names:
        raise ValueError("unknown case: " + ", ".join(unknown) if unknown else "no case selected")
    return [name for name in CASES if name in names]


def main(argv=None):
    parser = argparse.ArgumentParser(description="检查 Flash/Pro 的原生 Schema、工具往返和用量；默认离线模拟。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real-models", action="store_true", help="显式调用真实 DeepSeek，会产生模型用量")
    mode.add_argument("--offline", action="store_true", help="只用模拟 HTTP，不调用真实模型（默认）")
    parser.add_argument("--model", choices=("both", "flash", "pro"), default="both")
    parser.add_argument("--env-file", type=Path, help="显式选择密钥文件；从不自动寻找，进程环境优先")
    parser.add_argument("--output", type=Path, help="新建 JSON 报告文件；不覆盖已有报告")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "high", "max"), default="high")
    parser.add_argument("--schemas", default="research",
                        help="research（默认，带工具往返）、decision、critique、review、constraints（约束是否被强制），"
                             "逗号分隔，或 all 全部")
    args = parser.parse_args(argv)
    try:
        cases = selected_cases(args.schemas)
    except ValueError as error:
        parser.error("--schemas：" + str(error) + "；可选 " + "、".join(CASES) + " 或 all")
    if args.max_output_tokens < 1 or not 0 < args.request_timeout_seconds <= 300:
        parser.error("max-output-tokens 必须为正；request-timeout-seconds 必须在 (0, 300] 内")
    if args.env_file is not None and not args.real_models:
        parser.error("--env-file 只用于 --real-models；离线模式不需要密钥")
    models = list(MODELS.values()) if args.model == "both" else [MODELS[args.model]]
    output = args.output or REPO_ROOT / "artifacts/verification/deepseek-schema-smoke" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8] + ".json")
    report = {"mode": "real_models" if args.real_models else "offline", "started_at": utc_now(),
              "status": "running", "schemas": cases,
              "max_provider_requests": sum(CASES[name] for name in cases) * len(models), "cases": []}
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        print("无法新建报告文件：" + type(error).__name__, file=sys.stderr)
        return 2
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        def checkpoint():
            handle.seek(0)
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.truncate()
            handle.flush()

        checkpoint()
        settings = {"LUMIBOT_DISABLE_DOTENV": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
        if not args.real_models:
            settings.update(DEEPSEEK_API_KEY="synthetic-offline-key", DEEPSEEK_API_BASE="https://api.deepseek.com")
        exit_code = 2
        with patch.dict(os.environ, settings):
            try:
                load_secrets(args.env_file)
                if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
                    raise ProbeFailure("missing_DEEPSEEK_API_KEY")
                c = components()
                report["implementation"] = {"adapter_sha256": c.adapter_sha256, "runtime_sha256": c.runtime_sha256,
                    "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "schemas": cases, "sdk_versions": {name: importlib.metadata.version(name)
                        for name in ("google-adk", "litellm", "openai")}}
                print(("真实 DeepSeek" if args.real_models else "离线模拟（不验证真实服务）") +
                      f"：{'、'.join(cases)}；最多 {report['max_provider_requests']} 次请求；"
                      f"每次最多 {args.max_output_tokens} 输出 tokens。", flush=True)
                for model in models:
                    for name in cases:
                        case = {"model": model, "schema": name, "status": "running", "schema_valid": False,
                                "requests": []}
                        if name == "research":
                            case.update(tool_roundtrip=False, tool_calls=0)
                        report["cases"].append(case)
                        try:
                            asyncio.run(exercise(c, args, model, case, checkpoint) if name == "research" else
                                        exercise_case(c, args, model, case, checkpoint))
                        except Exception as error:
                            case.update(status="failed", error=safe_error(error))
                        known = [row["usage"] for row in case["requests"] if row["usage"] is not None]
                        case["usage"] = c.aggregate(known)
                        case["usage_complete"] = bool(case["requests"]) and len(known) == len(case["requests"])
                        detail = (f" tool_roundtrip={case['tool_roundtrip']} tool_rounds={case.get('tool_rounds', 0)}"
                                  if name == "research" else
                                  " " + json.dumps(case.get("constraint_findings", {}), ensure_ascii=False) +
                                  " observed=" + json.dumps(case.get("constraint_observed", {}), ensure_ascii=False)
                                  if name == "constraints" else "")
                        print(f"[{case['status'].upper()}] {model} {name} schema={case['schema_valid']}{detail} "
                              f"requests={len(case['requests'])}", flush=True)
                        if case.get("error"):
                            print("  " + json.dumps(case["error"], ensure_ascii=False), flush=True)
                        print("  用量：" + json.dumps(case["usage"], ensure_ascii=False) +
                              ("" if case["usage_complete"] else "；部分请求用量未知"), flush=True)
                        checkpoint()
                if "constraints" in cases:
                    print("约束说明：held 表示这次回答满足约束（可能是 DeepSeek 强制，也可能只是模型没照做）；"
                          "held_null 表示该字段允许为空，模型用 null 代替了被禁止的值；"
                          "not_enforced 表示 DeepSeek 没有强制该约束，本地校验仍会拒绝这类回答。", flush=True)
                exit_code = 0 if all(case["status"] == "passed" for case in report["cases"]) else 1
                report["status"] = "passed" if exit_code == 0 else "failed"
            except KeyboardInterrupt:
                report["status"] = "interrupted"
                if report["cases"] and report["cases"][-1]["status"] == "running":
                    report["cases"][-1]["status"] = "interrupted"
                exit_code = 130
            except Exception as error:
                report.update(status="preflight_failed", error=safe_error(error))
                print("启动检查失败：" + json.dumps(report["error"], ensure_ascii=False), file=sys.stderr)
            finally:
                report["finished_at"] = utc_now()
                checkpoint()
    print("报告：" + str(output), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
