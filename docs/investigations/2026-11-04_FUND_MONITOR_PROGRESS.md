# Autonomous Fund Monitor Progress and Status

Bound notification work, require observed continuous recovery, respect idle schedules and report runner liveness accurately.

**Last Updated:** 2026-11-04

**Status:** Corrected and verified locally; target-host installation and served behavior remain unverified

**Audience:** Autonomous fund maintainers and local/paper operators

## Overview

Read-only inspection and synthetic account reproductions found four application
defects. No running account or external provider is required to reproduce them.
Changes belong to the optional application under `scripts/autonomous_fund` and
do not change shared LumiBot code, provider adapters, prompts or account choice.

## Generic Contracts and Corrections

1. **Notifications must yield to independent supervision.** Telegram commands
   previously processed an entire backlog in one poll. Twelve successful status
   replies alone took 108 simulated seconds; with a fetch and two notifications,
   the regression poll took 135 seconds, exceeding both the 45-second execution
   gate and the 90-second supervisor freshness check. Notifications and commands now share a 20-second monotonic
   budget, checked before each unit of work; the existing ten-second network
   timeout still applies to an in-flight request. Queue priority alternates so
   either backlog can progress. The command offset advances only over processed
   messages, preserving deferred approvals and their existing authorization.
2. **Recovery requires continuous observations of the current runner.** The old
   persisted `healthy_since` survived an unobserved gap or a runner/monitor
   restart and could clear STOP on the first returning poll. Health observations
   now bind to the monitor instance and runner startup sequence. Missing prior
   observations, gaps over 45 seconds or backward time restart the health period.
   Operator stops, unresolved reservations and three automatic resumes per day
   retain their authority.
3. **A valid idle schedule must reach its next iteration.** The application
   accepts 15–300-second intervals; its previous 180-second stall threshold
   stopped a 300-second schedule after 190 idle seconds, before work was due.
   Only idle waits use `max(heartbeat_seconds, iteration_seconds + 10)`; started
   iterations retain the existing progress threshold, and active model calls
   retain their own threshold. Missing or invalid old interval records use the
   existing threshold. The default 30-second schedule retains 180 seconds.
4. **No STOP is permission state, not evidence of a running process.** An account
   with no heartbeat, or an exited runner with a two-hour-old heartbeat, formerly
   still printed that trading was running. Status now requires a current
   `running` heartbeat and distinguishes missing, exited and stale/invalid state.
   The presence of an operator STOP continues to take priority.

## Configuration Classes and Negative Controls

All local and broker-paper application accounts use these rules, without symbol,
strategy, customer or provider-specific branches. Resume can be enabled or
disabled; operator and non-resumable fault stops remain intact. Default, shorter
and maximum valid schedules are covered, as are started iterations.

Synthetic tests cover slow command and notification backlogs together, cursor
continuity, normal fresh status, an operator stop, monitor and runner restarts,
observation gaps and a backward clock. Existing resume tests retain the
unresolved-order, automatic-resume-count and disabled-resume negative controls.
No model inference or real Telegram delivery is part of these tests.

## Validation and Source-to-Served Boundary

Local verification on Python 3.12/macOS passed 172 tests with one Linux-only
systemd-verification skip across the progress, monitor, decision-drought, resume,
execution, server, realtime and shell-script test files. This includes 28 new
progress/status cases plus the existing authorization and order protections.
Dependencies used the repository's existing constraints; package manifests were
not changed. No paid model call or real Telegram delivery was made.

An independent negative control loaded the original HEAD monitor/status code
in memory, without modifying working-tree files, and ran the same final progress
tests in the final environment: 18 failed for the reproduced defects and ten
controls passed. Reports are `monitor-progress-original-control.xml` and
`monitor-progress-after.xml` under the ignored `artifacts/fund-qa`. The affected
public `common_mistakes` page built successfully with the repository's Sphinx
configuration and warnings treated as errors; its output is `artifacts/fund-qa/docs`.

These checks demonstrate application behavior only; the target Ubuntu host's installed
revision, restart, order reconciliation and new decision events must be verified
after its operator installs the correction. No remote update or deployment is
performed by this change.
