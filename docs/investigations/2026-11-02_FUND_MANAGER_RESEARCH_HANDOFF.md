# Autonomous Fund Research and Manager Handoff

**Last Updated:** 2026-11-02

**Status:** Local regression verified; remote runtime OSError still requires its retained location

## Failure and contract

The regular-session `_decide` previously called the researcher, validated its
answer, then called the portfolio manager in the same try block. A researcher
exception set the shared persisted decision backoff to 5, 15, then 60 minutes.
No manager call occurred. Healthy process telemetry did not expose this missing
portfolio assessment, and a restart retained the shared retry.

In local and broker-paper sessions the manager now runs directly, using current
account/risk/news context, its own read-only tools and any saved premarket packet.
The packet retains its original timestamps. `research_status` reports available,
unavailable, invalid or previous-session work; invalid packets are withheld and
missing research is explicitly disclosed in the manager's instructions. No
synthetic research or automatic investment conclusion replaces missing evidence.

One base intraday assessment needs one remaining manager call rather than two
calls. The daily counter, reserved review budget, manager review floor, shadow
budget checks, validated-target executor, STOP and order controls still apply.
Manager failures and invalid targets retain the existing 5/15/60-minute backoff,
now with a persisted failure role and a role-specific maintenance status label.
Historical runs retain the two-role chain and dated evidence validation, keeping
missing historical research from silently reaching a trading decision.

## Restart migration

Startup first restores the last accepted target's review time. A persisted retry
is detached only if its role is researcher, or the matching legacy backoff event
and the last relevant agent event before it identify that role. Later unrelated
pre/post-market events cannot reclassify the failed cycle. A manager failure,
missing attribution or mismatched retry evidence keeps the original schedule.

The migration atomically clears only the old decision-failure metadata and
records `research_backoff_detached`. It leaves STOP, accepted targets, approvals,
unresolved reservations, order counters and actual agent-run counts intact. The
normal accepted-target review time is never brought forward by this migration.

## Validation and limits

The regression first reproduced the researcher exception preventing the live
manager from running. Scripted tests cover both live application modes, absent
and invalid packets, original/older-session timestamps, the one-call budget
boundary, manager failure backoff and reset, attributed/unattributed restart
migration, STOP, observe-only and budget controls, and historical evidence gating.

A native LocalPaperBroker/StrategyExecutor test fails pre-open research, persists
an old researcher backoff, restarts after opening and verifies a manager-only
assessment reaches a validated target and a virtual order. The existing native
restart/allocation and plan-approval regressions use the updated manager-only
call expectation while retaining their order/fill assertions. No provider
inference or brokerage request is used for these tests.

The focused 13-file regression selection passes 268 tests. The modified public
documentation page also builds successfully with the repository's Sphinx setup.

This is a phase handoff, not a threaded scheduler: existing pre-open/post-close
hooks and runtime timeouts remain synchronous. It removes the intraday research
dependency and its retry gate. It does not establish that the remote OSError
affects only the researcher; if shared preparation fails for the manager too,
the manager error must be diagnosed from `operation_error` and fixed separately.

Deploy the commit and restart the runner with the original account directory.
Inspect STOP and unresolved orders through the existing controls. Once resumed
in an open session and with budget available, the next eligible assessment
records `agent_call_started` for manager without a new intraday researcher call.
Use `decision_validated` to confirm a successful decision; a heartbeat alone is
not evidence of success.
