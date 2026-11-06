"""Failures retain actionable locations and errno without provider secrets."""
import errno
import json

from scripts.autonomous_fund.diagnostics import exception_diagnostic
from scripts.autonomous_fund.status import summary
from .test_autonomous_fund_stability import live


def test_chained_os_error_retains_errno_without_messages_paths_or_locals():
    secret = "synthetic-private-token"
    try:
        try:
            raise OSError(errno.ENOSPC, "private response " + secret, "/private/account/credentials.env")
        except OSError as cause:
            raise RuntimeError("https://example.invalid/?key=" + secret) from cause
    except RuntimeError as error:
        detail = exception_diagnostic(error, phase="trading_iteration")
    assert detail["error"] == "RuntimeError"
    assert detail["phase"] == "trading_iteration"
    assert detail["causes"][0]["error"] == "OSError"
    assert detail["causes"][0]["errno"] == errno.ENOSPC
    assert detail["causes"][0]["errno_name"] == "ENOSPC"
    assert detail["frames"][-1]["function"] == "test_chained_os_error_retains_errno_without_messages_paths_or_locals"
    assert detail["frames"][-1]["line"] > 0
    encoded = json.dumps(detail)
    assert secret not in encoded
    assert "/private" not in encoded
    assert "example.invalid" not in encoded
    assert "private response" not in encoded


def test_message_only_os_error_still_retains_its_failure_location():
    def source_inspection():
        raise OSError("private provider payload")
    try:
        source_inspection()
    except OSError as error:
        detail = exception_diagnostic(error, phase="premarket_research")
    assert detail["error"] == "OSError"
    assert "errno" not in detail
    assert detail["frames"][-1]["function"] == "source_inspection"
    assert "private provider payload" not in json.dumps(detail)


def test_live_iteration_logs_a_safe_diagnostic_and_status_displays_it(tmp_path):
    strategy = live(tmp_path)
    strategy.halt_path.unlink()
    strategy._reconcile = lambda: None
    logs = []
    strategy.log_message = lambda message, **kwargs: logs.append(message)

    def unreadable_account():
        raise OSError(errno.EACCES, "private-provider-token", "/private/account.env")

    strategy._snapshot = unreadable_account
    strategy.on_trading_iteration()
    strategy.on_trading_iteration()
    events = strategy.journal.all_events()
    errors = [row for row in events if row["kind"] == "operation_error"]
    assert len(errors) == 1  # Repeated per-minute faults do not flood the journal.
    assert errors[0]["body"]["phase"] == "trading_iteration"
    assert errors[0]["body"]["errno_name"] == "EACCES"
    assert errors[0]["body"]["frames"][-1]["function"] == "unreadable_account"
    notices = [row["body"] for row in events if row["kind"] == "notification"]
    # Python maps OSError(EACCES, ...) to PermissionError; keep that existing
    # notification identity while retaining its otherwise missing errno.
    assert any(row == {"kind": "blocked", "details": "PermissionError"} for row in notices)
    strategy.journal.set_meta("decision_failures", 2)
    strategy.journal.set_meta("decision_retry_at", "2026-11-02T14:45:23+00:00")
    text = summary(tmp_path, "local", now=strategy.get_datetime())
    assert "EACCES" in text
    assert "unreadable_account" in text
    assert "连续失败 2 次" in text
    assert "2026-11-02T14:45:23+00:00" in text
    assert "private-provider-token" not in json.dumps(events) + text + str(logs)
    assert "/private/account.env" not in json.dumps(events) + text + str(logs)
    assert any("operation_error" in line for line in logs)


def test_diagnostic_logging_survives_a_broken_journal(tmp_path):
    strategy = live(tmp_path)
    logs, notices = [], []
    strategy.log_message = lambda message, **kwargs: logs.append(message)
    # Exercise app-notice failures while preserving the native public API.
    strategy._notify_fund = lambda kind, details: notices.append((kind, details))
    strategy.journal.event = lambda *args: (_ for _ in ()).throw(OSError("private journal error"))
    strategy._disclose_failure("blocked", OSError("private provider error"), phase="trading_iteration")
    assert notices == [("blocked", "OSError")]
    assert any("operation_error" in line for line in logs)
    assert "private" not in str(logs)


def test_premarket_failures_also_keep_the_source_location(tmp_path):
    strategy = live(tmp_path)
    strategy.halt_path.unlink()

    def failed_research(**kwargs):
        raise OSError("private provider error")

    strategy.agents["researcher"].run = failed_research
    strategy.before_market_opens()
    errors = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "operation_error"]
    assert errors[0]["phase"] == "premarket_research"
    assert errors[0]["frames"][-1]["function"] == "failed_research"
    assert "private provider error" not in json.dumps(errors)


def test_cyclic_exception_chains_are_bounded():
    first, second = OSError("private first"), RuntimeError("private second")
    first.__cause__, second.__cause__ = second, first
    detail = exception_diagnostic(first, phase="trading_iteration")
    assert len(detail["causes"]) == 1


def test_status_keeps_final_mkdir_errno_instead_of_handled_parent_errors(tmp_path):
    strategy = live(tmp_path)
    detail = {"phase": "trading_iteration", "error": "OSError", "errno": errno.EROFS,
              "errno_name": "EROFS", "frames": [
                  {"module": "lumibot.components.memory.store", "function": "__init__", "line": 140},
                  {"module": "pathlib", "function": "mkdir", "line": 1313}],
              "causes": [{"error": "FileNotFoundError", "errno": errno.ENOENT, "errno_name": "ENOENT",
                          "frames": [{"module": "pathlib", "function": "mkdir", "line": 1313}]}]}
    strategy.journal.event(strategy.get_datetime(), "operation_error", detail)
    text = summary(tmp_path, "local", now=strategy.get_datetime())
    assert "errno=30 (EROFS)" in text
    assert "errno=2 (ENOENT)" not in text
    assert "lumibot.components.memory.store.__init__:140" in text


def test_status_uses_os_cause_when_outer_wrapper_has_no_errno(tmp_path):
    strategy = live(tmp_path)
    detail = {"phase": "trading_iteration", "error": "RuntimeError", "frames": [],
              "causes": [{"error": "OSError", "errno": errno.ENOSPC, "errno_name": "ENOSPC",
                          "frames": [{"module": "synthetic.store", "function": "write", "line": 12}]},
                         {"error": "FileNotFoundError", "errno": errno.ENOENT, "errno_name": "ENOENT",
                          "frames": [{"module": "pathlib", "function": "mkdir", "line": 1313}]}]}
    strategy.journal.event(strategy.get_datetime(), "operation_error", detail)
    text = summary(tmp_path, "local", now=strategy.get_datetime())
    assert "ENOSPC" in text and "synthetic.store.write:12" in text
