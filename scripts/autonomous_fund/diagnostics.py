"""Failure locations and OS codes, excluding messages, source text and locals."""
import errno
import os
import re


def _record(error):
    result = {"error": type(error).__name__, "frames": []}
    code = getattr(error, "errno", None)
    if type(code) is int and -(2 ** 31) <= code < 2 ** 31:
        result.update(errno=code, errno_name=errno.errorcode.get(code, "UNKNOWN"), os_message=os.strerror(code))
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        module = frame.f_globals.get("__name__", "unknown")
        if not isinstance(module, str) or not re.fullmatch(r"[\w.]{1,160}", module):
            module = "unknown"
        result["frames"].append({"module": module, "function": frame.f_code.co_name, "line": trace.tb_lineno})
        result["frames"] = result["frames"][-8:]
        trace = trace.tb_next
    return result


def exception_diagnostic(error, *, phase):
    """Do not serialize exception messages: SDK errors can include credentials."""
    result = {"phase": phase, **_record(error), "causes": []}
    seen = {id(error)}
    for _ in range(3):
        cause = error.__cause__
        if cause is None and not error.__suppress_context__:
            cause = error.__context__
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        result["causes"].append(_record(cause))
        error = cause
    return result


def _validation_path(location, schema):
    """Show schema-owned field names and indices, never an untrusted extra key."""
    root, path = schema, ""
    for part in location:
        if "$ref" in schema:
            schema = root.get("$defs", {}).get(schema["$ref"].rsplit("/", 1)[-1], {})
        if type(part) is int and "items" in schema:
            path += f"[{part}]"
            schema = schema["items"]
        elif isinstance(part, str) and part in schema.get("properties", {}):
            path += ("." if path else "") + part
            schema = schema["properties"][part]
        else:
            return path + ("." if path else "") + "<extra>"
    return path or "$"


def structured_answer_diagnostic(result, schema):
    """Explain a failed answer locally; never echo input values or call a model.

    The native parser's human-readable error can include private input values
    and is truncated. Reuse its JSON extraction and the same application schema
    to recover field locations and error codes for operator notifications.
    """
    from pydantic import ValidationError
    from lumibot.components.agents.structured_output import _extract_json_value

    payload = getattr(result, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("runtime_error"):
        error = payload.get("error_class")
        return {"kind": "runtime_error", "error": error if error in {
            "TimeoutError", "ConnectionError", "OSError", "RuntimeError", "ValueError",
        } else "provider_failure"}
    text = getattr(result, "summary", None) or getattr(result, "text", None)
    if not isinstance(text, str) or not text.strip():
        return {"kind": "missing_answer"}
    shape = answer_shape(text)
    try:
        value = _extract_json_value(text)
    except ValueError:
        return {"kind": "invalid_json", "answer_shape": shape}
    if schema is not None:
        try:
            schema.model_validate(value)
        except ValidationError as error:
            errors = error.errors(include_input=False, include_context=False, include_url=False)
            json_schema = schema.model_json_schema()
            return {"kind": "schema_validation", "count": len(errors), "answer_shape": shape, "errors": [
                {"path": _validation_path(item["loc"], json_schema), "type": item["type"]}
                for item in errors[:5]]}
    return {"kind": "unparsed_answer", "answer_shape": shape}


def answer_shape(text):
    """What the answer was at its top level, without its content.

    object / array / string / number / boolean / null: the whole text is one JSON value of
    that type. fenced: a Markdown code block. prose: anything else, such as text before the
    JSON (a lenient parser may then pick a bracket out of the prose, e.g. "[1]" as a list).
    """
    import json
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except ValueError:
        return "fenced" if stripped.startswith("```") else "prose"
    for name, kind in (("object", dict), ("array", list), ("string", str), ("boolean", bool)):
        if isinstance(value, kind):
            return name
    return "null" if value is None else "number"
