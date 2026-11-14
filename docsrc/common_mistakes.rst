Common Mistakes and How to Avoid Them
======================================

.. meta::
   :description: This page documents the most common mistakes made when writing Lumibot strategies, along with the correct patterns to use instead.

This page documents the most common mistakes made when writing Lumibot strategies, along with the correct patterns to use instead.

Autonomous Fund Scorecards and Whole-Share Purchase Limits
--------------------------------------------------------------------------------

Prediction and allocation scorecards in the optional autonomous fund compare
returns over the same start and end dates, selected from the benchmark's
completed sessions. A missing endpoint is ``unavailable``; a benchmark horizon
that has not completed is ``pending``. A later symbol observation cannot replace
the missing endpoint. Missing intermediate closes do not prevent an
endpoint-to-endpoint comparison.

Research ideas use the NYSE calendar's actual closing time, including half days.
An idea formed at or after the close starts at the next close. Its horizon also
uses benchmark-selected endpoints; missing intermediate symbol bars cannot
leave a mature idea pending or shift the comparison dates.

The fund's ``affordability`` context counts whole shares using the executor's
configured limit buffer and upward cent rounding. Near a position ceiling,
local and paper modes refresh the execution ask. For example, an account worth
10,000 with a 10% position cap cannot buy one share at an ask of 999.50 with a
0.1% limit buffer: the rounded purchase limit is 1,000.50. Displayed share
prices and weights retain the source price before the buffer. A completed-close
fallback is indicative; submission still checks fresh quotes, cash, exposure
and trading eligibility.

Unknown Model Costs and Split-Adjusted Benchmark Returns
--------------------------------------------------------------------------------

An interrupted model call may have no usage information. Without a recorded
``cost_usd``, its cost remains unknown, and the autonomous fund's acceptance
report leaves system net returns unknown. Scripted calls, local answer-cache
hits and explicitly recorded zero costs retain their zero-cost treatment.

Benchmark returns apply recorded corporate actions in date order, paying cash
on the shares held at each dividend date. A later split changes shares while
preserving previously paid cash. For example, one share worth 100 that pays 2,
then splits two-for-one and ends at 49 per share, has a zero return: shares
worth 98 plus cash of 2. Same-day dividends use post-split shares, regardless
of event insertion order. Payouts are held as cash rather than automatically
reinvested.

Package Metadata Removing Existing Distributions
--------------------------------------------------------------------------------

Reading package metadata, including ``python setup.py --version``, preserves
existing files in ``dist/``. Wheel builds clean their intermediate build
directory to exclude stale package files. Remove completed distributions only
as an explicit cleanup step when they are no longer needed.

Pre-Open Hooks Delayed on a Non-UTC Server
--------------------------------------------------------------------------------

Continuous local, paper and live strategies should select the same opening
instant regardless of the server timezone. The shared broker clock now uses an
explicitly timezone-aware UTC instant when selecting the next session. Earlier
versions could interpret a local wall clock as UTC, choose tomorrow's session and
miss today's ``before_market_opens()`` hook and opening bell. Background telemetry
could continue while the strategy slept.

Deploy the corrected runtime and restart an affected runner so its existing long
wait is recalculated. Keep its account state and inspect the normal STOP and order
status before resuming. Changing a strategy's timezone or pre-open buffer is not
needed to compensate for this bug. Alpaca's provider-clock overrides and
backtesting's simulated clock retain their existing behavior.

Healthy Process, Failed Autonomous Fund Iterations
--------------------------------------------------------------------------------

In the optional autonomous paper-fund application, fresh heartbeats, ``healthy``
monitoring and a cleared STOP indicate process liveness. An individual research
or trading iteration may still fail. Identical short alerts are deduplicated for
30 minutes, so the absence of a new alert does not establish business recovery.

A missing STOP alone does not establish liveness. The status command reports
trading as running only with a ``running`` runner heartbeat no older than 180
seconds; absent, exited, stale and future-dated heartbeats are identified
separately. Inspect the current decisions and errors to establish business
progress.

The watchdog also rejects future-dated runner heartbeats as
``heartbeat_unavailable``; they cannot establish continuous health for automatic
resume. Real polling samples the UTC clock after reading the heartbeat, so a
normal update published during the poll remains valid. A corrected heartbeat
starts the normal continuous-health period again.

Malformed STOP field types, invalid supplied timestamps and non-UTF-8 content
retain an operator stop. They no longer crash STOP reads or permit a service
restart to replace that stop. The normal explicit resume command remains
available after inspection.

The monitor shares a 20-second work budget between Telegram notifications and
commands, alternating which queue goes first. An already-started request retains
its ten-second network timeout. Deferred commands remain unread at the durable
cursor. A monitor restart, an observation gap over 45 seconds, a backward clock
or a new runner startup restarts the continuous-health period before automatic
resume. Operator stops, unresolved orders and the daily resume cap still apply.

While waiting between iterations, the stall threshold is the greater of 180
seconds and the runner's recorded iteration interval plus ten seconds. An
iteration that has already started retains the original 180-second progress
threshold; model-call liveness retains its separate threshold. This lets valid
longer schedules wait for their next iteration without masking a stuck job.

The maintenance status now displays the most recent ``operation_error`` with its
timestamp, phase, module, function, line number and OS errno when available. It
also shows the persisted AI failure count and retry time. Detailed safe records
are written to the journal and runtime log without exception messages, local
variables, source text, filenames or authenticated request contents. Use the
retained location to diagnose the underlying fault; additional diagnostics alone
do not remediate that fault.

Intraday Market Coverage in the Autonomous Fund
---------------------------------------------------------------------------------

The optional autonomous fund's data desk prioritizes market benchmarks, holdings
and price alerts. When the source responds with only forming minutes, the desk
records those symbols as pending and can make one paced retry of the missing
priority symbols before the next full scan. A series of source failures still
ends the round.

In ``desk_latest(section="intraday")``, use ``coverage`` to check the number of
usable prices, the missing benchmarks and the size of the breadth sample.
Breadth and movers exclude suspect prices and bars whose source timestamps are
over 15 minutes old, future-dated or missing when read. A partial breadth sample
describes the covered symbols. The snapshot update time and each original
bar time are reported separately, along with any refresh error.

Opening Valuation and Corporate-Action Notices
---------------------------------------------------------------------------------

In the local autonomous fund, opening valuation can wait for the first fresh
holding quote. When the opening minute is still forming and only a prior
session's completed bar is available, the opening notice names the holding
and gives the earliest time the first minute can close. The broker poll retries
valuation and the next strategy iteration checks the account again. Inspect
``price_observation_rejected`` for the source time, quote age and rejection reason.

The daily Yahoo and Alpaca cross-check lists dates and event types for
unconfirmed corporate actions and identifies which provider lacked a
same-date match. The notice includes up to 20 items. The complete detail is
in the New York trading day's ``corporate_action_quality.json`` cache.
An Alpaca cash dividend with an unknown or different currency remains in the
audit as currency evidence. The comparison waits for compatible currency
information before comparing the same-date total, including when a separate
same-date payment is in USD. An explicitly USD amount from a foreign issuer
can be compared.

Concurrent Autonomous Fund Daily-Cache Refreshes
--------------------------------------------------------------------------------

Each shared daily-cache write uses its own staging file, including concurrent
threads in the same process. Publishing replaces the destination atomically.
A failed write keeps the previous complete value and removes only its own
staging file. Successful refreshes still change the cache version used by the
runner to reload data.

Earnings Windows and External Research Data
--------------------------------------------------------------------------------

The autonomous fund counts ``earnings_window_days`` using NYSE sessions,
including half days and skipping exchange holidays. Zero covers today only,
including on weekends; it does not roll the cutoff forward to Monday.

FRED observations from CSV, API responses and the daily cache must have a valid
date and a finite numeric value. Zero and negative rates remain usable. NaN
and infinity do not enter percentiles or produce a false low-volatility flag.
A refresh with no usable observations is unavailable. If a runner has fetched
valid data but cannot write its optional cache, it retains the data in memory
for the day rather than replacing it with a cache error or repeatedly fetching.
An unusable older cache is a miss: the runner fetches normally when preparation
budget permits, or defers the request when it does not.

Alpaca fallback daily bars require explicit source timezones and finite positive
closes. Host timezone settings cannot reinterpret a naive timestamp as an
earlier session. A malformed symbol response is reported independently and
does not substitute another symbol's history.

Read-Only Native Memory in the Autonomous Fund Service
--------------------------------------------------------------------------------

The generic native memory default is ``.lumibot/memory`` under the working
directory. The fund's systemd service permits writes under ``artifacts`` and
the selected account directory. Previously its CLI configured the cache but
omitted the separate memory root, so the first lazy ``MemoryStore`` load could
raise ``EROFS`` before either agent reached its model call.

The fund CLI now defaults an unset or empty ``LUMIBOT_MEMORY_DIR`` to
``<run-dir>/memory``. Explicit nonempty overrides remain in effect; select a
path the service may write. Restart the updated runner to load this setting,
keeping the same account journal and normal STOP/resume checks. Status displays
the final exception's OS errno before looking for an OS cause and identifies
the nearest non-standard-library caller. Intermediate handled ``ENOENT``
contexts from recursive directory creation no longer hide the final ``EROFS``.

Autonomous Fund STOP After Repeated Restarts
--------------------------------------------------------------------------------

An ``active`` systemd service and fresh runner heartbeat can coexist with a
watchdog STOP. After three automatic resumes in 24 hours, trading stays stopped
until the operator investigates and explicitly resumes. The initial liveness
fault and its time are now retained through the resulting supervised exit;
a stronger fault or operator stop still takes priority.

One reproduced cause is an AI call outlasting its scheduler interval.
APScheduler normally skips the overlapping job, but a LiteLLM log filter raised
``KeyError: 'litellm'`` while logging that warning and terminated the scheduler
thread. The local/paper application now guards its scheduler logger: normal
filters still run; a logging failure emits only a fixed diagnostic and exception
type, withholding the original record, and scheduling continues. Job concurrency,
trading gates and model errors retain their behavior. A normal monitor SIGINT
also exits cleanly without clearing STOP.

Run ``bash scripts/fund-logs.sh errors`` to see the last 80 warning/error records
within 24 hours, including complete tracebacks. The previous line filter removed
stack frames and included large successful research messages, obscuring the
exception. Inspect runner and monitor logs separately when reading raw journal
output. A process heartbeat alone does not establish that decisions recovered.

The application's optional precomputed macro context also uses a separate
attribute from native ``Strategy.macro``. This fixes ``MacroWatch`` missing-method
errors from FRED tools without replacing the native client. Direct FRED tools
still require ``FRED_API_KEY``; disabling the application's macro summary does
not disable those tools. Restart the updated application to load the correction.

Research Failures Delaying the Autonomous Fund Manager
--------------------------------------------------------------------------------

In the optional local and broker-paper fund application, regular-session
portfolio assessments now invoke the manager directly. Saved premarket research
is supplied with its original times and a status identifying missing, invalid or
previous-session work. The manager retains its own read-only research tools and
must verify material claims and disclose gaps. A fresh researcher call is no
longer a prerequisite for an opening or later portfolio assessment.

Manager failures still use the persisted review backoff and daily inference
budget. On startup, a matching legacy journal failure attributable only to the
researcher is detached from the manager's retry schedule and audited as
``research_backoff_detached``. Manager and unattributed failures retain their
retry times. STOP, order reconciliation, approvals and execution checks remain
in force. Restart the updated runner to load this behavior; a manager call can
still fail if the underlying runtime fault affects both roles.

Historical replays retain their dated research and evidence validation before a
manager decision. Pre-open and post-close research remain read-only lifecycle
work; this change does not introduce background research workers.

Stale Prices and Changing Historical Fund Reports
--------------------------------------------------------------------------------

The optional autonomous fund admits a daily close only after that New York
session's actual NYSE close, including daylight saving and early-close days.
Before that instant the current daily bar remains excluded. The source cache,
prefetched file and strategy cache refresh at the close so a morning response
cannot suppress an available closing price. Missing source closes remain
disclosed; a current quote is never substituted for a close.

In local and paper modes, an exception or empty daily-history response is
cached for 60 seconds before another attempt. Dependent risk estimates, styles,
factor views, snapshots and scorecards are refreshed when that failure expires;
successful symbol histories remain reusable. The source's own backoff still
applies, and missing-history risk controls remain active until data recovers.
Historical replays retain deterministic caching independent of wall-clock time.

Scorecards refresh when a relevant visible research, decision, review or
execution event is appended, at the close, and on the next New York date.
Unrelated heartbeats retain the cache. Future-dated events remain excluded
when querying with a simulated clock.

Rebuilding an old daily report uses mandate, reserved-cash, target and approval
evidence available at its cutoff. Approval expiry uses that same instant, and
the current monitor file is omitted. Later capital changes do not rewrite an
old report's authorization; missing historical mandate evidence stays unknown.
The rolling model-cost summary uses one window for call counts, tokens,
unpriced calls and costs. An empty recent window does not turn old priced
calls into a missing-price warning.

Account reports group daily marks by New York date and select them by observation
time, so a late backfill cannot replace a newer account mark. Benchmark price
returns use the account's exact first and last observation instants; a missing
endpoint stays unknown. A zero-equity account remains reportable. Returns with
a nonpositive denominator and drawdowns without a positive high-water mark
stay unknown, and an undefined daily return prevents a daily Sharpe estimate.

Many Fund Assessments but Few Telegram Notifications
--------------------------------------------------------------------------------

An autonomous-fund ``decision_validated`` event records a portfolio assessment
that passed schema, freshness and mandate checks. Holding the same targets is
also an assessment; it does not imply an order, fill or demonstrated investment
skill. Research, critique and shadow calls also use the model-call budget and
must not be counted as portfolio assessments.

Telegram announces the first daily assessment, security additions/removals,
weight changes of at least one percentage point, and changes to entry, stop,
take-profit or take-profit retention levels. Removing a level is announced too;
equivalent numeric formatting or a new rationale alone does not create another
alert. The outbox entry and its deduplication baseline commit together so a
failed database write cannot silently consume an announcement.

``fund-logs.sh`` separates assessment types, orders, fills and delivery states.
Use ``bash scripts/fund-logs.sh decisions YYYY-MM-DD`` to inspect a New York date
in the configured account. It is read-only and never creates a journal. New
events carry an explicit plan ID; legacy plans are linked only by an exact
decision match. Delivery states describe the query time. A missing fill alone
does not establish why a plan did not trade.

Use ``bash scripts/fund-logs.sh economics`` for offline economic comparisons in
that account. It reads the journal and saved daily history without model calls,
downloads or trading changes. New and prior targets start at the same first
exchange close after acceptance and are measured over 1, 5 and 20 sessions.
These are target-price scenarios, not actual fills or decision-day intraday PnL;
they exclude dividends, cash yield, limits, stops and risk scaling. Estimated
trading friction is 10 basis points per side of target turnover. A second value
subtracts only exactly linked manager, correction and fresh-critique costs;
research, triage, approval and other overhead remain outside that scope. Missing
legacy links or usage stay unknown. Overlap counts are shown; positive scenario
outcomes alone do not establish causal model value.

The all-observation mean can describe a different sample from the subset with
complete linked costs. Do not subtract those two means to explain the effect
of fees. ``cost_matched`` shows before-cost return, linked cost as a share of
equity, and after-cost return on the same complete-cost observations.
``cost_matched_non_overlapping`` first excludes incomplete costs, then selects
non-overlapping intervals in acceptance-time order, without selecting by later
performance. Adjacent intervals can share one closing endpoint. Non-overlap
does not establish statistical independence; an empty cohort has unknown means.

The separate account reference freezes beta at each interval's opening closing
book, using only daily bars then completed. It uses the recorded cash policy and
benchmark prices plus recorded corporate actions. Unknown betas or missing
receipts do not become a default beta of one. The reference is frictionless and
does not guarantee official exchange closing prices or historical vendor data
vintages. Beta matching does not imply equal volatility or expected shortfall.
Only comparable adjacent closing intervals are compounded; missing account
intervals are counted and this is not a continuous full-period return.
Fixed benchmarks are retained. Weekly reports include this diagnostic
at their report cutoff, over all available history. Existing decision scorecards
also use the acceptance-time cutoff for beta, including actual early closes.

Economic comparisons and manager scoreboards match benchmark evidence to the
valuation's session and instant. Source-timed observations attached to the
account take precedence; legacy scalar marks supply missing symbols only from
the regular session at or before the valuation. A closing receipt recovered on
the next day keeps its original valuation date and remains unavailable before
its receipt time. Later postmarket prices cannot replace closing marks, and
partial responses do not discard other symbols' valid observations. Manager
scoreboards also exclude intervals with missing intermediate exchange sessions.
Invalid saved daily-history rows are isolated without discarding the remaining
valid bars; the diagnostic reports rejected rows and unreadable files and does
not fill missing prices.

Quiet checkpoints stay within the configured maximum quiet interval; they do
not move the manager's deadline later by another review interval.

Actual Holdings and Target Weights in the Autonomous Fund
--------------------------------------------------------------------------------

Telegram opening and portfolio-change messages and ``fund-logs.sh`` status show
actual holdings in a separate block before the AI rationale. Each security has
its observed share count and average purchase price, alongside account equity,
cash and the original snapshot time. Target weights, target cash percentages
and entry limits describe a plan; they do not establish that an order filled.
The equity amount includes the snapshot age in parentheses, using seconds,
minutes, hours or days. Its age is calculated when status is queried or a notice
is delivered; a delayed notice never resets the original observation time.
Outside regular trading hours, the equity label shows that the market is closed.

Local and paper decisions refresh the account after the model returns and before
queuing the plan notice. A failed refresh is shown as unavailable. Confirmed fills
produce a separate account update with post-fill holdings and recent trades.
If that snapshot remains unavailable for sixty seconds, the monitor first sends
the confirmed trades with an explicit pending-snapshot label, then follows with
the account when it becomes available. Delivery cursors survive monitor restarts.

Average purchase prices come from the local book or broker position response
and cover only the AI capital sleeve. Older snapshots without a recorded cost
show it as unavailable until the next account observation. Quotes and target
prices are never used to reconstruct a missing purchase price. Cash-equivalent
ETF holdings are listed separately and their value is already included in cash.

The same block lists up to six confirmed buy/sell fills observed by the snapshot
time, with quantities, prices and observation times. A recovered cumulative fill
replaces that order's callbacks; other partial fills remain separate. Order
limits are never presented as execution prices. The complete decision rationale
is retained. Longer notices arrive as complete text attachments with holdings
visible in the caption.

In local simulation, the existing broker poll marks the capital sleeve about
every thirty seconds during the open session, and retries after book changes, even while an
agent call is running. Marks include cash, receivables and observed position
values, preserving reserved capital, external holdings and cash-equivalent
classification. Quotes retain source times. Stale open-session quotes or a book
change during the read prevent publication. Both cash-only and invested accounts
save one closing valuation from the final completed regular-session minute or,
when that minute is unavailable, the same session's completed unadjusted daily
close. Daily recovery requires the actual exchange close, including early
closes and daylight saving, and never admits a prior date or a forming bar.
The selected interval, bar start, session close and receipt time remain in the
valuation evidence. This supplies no minute bar for simulated fills. Marks are
reused across polls, restarts and reviews until the next open. If neither source
is available, retain the previous valuation and disclose the missing close.
Verified closing quotes retain their original receipt time across native balance
reads; the bounded cache is keyed by symbol and session close. A later session
needs its own evidence. After-hours recovery cannot apply a queued subscription
or redemption; capital flows still require an open session and fresh valuation.
If the newly fetched daily response first reveals a split or dividend, reconcile
its normalized actions through the existing ledger rules and retry valuation
with the changed book. Old quantities or receivables cannot be frozen against
the new close; late-split and action-revision reconciliation guards still apply.
Status exposes valuation errors without relabeling old equity
as a new observation. Paper accounts continue to use broker observations.

Subscriptions and redemptions recheck STOP, the regular-session calendar and
valuation freshness after the journal write lock is acquired, and again before
the first mutation. A wait can cross the close or expire a snapshot. Such a
request stays pending without changes to cash, units or risk baselines. The
transaction checks a local calendar, with broker status checked beforehand, so
no provider request holds the SQLite write lock. Confirmations use the actual
post-wait clock.

Units have eight decimal places. If rounding would issue/cancel zero units or
misprice the requested amount by at least half a cent, the flow is refused
before cash moves. An operator can submit an amount representable at the
current unit NAV. Ordinary one-cent transfers at a normal NAV remain supported.

An order reservation does not freeze capital authority. After quote I/O and
before the native send, local/Paper orders recheck pending redemptions, the
specific request authorizing a redemption sale, STOP, monitor health, the
regular session and explicit order expiry. A proven non-submission releases
its reservation while retaining attempt history. Once a Paper send starts,
broker acknowledgements and cancellation results remain authoritative.
Local acceptance and atomic fills also check the persisted request: cancellation
stops further redemption sales, and a queued redemption stops old purchases and
cash parking. These cancellations precede corporate-action history I/O, retain
committed partial fills and survive restart. A different request cannot revive
an old sale. Ordinary/protective sales remain eligible, and a pending
subscription alone does not block purchases.

Local simulated purchases and sales each deduct a fee of 0.1 percent of traded
notional. It is already reflected in cash and equity; spread and slippage are
additional execution-price assumptions. Paper fees follow broker evidence and
remain unknown when the broker does not report them.

``bash scripts/fund-logs.sh audit`` reconciles the selected local book from a
read-only database snapshot: opening cash, buys, sales, fees, weighted entry
prices, quantities, order totals, corporate actions and marked NAV arithmetic.
New simulated fills retain the OHLCV and execution assumptions in the same
transaction; reconciliation failure rolls that transaction back. Account reads
and reports withhold disputed balances and trade prices. Research remains
available with an explicit audit status in its context.

Submission quotes retain their own source times. Attribution uses the observed
ask for buys and bid for sales, and never substitutes a limit order price.
Missing arrivals leave timing and slippage unknown; unreported fees remain
unknown too. Historical records without the required inputs produce a partial
audit, not manufactured evidence. Exit codes are 0 for verified, 3 for partial
or not applicable, and 2 for inconsistent or unavailable books. Verification is
internal consistency of recorded evidence, not certification of vendor prices.

High-frequency account reads, submissions and pre/post-fill guards reconcile
the authoritative money, position, order, fill, fee and corporate-action records
on every call (``scope: ledger``). They do not scan historical NAV projections.
The explicit audit, reports and research audits retain full historical checking
(``scope: ledger_and_observations``). Monitor receipts and metrics independently
validate up to 200 recent candidate snapshots; without a valid candidate they
leave the account update pending or omit its metrics. Trade-guard work therefore
does not grow with NAV history, but still grows with actual orders and fills.

That growth is linear: each order and fill record is parsed once, and dividend
entitlements use per-symbol share changes instead of re-reading every fill for
every dividend. The replay runs when a valuation is due, when a minute bar could
fill an order (before and after the fill), when a fill is new to the monitor and
before a submission, not on every five-second poll. NAV marks are streamed one
at a time instead of loaded together; the agent-context audit checks each mark
once per process and then only new marks, while the explicit audit, reports and
the daily review still check every mark.

Reports (rebuilt every five minutes by the runner, and by status commands) and
the manager's strategy follow-up no longer parse and keep every NAV mark and
benchmark body. Every mark contributes its time, equity and closing flag to the
ordering, closing-mark rule, daily curve and observed drawdown; full bodies are
read only for the marks shown, and the follow-up uses each New York day's last
mark. The results are unchanged.

The post-market review returns as soon as it finds that the session is already
reviewed. Otherwise it reads that session's events, the previous session's last
NAV mark, the benchmark observations aligned with those marks, the day's
premarket research and saved review stages, instead of the whole journal. The
weekly report keeps each day's last NAV mark and last valid benchmark price, and
rehearsals read only the events their own run wrote. The monitor's metrics
reconcile the ledger again when cash, fills, orders or corporate actions change,
and otherwise at most once a minute instead of on every ten-second poll.

Notification lookups by type use the ``events_notification_kind`` index, which
the runner creates at its next start, and the daily report reads only the event
kinds it lists. A notification whose stored body is not valid JSON used to make
every monitor poll fail, so no later notice (a STOP alert included) was sent. It
is now recorded as ``suppressed`` with ``last_error`` set to
``malformed_notification_body``, and later notices are delivered.
Reports, review inputs, daily/weekly documents and JSONL exports retain a
``malformed_notification_body`` diagnostic instead of failing on that notice.
``report.json`` counts them in ``malformed_notifications``; JSONL exports retain
the event's time and sequence. The original text remains in SQLite.
Malformed money, fill and research evidence still raises an error.

Delivery selects due messages (and the explicitly selected message, when
requested) before loading at most two bodies. A large outage backlog therefore
does not load every queued body into Python on each poll. Due-time comparisons
preserve UTC offsets and microsecond precision, including in-flight leases.

A notice whose message could not be built (a daily or weekly report, an
approval or an ordinary notice) used to stay at the head of the delivery queue
and retry forever, so every later notice waited behind it. Build failures now
back off only that notice. After three attempts the operator receives a short
notice instead, and the traceback goes to the service log
(``bash scripts/fund-logs.sh errors``). Telegram rate limits and outages still
pause the whole queue.

**The weekly report did not arrive.** It is sent after the post-market review
of the ISO week's last NYSE session, normally Friday after the close.
``bash scripts/fund-logs.sh status`` shows the latest weekly report and its
delivery state, why the last week has none yet, and when the next one is due.
If the week's last session has no review (the runner was down, a STOP lasted
through it, or the review failed), the week is reported from its latest review
once the next week's first session opens. ``bash scripts/fund-logs.sh weekly``
prints this week's report so far without sending it.

**Which experiments are in effect.** ``bash scripts/fund-logs.sh experiments``
lists every experiment version the fund runs: factors, the learning and
construction comparisons, and the prediction statistics. For each it shows the
state (in effect on the paper account, scored in the shadow, rejected or retired)
and the history of state changes with their evidence. A changed configuration
is a new version and never overwrites the old one. The registry only records:
switching and retirement rules are unchanged.

**Whether the AI calls are worth their cost.** Each model call is journaled with
a ``call_id`` and a purpose, and an ``agent_outcome`` is written when its result
is actually used. That is a validated decision (and whether it changed the
target portfolio), saved research (and its ideas), a saved review (and its
predictions), a scored shadow decision, a critique the manager saw, an event
screen used for scheduling or a saved model-identity observation. A screen
that returns after the mandatory manager deadline is explicitly not adopted;
it cannot defer that review. Missing outcome records alone do not prove that
a call was discarded. Older instrumentation and failed outcome writes remain
unknown; the report never matches old calls to outcomes by nearby timestamps. The
weekly report lists, per purpose, the calls, how many were used, how many
changed the portfolio, and the cost per used call and per change. Unpriced calls
or incomplete adoption records make the cost ratios unknown rather than zero.
Status and the weekly report distinguish absent usage, incomplete usage,
invalid usage and missing rates. A response without usage is still an unpriced
call, even when it has no parse error. Whole-history SQL counts and a weekly
report's cutoff can cover different calls; neither rewrites recorded costs.

Weekly and investment-acceptance reports also show attribution coverage: all
application call records, those with IDs, legacy records without IDs, known
costs and unpriced counts. Legacy costs remain visible without inventing their
purpose or adoption. Per-purpose known costs are split into adopted, failed,
explicitly not adopted and unknown adoption, with unpriced counts separate.
Known spend stays visible beside missing prices. Cached and scripted answers
have no provider cost. Invalid historical costs (negative, boolean, non-finite
or unreadable) remain unknown in attribution coverage and value groups.
These are application records and configured estimates,
not a provider bill or its internal request count. Adoption and portfolio
changes do not establish return contribution; skipped calls and reused reviews
do not establish a dollar saving.

The decision timeline keeps the manager's actual portfolio-change result
separate from the event screen's miss result. A screen that escalated cannot
miss, even when the manager then changes the portfolio. Append ``--event N`` to
``bash scripts/fund-logs.sh decisions YYYY-MM-DD`` to print that event's complete
rationale, including text after an account-check preamble. Selection is limited
to the configured account and date and rejects an unmatched sequence. This
read-only command does not infer, send notifications or change the journal.

For new decisions, ``decision_basis`` binds the accepted plan ID to the actual
prior-plan, critique and recent-fill inputs, including after a timing correction.
The selected event view shows the critique as model judgment, with its source
call ID, original time and reuse flag. Older missing links remain explicit;
nearby calls are never substituted. Failure to write this diagnostic does not
block an accepted plan.

**The manager buys and soon reverses after another critique.** The original
plan is already included in each request. Intraday requests also provide the
latest six confirmed fills visible at the account snapshot, with recorded fees
and cumulative-observation deduplication. A timing reassessment refreshes those
fills with the new account cutoff. Critique reuse requires unchanged research,
holdings, triggers, prior plan, recent fills and prompt version; reused evidence
keeps its original time and source call.

The critic compares retaining and changing the plan over the same forward
horizon. The manager must identify new evidence or a newly recognized error,
address the earlier thesis and distinguish paid costs from prospective friction.
Shared model/data overhead is not an incremental benefit of either alternative,
and annual cost coverage alone does not select an exposure direction. These
instructions do not impose a holding period, require new news before correcting
an error, restrict sales or prevent research. They improve the information and
review contract; fewer reversals or better returns require later observed
decisions and are not established by deterministic handoff tests.

**Reading the acceptance reports.** ``acceptance`` writes two independent
reports. ``acceptance.md`` says whether the system runs reliably.
``investment_acceptance.md`` says whether there is an excess return after
costs: at least 60 sessions, beta-adjusted alpha against SPY, beating the
predefined risk-matched benchmark before and after model and data costs, a
deflated Sharpe ratio of at least 0.90, and a positive result in both halves of
the record. Its best verdict is ``preliminary_edge``. Passing the operational
report does not pass the investment report.

**When what is in effect gets worse.** After each review the fund checks fixed
thresholds. The learning and construction comparisons keep their full-record
switch rule. They also roll back to the other arm when the last 20 paired
sessions turn against the arm that trades with ``|t| >= 2``. When the mean
turnover of the last five sessions is above the 95th percentile of at least 40
earlier sessions, the manager's minimum review interval doubles. Protective
exits are unaffected, and the interval returns to normal at the earlier
median. Short-term profit or loss alone never triggers anything.

**Why a purchase was declined as not worth its cost.** The manager always sees the
round-trip cost of a trade (fee, slippage and the quoted spread). Once the
research ideas' scorecard verdict is ``informative``, an add of a symbol with an
idea for that day is held at the current weight when the idea's calibrated
edge does not cover that cost. The journal records it as ``trade_value_held``.
Reductions, sells and protective exits are never limited.

**A symbol the AI never buys.** The fund buys whole shares. When one share at
the last close is worth more than the symbol's ceiling, no target can buy it.
The ceiling is the smallest of the single-position limit and the caps of its
groups, such as 5% for a leveraged ETF. The AI's context lists such symbols, and
Telegram says when the list changes. Raise the authorized capital or the cap to
trade them. The list is recomputed at every pre-market research and decision
from the current equity. A symbol whose share weight is within 10% of its
ceiling is re-checked with a live quote in local and paper mode, so a move
during the session counts at the next decision. The context also lists
affordable symbols of which at most four shares fit, because a target is
rounded down to whole shares.

**Biases the review accounts for.** Seven things could make the record look better
than the skill behind it.

- Sector bets: decision excess beyond beta splits exactly into
  within-sector selection, the sector bet and holdings without a sector.
  Stock-cross-section factor tests also report a within-sector IC, and the AI
  sees ``mostly_sector_bet``.
- Survivorship: historical factor tests use today's names only; the AI's
  context and the reports say so. A factor that passes them stays provisional
  (no favored names for the AI) until its own live record agrees. From its
  first day the fund also keeps a daily point-in-time universe, so stocks that
  later leave (acquired, delisted) stay in factor tests for their membership
  period. Records before the snapshot began remain biased.
- Winner selection: today's large caps were picked because they grew. With
  ``factor_lab.index_membership`` (default on) a stock counts in historical
  factor cross-sections only from the date it joined the S&P 500, taken from
  the public constituents list.
- Optimistic simulated fills: investment acceptance requires the edge to stay
  positive at twice the realized trading costs, and scorecards show 2x and 3x.
- Fading results: decision and research-idea scorecards are grouped by market
  regime (SPY trend and volatility, from past closes only) and compare the most
  recent third of the record with the rest. A weaker recent record is flagged
  as evidence, never used to change trading by itself.
- Reversals: the manager sees how often it reversed a target within three
  sessions without a stop or take-profit, and what that cost. The daily and
  weekly reports show it too.
- Day trades: ``fund-logs.sh`` shows the same-day round trips of the last five
  sessions, the US pattern day trader count. Paper accounts are unaffected.

**"Invalid structured agent answer: manager; schema_validation: $ (model_type)".**
The manager's answer was not one JSON object. Before 2026-11-10 a prose answer
whose ``[1]`` was read as a list gave exactly this; LumiBot now only takes a
value of the schema's root type from prose. The decision request already carries the
schema (an enforced JSON schema for ``deepseek/deepseek-flash`` and
``deepseek/deepseek-v4-pro``), but providers do not always enforce it. The
fund now gives such an answer one correction call with the error locations
and types, within the STOP, call and cost limits; only a second failure skips
the round and backs off. Other models only get the schema as instructions,
and the runner says so once at startup.

**Intraday model cost is high and most manager calls change nothing.** Busy news
days used to wake the manager (the expensive model) at every review floor: in
one account, 1,031 news triggers in five days and 19 manager calls in a day
with no portfolio change. With ``FUND_TRIAGE=auto`` (the default) news and
price-move triggers are first screened by the fast research model, which only
answers whether the manager should reassess now. It takes effect at once;
every fifth dismissal still runs the manager (blind to the verdict) as an
audit, and when more than 15% of audited dismissals changed the portfolio it
returns to the shadow, where the manager always runs. It takes effect again
when, over the last 40 dismissals, the 95% upper bound of that rate is at most
15%. Set ``triage.start`` to ``shadow`` to begin in the shadow. Market stress,
the AI's own price alerts and stop or take-profit exits always reach the
manager directly, and the manager still reviews at least every two hours.
Batching and dismissal schedules are capped at that review deadline; if the
screening call crosses it, the manager runs with the original triggers intact.
Risk pauses and invalid or future scheduling state bypass the soft-event
screen. STOP arriving during screening or context preparation prevents a
follow-up manager call. Status uses the actual scoring window.
Calibration is bound to a ``triage_profile`` containing the resolved screening
model, instructions/schema, manager decision version and screening policy.
Matching restarts retain samples and state; changed profiles archive the old
samples and start with the configured ``mode``/``start``. Daily call and cost
budgets are not reset. Legacy samples without source identity are archived;
auto mode preserves its known control state rather than re-enabling a rolled
back screen. Results carry ``profile_id`` and cannot be scored into another
profile. Pricing and output-protocol diagnostics also include the screening
model; an unpriced second model cannot inherit a legacy single-model rate.

**Cash parking orders are canceled or reported as outside the AI universe.**
The configured cash-equivalent sweep is deliberately excluded from AI targets.
Local fill-time checks treat it as cash, as planning does: ordinary security
position/exposure limits do not cap parking, and remaining sweep holdings do
not count as investment exposure. Actual cash, reserved capital, fees, the cash
buffer, source data and order controls still apply. A kind label on an ordinary
stock order does not grant this classification. Unfilled parking orders also
obey the ordinary stale-order cancellation interval after allocation completes.
``local_risk_canceled.reason`` distinguishes position/exposure limits, a cash
buffer breach, insufficient cash and a drawdown pause. Older canceled orders
without a recorded reason cannot be assigned one from status alone.
Fill-time capital-sleeve checks subtract operator-owned holdings from equity
and risk exposure using the same attribution rules as account snapshots.
Those holdings cannot expand the sleeve's position allowance or inflate the
parking cash-buffer requirement. Frozen operator lots remain untradeable by
the sleeve, even when the symbol is also in its configured universe. Such
pending orders are canceled with ``external_holding``; other unadmitted symbols
are canceled with ``ineligible_asset``.

Postmarket agents receive a citable ``cash_management`` policy snapshot, so
they can distinguish AI eligibility from system cash parking. A retried review
reuses the saved policy; legacy snapshots without it remain missing rather
than being backfilled with the current configuration. This input improvement
does not rewrite already completed reviews or guarantee an AI's interpretation.

**Interpreting autonomous fund diagnostics.** Model identity probes run after
the exchange close, including on half days. They skip observe-only mode and
recheck STOP and cost limits before each call. The decision stability check
repeats the accepted task and strategy context, including any correction.
Account state, the runtime clock and tool results can change, so its distance
measures observed variation across those calls. The down-day scorecard reports
all recorded protective triggers on reviewed down days and separately counts
the ones with enough price evidence for next-session valuation.

**Wrong daily bars from a free source (local mode).** Set Alpaca paper keys even
in local mode: the fund then compares Yahoo's completed daily closes with
Alpaca's once a day. A session that differs by more than 2% is dropped (never
replaced or interpolated); a symbol that disagrees on many sessions or on its
latest close is withheld for the day, and one notice after the close lists
them. Alpaca's free feed is IEX only, so only gross errors are caught.

**A held stock was acquired or delisted (local book).** Its provider stops
serving daily bars, so the account cannot be valued and valuation and new
trading pause rather than use a guessed price. After each close the fund checks
every holding and, when no new completed bar arrived for more than three NYSE
sessions, says so once a day. Find out the cash actually paid per share (0 for a
worthless delisting), stop the fund, then run:

.. code-block:: bash

   bash scripts/run_autonomous_fund.sh settle-position --run-dir <run-dir> --symbol <SYMBOL> --cash-per-share <amount> --reason "<what happened>"

The command refuses while the runner is running, while an order for the symbol
is open, and when the book does not pass the strict ledger replay; the
settlement and its evidence commit together or not at all. Alpaca paper
accounts settle such holdings at the broker.

Reads of event kinds that contain no NAV marks (scorecards, shadow decisions)
no longer audit every NAV mark, and the daily attribution reads the bodies of
only the two marks it compares. A full NAV-mark check converts each value once
(100,000 marks: about 1.9 s to 1.0 s, same findings).

Account reads (LumiBot makes dozens each iteration) used to replay every fill
since the account opened. SQLite triggers on the audited tables and event kinds
now bump ``ledger_version`` on any insert, update or delete, from any connection
or tool. A read reuses the last replay while the version is unchanged, so 43
replays for a startup and one iteration became 5. The verdict is the same: a
change made through another connection is still caught on the next read,
failures are never reused, and a missing trigger disables reuse. Uncommitted
writes inside an order or fill transaction are still checked directly in that
transaction.

NAV marks store the audit's verdict (status, scope, counts, missing evidence,
error and warning codes), not the full replayed book, which ``fund-logs.sh
audit`` reproduces at any time. Before pruning snapshots, the daily backup checks
the source journal's SQLite integrity and rejects an existing destination. A
full copy must leave 512 MB free, above the monitor's 200 MB low-disk STOP. It may
delete snapshots its rotation would delete anyway, but retains at least the
newest completed copy until its replacement succeeds. When that cannot free
enough space, it skips without deleting any snapshot and the monitor sends a
``backup_skipped`` notice. This also applies to ``--keep 1``: the last copy is
never spent to make room for its replacement. SQLite connections close on
success and failure; failed copies are removed, and directories marked
``.incomplete`` after a hard interruption are not counted as completed backups.

After the daily backup, the fund removes data that nothing reads any more:
ordinary headlines after 30 days, material ones after 180, LumiBot's per-call
agent traces after 30 days, and monthly ``events-YYYY-MM.jsonl.gz`` export
archives after 12 months. The ledger, orders, fills, decisions, research,
reviews, scorecards, factor tests, notices and NAV marks are never removed.
Preview with ``bash scripts/fund-logs.sh prune``, and delete with ``--apply``.

New optional-fund calls also retain compressed, deduplicated application inputs
and returned answers in the account's SQLite journal, independently of trace
retention. They include task/system prompts, context, output schema, call
options, the existing prompt version, application source and selected package
versions. Existing SQLite backups include these objects. This needs no new
service, data subscription or inference. A 16 MiB uncompressed per-object bound
limits capture; a failure records only its exception type and does not change
model budgets, retries or trading permissions. Legacy inputs stay missing.

``bash scripts/fund-logs.sh evidence`` checks hashes, event references and
coverage without displaying private input text. ``evidence <call-id>`` displays
one exact private request and answer from the selected account. Equivalently,
use ``python -m scripts.autonomous_fund.run evidence --run-dir <account-directory>``
with optional ``--call-id <call-id>``. Integrity failures exit 2; verified,
partial and unavailable coverage exit 0 with gaps disclosed. Raw answers are
not proof of validation or adoption. This is serialized application-boundary
evidence, not a complete native-memory/tool-response/provider replay. Source
files are captured from disk at first capture in the process, not certified as
identical to every loaded module. Hashes detect corruption and reference
mismatches, not an editor rewriting every reference. Backups on the same host
still do not protect against loss of that host's disk.

Missing legacy analytics evidence does not block valid trades. An invalid
historical NAV projection is reported as a partial-audit warning and excluded
from reports, notifications and review inputs; cash, shares, fills and fees
remain independently checked. A real ledger mismatch still blocks execution
and requires reconciliation. Temporary unavailable, stale or invalid source
observations keep orders pending for automatic retry without a permanent stop.
Their limits, expiry and freshness checks continue to apply. A missing symbol's
execution data does not starve another symbol's valid sale; purchases still
require a reliable portfolio valuation, and unavailable corporate-action data
must be checked before execution can continue.

Read-only price alerts may observe symbols outside the tradable universe, such
as indices without trading volume, when the configured source supplies a fresh
positive price and its source timestamp. Missing, stale or future quotes are
disclosed and retried. An observation alert never authorizes an order or target
outside the tradable universe.

During an open session, a minute-history cache is refreshed at each new minute
even when its normal 15-second receipt TTL has not elapsed. Otherwise a quote
aged 59.7 seconds can be reused at age 60.5 seconds and incorrectly miss newly
available source evidence. Lagging source responses use a bounded five-second
cache retry; the original source timestamp and freshness limit remain intact.
Rejected observations include their price kind so completed-bar proxies can be
distinguished from live trades. Telegram messages identify an unavailable price
alert as a skipped alert check with automatic retry.

The local Yahoo stream retains the 60 most recently requested symbols. At
capacity it unsubscribes the least recently requested symbol and admits the
new one, rather than silently ignoring all later symbols. Diagnostics include
``capacity``, ``requested`` and ``subscription_evictions``. A failed minute-history
request still subscribes the requested symbol; an independently fresh
regular-session streamed trade can supply a quote while history is unavailable.
It supplies no invented minute volume, and simulated fills still require new
completed minute bars. If a quote ages during the watch loop, the alert reads
new source evidence once before reporting failure. Quote validation samples
the clock after source I/O so a legitimate new quote does not look future-dated.
Share-class aliases share one wire subscription; retiring one spelling does
not unsubscribe another spelling that is still being watched.
If a symbol has no fresh stream tick, the adapter next checks the same chart
response's ``regularMarketPrice`` and ``regularMarketTime`` pair. It uses this
only when both are valid, newer than the completed bar and fresh within the
current regular session. This is labeled ``chart_last_trade_with_spread_proxy``
and counted as ``chart_quote``; a forming minute bar is never used instead.
Absent, stale, future, naive or nonpositive source observations still fall back
to completed bars and remain subject to the normal freshness checks. Cached
metadata retains its original source time. Matching still requires new bars.

A valid chart last-trade pair is retained before OHLCV validation, so an empty
history, forming-only response, invalid bars or history backoff cannot discard
independent fresh quote evidence. At most 60 recently observed symbols retain
such evidence; symbols never share a price, older responses do not overwrite
newer valid trades, and each read rechecks source age and the regular session.
Stream priority remains intact. These quotes supply no fabricated volume and
cannot replace the completed bars required for simulated fills.

An invalid completed OHLCV row is omitted as a gap, preserving other real
daily/minute observations. No interpolation, forward fill or replacement
volume is added. If every completed row is invalid, that series remains
unavailable. Invalid split/dividend values still block accounting.
``market_data.discarded_rows`` and ``last_rejected_bar`` record the symbol,
interval, timestamp and affected fields. Completion is checked against the
clock after the download, including requests that cross a minute boundary.

``market_data.quotes_by_symbol`` retains at most 60 recently requested symbols
with the selected price kind, source time, check time and age, plus separate
stream/chart source times. Ages refer to the saved ``checked_at``, not the
time the status file is read. A missing stream timestamp identifies an
unobserved subscription; aggregate subscription counts cannot identify it.
``price_observation_rejected`` retains the rejected source clock, age, limit
and reason alongside each deduplicated alert notice. The errors log command
includes these INFO-level failures. A fallback cannot supply fresh evidence
when every real source is stale; freshness checks remain enforced.

A qualified current-minute stream trade is newer than every possible completed
minute bar, so it returns before history I/O. Older-minute trades still follow
the history comparison path. No minute volume or matching bar is manufactured;
simulated fills still read actual post-submission completed bars independently.

Active price alerts persist failed-check counts and first/latest failure times
in account-local ``price_alert_health`` metadata, independently of notice dedupe.
The next valid observation atomically queues ``price_alert_recovered`` and clears
that outage, including after a restart. A valid mark need not cross the alert
level to establish recovery. Removed alerts are pruned without claiming recovery.
``fund-logs.sh status`` shows pending evidence for active watches; ``errors``
includes recovery notices. These are discrete observations, not continuous
outage/coverage measurements, and no missed price crossing is reconstructed.
Checks made before this tracking existed are not retrospectively counted.
Each rejected local quote also retains its own stream-enabled flag and
stream/chart source clocks, so another consumer's later quote cannot replace
that rejection's evidence. Raw provider payloads are not logged.

Use ``bash scripts/fund-logs.sh quotes [SYMBOL]`` for an offline account-scoped
diagnostic. Without a symbol it lists saved observations; an exact symbol
selects only that symbol. It reads the configured mode/account snapshot and
active alert failures, with source/check timestamps, ages at that check, and
safe request-failure counts. It does not download data or invoke models.
Missing, corrupt, old, future or mode-mismatched snapshots remain explicit;
paper diagnostics never borrow a local snapshot. No record is not proof of
healthy prices. A recent heartbeat does not establish quote freshness, and
querying diagnostics does not clear an alert failure or reconstruct crossings.
Telegram status also includes pending active-alert evidence, independently of
whether its weekly-status convenience section succeeds.

Local/paper watches also append small, account-local ``fund_quote_checks`` rows
in one transaction per watch iteration, with separate portfolio/target and price
alert purposes. ``quotes [SYMBOL]`` includes the current New York date's recorded
check denominator, accepted/rejected counts, rejection causes, first successes
after a rejection and source-age buckets/mean/maximum. Negative source ages and
missing clocks stay separate from nonnegative age statistics. A reused quote
can serve another check; these are not independent provider requests or full
feed availability. Trading quote/liquidity restrictions are labeled separately
from known market-data failures. Recovery counts describe successive recorded
checks, not continuous outages, alert-state transitions or missed crossings.
They belong to the first-success date even when the prior rejection was earlier.

Daily and weekly documents select the exact review cutoff and date range;
later checks cannot rewrite that window. Legacy/unobserved checks remain
unknown, and a crash or storage failure may leave gaps. Statistics failures
emit ``quote_quality_error`` without blocking protective exits, altering watch
validation or suppressing the original exception. Historical/Mock watches do
not produce these records. Existing SQLite backups and retention preserve the
rows, which contain no raw quote payloads or provider exception text.

A missing final completed minute is disclosed as
``closing_valuation_pending`` and retried by background valuation. Read-only
review can proceed without claiming a closing NAV. Other closing account or
valuation failures retain their risk reason in ``report_error`` and a safe
location trace in ``operation_error``. Provider exception messages, source
text, local variables and private paths are excluded from that trace.

``market_data.source_failures`` retains up to 60 failing history series with
symbol, interval, failure-attempt cause, failure time, consecutive failures
and remaining retry delay. A cooling-down check retains that attempt's cause;
only success of the same series clears it. SDK exception payloads are not
copied into diagnostics.

The local application's failed background balance refresh retries after 15
seconds, rather than on every one-second cached-cash access. Both failure
and a deferred retry report failure and leave the last successful refresh
timestamp unchanged. Explicit refreshes, ``get_cash()`` and direct AI/order
snapshots still attempt a strict read immediately; the source's own backoff
remains effective. Retained native display values do not become fresh
account evidence.

Account snapshot/event times are sampled after the account and quote reads,
rather than before I/O. Locally held marks are revalidated before publication
after optional benchmark work. Historical reports cannot see a newly returned
valuation before its receipt time; genuine frozen closing NAVs keep their
separate session-close and observed-at timestamps.

Benchmark observations are independent per symbol, retain original source
timestamps and are checked again after later reads. Missing or stale benchmarks
do not discard other valid benchmarks or an otherwise valid cash account.
Held benchmark assets reuse their existing valid valuation mark, avoiding a
second request. ``benchmark_observations`` preserves accepted price/time pairs;
historical/scripted modes retain native historical-price semantics. Existing
snapshot timestamps are not rewritten or inferred retroactively.

Local buy-time checks also require original source timestamps for each held
position's risk mark, rather than Quote's potentially synthesized receipt
time. Bid/ask must be finite, positive and uncrossed; capacity uses the higher
of ask and the observed price. Original balance marks and subsequent holding
quotes are revalidated after other symbols' I/O and ledger checks, before a
bar is consumed or cash/shares change. A failed observation remains retryable
on that bar. Sales still require only their own valid execution data.

Local order acceptance rechecks STOP, matching enablement, the regular session,
the declared expiry and any required monitor after acquiring the write lock
and verifying the ledger. Creation time is the actual acceptance time; a bar
that started during an earlier wait cannot become post-submission evidence.
Known local non-submissions release their pending intent and retain an
audited ``local_order_rejected`` attempt. Ambiguous external outcomes remain
locked. Acceptance, fills and cancellations publish native callbacks in the
same polling order. STOP/expiry cancellations run before corporate-action
history reads, so an unavailable source cannot postpone them.

Live notification delivery starts retry delays after a failed build or HTTP
response, and claims each message using the current clock. Before HTTP it
rechecks recipient binding, shared backoff and lease ownership, then renews
its claim. Late build/HTTP results cannot overwrite a newer sender's state,
and a shared retry deadline cannot be shortened by another failure. Explicit
simulation clocks remain deterministic. These checks preserve local delivery
state; uncertain network outcomes still cannot guarantee remote exactly-once
delivery.

Future-dated notifications are persisted with their own due time so they do
not block later ready notices or current watchdog alerts. They cannot justify
deduplicating a current observation. Invalid or naive notice timestamps are
suppressed as ``malformed_notification_timestamp`` while the original event
is retained; an unusable timestamp in old deduplication history cannot block
a valid new notice. Financial ledger evidence keeps its strict validation.

An intraday manager's stale, future or expired decision in local or paper mode
can use the same single correction opportunity as a malformed answer. The
manager receives a refreshed account, risk and data-desk context and must
reassess and return a complete new decision. ``decision_time_rejected`` and
``decision_time_retry`` retain the original decision times, request time,
validation time, age and rejection reason. STOP and call/cost budgets are
checked again after preparation. A second failure backs off without a third
call; format and timing failures share this limit. The five-minute acceptance
limit and every execution check remain enforced. Python never retimestamps an
old answer or its evidence. Historical timing failures and other risk failures
do not trigger this timing correction.

Invalid Structured Answers in the Autonomous Fund
--------------------------------------------------------------------------------

Evidence uses ``estimate_reason`` for a publication-date explanation. The optional
fund also accepts ``estimated_note`` as an explicit input alias and saves only
the canonical field. An empty or identical duplicate is accepted; a nonempty
alias can supply an empty canonical explanation. Conflicting nonempty
explanations, incorrect types, missing required fields and other unknown fields
are still rejected. Historical timestamp and estimate-explanation checks remain
in force. This compatibility requires no additional model request, and the
original answer remains in the native trace.
Critiques also accept ``missing_evidence_placeholder: null`` as an empty rendering
placeholder. Non-null placeholders and other unknown fields remain invalid;
the actual ``missing_evidence`` list is preserved.

An ``Invalid structured agent answer`` notice identifies the agent, the phase
when supplied, and up to five field positions and error codes. For example,
``researcher/postmarket_review; schema_validation: evidence[0].observed_at (string_type)``
identifies a timestamp supplied with the wrong type. Unknown field names appear
as ``<extra>``; input values and provider exception messages are not copied into
the notice. The journal's ``agent.answer_diagnostic`` retains the error category,
count and safe positions; consult the original ``parse_error`` and the call's
``trace=`` file for the complete answer.

For postmarket research, ``summary (missing); evidence (missing)`` means the
extracted JSON lacks both required fields. Each research attempt explicitly
requests the full ``Research`` schema. Direct DeepSeek Flash and Pro calls now
also send that schema to the Responses API as native JSON Schema output,
including across research-tool continuations. Schema-invalid answers are still
rejected locally; provider failures and truncation remain possible.
The sender requires every declared property in a separate strict wire schema,
including defaulted fields and nested definitions. The original caller schema,
field types, defaults and local validation remain intact; the model must emit
those fields explicitly.
A schema mismatch or invalid JSON allows
one additional researcher correction per review attempt, with the original
answer, safe field diagnostics and the same input snapshot. Both calls retain
their answers and usage in the journal; ``postmarket_research_retry`` records
the correction. STOP and the existing call/cost budgets apply, including room
for the subsequent manager call. No fields or evidence are fabricated by the
application. A still-invalid correction is rejected; successful research is
saved and reused. Transport failures, empty answers and historical evidence
cutoff violations do not trigger this format correction. Live undated sources
remain allowed, and historical publication-date checks remain enforced.

For a standalone checkout diagnostic, run
``.venv/bin/python scripts/check_deepseek_schema.py --offline``. To explicitly
check the real Flash and Pro endpoints, use ``--real-models`` with
``DEEPSEEK_API_KEY`` supplied in the process environment or an explicitly
selected ``--env-file "<your-secret-file>"``. Real mode generates provider usage
and makes at most eight requests, with no automatic retry. A model may read the
synthetic tool up to three times before answering; a fourth request for it fails
as ``too_many_tool_rounds``. It checks the actual
``Research`` schema, a synthetic read-only tool round trip and reported usage;
JSON reports are saved under ``artifacts/verification/deepseek-schema-smoke/``.
Add ``--schemas all`` to also check the ``Decision``, ``Critique`` and
``PostmarketReview`` schemas and a constraint probe that asks for values the
schema forbids (an empty ``summary``, a weight above 1, a zero price); any such
value returned is reported as ``not_enforced`` and fails the check, and a
``null`` price, which the schema allows, as ``held_null`` (at most 18 requests
for both models).
It does not connect to a broker or establish that a running service has reloaded
the current code. An offline pass does not establish real provider support.
If both models return HTTP 400 on the first request, the provider has rejected
the request before the tool round trip; a status alone cannot identify the
cause. Updated diagnostics include ``provider_problem``, known provider codes,
API parameter names and allowlisted ``diagnostic_terms``. They distinguish
schema-format, keyword, required-field and tool/reasoning-combination rejections
without copying provider messages, input values, secrets or private endpoints.
An unknown category remains ``provider_request_rejected``. These are diagnostic
labels, not automatic fallback rules. Real-provider qualification remains
blocked until the actual request passes; local mock success cannot replace it.
A rejection saying ``required`` must include all ``properties`` identifies the
strict object rule. Earlier requests sent Pydantic's defaulted fields as
optional. Update both the adapter and script to use the strict wire-schema
adaptation for Flash and Pro, then rerun the same real-provider command. The
report distinguishes the original schema fingerprint from the wire schema and
validates the final JSON against both contracts.

Runtime failures such as ``runtime_error: TimeoutError`` are distinguished from
schema mismatches, ``missing_answer`` and ``invalid_json``. A rejected postmarket
answer does not activate a new strategy memo; the last valid memo stays in use.
``fund-logs.sh errors`` retains the following field-location and indented detail
lines in the same error record, including when other processes log between them.
It also retains custom exception summaries and chained tracebacks when the
exception name does not end in ``Error`` or ``Exception``, including exceptions
with no message. Such summaries are recognized only inside the same process's
active traceback, not as ordinary output or validation-detail lines.

The fund's roles explicitly request a complete schema JSON object. A decision's
entire explanation belongs in its single ``rationale`` string, not extra
continuation fields. Unknown fields remain invalid; the existing budgeted
manager and postmarket-research correction paths still apply. A format warning
alone does not establish that the round failed: inspect subsequent correction,
accepted-decision or backoff events.

In local/paper mode, a critic JSON/schema failure also permits one correction,
within STOP, daily call/cost limits and the reserved postmarket budget, while
leaving room for the manager's next call. Both attempts retain their exact
call IDs, usage and explicit validity outcomes; only a valid corrected answer
can enter the critique cache. Provider failures, missing answers and historical
runs keep their existing single-attempt behavior. A failed optional critique
still leaves the manager available. This does not guarantee model compliance.

The autonomous fund keeps the native ``Strategy.notify`` and ``notify_user``
contracts. Its ``fund_outbox`` provider queues AI notices durably for the selected
account's monitor, while native disabled/backtest behavior and other configured
providers remain effective. ``payload.status=queued`` acknowledges persistence;
Telegram delivery is confirmed separately. These notices use ``agent_notice``
and cannot be presented as operational approval or health events. Internal
application alerts use a separate ``_notify_fund`` method.

For DuckDB research, the fund's role prompts explain inspecting actual tables
and columns, keeping CTE aliases within one statement, and separating lagged
returns, rolling statistics and aggregates into successive CTEs. Context field
names do not establish table columns. For approximate research calculations,
cast operands to ``DOUBLE`` before multiplication or powers; casting an already
overflowing decimal product is too late. Exact decimal calculations need
operand precision and scale that accommodate intermediate results, within
DuckDB's maximum scale of 38. Widening precision alone cannot fix excess scale.
See `DuckDB numeric types <https://duckdb.org/docs/current/sql/data_types/numeric>`_.
Ledger values and source evidence must not be altered to hide a query error.
Query failures remain visible so the agent can correct and verify its work;
these instructions neither rewrite SQL nor guarantee error-free model queries.
The prompt supplies an operand-casting example for approximate notional
research. After adapting names to the actual table schema, use:

.. code-block:: sql

   SELECT CAST(price AS DOUBLE) * CAST(quantity AS DOUBLE) AS notional
   FROM holdings;

Casting only ``price * quantity`` after multiplication does not prevent an
intermediate decimal overflow. After a missing-column error, inspect the table
or CTE's actual output instead of guessing context names. Each call needs a
complete SQL statement, including its CTE definitions.

Forward-return queries calculate aligned symbol/benchmark endpoints before
filtering a signal. They report raw rows and non-NULL outcome counts separately
for every horizon; averages and win rates use the same valid outcomes. Missing
future endpoints remain NULL, never zero returns or losses. Overlapping and
small mined samples remain exploratory. A tested aggregate example accompanies
the role prompts. Macro comparisons use same-date FRED yields, long minus
short in percentage points; negative means inverted. Exact release dates,
times and results need the issuing agency, since a missing desk headline does
not establish that the release is unpublished.

The optional fund's Alpaca reference and fallback histories request
``adjustment=split,spin-off``, excluding cash dividends. Old split-only
comparison caches are recomputed without deleting shared caches; matching
previous rejections survive a reference outage. Corporate-action checks span
the requested and cached history instead of only 120 days. Spin-off parent,
child and distribution ratios are evidence, not price factors or automatic
share/cost-basis postings. Ambiguous share bases remain unconfirmed; unrelated
split contradictions and genuine price mismatches keep their existing checks.
Reported cross-source historical price differences are not daily returns.

Autonomous Fund Reports Missing the Latest Review or Scorecard
--------------------------------------------------------------------------

A review records its completion clock before writing its journal event. Weekly
reports include the exact selected review by sequence even when that write is
slightly later; other future reviews, prices and costs remain excluded. New
reviews atomically retain the scorecard actually supplied to their saved
research stage, including after a manager retry. Legacy reports can recover a
card from saved research with matching session, input time, model and answer
without rewriting the ledger. Legacy session projections remain readable;
unavailable or absent results do not imply zero samples.

Daily returns retain their frozen input window. A separate account observation
shows the latest recorded values through the report cutoff and distinguishes
ordinary marks from closing valuations. Cash already contains system cash
equivalents: free cash subtracts them once. Code-calculated allocations are
separate from unverified model prose. Repetitive news details are bounded to
the latest 20, while protective triggers and the full journal are retained.

Weekly excess returns also show known model costs divided by starting equity.
Unknown model costs make this an upper bound; data costs belong to the complete
system-cost assessment. Trading fees and slippage already affect account NAV
and must not be deducted twice. Simulation assumptions are shown only when
recorded with fills, without inventing historical settings. Such friction can
put simulated fills outside a raw minute bar's range.

The bounded review context prioritizes accepted plans and execution evidence
over routine observations and reports omitted counts. Review instructions ask
for prospective validation periods, benchmarks, costs and invalidation rules.
A few examples or overlapping predictions do not establish independent
evidence of an investment edge; prompts alone cannot enforce sound inference.

Evidence References Rejected in an Autonomous Fund Postmarket Review
--------------------------------------------------------------------------------

The optional autonomous fund supplies an ``allowed_evidence_refs`` list for each
postmarket review. The manager preferentially copies short IDs from
``evidence_catalog``: ``event:<sequence>``, ``research:n`` for a saved research
item, or ``context:<field>`` for input actually present in that snapshot.
Original source text and timestamps stay intact; exact original source names
remain valid. The catalog is retained with the review and strategy memory.
IDs belong to that review's snapshot and cannot be carried into another review
as new evidence.

A reference mismatch allows at most one additional manager correction per
attempt, using the same saved research and allowed references. It counts against
the existing call and cost budgets and respects STOP. The application never
fuzzy-matches a citation or automatically promotes model prose to evidence.
If the correction still fails, the review is rejected and the last valid
strategy memo stays in effect. Successful research is reused across retries.

The ``postmarket_review_error`` notice identifies the review date and the invalid
positions using zero-based indices. Look up those positions in the manager call's
``trace=`` file to compare the submitted references with the supplied list.
"Outside the supplied review" refers to a citation missing from that list, not
to an investment-universe or position-limit violation. The error positions alone
do not reveal which references the model submitted.

Choosing an Intraday Order Duration
--------------------------------------------------------------------------------

``Strategy.create_order()`` defaults to ``time_in_force="gtc"``, while constructing
``Order`` directly defaults to ``"day"``. Specify the intended duration explicitly.
For example, an intraday market short can use:

.. code-block:: python

   hedge = self.create_order(
       Asset("SPY"), 100, Order.OrderSide.SELL_SHORT,
       order_type=Order.OrderType.MARKET, time_in_force="day",
   )
   self.submit_order(hedge)

Schwab may reject GTC short-sale orders for hard-to-borrow securities. DAY does
not guarantee acceptance or a fill; other borrowing, account and market constraints
still apply. Inspect the broker's rejection reason before retrying. Preserve
intentional GTC orders instead of changing every order's duration globally.

Restoring Asset Variables After a Restart
--------------------------------------------------------------------------------

Keep instruments as ``Asset`` objects in ``self.vars``. New scheduled-file and
database backups preserve their type, including assets nested in lists,
dictionaries, or tuples. Restored objects can be passed directly to
``get_position()`` and ``add_ohlc()``.
Sets still restore as lists, with their asset values preserved.

Older backups may contain an untagged asset dictionary. Restoration reconstructs
it only when the same variable path already contains an ``Asset`` initialized by
the strategy. Saved contract details remain authoritative; initialization supplies
the expected type, not replacement quantities, strikes, or signals. Scheduled-file
restoration preserves the original bytes in a permission-restricted sibling
``.legacy-<sha256>.bak`` before applying the recovered variables.
Paths without an initialized instrument remain ordinary dictionaries because
the same shape can be strategy metadata. Migrate those explicitly with
``Asset.from_dict(value)`` after verifying the instrument path.
Do not apply this conversion to arbitrary dictionaries or discard other saved
strategy state. Backups written by the updated runtime should be restored by
the updated runtime; older versions do not understand the asset type tag.
For hosted scheduled bots, preserve the original remote state independently
before migration: a local sibling backup is not a durable cloud backup unless
the hosting system explicitly retains it.

FRED Data Errors
--------------------------------------------------------------------------------

Use official FRED series identifiers rather than market symbols. A failed series
can appear under ``errors`` even when other series succeed in the same snapshot.
HTTP and transport errors report a status or exception type without exposing the
API key in the request URL. Correct an invalid series; do not replace working
credentials because one series returns HTTP 400.

Critical Mistakes (Will Break Your Strategy)
--------------------------------------------

Using an Unfinished Crypto Candle as Historical Evidence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

CCXT candles are timestamped at their opening time. At 09:00, the eventual
close of the 09:00–09:01 candle is not known. Default CCXT backtest history
and last-price queries therefore use completed candles only. This applies to
minute, hour and day bars, including AI research through those methods.

Do not add a negative history ``timeshift`` to make that future closing price
available to your strategy. The backtesting broker uses an execution-only
offset to simulate orders against the current candle; this does not make the
whole candle valid evidence for the preceding decision. When no candle has
closed yet, missing history is expected, not permission to invent a price.

Using datetime.now() Instead of self.get_datetime()
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Backtest results will be completely wrong. Your strategy will think it's the current date instead of the simulated date.

.. code-block:: python

    # WRONG
    current_time = datetime.now()
    target_date = datetime.today() + timedelta(days=30)

    # CORRECT
    current_time = self.get_datetime()
    target_date = self.get_datetime() + timedelta(days=30)

Adding 'from __future__ import annotations'
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Causes immediate crash during backtesting.

.. code-block:: python

    # NEVER DO THIS - WILL CRASH YOUR STRATEGY
    from __future__ import annotations

Simply remove this import. It's not needed and will break everything.

Assigning Attributes Directly on self
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Overrides Lumibot internals, causing crashes or unexpected behavior.

.. code-block:: python

    # WRONG - collides with framework
    def initialize(self):
        self.name = "MyBot"
        self.asset = Asset("SPY")
        self.symbol = "SPY"

    # CORRECT - use self.vars
    def initialize(self):
        self.vars.strategy_label = "MyBot"
        self.vars.target_asset = Asset("SPY", asset_type=Asset.AssetType.STOCK)
        self.vars.target_symbol = "SPY"

Forgetting set_market("24/7") for Crypto
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Crypto bot stops trading at 4pm EST every day.

.. code-block:: python

    # WRONG - crypto bot stops at 4pm
    def initialize(self):
        self.sleeptime = "1M"

    # CORRECT - crypto trades 24/7
    def initialize(self):
        self.set_market("24/7")  # REQUIRED for crypto
        self.sleeptime = "1M"

Data Mistakes
-------------

Not Checking if get_last_price() Returns None
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Crashes with NoneType error when data is unavailable.

.. code-block:: python

    # WRONG - will crash on None
    price = self.get_last_price(asset)
    quantity = self.portfolio_value / price  # Crashes if price is None

    # CORRECT - always check for None
    price = self.get_last_price(asset)
    if price is None:
        self.log_message(f"No price for {asset.symbol}", color="red")
        return
    quantity = self.portfolio_value / price

Using get_historical_prices() for Real-Time Data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Strategy uses stale data, missing current price movements.

.. code-block:: python

    # WRONG - historical data can be 1 minute delayed
    bars = self.get_historical_prices(asset, 1, "minute")
    current_price = bars.df.iloc[-1]["close"]

    # CORRECT - use get_last_price for real-time
    current_price = self.get_last_price(asset)

    # BEST - use get_quote for bid/ask
    quote = self.get_quote(asset)
    if quote and quote.bid and quote.ask:
        mid_price = quote.mid_price

Returning Early When get_greeks() is None
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Strategy stops running for the entire iteration just because one option has no Greeks.

.. code-block:: python

    # WRONG - blocks entire strategy
    greeks = self.get_greeks(option_asset)
    if greeks is None:
        return  # Strategy stops here!

    # CORRECT - continue with other logic
    greeks = self.get_greeks(option_asset)
    if greeks is not None:
        delta = greeks.get("delta")
        # Use delta here
    else:
        self.log_message("Greeks unavailable, skipping delta check", color="yellow")

    # Strategy continues with other logic...

Options Mistakes
----------------

Manually Selecting Option Expirations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Selected expiration may not have tradeable data during backtesting.

.. code-block:: python

    # WRONG - expiration may not exist
    target_expiry = self.get_datetime() + timedelta(days=30)
    option = Asset("SPY", asset_type=Asset.AssetType.OPTION,
                   expiration=target_expiry.date(), strike=400, right="call")

    # CORRECT - use OptionsHelper
    chains = self.get_chains(underlying_asset)
    target_expiry = self.get_datetime() + timedelta(days=30)
    valid_expiry = self.options_helper.get_expiration_on_or_after_date(
        target_expiry, chains, "call"
    )

Brute-Forcing Deltas by Scanning Strikes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Backtests become very slow because calling ``get_greeks()`` repeatedly can trigger many option quote-history downloads.

.. code-block:: python

    # WRONG - brute-force scan (slow)
    strikes = chains.strikes(expiry, "PUT")
    for strike in strikes:
        option = Asset("SPY", asset_type=Asset.AssetType.OPTION, expiration=expiry, strike=strike, right="put")
        greeks = self.get_greeks(option, underlying_price=underlying_price)
        ...

    # CORRECT - use OptionsHelper (bounded probing + caching)
    strike = self.options_helper.find_strike_for_delta(
        underlying_asset=underlying_asset,
        underlying_price=float(underlying_price),
        target_delta=-0.20,
        expiry=expiry,
        right="put",
    )

Forgetting Options are 100x Multiplied
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Position sizing is off by 100x.

.. code-block:: python

    # WRONG - buys 100x too many contracts
    option_price = 1.50  # $1.50 premium
    contracts = 10000 / option_price  # Wrong: 6666 contracts!

    # CORRECT - account for multiplier
    option_price = 1.50
    actual_cost_per_contract = option_price * 100  # $150
    contracts = int(10000 / actual_cost_per_contract)  # Correct: 66 contracts

Using get_last_price() for Options Pricing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Stale or missing prices for illiquid options.

.. code-block:: python

    # WRONG - last trade can be very stale for options
    price = self.get_last_price(option_asset)

    # CORRECT - use quote for bid/ask
    quote = self.get_quote(option_asset)
    if quote and quote.bid and quote.ask:
        fair_price = quote.mid_price
    else:
        self.log_message("No valid quote for option", color="yellow")

Order Mistakes
--------------

Expecting Immediate Position Updates After submit_order()
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Strategy logic based on outdated position data.

.. code-block:: python

    # WRONG - position hasn't updated yet
    self.submit_order(order)
    position = self.get_position(asset)  # Still shows old data!

    # CORRECT - check on next iteration
    self.submit_order(order)
    # In the NEXT on_trading_iteration():
    position = self.get_position(asset)  # Now updated

Using submit_order() to Close Crypto Futures
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Opens a new position instead of closing existing one.

.. code-block:: python

    # WRONG - opens opposite position instead of closing
    order = self.create_order(futures_asset, quantity, "sell")
    self.submit_order(order)

    # CORRECT - use close_position
    self.close_position(futures_asset)

Using Deprecated take_profit_price/stop_loss_price
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** May cause order errors or unexpected behavior.

.. code-block:: python

    # WRONG - deprecated parameters
    order = self.create_order(asset, 100, "buy",
        take_profit_price=110,
        stop_loss_price=90)

    # CORRECT - use secondary_ parameters
    order = self.create_order(asset, 100, "buy",
        order_class=Order.OrderClass.BRACKET,
        secondary_limit_price=110,      # Take profit
        secondary_stop_price=90)        # Stop loss

Visualization Mistakes
----------------------

Adding Markers Every Iteration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Chart crashes or becomes unusable due to thousands of markers.

.. code-block:: python

    # WRONG - adds marker every iteration
    def on_trading_iteration(self):
        self.add_marker("Price", price, color="blue")  # Chart explodes!

    # CORRECT - markers only for significant events
    def on_trading_iteration(self):
        if signal_detected:  # Only when something happens
            self.add_marker("Buy Signal", price, color="green", asset=my_asset)

        # Use add_line for continuous data
        self.add_line("SMA_20", sma_value, color="blue", asset=my_asset)

Using 'text' Parameter in add_marker/add_line/add_ohlc
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** TypeError crash - there is no 'text' parameter.

.. code-block:: python

    # WRONG - causes TypeError
    self.add_marker("Signal", price, text="Buy now!")

    # CORRECT - use detail_text for hover text
    self.add_marker("Signal", price, detail_text="Buy signal triggered", asset=my_asset)

Forgetting to Pass asset Parameter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Indicators appear in separate subplot instead of overlaying price chart.

.. code-block:: python

    # WRONG - indicator in separate subplot
    self.add_line("SMA_20", sma_value, color="blue")

    # CORRECT - overlays on asset's price chart
    self.add_line("SMA_20", sma_value, color="blue", asset=spy_asset)

Code Organization Mistakes
--------------------------

Hardcoding API Keys
~~~~~~~~~~~~~~~~~~~

**Impact:** Security risk and deployment problems.

.. code-block:: python

    # WRONG - hardcoded fallback
    api_key = os.getenv('PERPLEXITY_API_KEY', 'your_api_key_here')

    # CORRECT - None fallback
    api_key = os.getenv('PERPLEXITY_API_KEY')
    if api_key is None:
        self.log_message("PERPLEXITY_API_KEY not set", color="red")

Setting Parameters in Multiple Places
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Confusing, parameters override each other unpredictably.

.. code-block:: python

    # WRONG - parameters set in multiple places
    class MyStrategy(Strategy):
        parameters = {"symbol": "SPY"}

    if __name__ == "__main__":
        strategy = MyStrategy(parameters={"symbol": "AAPL"})  # Which one wins?

    # CORRECT - one place only
    class MyStrategy(Strategy):
        parameters = {
            "symbol": "SPY",
            "period": 20
        }
        # Never override parameters elsewhere

Using try/except to Hide Errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Bugs are hidden, making debugging nearly impossible.

.. code-block:: python

    # WRONG - hides real errors
    try:
        price = self.get_last_price(asset)
        quantity = self.portfolio_value / price
    except:
        pass  # What went wrong? No idea!

    # CORRECT - explicit error handling
    price = self.get_last_price(asset)
    if price is None:
        self.log_message(f"No price for {asset.symbol}", color="red")
        return

    quantity = self.portfolio_value / price

Using sleep() in Strategy
~~~~~~~~~~~~~~~~~~~~~~~~~

**Impact:** Blocks the entire bot, preventing important code from running.

.. code-block:: python

    # WRONG - blocks everything
    def on_trading_iteration(self):
        self.submit_order(order)
        time.sleep(5)  # Bot frozen for 5 seconds!

    # CORRECT - check conditions next iteration
    def on_trading_iteration(self):
        if not hasattr(self.vars, "order_time"):
            self.submit_order(order)
            self.vars.order_time = self.get_datetime()
            return

        elapsed = self.get_datetime() - self.vars.order_time
        if elapsed > timedelta(seconds=5):
            # Now do the next step
            pass


IBKR history waits and data-health diagnostics
--------------------------------------------------------------------------------

A provider rate limit is a retryable wait, not evidence that an instrument has
no prices. When the downloader supplies structured rate-limit information,
LumiBot retains the request identity and exposes the provider wait in download
status. Buying more simultaneous quotes does not automatically remove
historical-data pacing restrictions.

Inspect the ``data_health`` field in backtest settings alongside the requested
window and provider error details. An incomplete diagnostic is not an automatic
backtest failure; a legitimate strategy can also produce no trades. Short daily
requests include the full required history and calendar padding. Longer
stock/index windows retain the five-year page cap and backward pagination.

Paper Fund Automation and Purchase Authority
--------------------------------------------------------------------------------

The optional autonomous fund defaults to ``FUND_AUTOMATION_MODE=full``. A
separate read-only risk reviewer approves or rejects exception purchases;
``semi`` preserves operator approval. The manager cannot approve its own
proposal. Both modes retain the same automatic envelope, quantitative hard
limits, STOP, independent monitoring and account permissions. Full automation
does not authorize funding, credentials, live-money trading or changing limits.

A pending exception request can coexist with purchases inside the automatic
envelope. The selected ``decisions ... --event N`` view shows the recorded
authority and computed envelope per order, rather than treating the manager's
rationale as proof of approval. Older missing receipts remain unknown. Status
shows the mode loaded at startup; editing the environment requires a restart
and changes the policy digest, retiring old allocations and approvals.

AI approval binds the exact account, request, digest, policy, scope and expiry.
Invalid or late results, denials and timeouts never become approval. Attempts
and retry delays persist across restart and consume normal model/cost budgets.
Approval waits for a fresh account/quote/risk check on the next iteration.
Submission and local simulated fills recheck authority; revocation or expiry
cancels the remainder while preserving fills already committed. Paper orders
already sent still depend on the broker's cancel and fill acknowledgements.
Buy-turnover cash includes fees; notional budgets reserve the configured fee
buffer and at least the local broker's known fee.

A small NAV increase can raise the theoretical daily cash ceiling without
changing the remaining purchases. Plan approval reuse checks whether the old
cash scope still covers observed spending plus the current remaining buys,
including fee reserves. It preserves the original ID, digest, limits and expiry;
actual purchases beyond that immutable cash scope still need new approval.

Digest-bound pending approval notices are revalidated when queued, when claimed
for delivery and after message construction. Requests already resolved,
superseded, expired or invalidated by a policy/account change are suppressed with
an ``approval_*`` delivery reason, while resolution notices and original audit
records remain available. Transport retries use the same checks; older
informational notices without a digest retain their compatibility behavior.

A restarted observer also preserves the last AI review attempt's unexpired
lease. Reaching the attempt count alone does not justify waking a replacement
manager while that attempt is running. A completed failure or abandoned lease
can trigger one reassessment without extending the retry budget.

Fresh Fund Costs Versus Completed-Session Performance
--------------------------------------------------------------------------------

The optional autonomous fund provides ``fund_costs`` before research, critique,
manager decisions and correction calls, including on the first day. A manager
sees the costs of research and critique already completed in the current cycle.
Post-market reviews receive refreshed cost evidence separately from their saved
market inputs. Stability repetitions retain the accepted request's original
context. The current call's final cost is not yet known when it starts.

These are estimates at the operator's configured token rates, not provider
bills. Missing or invalid input/output usage remains unpriced; explicit zero
usage can be priced at zero. Unknown calls or data rates make net figures upper
bounds. Trading fees and slippage already affect account equity and must not be
deducted a second time. The cost cutoff and the account observation time remain
separate; an absent account observation never becomes an invented net value.

Data subscriptions accrue once per New York date with a benchmark observation,
at the rate effective when that date's first observation was recorded (monthly
rate divided by 21). A later restart or configuration change does not reprice
past sessions. Known data charges are still deducted beside unknown ones.
This allocation is not a reconstruction of the provider's billing calendar.

``fund_scoreboard`` covers only its named, completed closing intervals; current
``fund_costs`` covers all calls recorded through the request. Since model and
data costs accrue outside the brokerage account, the scoreboard and acceptance
statistics subtract accrued expenses from observed wealth, without assuming
those dollars have already stopped earning account returns. With flat $10,000
equity and two $100 expense intervals, the net return is -2%, not -1.99%.
Cost feedback never reduces research or analysis, changes review frequency,
or obliges the manager to trade to recover sunk costs. Operator budgets still
apply. Cash loses money after costs only when its income is insufficient;
benchmark-relative results must be observed, not assumed.
