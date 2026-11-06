"""Keep dependency logging failures from terminating the fund's scheduler."""
import logging
import json
import sys


class SchedulerLogger(logging.LoggerAdapter):
    """Retain normal filters; withhold the original record if logging fails.

    A long AI iteration can legitimately trigger APScheduler's max-instances
    warning. LiteLLM installs a filter on that logger which can fail during a
    lazy import. Logging must not turn a skipped overlap into a dead scheduler.
    """

    def process(self, msg, kwargs):
        return msg, kwargs  # Keep the scheduler's original structured extras.

    def log(self, level, msg, *args, **kwargs):
        try:
            return super().log(level, msg, *args, **kwargs)
        except Exception as error:
            # Never bypass a failed redaction filter with the original message,
            # arguments, exception text or extras; they can contain credentials.
            try:
                from .diagnostics import exception_diagnostic
                diagnostic = json.dumps(exception_diagnostic(error, phase="scheduler_logging"),
                                        ensure_ascii=True, separators=(",", ":"))
                sys.stderr.write(f"FUND scheduler_logging_error: {type(error).__name__}; "
                                 "original log record withheld; scheduler continues; "
                                 f"diagnostic={diagnostic}\n")
            except Exception:
                pass  # Even an unavailable stderr cannot end scheduling.


def configure_scheduler_logging(strategy):
    """Wrap this scheduler's existing logger before the native scheduler starts."""
    scheduler = strategy._executor.ensure_scheduler()
    if not isinstance(scheduler._logger, SchedulerLogger):
        # configure(logger=...) also clears native job stores/executors. Change
        # only this instance's logger, retaining its filters and scheduling rules.
        scheduler._logger = SchedulerLogger(scheduler._logger, {})
