# Autonomous Fund Failure Diagnostics

Retain actionable failure locations when a healthy runner reports a skipped iteration.

**Last Updated:** 2026-11-03

**Status:** Remote native-memory EROFS identified; see the memory-storage follow-up

**Audience:** Autonomous fund maintainers and local/paper operators

## Overview

`healthy`, fresh heartbeats and an absent STOP describe process liveness and the
permission to attempt work. They do not establish that research, portfolio
decisions or execution succeeded. A trading iteration catches unexpected errors
and previously retained only their type, such as `OSError`. The same short
notification is deduplicated for 30 minutes, so its absence in later iterations
does not establish recovery.

## Diagnostic contract

Unexpected failures in trading iterations and premarket research now persist an
`operation_error` journal event and write a corresponding runtime log entry.
They retain the phase, exception class, integer OS errno and its standard name
when available, and up to eight module/function/line frames per exception.
Up to three underlying exception causes are retained, with cycle protection.
Identical diagnostics are rate-limited to one per 30 minutes per process.

No exception messages, source lines, local variables, filenames, request bodies
or authenticated URLs are serialized. Generic OS messages come from
`os.strerror(errno)`, not the exception's potentially private message.
The original short notifications and the existing order, STOP, retry and budget
behavior are preserved. A failed diagnostic write cannot end the strategy;
runtime logging is attempted independently of the journal write.

The maintenance status displays the most recent diagnostic with its timestamp,
location and errno, plus any persisted AI failure count and retry time. The
diagnostic is a historical record, not a second process-health verdict.

The follow-up trace identified `MemoryStore.__init__` during lazy native-memory
loading. Its final errno was EROFS; recursively handled ENOENT contexts had
misled the first status formatter. Status now prefers the final error's own
errno and a non-stdlib caller, using a chained OS cause only when the final
error has no errno. See
`2026-11-03_FUND_SERVICE_MEMORY_STORAGE.md` for the path configuration fix.

## Investigation and deployment

Confirm the deployed commit and imported source before interpreting old output.
The non-UTC opening-clock fix is commit `64f7336b`; branch updates that omit it
do not incorporate that fix. A later diagnostic-only patch does not prove that
the remote OSError itself is fixed.

After deploying and restarting the updated runner, inspect
`bash scripts/fund-logs.sh status` and the `FUND operation_error` runtime entries.
Use the retained location and errno to reproduce and fix the underlying failure.
No model inference or real account order is needed to test this diagnostic path.

## Validation

The focused diagnostics, bookkeeping-stability, premarket/native-lifecycle and
automatic-resume suites pass 44 tests. The public documentation page builds with
Sphinx. A broader hardening run passes another 39 tests; three Linux deployment
checks fail on the Windows development host because of POSIX permission/path
expectations and the deliberate refusal to generate Linux services on Windows.
Those platform-specific checks were left unchanged.
