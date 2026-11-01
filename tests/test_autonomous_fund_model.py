"""Exercise the real model SDK tool path with mock responses, never inference."""
import asyncio
import json

import httpx
import pytest


TOOL = {"type": "function", "function": {
    "name": "fixture_lookup", "description": "Read-only synthetic fixture",
    "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}}


@pytest.mark.parametrize("model", ["deepseek/deepseek-flash", "openai/gpt-6-luna", "anthropic/claude-sonnet-4-5"])
def test_installed_sdk_accepts_plain_function_tools_with_mcp_one(model, monkeypatch):
    import litellm

    def no_network(*args, **kwargs):
        raise AssertionError("SDK dependency regression must not call a provider")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)

    async def complete():
        return await litellm.acompletion(
            model=model, messages=[{"role": "user", "content": "Dependency-path fixture only"}],
            tools=[TOOL], api_key="synthetic-not-a-real-key", mock_response="fixture response", num_retries=0,
        )

    result = asyncio.run(complete())
    assert result.choices[0].message.content == "fixture response"


def test_installed_sdk_preserves_tool_call_and_followup_json(monkeypatch):
    import litellm

    def no_network(*args, **kwargs):
        raise AssertionError("Mock tool roundtrip must not call a provider")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)

    async def roundtrip():
        messages = [{"role": "user", "content": "Look up the synthetic fixture"}]
        first = await litellm.acompletion(
            model="deepseek/deepseek-flash", messages=messages, tools=[TOOL],
            api_key="synthetic-not-a-real-key", mock_response="", num_retries=0,
            mock_tool_calls=[{"id": "fixture-call", "type": "function", "function": {
                "name": "fixture_lookup", "arguments": '{"symbol":"ALFA"}'}}],
        )
        call = first.choices[0].message.tool_calls[0]
        assert call.function.name == "fixture_lookup"
        assert json.loads(call.function.arguments) == {"symbol": "ALFA"}
        messages.append(first.choices[0].message.model_dump(exclude_none=True))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": '{"price":100}'})
        final = await litellm.acompletion(
            model="deepseek/deepseek-flash", messages=messages, tools=[TOOL],
            api_key="synthetic-not-a-real-key", mock_response='{"summary":"fixture observed"}', num_retries=0,
        )
        assert json.loads(final.choices[0].message.content) == {"summary": "fixture observed"}

    asyncio.run(roundtrip())


def test_native_adk_deepseek_tool_roundtrip_through_mock_http(monkeypatch):
    import importlib
    import requests as requests_library
    import tiktoken

    from lumibot.components.agents.runtime import GoogleADKRuntime, RuntimeRequest
    from lumibot.components.agents.schemas import BoundTool

    requests = []
    lookups = []

    def lookup(symbol: str):
        lookups.append(symbol)
        return {"symbol": symbol, "price": 100}

    def no_network(*args, **kwargs):
        raise AssertionError("Native runtime regression must not reach the network")

    async def fake_send(client, request, **kwargs):
        assert request.url.host == "api.deepseek.com"
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        requests.append(body)
        if len(requests) == 1:
            assert any(t["function"]["name"] == "fixture_lookup" for t in body["tools"])
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "fixture-call", "type": "function", "function": {
                    "name": "fixture_lookup", "arguments": json.dumps({
                        "symbol": "ALFA", "agent_purpose": "Read a synthetic test fixture."})}}]}
            finish_reason = "tool_calls"
        else:
            assert len(requests) == 2
            tool_result = next(m for m in body["messages"] if m["role"] == "tool")
            assert json.loads(tool_result["content"])["price"] == 100
            message = {"role": "assistant", "content": '{"summary":"fixture observed"}'}
            finish_reason = "stop"
        return httpx.Response(200, request=request, json={
            "id": "synthetic-completion", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": message,
                "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    # Token-count accuracy is outside this regression. Supply a local byte
    # tokenizer so a clean machine never downloads tokenizer assets in pytest.
    encoding = tiktoken.Encoding(name="synthetic-byte-fixture", pat_str=r"(?s).",
        mergeable_ranks={bytes([i]): i for i in range(256)}, special_tokens={})
    # Patch the request's encoding owner directly so the fixture cannot become
    # a cached global SDK encoding or alter cache environment for later tests.
    monkeypatch.setattr(importlib.import_module("litellm.main"), "_get_encoding", lambda: encoding)
    monkeypatch.setattr(requests_library.Session, "send", no_network)
    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    request = RuntimeRequest(
        agent_name="fixture_researcher", model="deepseek/deepseek-flash",
        system_prompt="Read the synthetic fixture and return its summary as JSON.",
        task_prompt="Look up ALFA.", context={}, runtime_context={}, memory_state=None,
        memory_notes=[], bound_tools=[BoundTool("fixture_lookup", "Read a synthetic fixture", lookup)],
        include_builtin_skills=False, model_request_timeout_seconds=5, run_timeout_seconds=15,
    )
    # Exercise the actual ADK and provider serializer, replacing only HTTP. A
    # single bounded attempt keeps SDK defects from becoming a retrying test.
    result = asyncio.run(GoogleADKRuntime()._run_async_with_timeout(request, 15))
    assert lookups == ["ALFA"]
    assert len(requests) == 2
    assert json.loads(result.summary) == {"summary": "fixture observed"}
    assert any(event.kind == "tool_result" for event in result.events)
