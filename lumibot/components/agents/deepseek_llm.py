"""DeepSeek Responses transport for agents that request an output schema.

Keep provider protocol conversion here: application schemas, research tools and
trading permissions continue to belong to their existing callers.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import _get_completion_inputs
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from openai import AsyncOpenAI
from pydantic import PrivateAttr


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt object fields to DeepSeek's strict request contract on a copy.

    Pydantic omits defaulted fields from ``required``; the provider requires
    every declared property there, including inside references and unions.
    Keep their types/defaults and the caller's local validation unchanged:
    the model must emit valid values, rather than Python filling in an answer
    or making non-nullable fields nullable to satisfy the transport.
    """
    result = json.loads(json.dumps(schema))

    def visit(node):
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = node.get("required", [])
            if isinstance(required, list):
                # Preserve existing requirements, even for an invalid caller
                # schema; transport normalization must not weaken constraints.
                node["required"] = list(dict.fromkeys([*properties, *required]))
            node.setdefault("additionalProperties", False)
        # Only descend schema keywords. Defaults, examples, enum and const can
        # contain arbitrary objects whose literal values must remain intact.
        for keyword in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas", "dependencies"):
            children = node.get(keyword)
            if isinstance(children, dict):
                for child in children.values():
                    visit(child)
        for keyword in ("items", "additionalItems", "contains", "additionalProperties", "unevaluatedProperties",
                        "unevaluatedItems", "propertyNames", "not", "if", "then", "else", "contentSchema"):
            child = node.get(keyword)
            if isinstance(child, list):
                for item in child:
                    visit(item)
            else:
                visit(child)
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            children = node.get(keyword)
            if isinstance(children, list):
                for child in children:
                    visit(child)

    visit(result)
    return result


def _content(value, *, assistant=False):
    if not isinstance(value, list):
        return value
    parts = []
    for part in value:
        if part["type"] == "text":
            parts.append({"type": "output_text" if assistant else "input_text", "text": part["text"]})
        elif part["type"] == "image_url":
            image = part["image_url"]
            parts.append({"type": "input_image", **({"image_url": image} if isinstance(image, str) else {
                "image_url": image["url"], **({"detail": image["detail"]} if "detail" in image else {})})})
        else:
            raise ValueError("DeepSeek Responses received an unsupported content type")
    return parts


def _input_items(messages):
    """Replay complete stateless history, including DeepSeek's plain reasoning.

    The generic Responses bridge only replays encrypted reasoning/summary
    items. DeepSeek instead requires its original reasoning text with the
    assistant's function calls; never replace it with a blank placeholder.
    """
    items = []
    for message in messages:
        role, content = message["role"], message.get("content")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": _content(content) if content is not None else ""})
            continue
        reasoning = message.get("reasoning_content")
        if role == "assistant" and reasoning:
            items.append({"type": "reasoning", "content": [{"type": "reasoning_text", "text": reasoning}]})
        if content is not None:
            items.append({"type": "message", "role": role, "content": _content(content, assistant=role == "assistant")})
        for call in message.get("tool_calls") or []:
            function = call["function"]
            items.append({"type": "function_call", "call_id": call["id"],
                          "name": function["name"], "arguments": function["arguments"]})
    return items


def _usage(response):
    usage = response.usage
    if usage is None:
        return None
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=usage.input_tokens,
        # DeepSeek output_tokens already includes reasoning; report it once.
        candidates_token_count=usage.output_tokens,
        total_token_count=usage.total_tokens,
        cached_content_token_count=getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None),
        thoughts_token_count=getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None),
    )


def _answer(response):
    usage = _usage(response)
    # Never promote a truncated JSON prefix or an unsuccessful response to a
    # completed answer, even if its partial text happens to pass a schema.
    if response.status != "completed":
        code = "deepseek_response_failed" if response.status == "failed" else "deepseek_response_incomplete"
        return LlmResponse(error_code=code, error_message="DeepSeek response did not complete.", usage_metadata=usage)
    parts = []
    for item in response.output:
        if item.type == "reasoning":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "reasoning_text" and getattr(part, "text", None):
                    parts.append(types.Part(text=part.text, thought=True))
        elif item.type == "message":
            text = []
            for part in item.content:
                if part.type != "output_text":
                    return LlmResponse(error_code="deepseek_response_refused",
                                       error_message="DeepSeek returned no usable answer.",
                                       usage_metadata=usage)
                text.append(part.text)
            if text:
                parts.append(types.Part(text="".join(text)))
        elif item.type == "function_call":
            try:
                arguments = json.loads(item.arguments)
            except (ValueError, TypeError):
                arguments = None
            if not isinstance(arguments, dict):
                return LlmResponse(error_code="deepseek_function_arguments_invalid",
                                   error_message="DeepSeek function arguments must be a JSON object.",
                                   usage_metadata=usage, finish_reason=types.FinishReason.MALFORMED_FUNCTION_CALL)
            parts.append(types.Part(function_call=types.FunctionCall(
                id=item.call_id, name=item.name, args=arguments)))
        else:
            return LlmResponse(error_code="deepseek_output_type_unsupported",
                               error_message="DeepSeek returned an unsupported output type.", usage_metadata=usage)
    return LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage,
                       finish_reason=types.FinishReason.STOP)


class DeepSeekResponsesLlm(BaseLlm):
    """Use native JSON Schema output without changing the agent's tools.

    Buffer each response until completion, including for streaming ADK callers:
    incomplete structured answers must never be exposed as completed output.
    The outer runtime continues to own retry and whole-run time limits.
    """
    _schema: dict[str, Any] = PrivateAttr()
    _wire_schema: dict[str, Any] = PrivateAttr()
    _api_key: str = PrivateAttr()
    _api_base: str = PrivateAttr()
    _timeout: float | None = PrivateAttr()
    _effort: str | None = PrivateAttr()
    _transport: Any = PrivateAttr(default=None)

    def __init__(self, *, model, output_schema, model_request_timeout_seconds=None,
                 reasoning_effort=None, transport=None):
        if not model.startswith("deepseek/") or not model.removeprefix("deepseek/"):
            raise ValueError("DeepSeek Responses requires an exact deepseek/<model-id>")
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ValueError("DeepSeek structured output requires DEEPSEEK_API_KEY")
        super().__init__(model=model)
        self._schema = json.loads(json.dumps(output_schema))
        self._wire_schema = strict_output_schema(self._schema)
        self._api_key = key
        # Preserve the provider SDK's existing endpoint override; never borrow
        # an OpenAI endpoint/key or silently downgrade to prompt-only output.
        self._api_base = os.environ.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
        self._timeout = model_request_timeout_seconds
        self._effort = reasoning_effort
        self._transport = transport

    async def generate_content_async(self, llm_request, stream=False):
        messages, tools, _, params, choice = await _get_completion_inputs(llm_request, self.model)
        body = {"model": self.model.removeprefix("deepseek/"), "input": _input_items(messages),
                "text": {"format": {"type": "json_schema", "name": "agent_answer",
                                     "schema": self._wire_schema, "strict": True}}}
        if tools:
            if any(tool.get("type") != "function" for tool in tools):
                raise ValueError("DeepSeek Responses requires function tools")
            body["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
        if choice is not None:
            body["tool_choice"] = ({"type": "function", "name": choice["function"]["name"]}
                                   if isinstance(choice, dict) else choice)
        if self._effort is not None:
            body["reasoning"] = {"effort": self._effort}
        config = llm_request.config
        if config is not None and config.max_output_tokens is not None:
            body["max_output_tokens"] = config.max_output_tokens
        for key in ("temperature", "top_p"):
            if (params or {}).get(key) is not None:
                body[key] = params[key]
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http_client:
            async with AsyncOpenAI(api_key=self._api_key, base_url=self._api_base,
                                   # Prevent the compatible SDK from borrowing
                                   # another provider's organization/project.
                                   organization="", project="", http_client=http_client,
                                   timeout=self._timeout, max_retries=0) as client:
                response = await client.responses.create(**body)
        yield _answer(response)
