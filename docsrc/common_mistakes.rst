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
