Common Mistakes and How to Avoid Them
======================================

.. meta::
   :description: This page documents the most common mistakes made when writing Lumibot strategies, along with the correct patterns to use instead.

This page documents the most common mistakes made when writing Lumibot strategies, along with the correct patterns to use instead.

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
save one closing valuation from the final completed regular-session minute and
reuse it across polls, restarts and reviews until the next open. Missing final
minutes are disclosed rather than labeling an old intraday mark as a close.
Status exposes valuation errors without relabeling old equity
as a new observation. Paper accounts continue to use broker observations.

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

The autonomous fund keeps the native ``Strategy.notify`` and ``notify_user``
contracts. Its ``fund_outbox`` provider queues AI notices durably for the selected
account's monitor, while native disabled/backtest behavior and other configured
providers remain effective. ``payload.status=queued`` acknowledges persistence;
Telegram delivery is confirmed separately. These notices use ``agent_notice``
and cannot be presented as operational approval or health events. Internal
application alerts use a separate ``_notify_fund`` method.

For DuckDB research, the fund's role prompts explain inspecting actual tables
and columns, keeping CTE aliases within one statement, and separating lagged
returns, rolling statistics and aggregates into successive CTEs. Query failures
remain visible so the agent can correct and verify its work.

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
