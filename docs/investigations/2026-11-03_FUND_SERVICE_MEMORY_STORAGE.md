# Native Memory Storage in the Autonomous Fund Service

**Last Updated:** 2026-11-03

**Status:** Remote failure identified and reproduced through the native CLI path; target-host deployment remains to be verified

## Evidence and cause

The retained operation error shows this path before a model runtime is called:

```text
AgentHandle._memory_state
_LazyStrategyComponent.__getattr__ / _load
MemoryStore.__init__
Path.mkdir
OSError errno=30 EROFS
```

`MemoryStore` supports the existing `LUMIBOT_MEMORY_DIR` setting and otherwise
uses `<working-directory>/.lumibot/memory/<strategy>/memory.sqlite`. The fund
CLI configured `LUMIBOT_CACHE_FOLDER` under its run directory, but did not set
the separate memory root. The generated systemd unit uses `ProtectSystem=strict`
and grants writes under `artifacts` and the selected account directory. The
unconfigured working-directory memory default is outside those writable roots.
This preparation failure can affect researcher, critic and manager alike.

The final error also retained recursive `mkdir(parents=True)` ENOENT contexts.
The original status formatter always selected the deepest context, so it showed
errno 2 at pathlib instead of the actual errno 30 and native memory caller.
The full diagnostic record itself had preserved the correct error.

## Changes and invariants

Before native components load, a working CLI command now defaults an unset or
empty `LUMIBOT_MEMORY_DIR` to the absolute `<run-dir>/memory`. Local and broker
paper entrypoints share this setup. Nonempty operator overrides retain priority.
Read-only status/configuration paths do not set or create a memory directory.
The generic MemoryStore default, permissions and persistence API are unchanged;
the application uses its existing configuration contract. No systemd filesystem
permissions are broadened, and no memory tool is removed or silently bypassed.

Status prefers the final exception's errno, falls back to an OS cause for a
non-OS wrapper, and displays the nearest non-standard-library frame when one is
available. Raw diagnostics remain bounded and omit paths/messages/locals.

Account binding, SQLite trading journal, approvals, drawdown state, STOP,
unresolved orders, daily call counts and retry controls are untouched. The
application's post-market strategy memo remains in its existing journal.

## Validation

The native CLI regression reproduces the pre-fix failure by rejecting the
working-directory `.lumibot` subtree with EROFS, while leaving the selected
account directory writable. It uses the real `run.main(local --once)`, Trader,
StrategyExecutor, LocalPaperBroker, lazy memory and AgentHandle preparation.
Only public-market downloads, final-report risk-free data and inference are
replaced with explicit synthetic fixtures. Before the fix, the manager runtime
is never reached and an EROFS/backoff is recorded. After the fix, its memory
database is created inside the account, a decision validates and a virtual order
is submitted, without attempting the forbidden directory.

Additional tests cover both CLI execution modes, explicit memory-root priority,
read-only status, EROFS/ENOENT error selection and wrapped OS errors. The focused
14 tests and the 166-test selection of memory, diagnostics, role scheduling,
native premarket, settings and durable execution pass. One existing Windows path
separator assertion was excluded from the broader selection. The native CLI
test's assertions also pass inside the restricted Windows sandbox, but process
shutdown there returns INVALID_HANDLE; the verified run exits normally outside
that sandbox. No real inference or brokerage connection is used. This models
the confirmed filesystem failure, not a complete Linux mount-namespace test.

## Deployment and existing memory

Update the version branch and restart the runner so the new environment default
applies. Keep the original account directory and inspect/resume STOP through the
existing scripts. Existing manager retries and budgets remain effective; success
is established by a new `decision_validated`, not by a heartbeat or an old error
remaining visible in status.

If native memory was previously populated by a command-line run at the generic
working-directory default, the new account-local default does not copy or delete
that older store. Preserve it and migrate the selected strategy's memory while
stopped, or explicitly select an existing writable memory root under the service
policy. The reported failure occurred before the affected store could be created.
An explicit override outside the service's writable roots must be corrected by
the operator; the launcher preserves it rather than widening access.
