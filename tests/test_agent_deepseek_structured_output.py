"""Native schema requests and ADK tool continuations, without provider inference."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from lumibot.components.agents.deepseek_llm import DeepSeekResponsesLlm, _input_items
from lumibot.components.agents.runtime import GoogleADKRuntime, RuntimeRequest, _resolve_model_for_adk
from lumibot.components.agents.schemas import AgentRunResult, BoundTool
from lumibot.components.agents.structured_output import parse_structured_output


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1},
          "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
          "required": ["summary", "evidence"], "additionalProperties": False}
ANSWER = {"summary": "Synthetic observed result", "evidence": ["Recorded observation"]}
MODELS = ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]


def terminal(model, *, output=None, status="completed"):
    return {"id": "resp_synthetic", "object": "response", "created_at": 1,
            "model": model.removeprefix("deepseek/"), "status": status,
            "output": output if output is not None else [{"id": "msg_synthetic", "type": "message",
                "role": "assistant", "status": "completed", "content": [
                    {"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 7},
                      "output_tokens_details": {"reasoning_tokens": 4}}}


@pytest.fixture(autouse=True)
def provider_boundary(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-deepseek-key")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("OPENAI_ORG_ID", "must-not-be-used")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "must-not-be-used")
    monkeypatch.delenv("DEEPSEEK_API_BASE", raising=False)
    original = httpx.AsyncClient.send

    async def mock_only(client, request, **kwargs):
        assert isinstance(client._transport, httpx.MockTransport), "Unexpected provider network request"
        return await original(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_only)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("stream", [False, True])
def test_schema_is_sent_to_deepseek_responses_and_usage_is_retained(model, stream):
    sent = []

    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/responses"
        assert request.headers["authorization"] == "Bearer synthetic-deepseek-key"
        assert not request.headers.get("OpenAI-Organization")
        assert not request.headers.get("OpenAI-Project")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=terminal(model))

    schema = deepcopy(SCHEMA)
    adapter = DeepSeekResponsesLlm(model=model, output_schema=schema, reasoning_effort="high",
                                   model_request_timeout_seconds=12, transport=httpx.MockTransport(handle))
    request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="Return the observed JSON.")])],
                         config=types.GenerateContentConfig(max_output_tokens=256))

    async def consume():
        return [response async for response in adapter.generate_content_async(request, stream=stream)]

    [response] = asyncio.run(consume())
    [body] = sent
    assert body["model"] == model.removeprefix("deepseek/")
    assert body["text"]["format"] == {"type": "json_schema", "name": "agent_answer", "schema": SCHEMA, "strict": True}
    assert body["reasoning"] == {"effort": "high"} and body["max_output_tokens"] == 256
    assert schema == SCHEMA
    assert parse_structured_output(response.content.parts[0].text, SCHEMA, None) == (ANSWER, None)
    assert response.usage_metadata.prompt_token_count == 20
    assert response.usage_metadata.candidates_token_count == 10
    assert response.usage_metadata.cached_content_token_count == 7
    assert response.usage_metadata.thoughts_token_count == 4
    assert response.usage_metadata.total_token_count == 30


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_unsuccessful_response_never_exposes_even_schema_valid_partial_text(model, status):
    adapter = DeepSeekResponsesLlm(model=model, output_schema=SCHEMA, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=terminal(model, status=status))))

    async def consume():
        return [response async for response in adapter.generate_content_async(LlmRequest(
            contents=[types.Content(role="user", parts=[types.Part(text="Return JSON.")])]))]

    [response] = asyncio.run(consume())
    # A failed request and an incomplete generation retain distinct provider outcomes.
    assert response.content is None and response.error_code == f"deepseek_response_{status}"
    assert response.usage_metadata.total_token_count == 30


def test_missing_deepseek_key_cannot_fall_back_to_an_openai_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        _resolve_model_for_adk(MODELS[0], output_schema=SCHEMA)


def test_existing_provider_endpoint_override_is_preserved(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://provider.example.test/v1")
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=terminal(MODELS[0]))

    adapter = _resolve_model_for_adk(MODELS[0], output_schema=SCHEMA, model_request_timeout_seconds=0)
    adapter._transport = httpx.MockTransport(handle)

    async def consume():
        return [response async for response in adapter.generate_content_async(LlmRequest())]

    asyncio.run(consume())
    assert urls == ["https://provider.example.test/v1/responses"]
    assert adapter._timeout is None


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("body", [ANSWER, {"summary": "Missing evidence"}])
def test_actual_adk_tool_loop_keeps_schema_reasoning_permissions_and_usage(monkeypatch, model, body):
    from lumibot.components.agents import runtime as runtime_module

    sent, observed = [], []

    def observe(value: int):
        """Return a synthetic read-only observation."""
        observed.append(value)
        return {"observed": value}

    def handle(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(200, json=terminal(model, output=[
                {"id": "rs_synthetic", "type": "reasoning", "summary": [], "content": [
                    {"type": "reasoning_text", "text": "Inspect the recorded observation before answering."}]},
                {"id": "fc_synthetic", "type": "function_call", "call_id": "call_synthetic", "name": "observe",
                 "arguments": json.dumps({"value": 3, "agent_purpose": "Inspect the synthetic observation."})}]))
        assert len(sent) == 2
        return httpx.Response(200, json=terminal(model, output=[
            {"id": "msg_final", "type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": json.dumps(body), "annotations": []}]}]))

    original = runtime_module._resolve_model_for_adk

    def resolve(selected, **kwargs):
        adapter = original(selected, **kwargs)
        assert isinstance(adapter, DeepSeekResponsesLlm)
        adapter._transport = httpx.MockTransport(handle)
        return adapter

    monkeypatch.setattr(runtime_module, "_resolve_model_for_adk", resolve)
    monkeypatch.setenv("LUMIBOT_AGENT_MAX_RUN_ATTEMPTS", "1")
    request = RuntimeRequest(agent_name="observer", model=model, system_prompt="Use the read-only observation tool.",
        task_prompt="Observe value 3, then return JSON with summary and evidence.", context={}, runtime_context={},
        memory_state=None, memory_notes=[], include_builtin_skills=False,
        bound_tools=[BoundTool("observe", "Read a synthetic observation.", observe, source="custom",
                               metadata={"mutates_trading": False})],
        max_output_tokens=256, reasoning_effort="high", output_schema=SCHEMA)
    result = GoogleADKRuntime().run(request)
    assert observed == [3] and len(sent) == 2
    assert all(row["text"]["format"]["schema"] == SCHEMA for row in sent)
    assert all([tool["name"] for tool in row["tools"]] == ["observe"] for row in sent)
    assert any(item["type"] == "reasoning" and item["content"][0]["text"] ==
               "Inspect the recorded observation before answering." for item in sent[1]["input"])
    assert any(item["type"] == "function_call" and item["call_id"] == "call_synthetic" for item in sent[1]["input"])
    assert any(item["type"] == "function_call_output" and item["call_id"] == "call_synthetic" for item in sent[1]["input"])
    assert result.model == model
    assert result.usage["prompt_token_count"] == 40
    assert result.usage["candidates_token_count"] == 20
    assert result.usage["cached_content_token_count"] == 14
    assert result.usage["thoughts_token_count"] == 8
    parsed, error = parse_structured_output(result.summary, SCHEMA, None)
    assert parsed == (ANSWER if body == ANSWER else None)
    assert bool(error) == (body != ANSWER)


def test_image_and_tool_output_conversion_keeps_original_content():
    messages = [{"role": "user", "content": [{"type": "text", "text": "Read this image."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj", "detail": "high"}}]},
        {"role": "tool", "tool_call_id": "call_image", "content": [
            {"type": "image_url", "image_url": "https://images.example.test/observed.png"}]}]
    original = deepcopy(messages)
    items = _input_items(messages)
    assert items[0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj", "detail": "high"}
    assert items[1]["output"][0]["image_url"] == "https://images.example.test/observed.png"
    assert messages == original


@pytest.mark.parametrize("model", MODELS)
def test_manager_passes_create_and_run_schemas_to_the_runtime_and_separates_old_replays(tmp_path, monkeypatch, model):
    from lumibot.components.agents.manager import AgentManager

    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("LUMIBOT_MEMORY_DIR", str(tmp_path / "memory"))
    seen = []

    class Vars(dict):
        def set(self, key, value):
            self[key] = value

    class Strategy:
        is_backtesting = False
        parameters = {}
        vars = Vars()

        def get_datetime(self):
            return datetime(2026, 1, 2, tzinfo=timezone.utc)

        def log_message(self, *args, **kwargs):
            pass

    class Runtime:
        def run(self, request):
            seen.append(request)
            return AgentRunResult(summary=json.dumps(ANSWER), model=request.model, events=[])

    manager = AgentManager(Strategy())
    agent = manager.create(name="observer", model=model, output_schema=SCHEMA,
                           include_builtin_tools=False, include_builtin_skills=False, _runtime=Runtime())
    payloads = []
    original = manager.replay_cache.compute_key

    def key(payload):
        payloads.append(payload)
        return original(payload)

    monkeypatch.setattr(manager.replay_cache, "compute_key", key)
    assert agent.run().parsed == ANSWER
    override = {"type": "object", "required": ["summary"]}
    assert agent.run(output_schema=override).parsed == ANSWER
    assert seen[0].output_schema == SCHEMA and seen[1].output_schema == override
    assert all(payload["structured_output_protocol"] == "deepseek_responses_json_schema_v1" for payload in payloads)
    old = {key: value for key, value in payloads[0].items() if key != "structured_output_protocol"}
    assert original(old) != original(payloads[0])


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("status", [400, 401, 429])
def test_provider_rejection_does_not_downgrade_route_or_retry_below_the_runtime(model, status):
    from openai import APIStatusError

    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(status, json={"error": {"message": "Synthetic provider rejection", "type": "invalid_request"}})

    adapter = DeepSeekResponsesLlm(model=model, output_schema=SCHEMA, transport=httpx.MockTransport(handle))

    async def consume():
        return [response async for response in adapter.generate_content_async(LlmRequest())]

    with pytest.raises(APIStatusError):
        asyncio.run(consume())
    assert len(sent) == 1 and str(sent[0].url) == "https://api.deepseek.com/responses"
    assert json.loads(sent[0].content)["text"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("arguments", ["{", "[]", "null"])
def test_bad_tool_arguments_are_not_executed_and_reported_usage_is_preserved(arguments):
    response = terminal(MODELS[0], output=[{"id": "fc_invalid", "type": "function_call",
        "call_id": "call_invalid", "name": "observe", "arguments": arguments}])
    adapter = DeepSeekResponsesLlm(model=MODELS[0], output_schema=SCHEMA,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))

    async def consume():
        return [result async for result in adapter.generate_content_async(LlmRequest())]

    [result] = asyncio.run(consume())
    assert result.content is None and result.error_code == "deepseek_function_arguments_invalid"
    assert result.usage_metadata.total_token_count == 30
    assert result.finish_reason == types.FinishReason.MALFORMED_FUNCTION_CALL


@pytest.mark.parametrize("model,schema", [(MODELS[0], None), (MODELS[1], None),
    ("deepseek/deepseek-chat", SCHEMA), ("anthropic/claude-sonnet-5", SCHEMA),
    ("openai/gpt-5.4-mini", SCHEMA)])
def test_unstructured_and_other_model_routes_keep_their_existing_provider_adapter(monkeypatch, model, schema):
    import sys
    from types import ModuleType

    created = {}

    class ExistingAdapter:
        def __init__(self, **kwargs):
            created.update(kwargs)

    module = ModuleType("google.adk.models.lite_llm")
    module.LiteLlm = ExistingAdapter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    result = _resolve_model_for_adk(model, output_schema=schema)
    assert isinstance(result, ExistingAdapter) and created["model"] == model
    assert "response_format" not in created and "output_schema" not in created


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("role", ["researcher", "critic", "manager", "review"])
def test_all_fund_role_schemas_reach_native_requests_unchanged(model, role):
    from types import SimpleNamespace
    from scripts.autonomous_fund.review import PostmarketReview
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import Critique, Decision, Research

    schema_model = {"researcher": Research, "critic": Critique, "manager": Decision, "review": PostmarketReview}[role]
    schema = schema_model.model_json_schema()
    fixture = ScriptedRuntime().run(SimpleNamespace(bound_tools=[], agent_name="manager" if role == "review" else role,
        context={"as_of": "2026-11-02T21:05:00+00:00", "eligible_universe": ["ALFA"],
                 "phase": "postmarket_review" if role == "review" else "research", "session": {"session_date": "2026-11-02"}}))

    def handle(request):
        assert json.loads(request.content)["text"]["format"]["schema"] == schema
        return httpx.Response(200, json=terminal(model, output=[{"id": "msg_role", "type": "message",
            "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": fixture.summary, "annotations": []}]}]))

    adapter = DeepSeekResponsesLlm(model=model, output_schema=schema, transport=httpx.MockTransport(handle))

    async def consume():
        return [result async for result in adapter.generate_content_async(LlmRequest())]

    [result] = asyncio.run(consume())
    parsed, error = parse_structured_output(result.content.parts[0].text, schema, schema_model)
    assert error is None
    assert parsed.model_dump() == schema_model.model_validate_json(fixture.summary).model_dump()
