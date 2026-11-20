"""Keep dependency logging failures from terminating the fund's scheduler."""
import json
import logging
import sys

from apscheduler.executors.base import MaxInstancesReachedError


OVERLAP_WARNING = ('Execution of job "%s" skipped: maximum number of running '
                   'instances reached (%d)')


class SchedulerLogger(logging.LoggerAdapter):
    """Log ordinary records through the configured filters.

    A long AI iteration can legitimately trigger APScheduler's max-instances
    event. Emit a constant diagnostic for that event because the dependency's
    redaction filter can fail during a lazy import. Withhold the original
    record if a filter fails on any other event.
    """

    def process(self, msg, kwargs):
        return msg, kwargs  # Keep the scheduler's original structured extras.

    def log(self, level, msg, *args, **kwargs):
        try:
            if (level == logging.WARNING and msg == OVERLAP_WARNING
                    and isinstance(sys.exc_info()[1], MaxInstancesReachedError)):
                if self.isEnabledFor(level):
                    # This event needs no job repr or exception message. Avoid
                    # sending a routine scheduling skip into SDK redactors.
                    sys.stderr.write("FUND scheduler_overlap: previous invocation still running, overlap skipped\n")
                return
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
