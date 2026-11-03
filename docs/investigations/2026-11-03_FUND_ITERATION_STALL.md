# Autonomous Fund Iteration Stall After a Midday Update

Why the runner was stopped as `iteration_stalled` three times on 2026-11-02 and how optional data preparation is now bounded.

**Last Updated:** 2026-11-03

**Status:** Cause reproduced with a fake clock and fixed; target-host behavior after deployment remains to be confirmed from its journal

**Audience:** Autonomous fund maintainers and local/paper operators

## Overview

The operator's status showed a watchdog STOP (`iteration_stalled`, 16:35 UTC), a
supervision stop, and `auto_resume_exhausted` after three automatic resumes in 24
hours, with the account untouched (cash only, no orders). The separate native-memory
EROFS shown in the same status was earlier (16:01 UTC) and is fixed by
`2026-11-03_FUND_SERVICE_MEMORY_STORAGE.md`.

## Cause

The independent monitor declares `iteration_stalled` when, during the regular
session, more than `heartbeat_seconds` (180) pass without an `iteration_started`,
`iteration_finished`, `agent_call_started` or `agent_call_finished` event.

The deployed integration (`66faf83`, `efa8b64`) added optional research data that
the first decision of a process built synchronously, before any model call:
factor-lab history for 16 tradable stocks, a 40-name research universe and 14 ETFs
(one full-history download each), SEC company facts for up to 56 companies on first
use, the earnings calendar (16 calendar calls), FRED (6 series) and stress-window
histories. A midday restart has no pre-open warm-up, so this ran inside a trading
iteration and exceeded 180 seconds without a progress event. The STOP restarted the
runner, the restart lost the in-memory caches (daily bars, factor panel), and the
next decision repeated the same work: three stalls exhausted the automatic resumes.

The status text then still promised an automatic resume, and `fund-start.sh` did not
offer one for a watchdog STOP, so the operator had no prompted way back. Separately,
`fund-start.sh` uses `systemctl enable --now`, which leaves running services on their
old code, so running it after an update appeared to do nothing.

## Fix and invariants

- Each local/paper trading iteration gets `PREPARATION_BUDGET_SECONDS` (45) for optional
  data. Factor history, SEC downloads, earnings-calendar and FRED fetches and
  stress-window histories check it before each network unit and defer the rest to
  later iterations. Progress is kept (daily bars per day in memory, SEC extracts on
  disk, calendar rows in the journal). Incomplete results are never cached as final.
- Idle iterations and STOP iterations spend the remaining budget on preparation, so
  data is ready when trading resumes. Preparation never places orders.
- A factor cross-section is used only when complete (`warming` meanwhile): a partial
  cross-section would bias every IC. Deferred earnings dates are listed and do not
  cap; deferred FRED series are marked; deferred stress histories are proxied
  pessimistically and the benchmark is always fetched first.
- Required risk data (tradable daily history, quotes, account) is unchanged and
  synchronous. Pre-open/post-close hooks and historical runs have no budget, so
  replays stay deterministic.
- `status` says when automatic resumes are exhausted for the current STOP;
  `fund-start.sh`/`fund-restart.sh` then offer the resume like an operator stop.
  `fund-restart.sh` really restarts both services (optionally after a fast-forward
  pull and a pinned reinstall) and waits for a monitor state newer than the restart.

## Validation

`tests/test_autonomous_fund_preparation_budget.py` first failed (no budget existed),
then passes: with a fake clock and ten-second downloads, one iteration stops within the
budget and later iterations complete the panel without re-downloading a symbol;
hooks are unbounded; replays have no budget; calendar, FRED, SEC and stress-window
downloads defer and resume. Script tests cover exhausted resumes, the restart order
(`enable --now` then `restart`), `--update` and the freshness check. No network,
model or broker is used.

## Deployment

On the server: `bash scripts/fund-restart.sh --update -y`, then confirm with
`bash scripts/fund-logs.sh` that trading runs and, after the next review, that a
`decision_validated` event appears. A heartbeat alone does not prove a decision.
