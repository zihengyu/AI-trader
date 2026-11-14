"""Stop-cause retention and journal diagnostics; synthetic events only."""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund import stops


def test_a_supervised_exit_preserves_the_original_liveness_stop(tmp_path):
    now = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
    original = stops.write(tmp_path, "watchdog", "iteration_stalled", now)
    for reason in ("runner_exited", "heartbeat_stale", "agent_call_stalled"):
        stops.write(tmp_path, "watchdog", reason, now + timedelta(seconds=30))
    assert stops.read(tmp_path) == original
    # A stronger fault and an explicit operator stop must still take priority.
    stops.write(tmp_path, "watchdog", "low_disk_space", now + timedelta(minutes=1))
    assert stops.read(tmp_path)["reason"] == "low_disk_space"
    stops.write(tmp_path, "operator", "operator_stop", now + timedelta(minutes=2))
    assert stops.read(tmp_path)["class"] == "operator"


def test_errors_keep_interleaved_tracebacks_and_skip_large_successful_research():
    def entry(message, pid="1"):
        return json.dumps({"MESSAGE": message, "_PID": pid, "__REALTIME_TIMESTAMP": "1793793600000000",
                           "_SYSTEMD_UNIT": "synthetic.service"})

    lines = [entry("Exception in thread synthetic-scheduler:"), entry("Traceback (most recent call last):"),
             entry("2026-11-04 15:00:00 | INFO | FUND premarket_research: quoted ERROR " + "x" * 20000, "2"),
             entry('  File "synthetic_monitor.py", line 12, in poll'),
             entry("    read_status()"), entry("KeyError: 'synthetic-field'"),
             entry("During handling of the above exception, another exception occurred:"),
             entry("Traceback (most recent call last):"), entry("  File \"synthetic.py\", line 3, in run"),
             entry("    raise RuntimeError('synthetic')"), entry("RuntimeError: synthetic"),
             entry("2026-11-04 15:00:01 | INFO | normal heartbeat"),
             entry("synthetic.service: Main process exited, code=exited, status=254/n/a", "3")]
    result = subprocess.run([sys.executable, "-m", "scripts.autonomous_fund.log_errors"],
                            input="\n".join(lines), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for text in ("synthetic-scheduler", "read_status()", "synthetic_monitor.py", "KeyError", "During handling", "RuntimeError", "status=254"):
        assert text in result.stdout
    assert "premarket_research" not in result.stdout and "normal heartbeat" not in result.stdout


def test_error_record_limit_never_splits_a_traceback():
    from scripts.autonomous_fund.log_errors import error_records
    records = [{"MESSAGE": f"WARNING older {i}", "_PID": "2"} for i in range(100)]
    records += [{"MESSAGE": line, "_PID": "1"} for line in
                ["Traceback (most recent call last):", *["    synthetic_frame()"] * 300, "ValueError: synthetic"]]
    result = error_records(records, limit=2)
    assert len(result) == 2 and "older 99" in result[0]
    assert "Traceback" in result[1] and "ValueError" in result[1]
    assert result[1].count("synthetic_frame()") == 300


@pytest.mark.parametrize("exception_name", ["ObservationPending", "QuoteNotReady",
                                           "synthetic.feed.DataUnavailable", "lowercase_signal",
                                           "synthetic.read.<locals>.FeedPending"])
@pytest.mark.parametrize("message", ["", ": synthetic diagnostic"])
def test_errors_keep_custom_exception_summaries_with_or_without_a_message(exception_name, message):
    from scripts.autonomous_fund.log_errors import error_records

    def entry(text, pid="1"):
        return {"MESSAGE": text, "_PID": pid, "_SYSTEMD_UNIT": "synthetic.service"}

    summary = exception_name + message
    records = [entry("Traceback (most recent call last):"),
               entry('  File "synthetic.py", line 12, in read_quote'),
               entry("    observe()"), entry("INFO other-process heartbeat", "2"),
               entry(summary), entry("INFO normal iteration")]
    [trace] = error_records(records)
    # Python exception classes need not end in Error/Exception or carry a message.
    assert trace.splitlines()[-1] == summary
    assert "read_quote" in trace and "heartbeat" not in trace and "normal iteration" not in trace


def test_errors_keep_chained_custom_exceptions_in_one_record():
    from scripts.autonomous_fund.log_errors import error_records

    lines = ["Traceback (most recent call last):", '  File "synthetic.py", line 1, in read_quote',
             "    observe()", "synthetic.feed.DataUnavailable: stale observation", "",
             "The above exception was the direct cause of the following exception:", "",
             "Traceback (most recent call last):", '  File "synthetic.py", line 2, in value_book',
             "    read_quote()", "ValuationPending: waiting for source evidence", "INFO normal iteration"]
    [trace] = error_records([{"MESSAGE": line, "_PID": "1"} for line in lines])
    assert "DataUnavailable: stale observation" in trace
    assert "direct cause" in trace and "ValuationPending: waiting for source evidence" in trace
    assert trace.count("Traceback (") == 2 and "normal iteration" not in trace


def test_custom_exception_matching_is_scoped_to_the_same_process_traceback():
    from scripts.autonomous_fund.log_errors import error_records

    records = [{"MESSAGE": "Traceback (most recent call last):", "_PID": "1"},
               {"MESSAGE": '  File "synthetic.py", line 1, in read_quote', "_PID": "1"},
               {"MESSAGE": "Inventory: ordinary output from another process", "_PID": "2"},
               {"MESSAGE": "QuoteNotReady: waiting for evidence", "_PID": "1"},
               {"MESSAGE": "Inventory: ordinary output immediately after the summary", "_PID": "1"},
               {"MESSAGE": "INFO normal iteration", "_PID": "1"},
               {"MESSAGE": "Inventory: ordinary output after the traceback", "_PID": "1"},
               {"MESSAGE": "WARNING answer has 1 validation error for Research", "_PID": "3"},
               {"MESSAGE": "summary", "_PID": "3"},
               {"MESSAGE": "  Input should be a valid string", "_PID": "3"},
               {"MESSAGE": "Inventory: ordinary output after validation", "_PID": "3"}]
    traces = error_records(records)
    assert len(traces) == 2 and "QuoteNotReady: waiting for evidence" in traces[0]
    assert "summary\n  Input should be a valid string" in traces[1]
    assert "Inventory:" not in "\n".join(traces)


def test_monitor_sigint_exits_cleanly_without_clearing_the_stop(tmp_path, monkeypatch):
    from scripts.autonomous_fund import monitor, run
    stops.write(tmp_path, "operator", "operator_stop")

    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(monitor, "run_monitor", interrupted)
    run.main(["monitor", "--run-dir", str(tmp_path), "--console-only"])
    assert stops.read(tmp_path)["class"] == "operator"


def test_errors_keep_validation_fields_and_details_across_interleaved_processes():
    from scripts.autonomous_fund.log_errors import error_records

    def entry(message, pid="1"):
        return {"MESSAGE": message, "_PID": pid, "_SYSTEMD_UNIT": "synthetic.service"}

    records = [entry("WARNING Agent answer did not match Research: 1 validation error for Research"),
               entry("evidence.3.observed_at"), entry("INFO monitor heartbeat", "2"),
               entry("  Input should be a valid string [type=string_type, input_value=None]"),
               entry("    For further information visit https://errors.pydantic.dev/example"),
               entry("INFO [agents] summary=" + "x" * 20000),
               entry("WARNING structured_output_invalid: 1 validation error for Critique"),
               entry("missing_evidence_placeholder"),
               entry("  Extra inputs are not permitted [type=extra_forbidden]"),
               entry("INFO normal iteration")]
    result = error_records(records)
    assert len(result) == 2
    assert "evidence.3.observed_at" in result[0] and "string_type" in result[0]
    assert "missing_evidence_placeholder" in result[1] and "extra_forbidden" in result[1]
    assert "summary=" not in "\n".join(result) and "heartbeat" not in "\n".join(result)
