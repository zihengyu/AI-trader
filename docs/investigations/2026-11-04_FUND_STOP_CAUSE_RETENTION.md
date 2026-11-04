# Autonomous Fund Scheduler Failure, Stop Causes and Macro Tools

Keep dependency logging failures from stopping scheduling, preserve diagnostic evidence and restore native FRED tools.

**Last Updated:** 2026-11-04

**Status:** Scheduler failure reproduced and application correction verified locally; target-host served correction remains unverified

**Audience:** Local and paper fund maintainers and operators

## Overview

The supplied service output establishes an `iteration_stalled` supervision stop,
subsequent runner exits, and exhaustion of three automatic resumes within 24
hours. A fresh runner heartbeat and `active` services then coexist with STOP.
Additional excerpts contain monitor exits with status 254, scheduler overlap,
a logging `KeyError`, native FRED tool attribute errors and SQL tool errors.
The original errors command removed traceback frames and printed large research
successes. A subsequent raw-log tail contained the exit report instead of the
earlier exception. Narrowing each service's time window supplied the actual stack.

## Established Failure Path

An AI critique overlaps the next scheduled iteration. APScheduler correctly raises
and handles `MaxInstancesReachedError` for the one-instance job; skipping that
overlap is normal. Its `_process_jobs` then logs a warning. LiteLLM's
`SecretRedactionFilter` calls `_process_record`, then `rust_bridge.diagnostics.run`,
which imports `rust_bridge.catalog` and `types.secret_managers.main`. Importlib's
namespace-parent lookup raises `KeyError: 'litellm'`. This exception escapes logging
and kills the APScheduler thread, so a living process stops scheduling iterations.
The independent watchdog can then detect lost progress and stop/restart the runner.
The excerpt does not establish why that namespace was missing in the first place.

Separately, the monitor's status-254 stack follows systemd's explicit `Stopping`
entry and ends in `KeyboardInterrupt`; it is a stop/restart interruption, not
evidence of an independently crashing monitor. Traceback source-line text can
also reflect files updated after that process loaded its bytecode.

## Contracts and Changes

- All local/paper fund configurations retain native `Strategy.macro` for FRED
  tools. The application's optional `MacroWatch` moves to `macro_watch`; enabling
  or disabling its summary cannot replace the native client's public interface.
  No shared LumiBot implementation changes are needed.
- The local/paper CLI wraps its native scheduler instance's existing logger.
  Successful log filters and rejected records retain their behavior. If a filter
  or handler throws, only a fixed diagnostic and exception class go to stderr;
  the original message, arguments, traceback and extras are withheld rather than
  bypassing failed secret redaction. The scheduler continues. Native job stores,
  executors, concurrency and schedule are preserved. Actual model or trading
  exceptions are not caught by this logging adapter.
- The first resumable watchdog reason and timestamp survive later resumable
  watchdog writes until that STOP is cleared. A non-resumable watchdog fault
  and an operator stop still override it. The current health continues to be
  reported independently; preserving the cause does not pretend health recovered.
- `fund-logs.sh errors` consumes journal JSON, keeps the last 80 error records,
  and groups traceback frames by service/PID so interleaved services and line
  limits do not hide the original stack. The raw journal remains available.
- No heartbeat threshold, inference timeout, daily resume limit, order gate,
  account selection or trading authorization is relaxed.
- Monitor SIGINT exits cleanly without clearing STOP. Start/restart option
  forwarding also works with Bash 3.2's empty-array/nounset behavior; variable
  names adjacent to Chinese punctuation are explicitly delimited.

## Validation and Negative Controls

An actual native APScheduler runs a blocked job with max_instances=1, reaches
the overlap warning with a failing log filter, and schedules another iteration
after release. An unguarded negative-control run fails at the overlap event with
the same scheduler-thread exception. A second test invokes the installed
LiteLLM redaction filter with its diagnostics import failure injected, asserting
that private fixture text is withheld. Successful redaction and rejected records
are negative controls too.

Synthetic native-tool tests exercise FRED series, latest and snapshot calls
through the initialized fund with the app summary both enabled and disabled.
HTTP/model responses are fixtures. Preparation tests retain incremental FRED
warm-up, and data-desk tests retain intraday refresh behavior.

STOP tests cover a supervised exit after a stall, escalation to low disk, and
operator priority. Existing resume tests cover continuous health, the three-resume
limit and unresolved orders. Journal tests cover interleaved and chained traces,
a trace longer than the record count and omission of large successful research.

Local verification: the scheduler, native FRED tools, incident, stability, resume,
realtime, server and shell-script batch passed 106 tests with one Linux-only skip
on macOS. The incident, macro/calendar, preparation, data-desk, settings and polling
batch passed 93 tests. The final HTML documentation build completed; an existing
unrelated toctree warning remains. The deliberate unguarded scheduler negative
control failed with the expected thread-ending `KeyError`.

## Remaining Host Evidence

The first earlier `iteration_stalled` event may have additional causes; the
later scheduler-thread failure is established by the supplied full stack.
A cleanup-time nanobind leak warning alone does not prove it initiated the
stop. SQL binder/parser errors are returned tool results; the excerpts do not
establish that they terminated the runner. Confirm the server's exact commit.

Local regressions are application evidence, not source-to-served proof. After an
operator installs and restarts the corrected revision on the selected host,
record that revision, check the normal STOP/resume and order state, and confirm
fresh `decision_validated` events. Keep the account journal and caches intact.
