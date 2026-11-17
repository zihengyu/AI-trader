# Autonomous Strategy Lab

Open strategy proposals, reproducible research, independent simulation and bounded paper-library admission for the optional autonomous fund.

Last Updated: 2026-11-17
Status: Initial implementation; research-only defaults; no live-trading authorization
Audience: Fund operators, researchers and contributors

## Overview

The fund previously combined market research, falsifiable directional ideas,
factor mining, discretionary manager decisions and deterministic risk/execution.
This increment adds a complete-strategy research lane. It does not claim that any
strategy has alpha, that the historical datasets are complete, or that the fund
has acquired a profitable strategy. No account credentials, broker permissions,
existing order settings or deployed services are changed by this implementation.

Researcher remains the principal investigator. Manager remains the investment
decision maker. Critic challenges evidence; it cannot grant statistical approval.
The lab is deterministic infrastructure, not another paid AI role.

## Existing owners and extension points

| Capability | Existing owner | Increment |
| --- | --- | --- |
| Research and handoff | `strategy.py`, `review.py` | Optional full proposals, persistent task updates, saved research context |
| Factor discovery | `factors.py` | Retained as a specialist tool, not a required strategy representation |
| Version/state reporting | `experiments.py` | Full strategy projections; immutable explicit-version configurations; independently retained candidate versions |
| Virtual accounting | `comparison.Book` | Next-open execution, close valuation, separate strategy/reference books, dated distribution receivables |
| Research economics | `costs.py`, `call_value.py` | Atomic resource reservations linked to existing paid-call receipts; daily/monthly/project bounds |
| Trade authority | `execution.py`, local broker | Optional admitted-strategy checks at decision acceptance, purchase submission and local acceptance/fill |
| Existing experiments | `learning.py`, `construction.py` | Unchanged; they do not become evidence that a new strategy passed |

New implementation files are under `scripts/autonomous_fund/`. Shared LumiBot
agent tools, research SQL fallback, provider adapters and example strategies are
unchanged. Research is never limited to selecting a developer-defined list of
economic strategies.

## Proposal and version contract

`Research` accepts empty `strategy_proposals` and `research_tasks`. There is no
daily proposal quota. A proposal records identity/version/parent/change type,
economic hypothesis, mechanism, instruments, optional additional input series,
market conditions, entry/exit/sizing, holding period, failure conditions, data
requirements, costs, predeclared validation and optional implementation details.

The mechanism is free text, not an enum. Supplemental research text and an
unsupported implementation remain inert data. The initial executable adapters are
`daily_rules_v1` and `daily_cash_rules_v1`; other implementations are retained as `needs_adapter` or
`needs_formalization`. Ordinary SQL and research tools remain available when
standard statistics do not answer a research question.

The complete proposal, initial capital, execution source and frozen policy are content-hashed. The same
id/version cannot receive different parameters, implementation or policy. New
versions require a known parent and identify revision, retraining or
revalidation. Earlier results and failures are retained. A new candidate does
not automatically retire an older usable version.

Research task updates can continue, request data, pause or retire *research work*.
They cannot promote a strategy, rewrite an implementation, alter production
permissions or disable an existing investment mandate. Task progress is available
across restarts and through the read-only `strategy_research` tool. Default agent
context includes a bounded set of detailed proposals; older versions are
retrievable without including every artifact in every paid prompt.

## Executable daily research

An implementation contains independent position rules with Boolean `entry` and
`exit` expressions, a numeric `weight` expression and optional numeric `p_`
parameters represented as typed name/value/integer entries for strict provider
schema compatibility. Supported operations include bounded arithmetic, comparisons,
Boolean logic, `field`, `mean`, `ret`, `stdev`, `min`, `max` and `abs`. State includes
holding sessions, current position weight, entry price and session index.

This can express stateful entry/exit, trend/reversal, cross-asset comparisons and
dated event/macro conditions. It does not require cross-sectional factor scores.
Features use their own observation/publication times. Arbitrary Python,
training a new ML model, intraday limit/stop simulation and unsupported execution
mechanisms require a separately reviewed adapter. They are not silently replaced
with a daily approximation and marked validated.

Signals use observations available at a completed session close. Orders execute
at the following session open with explicit fees/slippage and whole shares; the
book is valued at the close. Missing observations are never filled forward. A
missing input while holding cannot silently create a sale. Each test split and
each candidate has independent capital, cash, positions and trade receipts.

Daily-adapter `open_at` and `at` must match the actual NYSE session open and
close, including half days and daylight-saving changes. Publication delays belong
in `available_at`. Historical and forward windows must contain every exchange
session. Forward coverage begins with the first open after the frozen validation
receipt; a missing first or interior session is a data gap, not a shorter test.
Holding durations use the benchmark's session clock, not extra dates on another
instrument. These checks apply to daily replay data, not ordinary research sources.

Splits require explicit action receipts. Dividends belong to the position entering
ex-date, are receivables until payment, and cannot finance purchases before cash
arrives. Fractional split entitlements require cash-in-lieu support and are
currently withheld. `daily_cash_rules_v1` additionally reuses the configured cash
policy and existing parking planner in a daily-open funding/parking scenario,
including its buffer, band, idle-session delay, funding-sale costs, dividend
receivables and payment dates. Strategy and reference use the same cash convention.
Actual asynchronous orders, quote queues and approval delays are not reproduced.
The plain daily-rule adapter adds a blocking limitation when automatic sweeping
is configured; it cannot assume sweep income or execution is free.
Shared data costs apply to the passive reference too; its
model cost is zero. Model/data overhead reduces economic NAV without inventing
a broker cash withdrawal.

## Historical and forward validation

The frozen plan specifies training, validation, final and forward periods,
holding horizon, sample requirement, benchmark and its fixed exposure, drawdown
cap, excess-return requirement, robustness fraction and degradation conditions.
The method name is open: the initial `daily_forward_v1` verifier never forces an
unsupported rare-event or other protocol into daily significance tests. Such
methods remain explicit feasibility tasks until an appropriate verifier exists.
The evaluator records all tried versions when adjusting its statistical threshold.

Historical execution includes purged split boundaries, non-overlapping
holding-horizon blocks, HAC-adjusted uncertainty, a trials-adjusted hurdle,
fixed-parameter walk-forward diagnostics, declared-parameter sensitivity and an
additional observed-friction stress. A defensive mandate is assessed against
its declared protection objective and is never labeled evidence of alpha.

The first eligible validation freezes the exact benchmark sessions and input
snapshot digest for the version. The queued validation retains that snapshot.
If it diagnoses missing data, a new attested snapshot can repair those same
sessions. Adding later prices never moves the splits. New variants need a final
window disjoint from already reserved windows. Exploratory training, validation
and sensitivity tests remain available before a window can be reserved. They
skip the final test. Source coverage or modeled-cost gaps also skip it. Agent
context omits final results, including the individual pass flag. Operator
diagnostics can inspect the private evidence. Public historical prices may
already be known to the model or accessible through ordinary research tools:
this isolation cannot certify historical hypotheses as uncontaminated. Historical
results are exploratory, and a version frozen before the forward period is
mandatory for paper-library admission.

Raw-price identity, action completeness, historical membership and publication
coverage each require an operator or adapter receipt bound to the snapshot hash.
The existing daily cache does not establish those
facts. The automatic cache bridge sets these coverage flags to false and records
the session-boundary publication estimate as an estimate. It never invents
original publication receipts, current-universe completeness or delisting
returns. Such inputs can yield research diagnostics but cannot pass admission.

Passed historical versions enter an independent forward book. The worker first
evaluates the last completed close with fresh capital. Each accepted close then
produces the next signal. The journal binds that signal to the strategy and
policy hashes, exact input dataset, exchange open and job. The signal must be
saved before the next exchange open and match the signal actually used in the
simulated fill. Later historical corrections cannot rewrite a recorded input or
signal. An imported batch can provide replay results, with each session labeled
as prospective or replayed. Replayed sessions do not qualify. A missed signal
remains a gap for that version. A new revalidation version earns its own
genuinely forward sample. The benchmark uses independent capital over the same
sessions. Neither book makes a model call per bar.

The comparison read by the Manager and the operator uses exact exchange dates
from both books. Full-span returns are labeled when they include sessions
without timely evidence. Cross-strategy correlations use common close-to-close
returns from each book's uninterrupted prospective prefix. A replayed session
can change subsequent holdings, so later signals cannot restore that prefix.

For the risk comparison, the first 20 complete close-to-close pairs estimate
beta to the predeclared net passive book. The beta stays fixed for scoring the
remaining prospective sessions, using the same dates in both legs. The report
shows the calibration dates, evaluation dates, paired count and difference in
compounded net return. Fewer than 20 evaluation sessions carry a limited sample
label. Flat references and missing evidence remain unavailable. Scaling the
passive book assumes zero return on residual cash and excludes additional
reweighting or financing costs. This is a single-reference diagnostic. It
leaves the strategy qualification rule and the fund's authority to trade unchanged.

Fresh matching-policy forward evidence must satisfy both minimum durations
before optional library admission. A portfolio target must also match the signal
on file before the next open. Previous observations remain visible after
an execution fingerprint changes. The new contract requires revalidation.
Predeclared drawdown or sustained relative-loss degradation pauses the version;
already accepted strategy purchases lose authority at existing recheck points.
Researcher receives the failure and must propose a new version rather than
rewrite the paused implementation.

## Portfolio contribution study

An operator can declare a comparison between an existing research portfolio and
the same portfolio with part of its capital assigned to one candidate version.
Supplying the exact strategy IDs and versions keeps the comparison bound to the
selected journal. The declaration does not require paper-library admission.
The CLI adds an immutable research event, which appears in Manager context and
the Strategy Lab status command when results are available.

For example, save a study such as the following in `portfolio.json`. These are
illustrative IDs and costs. Provide the versions and assumptions for your study.

```json
{
  "study_id": "candidate-contribution-v1",
  "base": [
    {"strategy_id": "baseline-a", "version": "v1", "weight": 0.60},
    {"strategy_id": "baseline-b", "version": "v3", "weight": 0.40}
  ],
  "candidate": {"strategy_id": "candidate-a", "version": "v2"},
  "candidate_weight": 0.20,
  "fee_bps": 2,
  "slippage_bps": 8
}
```

```bash
python -m scripts.autonomous_fund.lab declare-portfolio --run-dir path/to/selected-run --portfolio portfolio.json
python -m scripts.autonomous_fund.lab status --run-dir path/to/selected-run
```

The base weights sum to one. In the second arm, the candidate replaces its
declared fraction of each base sleeve, preserving their relative allocation.
Both arms start with equal capital. The last 20 common prospective close-to-close
returns available at declaration estimate each arm's annualized volatility.
Both arms use the lower estimate as their target. The higher-risk arm reduces
its allocation and keeps the remainder in USD. The declaration freezes the
strategy and policy fingerprints, calibration return digest and allocation scales.
An incomplete restore leaves the comparison unavailable. Evaluation
starts with the first complete interval whose starting exchange close follows
the declaration. Subsequent results use the same dates in both arms. A replayed
or missing session stops the usable prefix for that sleeve. Results with fewer
than 20 evaluation sessions carry a limited-sample label.

Each arm rebalances to its declared weights at the start of each interval. The
saved net sleeve NAVs already include underlying modeled trading and operating
costs. The additional fee and slippage assumptions apply once to transfers of
capital between the sleeves and outside cash, including initial allocation.
Turnover and transfer expenses are reported as fractions of starting capital.
The report also shows the outside cash allocation, net return, drawdown and
realized volatility. Outside cash earns zero, and the final balances remain
marked at closing NAV. The study scales the underlying book costs with the
allocation. Actual fixed costs can behave differently. The study cannot estimate
security-level order netting, funding returns, whole-share differences at the
new sleeve sizes or actual account execution.

The cost sensitivity uses the same sessions and allocation scales. It keeps
the base arm at its declared costs and adds 5, 10, 25, 50 and 100 basis points
per dollar of combined-arm transfer. Each scenario replays the self-financing
transfers. When the incremental net return turns nonpositive, the report
shows the adjacent tested rates that bracket the threshold. It reports the
test limit when the advantage survives every tested rate. These are extra
transfer-cost assumptions, separate from the costs already in sleeve NAVs.

New portfolio declarations freeze an account-local execution reference from the
90 calendar days before declaration. It pairs only orders with a recorded
arrival bid and ask, complete fees, and fills that account for the full requested
quantity or a terminal observed filled quantity. All-in basis points are the
directional difference from arrival midpoint plus known fees, divided by the
matched shares' arrival midpoint notional. Price improvement can be negative.
The amount of verified execution covered by the matched sample is calculated
using actual fill notional for both numerator and denominator. Open or incomplete
orders and missing quotes or fees are reported separately. The sample also
reports the oldest arrival quote age observed within the matched orders.

Local simulation and broker paper data remain separate. Forward study returns
use the explicitly declared transfer costs. The saved reference documents what
was known about securities execution when the study was declared. Securities
costs and portfolio sleeve transfers represent different transactions. Later
reconciliations appear in current economic diagnostics. Existing declarations
retain their saved evidence, including the absence of an execution reference.

The paired panel compares compounded net returns within consecutive,
non-overlapping blocks of five trading sessions. The final partial block
is excluded from this panel and remains part of the full-period comparison.
With at least ten complete blocks and meaningful variation, the panel shows
an approximate 95% interval for the average five-session difference. This
interval can understate uncertainty when the blocks remain dependent. It is
a descriptive estimate for the declared study, separate from the portfolio's
cumulative net return and qualification rules.

## Authority and migration

Three permissions remain separate: scientific evidence, account-level strategy
library admission, and approval of a particular order plan. None replaces hard
risk constraints, source quote validation, account reconciliation, STOP or order
approval. Local simulation and Alpaca Paper remain the only connected fund modes;
the lab has no live broker mode.

Defaults in `config.json`:

```json
{
  "strategy_lab": {
    "enabled": true,
    "admission": "research_only",
    "allocation_mode": "legacy_mandate",
    "max_jobs_per_day": 2,
    "job_timeout_seconds": 120,
    "min_forward_sessions": 60
  },
  "research_budget": {
    "daily_usd": "2",
    "monthly_usd": "30",
    "project_usd": "10",
    "model_reservation_usd": "0.50",
    "validation_reservation_usd": "0.05"
  }
}
```

`research_only` grants no new portfolio authority. `legacy_mandate` retains the
existing discretionary manager's explicit investment mandate and does not label
it historically validated. An operator may separately select `paper_library`;
Manager may then allocate among qualified versions using strategy id, version,
artifact digest and capital weight. Target weights cannot exceed the combined
strategy signals after the same static-cap projection and cash/friction reserve
used by replay. Raw program weights are diagnostic only: a 50% capital allocation
to a sleeve capped at 10% permits at most a 5% fund target. Account/group/exposure limits continue to apply to the net
portfolio through the existing risk engine.

`validated_only` additionally requires library allocations for new purchases.
It requires an enabled paper library. Existing holdings may still be reduced,
and protective exits and system cash management keep their original authority.
Missing tables or unreadable library evidence cannot turn this policy into legacy
purchase authority. Such purchases receive the normal risk-denial result.
No startup migration implicitly enables this mode. `orders_enabled=false` in a
research-phase context is descriptive; execution capability isolation and the
existing order guards enforce the boundary.

Strategy accounting remains separate in the research lane. Manager allocations
are combined before using the existing single account execution ledger; this
initial implementation does not claim a fully reconciled production multi-sleeve
tax-lot attribution system. Different strategy versions sharing a security cannot
independently send broker orders.

## Resources and isolation

Research model and validation-compute work reserve resources in one atomic
SQLite transaction. Daily, calendar-month and lifetime project limits include
both actual observed estimates and unresolved reservations. Cache hits release
the corresponding model reservation at zero; unknown provider usage remains
reserved after interruption/restart. Charges are append-only and reference the
existing call id/cost ledger. A shared investigation is labeled shared rather
than charging its entire bill to every proposed strategy. Routine shared research
uses a calendar-month project identity so a lifetime per-strategy cap cannot
permanently disable all future daily research. Unknown reservations retain their
exact call/project link in the started-call receipt.

A validation job's lease, daily job slot and compute reservation are claimed in
the same write transaction. Concurrent workers cannot start extra jobs between
the quota check and reservation; a failed lease write rolls back its reservation.
An exhausted project stays queued while the next affordable project can run.
Shared daily and monthly limits still apply to every job. Consecutive jobs use
their own start and completion times so a faster job cannot backdate its result.

Reservations are configured economic estimates, not a provider-enforced invoice
cap. Actual model overruns are recorded and restrict subsequent optional work.
The compute allowance is explicitly a configured per-job resource charge, not a
measured cloud invoice. Operator data/compute receipts require their own evidence;
unknown bills must not be treated as zero. Existing decision-model accounting and
the operational model budget retain their separate owner.

Budget exhaustion pauses optional research only. It does not disable account
polling, risk checks, reconciliation, protective exits or existing orders. The
lab uses the data desk's cached data without new provider downloads on the
trading thread. A bounded single-worker queue prioritizes forward observations.
Technical failures retain their failed leases and may retry the identical
immutable job within budgets; they are not negative investment evidence.
Leases support restart recovery and prevent a stale worker from overwriting
newer results. STOP prevents new research jobs. Existing audit evidence is never
deleted to free a research allowance.

Execution uses a separate isolated Python interpreter with a cleared environment,
closed inherited file descriptors, bounded input/output, timeout and available
OS CPU/file/memory limits. It runs trusted repository code interpreting a
restricted expression tree. The language has no file, network, import, object
attribute, broker or arbitrary-code operation. This is capability isolation for
this adapter, **not** a claim that Python `exec` is safely sandboxed. Generated
Python is never imported into the worker or production process. A future generic
code adapter needs a separately reviewed OS/container boundary.

## Operator diagnostics

Read-only account-specific status:

```bash
bash scripts/fund-logs.sh strategy-lab
python -m scripts.autonomous_fund.lab status --run-dir artifacts/fund-local
```

Controlled import of an operator-owned snapshot and one deterministic job:

```bash
python -m scripts.autonomous_fund.lab import-data --run-dir artifacts/fund-local --dataset path/to/snapshot.json
python -m scripts.autonomous_fund.lab run-once --run-dir artifacts/fund-local
```

An imported dataset contains `bars` by symbol (day, timezone-aware open/close and
availability timestamps, positive raw OHLCV), optional dated `features`, action
receipts (symbol, ex-date, pay-date, amount/ratio, publication time), `source`, and
`coverage`. The four coverage keys are `raw_prices`, `corporate_actions_complete`,
`point_in_time_membership` and `publication_times_complete`. For formal validation,
set them using reviewed evidence. The `provenance` object has two fields.

- `snapshot_sha256`, computed by `strategy_validation.digest` on the exact JSON
  object excluding `provenance`.
- `claims`, keyed by the four coverage fields. Each entry has a `source`, a
  retrievable `reference` to the relevant evidence and its `artifact_sha256`.

Retain the original evidence separately for review. The Lab checks the snapshot
binding, receipt shape, publication clocks and market sessions. It does not
retrieve the referenced artifacts or independently certify a provider's claims.
Missing manifests, changed snapshot contents or estimated timestamps support
exploratory work only. The importer rejects future and invalid timestamps and
excessive inputs.

For automated intake, atomically replace `strategy_lab_snapshot.json` in the
account's run directory from an authorized data adapter. The existing off-hours
hook imports the file and starts the available jobs. To build the forward record,
deliver each completed daily bar and its evidence before the following open.
Daily additions can carry only that session and new available observations. The
Lab joins them to the immutable previously accepted inputs. Check status for
prospective and replayed session counts, the last validation, and remaining
budget. The provider access and source evidence remain the operator's responsibility.

## Acceptance and remaining qualification

Deterministic tests cover open non-factor proposals; no-new-idea output; immutable
versions; independent candidates and cross-day task progress; malformed and
capability-escaping expressions; future and unpublished observations; next-open
fills; in-sample winners rejected out of sample; missing membership evidence;
atomic concurrent/monthly budgets; isolated worker operation; holdout reuse;
forward-only admission; revocation; distribution receivables; and read-only
diagnostics. Regression runs cover the existing fund lifecycle, risk, approvals,
execution and reporting. Synthetic passes establish software behavior, never
investment returns or real-model strategy creativity.

Further qualification requires real provider provenance, usable historical
membership/actions, forward observations, and evidence that incremental returns
cover research/operation costs. Advanced execution/ML adapters, independent
strategy-generation A/B experiments and full production sleeve attribution are
not established by this initial adapter. Existing A/B controls remain available,
and no extra paid agents are enabled to imitate missing statistical evidence.
