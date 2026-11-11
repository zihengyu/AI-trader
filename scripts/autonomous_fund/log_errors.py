"""Select journal errors without discarding their traceback frames (stdin: journalctl -o json)."""
import json
import re
import sys
from collections import deque
from datetime import datetime, timezone


ERROR = re.compile(
    r"(?:^|\|\s*)(?:WARNING|ERROR|CRITICAL)\b|^(?:[\w.]+(?:Error|Exception)|KeyboardInterrupt|SystemExit):"
    r"|Main process exited|Failed to |Server supervision stopped"
    r"|FUND (?:\w*error|stopped|stop_active|watchdog_stop|server_supervision_stop|auto_resume_exhausted"
    r"|price_alert_unavailable|price_observation_rejected|local_market_data_pending|closing_valuation_pending):"
    r"|\[observability_warning\]"
)
TRACE = re.compile(
    r"^(?:\s|Traceback \(|Exception in thread |--- Logging error ---|During handling of the above exception|"
    r"The above exception was the direct cause|[\w.]+(?:Error|Exception)(?::|$)|"
    r"KeyboardInterrupt|SystemExit)"
)
VALIDATION = re.compile(r"validation errors? for\b|structured_output_invalid:")
VALIDATION_FIELD = re.compile(r"^[\w]+(?:\.[\w]+)*(?:\[[\d,]+\])?$")


def _label(entry):
    try:
        at = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        at = "unknown-time"
    return f"{at} {entry.get('_SYSTEMD_UNIT', 'unknown-unit')}[{entry.get('_PID', '?')}]"


def error_records(entries, limit=80):
    """Keep the last `limit` error records, with chained traces grouped per process.

    Another service's output must not break a traceback. The limit applies to
    records rather than lines, so even a long traceback retains its first frame.
    """
    records, traces, validations = deque(maxlen=limit), {}, set()
    for entry in entries:
        message = entry.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        key = (entry.get("_SYSTEMD_UNIT"), entry.get("_PID"))
        current = traces.get(key)
        if current is not None and (not message or TRACE.match(message) or
                                    (key in validations and VALIDATION_FIELD.fullmatch(message))):
            current.append(message)
            continue
        traces.pop(key, None)
        validations.discard(key)
        is_trace = message.startswith(("Traceback (", "Exception in thread ", "--- Logging error ---"))
        if is_trace or ERROR.search(message):
            record = [_label(entry), message]
            records.append(record)
            if is_trace or VALIDATION.search(message):
                traces[key] = record
            if VALIDATION.search(message):
                validations.add(key)
    return ["\n".join(record) for record in records]


def main():
    def entries():
        for line in sys.stdin:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry
    for record in error_records(entries()):
        print(record)


if __name__ == "__main__":
    main()
