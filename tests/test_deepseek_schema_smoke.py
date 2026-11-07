"""Operator smoke-script behavior with mocked provider HTTP only."""
import json
import os

import httpx
import pytest

from scripts import check_deepseek_schema as smoke


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-process-key")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-other-provider-key")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    original = httpx.AsyncClient.send

    async def mock_only(client, request, **kwargs):
        assert isinstance(client._transport, httpx.MockTransport), "A smoke unit test attempted external inference"
        return await original(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_only)


def test_default_mode_exercises_both_models_offline_and_reports_known_usage(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert smoke.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["mode"] == "offline" and report["status"] == "passed"
    assert report["max_provider_requests"] == 4
    assert [case["model"] for case in report["cases"]] == list(smoke.MODELS.values())
    for case in report["cases"]:
        assert case["schema_valid"] and case["tool_roundtrip"] and case["usage_complete"]
        assert len(case["requests"]) == 2
        assert case["usage"]["prompt_token_count"] == 22
        assert case["usage"]["candidates_token_count"] == 14
        assert case["usage"]["cached_content_token_count"] == 10
        assert case["usage"]["thoughts_token_count"] == 4
        assert case["answer"]["evidence"][0]["source"].startswith("probe:")
    assert os.environ["DEEPSEEK_API_KEY"] == "synthetic-process-key"
    assert "离线模拟" in capsys.readouterr().out
    assert "synthetic-process-key" not in output.read_text()


def test_missing_real_provider_key_fails_before_any_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.setattr(smoke, "components", lambda: pytest.fail("Dependencies should not load before key preflight"))
    output = tmp_path / "missing.json"
    assert smoke.main(["--real-models", "--output", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["status"] == "preflight_failed" and report["cases"] == []
    assert report["error"]["code"] == "missing_DEEPSEEK_API_KEY"


def test_existing_report_is_never_overwritten(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("previous evidence")
    assert smoke.main(["--output", str(output)]) == 2
    assert output.read_text() == "previous evidence"


def mock_real_provider(monkeypatch, mutation):
    c = smoke.components()
    original = c.resolve

    def resolve(model, **kwargs):
        adapter = original(model, **kwargs)
        normal = smoke.offline_transport(c, model, kwargs["output_schema"])

        def handle(request):
            response = normal.handle_request(request)
            body = json.loads(request.content)
            assert request.headers["authorization"] == "Bearer synthetic-process-key"
            if model == smoke.MODELS["pro"]:
                return response
            return mutation(c, body, response)

        adapter._transport = httpx.MockTransport(handle)
        return adapter

    c.resolve = resolve
    monkeypatch.setattr(smoke, "components", lambda: c)


@pytest.mark.parametrize("failure,code,schema_valid,request_count", [
    ("reject", None, False, 1),
    ("missing_fields", "schema_validation", False, 2),
    ("incomplete", "provider_response_unsuccessful", False, 2),
    ("wrong_evidence", "evidence_does_not_match_tool", True, 2),
])
def test_real_mode_reports_failures_without_fabricating_fields_and_continues_the_other_model(
        tmp_path, monkeypatch, capsys, failure, code, schema_valid, request_count):
    def mutate(c, body, response):
        if failure == "reject":
            return c.httpx.Response(400, json={"error": {"message": "synthetic-private-provider-content"}})
        final = any(item["type"] == "function_call_output" for item in body["input"])
        if not final:
            return response
        value = response.json()
        if failure == "incomplete":
            value["status"] = "incomplete"
        else:
            answer = json.loads(value["output"][0]["content"][0]["text"])
            if failure == "missing_fields":
                answer = {"ideas": []}
            else:
                answer["evidence"][0]["source"] = "fabricated:synthetic"
            value["output"][0]["content"][0]["text"] = json.dumps(answer)
        return c.httpx.Response(200, json=value)

    mock_real_provider(monkeypatch, mutate)
    output = tmp_path / (failure + ".json")
    assert smoke.main(["--real-models", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    flash, pro = report["cases"]
    assert report["mode"] == "real_models" and report["status"] == "failed"
    assert flash["status"] == "failed" and pro["status"] == "passed"
    assert flash["schema_valid"] is schema_valid and not flash["tool_roundtrip"]
    assert len(flash["requests"]) == request_count and "answer" not in flash
    if code:
        assert flash["error"]["code"] == code
        assert flash["usage_complete"]
    else:
        assert flash["error"]["http_status"] == 400
        assert flash["requests"][0]["usage"] is None and not flash["usage_complete"]
    assert "synthetic-private-provider-content" not in output.read_text() + capsys.readouterr().out


def test_explicit_secret_file_keeps_process_precedence_and_loads_no_other_provider(tmp_path, monkeypatch):
    selected = tmp_path / "selected-secrets"
    selected.write_text("DEEPSEEK_API_KEY=synthetic-file-key\nOPENAI_API_KEY=synthetic-unselected-key\n")
    selected.chmod(0o600)
    mock_real_provider(monkeypatch, lambda c, body, response: response)
    output = tmp_path / "env.json"
    assert smoke.main(["--real-models", "--env-file", str(selected), "--output", str(output)]) == 0
    assert os.environ["DEEPSEEK_API_KEY"] == "synthetic-process-key"
    assert os.environ["OPENAI_API_KEY"] == "synthetic-other-provider-key"
    assert "synthetic-file-key" not in output.read_text()


def test_interrupt_retains_the_started_request_and_stops_before_the_next_model(tmp_path, monkeypatch):
    async def interrupted(c, args, model, case, checkpoint):
        case["requests"].append({"sequence": 1, "status": "started", "usage": None})
        checkpoint()
        raise KeyboardInterrupt()

    monkeypatch.setattr(smoke, "exercise", interrupted)
    output = tmp_path / "interrupted.json"
    assert smoke.main(["--output", str(output)]) == 130
    report = json.loads(output.read_text())
    assert report["status"] == report["cases"][0]["status"] == "interrupted"
    assert len(report["cases"]) == 1 and report["cases"][0]["requests"][0]["usage"] is None


@pytest.mark.parametrize("arguments", [["--offline", "--real-models"], ["--env-file", "not-read"],
                                      ["--request-timeout-seconds", "0"], ["--max-output-tokens", "0"]])
def test_invalid_options_fail_before_dependencies_or_credentials(arguments):
    with pytest.raises(SystemExit) as raised:
        smoke.main(arguments)
    assert raised.value.code == 2


def test_explicit_missing_secret_file_does_not_get_silently_ignored(tmp_path):
    output = tmp_path / "missing-file.json"
    assert smoke.main(["--real-models", "--env-file", str(tmp_path / "absent"), "--output", str(output)]) == 2
    assert json.loads(output.read_text())["error"]["code"] == "secret_file_not_found"


def test_wall_timeout_keeps_unknown_usage_and_does_not_retry(tmp_path, monkeypatch):
    import asyncio

    c = smoke.components()
    original = c.resolve
    sent = []

    def resolve(model, **kwargs):
        adapter = original(model, **kwargs)

        async def delayed(request):
            sent.append(request)
            await asyncio.sleep(100)
            return httpx.Response(200, json={})

        adapter._transport = httpx.MockTransport(delayed)
        return adapter

    c.resolve = resolve
    monkeypatch.setattr(smoke, "components", lambda: c)
    output = tmp_path / "timeout.json"
    assert smoke.main(["--real-models", "--model", "flash", "--request-timeout-seconds", "1",
                       "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    [case] = report["cases"]
    assert len(sent) == len(case["requests"]) == 1
    assert case["error"]["type"] == "TimeoutError" and case["requests"][0]["usage"] is None
    assert not case["usage_complete"]


@pytest.mark.parametrize("message,problem", [
    ("This response_format type is unavailable now", "schema_format_unavailable"),
    ("Unknown variant json_schema; supported values: text, json_object", "schema_format_unavailable"),
    ("Invalid schema: required must include all properties", "schema_required_fields_rejected"),
    ("Invalid schema: minItems is not supported", "schema_keyword_rejected"),
    ("The json_schema format is not supported with tools", "schema_tools_combination_rejected"),
    ("The json_schema format is unsupported in thinking mode", "schema_reasoning_combination_rejected"),
    ("Invalid schema: additionalProperties must be false", "schema_definition_rejected"),
    ("Invalid model: model does not exist", "model_rejected"),
])
def test_provider_rejections_keep_safe_actionable_categories_without_messages(message, problem):
    from openai import BadRequestError

    private = " synthetic-private-token https://private-endpoint.example.test/secret"
    error = BadRequestError("SDK message: " + private,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.deepseek.com/responses")),
        body={"error": {"message": message + private, "code": "invalid_request_error", "param": "text.format"}})
    result = smoke.safe_error(error)
    assert result["http_status"] == 400 and result["provider_problem"] == problem
    assert result["provider_code"] == "invalid_request_error" and result["parameter"] == "text.format"
    serialized = json.dumps(result)
    assert "synthetic-private" not in serialized and "private-endpoint" not in serialized
    assert "message" not in result and len(result["message_sha256"]) == 64


@pytest.mark.parametrize("body", [{"message": "opaque", "code": {"private": "value"}, "param": ["secret"]},
                                  ["private body"], "private body"])
def test_provider_error_unknown_body_shapes_and_values_do_not_leak(body):
    from openai import BadRequestError

    error = BadRequestError("synthetic-private-error",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.deepseek.com/responses")), body=body)
    result = smoke.safe_error(error)
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)


def test_connection_diagnostic_distinguishes_dns_without_echoing_the_hostname():
    import socket

    error = RuntimeError("synthetic-private-host")
    error.__cause__ = socket.gaierror(8, "synthetic-private-host could not resolve")
    result = smoke.safe_error(error)
    assert result["connection_problem"] == "dns_resolution_failed"
    assert "private" not in json.dumps(result)


def test_real_mode_report_keeps_specific_schema_rejection_for_operator(tmp_path, monkeypatch):
    def reject(c, body, response):
        return c.httpx.Response(400, json={"error": {"message": "This response_format type is unavailable now",
                                                   "type": "invalid_request_error", "code": "invalid_request_error"}})

    mock_real_provider(monkeypatch, reject)
    output = tmp_path / "rejected-schema.json"
    assert smoke.main(["--real-models", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    error = report["cases"][0]["error"]
    request_error = report["cases"][0]["requests"][0]["error"]
    assert error["provider_problem"] == request_error["provider_problem"] == "schema_format_unavailable"
    assert len(report["cases"][0]["requests"]) == 1  # No retry or format downgrade hides a rejection.
    assert report["cases"][1]["status"] == "passed"
