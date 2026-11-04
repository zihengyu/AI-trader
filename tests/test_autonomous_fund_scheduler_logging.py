"""A dependency log-filter failure must not kill the actual native scheduler."""
import logging
import threading
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.runtime_logging import SchedulerLogger, configure_scheduler_logging


def test_native_scheduler_continues_after_an_overlapping_job_log_fails(monkeypatch, capsys):
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES
    from lumibot.strategies.strategy_executor import StrategyExecutor

    class FailedDependencyFilter(logging.Filter):
        def filter(self, record):
            raise KeyError("litellm")

    logger = logging.getLogger("apscheduler.scheduler")
    monkeypatch.setattr(logger, "filters", [FailedDependencyFilter()])
    monkeypatch.setattr(logger, "level", logging.WARNING)
    executor = object.__new__(StrategyExecutor)
    executor.scheduler = None
    configure_scheduler_logging(SimpleNamespace(_executor=executor))
    scheduler = executor.scheduler
    assert "On_Trading_Iteration" in scheduler._jobstores
    assert scheduler._executors["default"] is executor._scheduler_job_executor
    started, release, skipped, continued = (threading.Event() for _ in range(4))
    runs = []

    def job():
        runs.append(1)
        started.set()
        if len(runs) == 1:
            release.wait(5)
        else:
            continued.set()

    scheduler.add_listener(lambda event: skipped.set(), EVENT_JOB_MAX_INSTANCES)
    scheduler.add_job(job, "interval", seconds=.05, id="synthetic-iteration", max_instances=1)
    try:
        scheduler.start()
        assert started.wait(3)
        assert skipped.wait(3)  # The warning's exception did not end _process_jobs.
        assert scheduler._thread.is_alive()
        release.set()
        assert continued.wait(3)  # A real later iteration is scheduled, not just a heartbeat.
    finally:
        release.set()
        scheduler.shutdown(wait=True)
    assert "scheduler_logging_error: KeyError" in capsys.readouterr().err


def test_actual_sdk_redaction_failure_withholds_sensitive_record(monkeypatch, capsys):
    from litellm import _logging as sdk_logging
    logger = logging.getLogger("synthetic.fund.scheduler")
    monkeypatch.setattr(logger, "filters", [sdk_logging.SecretRedactionFilter()])
    monkeypatch.setattr(logger, "level", logging.WARNING)

    def broken_namespace(*args, **kwargs):
        raise KeyError("litellm")

    monkeypatch.setattr(sdk_logging.diagnostics, "run", broken_namespace)
    SchedulerLogger(logger, {}).warning("synthetic-private-token %s", "synthetic-private-argument")
    output = capsys.readouterr()
    assert "scheduler_logging_error: KeyError" in output.err
    assert "synthetic-private" not in output.err + output.out


def test_successful_filters_still_redact_and_rejected_records_stay_rejected(monkeypatch, caplog):
    logger = logging.getLogger("synthetic.fund.redaction")

    class Redact(logging.Filter):
        def filter(self, record):
            record.msg = "synthetic-redacted"
            record.args = ()
            return True

    monkeypatch.setattr(logger, "filters", [Redact()])
    with caplog.at_level(logging.WARNING):
        SchedulerLogger(logger, {}).warning("synthetic-private-token")
    assert "synthetic-redacted" in caplog.text and "synthetic-private-token" not in caplog.text
    caplog.clear()
    monkeypatch.setattr(logger, "filters", [lambda record: False])
    SchedulerLogger(logger, {}).warning("synthetic-rejected")
    assert "synthetic-rejected" not in caplog.text
