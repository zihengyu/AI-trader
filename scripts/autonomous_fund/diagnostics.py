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
