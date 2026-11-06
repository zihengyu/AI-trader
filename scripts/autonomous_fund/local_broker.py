"""Durable virtual-account adapter on native Broker/Order/Position interfaces.

This module has no provider trading client, URL or authentication path.
"""
import json
import os
import threading
from dataclasses import asdict, replace
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from zoneinfo import ZoneInfo

from lumibot.brokers import Broker
from lumibot.entities import Asset, Order, Position
from lumibot.trading_builtins import PollingStream

from .journal import encode
from .market_data import CachedMarketHours
from .risk import Account, MarketDataUnavailable, ObservedPrice, RiskError, instant, number, observed_quote, trading_day


class VirtualRiskCancellation(RiskError):
    pass


class LocalPaperBroker(CachedMarketHours, Broker):
    IS_BACKTESTING_BROKER = False
    LOCAL_SIMULATION = True

    def __init__(self, data_source, journal, limits, *, connect_stream=True,
                 fee_rate="0.001", slippage_bps=5, volume_fraction="0.01", matching_enabled=True, require_monitor=False,
                 starting_cash="10000"):
        self.journal, self.limits = journal, limits
        self.matching_enabled = matching_enabled
        self.require_monitor = require_monitor
        self.fee_rate, self.slippage = number(fee_rate), number(slippage_bps) / 10000
        self.volume_fraction = number(volume_fraction)
        self._last_poll_error = None
        self._market_data_error = None
        self._valuation_policy = None
        self._valuation_error = None
        self._last_valuation = None
        self._next_valuation = None
        self._valuation_book_key = None
        # Native polling and strategy iterations can both request a sync. Keep
        # risk snapshots and fills serial, including across a minute boundary.
        self._poll_lock = threading.RLock()
        if not 0 <= self.fee_rate <= number("0.01") or not 0 <= self.slippage <= number("0.01") or not 0 < self.volume_fraction <= number("0.01"):
            raise RiskError("Invalid local simulation cost or volume parameters")
        if not 0 < number(starting_cash) <= number("10000000"):
            raise RiskError("Invalid local virtual account size")
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS local_account (id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT NOT NULL, receivables TEXT NOT NULL);
                INSERT OR IGNORE INTO local_account VALUES(1,'%s','0');
                CREATE TABLE IF NOT EXISTS local_positions (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, average TEXT NOT NULL, opened_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_orders (
                    id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL, strategy TEXT NOT NULL, body TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
                    filled INTEGER NOT NULL DEFAULT 0, notional TEXT NOT NULL DEFAULT '0', fee TEXT NOT NULL DEFAULT '0', last_bar TEXT);
                CREATE TABLE IF NOT EXISTS local_fills (
                    order_id TEXT NOT NULL, bar_start TEXT NOT NULL, quantity INTEGER NOT NULL,
                    price TEXT NOT NULL, fee TEXT NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(order_id,bar_start));
                CREATE TABLE IF NOT EXISTS local_actions (id TEXT PRIMARY KEY, amount TEXT NOT NULL);
            """ % str(number(starting_cash)))
        super().__init__(name="local-paper", data_source=data_source, config={"MARKET": "NYSE"},
                         connect_stream=connect_stream, start_orders_thread=False)
        # A crash before local order creation is provably a non-submission: all
        # accepted orders and their intent links commit in this same database.
        with journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id FROM intents WHERE status!='terminal'").fetchall():
                local = db.execute("SELECT id FROM local_orders WHERE intent_id=?", (row["id"],)).fetchone()
                if local:
                    db.execute("UPDATE intents SET broker_id=? WHERE id=?", (local["id"], row["id"]))
                else:
                    db.execute("UPDATE intents SET status='terminal' WHERE id=?", (row["id"],))
                    self._event(db, "local_intent_not_submitted", {"intent_id": row["id"]})

    def _event(self, db, kind, body):
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(self.data_source.get_datetime()), kind, encode(body)))

    def _get_stream_object(self):
        return PollingStream(polling_interval=5)

    def _register_stream_events(self):
        self.stream.add_action(self.stream.POLL_EVENT)(self.do_polling)

    def _run_stream(self):
        self._stream_established()
        self.stream.run(self.name)

    def sleep(self, sleeptime):
        # Native market-open waits can span a weekend. An ordinary time.sleep
        # keeps Trader.join() and the account lock alive after Ctrl+C.
        self._stop_event.wait(max(0, float(sleeptime)))

    def _close_connection(self):
        # StrategyExecutor.gracefully_exit() invokes this native broker hook.
        # Wake market waits before stopping the polling stream; both are local.
        self._stop_event.set()
        if getattr(self, "stream", None):
            self.stream.stop()

    def _pull_broker_order(self, identifier):
        with self.journal.connection() as db:
            row = db.execute("SELECT * FROM local_orders WHERE id=?", (str(identifier),)).fetchone()
            return dict(row) if row else None

    def _pull_broker_all_orders(self):
        with self.journal.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM local_orders ORDER BY created_at,id")]

    def _parse_broker_order(self, response, strategy_name, strategy_object=None):
        body = json.loads(response["body"])
        order = Order(response["strategy"], Asset(body["symbol"], asset_type="stock"),
                      body["quantity"], body["side"], order_type="limit", limit_price=float(number(body["limit"])),
                      identifier=response["id"], status=response["status"], time_in_force="day",
                      date_created=instant(response["created_at"]),
                      avg_fill_price=float(number(response["notional"]) / response["filled"]) if response["filled"] else None,
                      trade_cost=float(number(response["fee"])))
        if response["filled"]:
            order.add_transaction(float(number(response["notional"]) / response["filled"]), response["filled"])
        order.update_raw(response)
        return order

    def sync_orders(self, strategy):
        # Positions come from the same durable book. Registering terminal history
        # must not replay historical fills into those restored positions.
        for order in self._pull_all_orders(self._strategy_name_from_input(strategy), strategy):
            tracked = self.get_tracked_order(order.identifier)
            if tracked is not None:
                self._apply_direct_order_observation(tracked, order)
            else:
                bucket = {"fill": self._filled_orders, "canceled": self._canceled_orders,
                          "expired": self._canceled_orders, "partial_fill": self._partially_filled_orders}.get(str(order.status), self._new_orders)
                bucket.append(order)

    def _pull_positions(self, strategy):
        self.ledger_audit()
        name = self._strategy_name_from_input(strategy)
        with self.journal.connection() as db:
            return [Position(name, Asset(row["symbol"], asset_type="stock"), row["quantity"],
                             avg_fill_price=float(number(row["average"])))
                    for row in db.execute("SELECT * FROM local_positions WHERE quantity>0")]

    def _pull_position(self, strategy, asset):
        return next((position for position in self._pull_positions(strategy) if position.asset == asset), None)

    def receivables(self):
        with self.journal.connection() as db:
            return number(db.execute("SELECT receivables FROM local_account WHERE id=1").fetchone()[0])

    def _get_balances_at_broker(self, quote_asset, strategy):
        with self._poll_lock:
            return self._read_balances(quote_asset, strategy)

    def ledger_audit(self, *, strict=True):
        from .ledger_audit import audit_connection, require_consistent
        with self.journal.connection() as db:
            db.execute("BEGIN")
            return require_consistent(db) if strict else audit_connection(db)

    def _read_balances(self, quote_asset, strategy):
        self.ledger_audit()
        self._corporate_actions()
        positions = self._pull_positions(strategy)
        with self.journal.connection() as db:
            row = db.execute("SELECT cash,receivables FROM local_account WHERE id=1").fetchone()
            cash, receivables = number(row[0]), number(row[1])
            splits = {}
            for action in db.execute("SELECT id FROM local_actions"):
                symbol, day, kind = action[0].rsplit(":", 2)
                if kind == "split":
                    splits[symbol] = max(day, splits.get(symbol, day))
        value = number(0)
        observed = {}
        market_open = self.is_market_open()
        for position in positions:
            if not market_open:
                mark = self._closing_quote(position.asset, self.data_source.session.last_close())
                mark_day = trading_day(mark.as_of)
                if mark_day < splits.get(position.asset.symbol, mark_day):
                    raise MarketDataUnavailable("Price predates applied split; valuation paused")
                observed[position.asset.symbol] = {"price": str(mark.price), "as_of": str(mark.as_of)}
                value += number(position.quantity) * mark.price
                continue
            quote = self.data_source.get_quote(position.asset)
            mark_day = trading_day(instant(quote.timestamp))
            if mark_day < splits.get(position.asset.symbol, mark_day):
                raise MarketDataUnavailable("Price predates applied split; valuation paused")
            mark = observed_quote(quote, self.data_source.get_datetime(), self.limits)
            observed[position.asset.symbol] = {"price": str(mark.price), "as_of": str(mark.as_of)}
            value += number(position.quantity) * number(observed[position.asset.symbol]["price"])
        if market_open:
            for mark in observed.values():
                ObservedPrice(number(mark["price"]), instant(mark["as_of"])).validate(self.data_source.get_datetime(), self.limits)
        self._last_balance_marks = observed
        return float(cash), float(value), float(cash + value + receivables)

    def get_historical_account_value(self):
        return {}

    def configure_valuation(self, *, universe, mandate, sweep=None, daily_loss_limit=None,
                            interval_seconds=30, benchmark_symbols=()):
        """Use the existing broker poll to mark the bound sleeve during long AI calls."""
        if not 5 <= interval_seconds <= 300:
            raise RiskError("Valuation interval must be from 5 to 300 seconds")
        self._valuation_policy = dict(universe=frozenset(universe), mandate=mandate, sweep=sweep,
                                      daily_loss_limit=daily_loss_limit, interval_seconds=interval_seconds,
                                      benchmark_symbols=tuple(benchmark_symbols))
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (not db.execute("SELECT 1 FROM events WHERE kind IN ('local_ledger_origin','startup') LIMIT 1").fetchone()
                    and not db.execute("SELECT 1 FROM local_fills LIMIT 1").fetchone()
                    and not db.execute("SELECT 1 FROM local_positions WHERE quantity>0 LIMIT 1").fetchone()):
                row = db.execute("SELECT cash,receivables FROM local_account WHERE id=1").fetchone()
                if number(row[0]) == number(mandate.local_account_usd) and number(row[1]) == 0:
                    self._event(db, "local_ledger_origin", {"cash": row[0], "receivables": row[1], "positions": {}})

    @staticmethod
    def _valuation_book(db):
        """One read transaction's book identity, also checked before publishing marks."""
        return {
            "balance": dict(db.execute("SELECT cash,receivables FROM local_account WHERE id=1").fetchone()),
            "positions": [dict(row) for row in db.execute("SELECT * FROM local_positions WHERE quantity>0 ORDER BY symbol")],
            "meta": dict(db.execute("SELECT key,value FROM meta WHERE key IN "
                                    "('mandate_reserve_cash','mandate_external')")),
            "splits": [row[0] for row in db.execute("SELECT id FROM local_actions WHERE id LIKE '%:split' ORDER BY id")],
            "pending": db.execute("SELECT count(*) FROM intents WHERE status!='terminal'").fetchone()[0],
            "last_fill": db.execute("SELECT coalesce(max(rowid),0) FROM local_fills").fetchone()[0],
        }

    def _publish_valuation(self):
        policy = self._valuation_policy
        if policy is None:
            return
        start = self.data_source.get_datetime()
        closed_at = None if self.is_market_open() else self.data_source.session.last_close()
        book_existed = True
        if closed_at is not None:
            # A closing observation is durable, including all-cash books. Reuse
            # it across polls, pre-open research and restarts until another close.
            with self.journal.connection() as db:
                origin = db.execute("SELECT at FROM events WHERE kind IN ('local_ledger_origin','startup') ORDER BY sequence LIMIT 1").fetchone()
                book_existed = origin is not None and instant(origin[0]) <= closed_at
                saved = db.execute("SELECT body FROM events WHERE kind='account' AND "
                                   "json_extract(body,'$.valuation.session_close')=? ORDER BY sequence DESC LIMIT 1",
                                   (str(closed_at),)).fetchone()
            if saved:
                self._last_valuation = json.loads(saved[0])
                self._valuation_error = None
                return
        with self.journal.connection() as db:
            db.execute("BEGIN")
            from .ledger_audit import require_consistent
            integrity = require_consistent(db)
            book = self._valuation_book(db)
        key = encode(book)
        if self._next_valuation is not None and start < self._next_valuation and key == self._valuation_book_key:
            return
        self._next_valuation = start + timedelta(seconds=policy["interval_seconds"])
        self._valuation_book_key = key
        mandate, meta = policy["mandate"], book["meta"]
        if mandate.capital_usd is not None and "mandate_reserve_cash" not in meta:
            raise RiskError("Capital sleeve must be initialized before independent valuation")
        marks = {}
        positions = []
        for row in book["positions"]:
            asset = Asset(row["symbol"], asset_type="stock")
            marks[row["symbol"]] = (self._closing_quote(asset, closed_at) if closed_at is not None else
                observed_quote(self.data_source.get_quote(asset), self.data_source.get_datetime(), self.limits))
            positions.append(Position("local-valuation", asset, row["quantity"], avg_fill_price=float(number(row["average"]))))
        now = self.data_source.get_datetime()
        for symbol, mark in marks.items():
            mark.validate(closed_at or now, self.limits)
            split_days = [item.rsplit(":", 2)[1] for item in book["splits"] if item.rsplit(":", 2)[0] == symbol]
            if split_days and trading_day(mark.as_of) < max(split_days):
                raise RiskError("Price predates applied split; independent valuation paused")
        from .holdings import average_prices, split_holdings
        limited = mandate.capital_usd is not None
        baseline = json.loads(meta["mandate_external"]) if limited and "mandate_external" in meta else None
        holdings, _, external_value, _ = split_holdings(
            positions, policy["universe"], allow_external=mandate.allow_external_holdings,
            baseline=baseline, initialized=baseline is not None, price=lambda symbol: marks[symbol].price)
        cash, receivables = number(book["balance"]["cash"]), number(book["balance"]["receivables"])
        reserve = number(meta.get("mandate_reserve_cash", 0)) if limited else number(0)
        equity = cash + receivables + sum((number(row["quantity"]) * marks[row["symbol"]].price
                                         for row in book["positions"]), number(0))
        account = Account(cash - reserve, equity - external_value - reserve, holdings, now,
                          bool(book["pending"]), receivables, position_average_prices=average_prices(positions, holdings))
        if account.cash < 0 or account.equity <= 0:
            raise RiskError("Independent sleeve valuation has invalid cash or equity")
        if policy["sweep"] is not None:
            from .cash import merge
            sweep = policy["sweep"]
            account = merge(account, sweep, marks[sweep.symbol].price if sweep.symbol in marks else 0)
        benchmarks = {}
        for symbol in policy["benchmark_symbols"]:
            try:
                asset = Asset(symbol, asset_type="stock")
                mark = marks.get(symbol) or (self._closing_quote(asset, closed_at) if closed_at is not None else
                    observed_quote(self.data_source.get_quote(asset), self.data_source.get_datetime(), self.limits))
                benchmarks[symbol] = str(mark.price)
            except Exception:
                continue  # Missing benchmark evidence must never invent a return.
        now = self.data_source.get_datetime()
        for mark in marks.values():
            mark.validate(closed_at or now, self.limits)
        account = replace(account, as_of=closed_at if closed_at is not None and book_existed else now)
        body = {**asdict(account), "ledger_audit": integrity, "valuation": {"source": "local_session_close" if closed_at else "local_broker_poll",
            **({"session_close": str(closed_at), "observed_at": str(now), "book_existed_at_close": book_existed} if closed_at else {}), "quotes": {
            symbol: {"price": str(mark.price), "as_of": str(mark.as_of)}
            for symbol, mark in marks.items() if symbol in holdings}}}
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._valuation_book(db) != book:
                raise RiskError("Book changed during valuation; retry with the next observation")
            if closed_at is not None:
                if any(instant(row[0]) > closed_at for row in db.execute("SELECT observed_at FROM local_fills")):
                    raise RiskError("Post-close trades prevent reconstructing the closing book")
                if any(row[0].rsplit(":", 2)[1] > trading_day(closed_at) for row in db.execute("SELECT id FROM local_actions")):
                    raise RiskError("Post-close corporate action prevents reconstructing the closing book")
                if any(instant(row[0]) > closed_at for row in db.execute("SELECT at FROM events WHERE kind IN "
                        "('mandate_capital_changed','operator_reserve_adjusted')")):
                    raise RiskError("Post-close reserve change prevents reconstructing the closing sleeve")
            # Record when the evidence was obtained, retaining the price cutoff
            # separately. Historical reports cannot see later recovery evidence.
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "account", encode(body)))
            if benchmarks:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "benchmark", encode(benchmarks)))
        self.journal.mark_equity(account.as_of, account.equity, self.limits)
        self.journal.observe_session(account.as_of, account, policy["daily_loss_limit"])
        self._last_valuation = json.loads(encode(body))
        self._valuation_error = None

    def _closing_quote(self, asset, closed_at):
        # Demand the final completed regular-session minute. A stale intraday
        # mark must never acquire the label or timestamp of a closing price.
        frame = self.data_source.execution_bars(asset, required_through=closed_at)
        rows = frame[frame.index + timedelta(minutes=1) == closed_at]
        if rows.empty:
            raise RiskError("Closing minute unavailable; retaining the last observed valuation")
        return ObservedPrice(number(rows.iloc[-1].close), closed_at).validate(closed_at, self.limits)

    def _submit_order(self, order):
        if self.require_monitor:
            from .monitor import require_monitor
            require_monitor(self.journal.root, self.data_source.get_datetime())
        if not self.matching_enabled or not self.is_market_open() or (self.journal.root / "STOP").exists():
            raise RiskError("Local simulator is stopped or market is closed")
        if str(order.asset.asset_type) != "stock" or str(order.order_type) != "limit" or str(order.side) not in {"buy", "sell"} or order.child_orders:
            raise RiskError("Local simulator only accepts simple long-only stock limit orders")
        key = (order.custom_params or {}).get("local_intent")
        identifier = "local-" + str(key)
        now = self.data_source.get_datetime()
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM intents WHERE id=? AND status!='terminal'", (key,)).fetchone()
            from .ledger_audit import require_consistent
            require_consistent(db)
            if row is None:
                raise RiskError("A durable risk reservation is required")
            body = json.loads(row["body"])
            if (order.asset.symbol, str(order.side), number(order.quantity), number(order.limit_price)) != (
                    body["symbol"], body["side"], number(body["quantity"]), number(body["limit"])):
                raise RiskError("Local order differs from its reserved intent")
            if db.execute("SELECT 1 FROM local_orders WHERE intent_id=?", (key,)).fetchone():
                raise RiskError("Local intent was already submitted")
            if (self.journal.root / "STOP").exists():
                raise RiskError("Emergency stop active")
            db.execute("INSERT INTO local_orders(id,intent_id,strategy,body,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?)", (
                identifier, key, order.strategy, row["body"], str(now), str(min(
                    instant(self.data_source.session.status()["close"]),
                    instant((order.custom_params or {}).get("decision_expires_at", self.data_source.session.status()["close"]))
                )), "new"))
            db.execute("UPDATE intents SET broker_id=?,status='pending' WHERE id=?", (identifier, key))
            self._event(db, "local_order_accepted", {"broker_id": identifier, "intent_id": key})
        order.set_identifier(identifier)
        order.update_raw({"id": identifier})
        self._process_trade_event(order, self.NEW_ORDER)
        return order

    def cancel_order(self, order):
        with self.journal.connection() as db:
            changed = db.execute("UPDATE local_orders SET status='canceled' WHERE id=? AND status IN ('new','partial_fill')", (str(order.identifier),)).rowcount
            if changed:
                self._event(db, "local_order_canceled", {"broker_id": str(order.identifier)})
        if changed:
            self._process_trade_event(order, self.CANCELED_ORDER)

    def _modify_order(self, order, **kwargs):
        raise RiskError("Local orders cannot be modified; cancel and obtain a new risk reservation")

    def is_market_open(self):
        return self.data_source.session.status()["open"]

    def do_polling(self):
        with self._poll_lock:
            self._poll_once()

    def _poll_once(self):
        try:
            if self._stop_event.is_set():
                return
            self._corporate_actions()
            pending = []
            for row in self._pull_broker_all_orders():
                if row["status"] not in {"new", "partial_fill"}:
                    continue
                order = self.get_tracked_order(row["id"]) or self._parse_broker_order(row, row["strategy"])
                now = self.data_source.get_datetime()
                if (self.journal.root / "STOP").exists() or now >= instant(row["expires_at"]):
                    self.cancel_order(order)
                    continue
                if self.matching_enabled and self.is_market_open():
                    try:
                        self._match(row, order)
                    except MarketDataUnavailable as exc:
                        # A missing symbol cannot prevent a risk-reducing sale
                        # of another symbol with its own valid execution data.
                        pending.append(str(exc))
            if pending:
                self._note_market_data_pending("; ".join(dict.fromkeys(pending)))
            else:
                self._last_poll_error = None
                self._market_data_error = None
            try:
                self._publish_valuation()
            except Exception as exc:
                self._valuation_error = str(exc) if isinstance(exc, RiskError) else type(exc).__name__
        except MarketDataUnavailable as exc:
            self._note_market_data_pending(str(exc))
        except Exception as exc:
            message = str(exc) if isinstance(exc, RiskError) else type(exc).__name__
            if message != self._last_poll_error:
                self.journal.event(self.data_source.get_datetime(), "notification", {"kind": "local_matching_blocked", "details": message})
                self._last_poll_error = message
        finally:
            try:
                with self.journal.connection() as db:
                    account = dict(db.execute("SELECT cash,receivables FROM local_account WHERE id=1").fetchone())
                    pending = db.execute("SELECT count(*) FROM local_orders WHERE status IN ('new','partial_fill')").fetchone()[0]
                body = {"mode": "local", "heartbeat_at": self.data_source.get_datetime().isoformat(),
                        "runner_state": "exited" if self._stop_event.is_set() else "running",
                        "matching_enabled": self.matching_enabled,
                        "market": self.data_source.session.status(), "stopped": (self.journal.root / "STOP").exists(),
                        "matching_error": self._last_poll_error, "pending_orders": pending, **account}
                body.update(valuation=self._last_valuation, valuation_error=self._valuation_error,
                            market_data_error=self._market_data_error)
                if hasattr(self.data_source, "diagnostics"):
                    body["market_data"] = self.data_source.diagnostics()
                target = self.journal.root / "status.json"
                temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                temporary.write_text(encode(body), encoding="utf-8")
                temporary.replace(target)
            except Exception:
                pass

    def _note_market_data_pending(self, message):
        if message != self._market_data_error:
            self.journal.event(self.data_source.get_datetime(), "notification", {
                "kind": "local_market_data_pending", "details": message})
        self._market_data_error = message

    def _match(self, row, order):
        frame = self.data_source.execution_bars(order.asset)
        created = instant(row["created_at"])
        for start, bar in frame.iterrows():
            now, end = self.data_source.get_datetime(), start + timedelta(minutes=1)
            if start < created or not 0 <= (now - end).total_seconds() <= self.limits.quote_max_seconds:
                continue
            if row["last_bar"] and start <= instant(row["last_bar"]):
                continue
            try:
                self._fill_bar(row["id"], order, start, bar)
            except VirtualRiskCancellation:
                self.cancel_order(order)
                self.journal.event(self.data_source.get_datetime(), "local_risk_canceled", {"broker_id": row["id"]})
                return

    def _fill_bar(self, identifier, order, start, bar):
        if self.require_monitor:
            from .monitor import require_monitor
            require_monitor(self.journal.root, self.data_source.get_datetime())
        if not self.matching_enabled:
            raise RiskError("Observation-only broker cannot simulate executions")
        # Conservative complete-bar execution: high plus friction for buys, low
        # minus friction for sells. No fill at a pre-submission observed price.
        buy = str(order.side) == "buy"
        friction = self.slippage + self.data_source.spread / 2
        raw = number(bar.high) * (1 + friction) if buy else number(bar.low) * (1 - friction)
        price = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING if buy else ROUND_FLOOR)
        # Fill-time checks use the operator's capital sleeve, never reserved cash.
        reserve = self.journal.reserve_cash()
        paused, marks, equity = False, {}, number(0)
        if buy:
            # Admitting a purchase needs the whole book valued from fresh source observations.
            _, _, equity = self._get_balances_at_broker(None, order.strategy)
            equity = number(equity) - reserve
            paused = self.journal.mark_equity(self.data_source.get_datetime(), equity, self.limits)
            for position in self._pull_positions(order.strategy):
                quote = self.data_source.get_quote(position.asset)
                age = (self.data_source.get_datetime() - instant(quote.timestamp)).total_seconds()
                if not 0 <= age <= self.limits.quote_max_seconds:
                    raise MarketDataUnavailable("Stale holding mark; virtual fills paused")
                marks[position.asset.symbol] = number(position.quantity) * number(quote.ask)
        else:
            # A risk-reducing sale never waits on OTHER holdings' marks. Its own bar
            # must still postdate an applied split, or the sale would be mispriced.
            with self.journal.connection() as db:
                splits = [row[0].rsplit(":", 2)[1] for row in db.execute(
                    "SELECT id FROM local_actions WHERE id LIKE ?", (f"{order.asset.symbol}:%:split",))]
            if splits and trading_day(start) < max(splits):
                raise MarketDataUnavailable("Price predates applied split; virtual fills paused")
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            from .ledger_audit import require_consistent
            require_consistent(db)
            row = db.execute("SELECT * FROM local_orders WHERE id=?", (identifier,)).fetchone()
            if row["status"] not in {"new", "partial_fill"} or (row["last_bar"] and start <= instant(row["last_bar"])):
                return
            if (self.journal.root / "STOP").exists():
                return
            if not self.is_market_open() or self.data_source.get_datetime() >= instant(row["expires_at"]):
                return
            age = (self.data_source.get_datetime() - (start + timedelta(minutes=1))).total_seconds()
            if not 0 <= age <= self.limits.quote_max_seconds:
                return
            db.execute("UPDATE local_orders SET last_bar=? WHERE id=?", (start.isoformat(), identifier))
            if (buy and price > number(order.limit_price)) or (not buy and price < number(order.limit_price)):
                return
            remaining = int(number(order.quantity)) - row["filled"]
            quantity = min(remaining, int(number(bar.volume) * self.volume_fraction))
            if quantity <= 0:
                return
            account = db.execute("SELECT cash FROM local_account WHERE id=1").fetchone()
            cash = number(account[0])
            sleeve_cash = cash - reserve
            position = db.execute("SELECT * FROM local_positions WHERE symbol=?", (order.asset.symbol,)).fetchone()
            held = position["quantity"] if position else 0
            notional, fee = price * quantity, price * quantity * self.fee_rate
            if buy:
                net_equity = number(equity) - fee
                if (paused or sleeve_cash < notional + fee or marks.get(order.asset.symbol, 0) + notional > net_equity * self.limits.max_position
                        or sum(marks.values()) + notional > net_equity * self.limits.max_exposure):
                    raise VirtualRiskCancellation("Virtual fill exceeds current risk capacity")
                average = (number(position["average"]) * held + notional) / (held + quantity) if position else price
                db.execute("INSERT OR REPLACE INTO local_positions VALUES(?,?,?,?)", (
                    order.asset.symbol, held + quantity, str(average), position["opened_at"] if held else str(self.data_source.get_datetime())))
                cash -= notional + fee
            else:
                if quantity > held:
                    raise RiskError("Virtual sale would create a short position")
                db.execute("UPDATE local_positions SET quantity=? WHERE symbol=?", (held - quantity, order.asset.symbol))
                cash += notional - fee
            filled, total, total_fee = row["filled"] + quantity, number(row["notional"]) + notional, number(row["fee"]) + fee
            status = "fill" if filled == int(number(order.quantity)) else "partial_fill"
            db.execute("UPDATE local_account SET cash=? WHERE id=1", (str(cash),))
            db.execute("UPDATE local_orders SET filled=?,notional=?,fee=?,status=? WHERE id=?", (filled, str(total), str(total_fee), status, identifier))
            db.execute("INSERT INTO local_fills VALUES(?,?,?,?,?,?)", (
                identifier, start.isoformat(), quantity, str(price), str(fee), str(self.data_source.get_datetime())))
            self._event(db, "fill", {"broker_id": identifier, "symbol": order.asset.symbol, "side": str(order.side),
                                      "quantity": str(quantity), "price": str(price), "trade_cost": str(fee),
                                      "execution_id": identifier + ":" + start.isoformat(), "bar_start": start.isoformat(),
                                      "evidence_kind": "local_completed_bar_simulation",
                                      "bar": {key: str(number(bar[key])) for key in ("open", "high", "low", "close", "volume")},
                                      "execution_assumptions": {"fee_rate": str(self.fee_rate),
                                          "spread_bps": str(self.data_source.spread * 10000),
                                          "slippage_bps": str(self.slippage * 10000),
                                          "volume_fraction": str(self.volume_fraction)}})
            require_consistent(db)  # Cash, lots, fills and evidence commit together or all roll back.
        order.trade_cost = float(total_fee)
        order.avg_fill_price = float(total / filled)
        self._process_trade_event(order, self.FILLED_ORDER if status == "fill" else self.PARTIALLY_FILLED_ORDER,
                                  price=float(price), filled_quantity=quantity, multiplier=1)

    def _corporate_actions(self):
        # Splits adjust integer shares; dividends accrue as NON-SPENDABLE
        # receivables because Yahoo does not establish the actual payment date.
        with self.journal.connection() as db:
            positions = [dict(row) for row in db.execute("SELECT * FROM local_positions WHERE quantity>0")]
            # Read once; a write lock is only taken for an action not seen before.
            known = {row[0]: number(row[1]) for row in db.execute("SELECT id,amount FROM local_actions")}
        for position in positions:
            asset = Asset(position["symbol"], asset_type="stock")
            actions = self.data_source.corporate_actions(asset, since=position["opened_at"])
            for action in actions:
                if action["date"] <= trading_day(instant(position["opened_at"])):
                    continue
                for kind in ("split", "dividend"):
                    amount = number(action.get(kind, 0))
                    if amount == 0:
                        continue
                    key = f"{asset.symbol}:{action['date']}:{kind}"
                    if key in known:
                        self._same_action(kind, known[key], amount, action, actions)
                        continue
                    with self.journal.connection() as db:
                        db.execute("BEGIN IMMEDIATE")
                        prior = db.execute("SELECT amount FROM local_actions WHERE id=?", (key,)).fetchone()
                        if prior:
                            self._same_action(kind, number(prior[0]), amount, action, actions)
                            continue
                        row = db.execute("SELECT * FROM local_positions WHERE symbol=?", (asset.symbol,)).fetchone()
                        if kind == "split":
                            # A newly discovered split after ex-date trades cannot
                            # safely multiply the current book. Require review.
                            if self._has_fills_on_or_after(db, asset.symbol, action["date"]):
                                raise RiskError("Late split after trading requires operator reconciliation")
                            quantity = row["quantity"] * amount
                            if quantity != int(quantity):
                                raise RiskError("Fractional split outcome requires operator reconciliation")
                            db.execute("UPDATE local_positions SET quantity=?,average=? WHERE symbol=?", (
                                int(quantity), str(number(row["average"]) / amount), asset.symbol))
                        else:
                            receivable = number(db.execute("SELECT receivables FROM local_account WHERE id=1").fetchone()[0])
                            entitled = self._dividend_quantity(db, asset.symbol, action["date"])
                            db.execute("UPDATE local_account SET receivables=? WHERE id=1", (str(receivable + entitled * amount),))
                        db.execute("INSERT INTO local_actions VALUES(?,?)", (key, str(amount)))
                        self._event(db, "local_corporate_action", {"symbol": asset.symbol, **action, "kind": kind,
                            **({"entitled_quantity": str(entitled)} if kind == "dividend" else {})})

    @staticmethod
    def _same_action(kind, recorded, amount, action, actions):
        """An already-applied action reported again; only a genuine revision needs the operator.

        Yahoo restates earlier dividends for later splits (0.40 becomes 0.20 after a
        2:1 split): the cash entitlement booked with pre-split shares is unchanged.
        """
        tolerance = number("0.0001") * max(abs(recorded), abs(amount))
        if abs(recorded - amount) <= tolerance:
            return
        if kind == "dividend":
            factor = number(1)
            for later in actions:
                split = number(later.get("split", 0) or 0)
                if later["date"] > action["date"] and split > 0:
                    factor *= split
            if factor != 1 and abs(recorded / factor - amount) <= tolerance:
                return
        raise RiskError("Corporate action revision requires operator reconciliation")

    @staticmethod
    def _exdate(date):
        return datetime.combine(datetime.fromisoformat(date).date(), time(), tzinfo=ZoneInfo("America/New_York"))

    def _symbol_fills(self, db, symbol):
        for row in db.execute("SELECT f.quantity,f.observed_at,o.body FROM local_fills f JOIN local_orders o ON o.id=f.order_id"):
            body = json.loads(row["body"])
            if body["symbol"] == symbol:
                yield instant(row["observed_at"]), row["quantity"] if body["side"] == "buy" else -row["quantity"]

    def _has_fills_on_or_after(self, db, symbol, date):
        return any(at >= self._exdate(date) for at, _ in self._symbol_fills(db, symbol))

    def _dividend_quantity(self, db, symbol, date):
        # Entitlement comes from the book before the ex-date, including splits,
        # rather than shares bought later while the provider action was delayed.
        cutoff = self._exdate(date)
        changes = [(at, "fill", quantity) for at, quantity in self._symbol_fills(db, symbol) if at < cutoff]
        for row in db.execute("SELECT id,amount FROM local_actions"):
            name, day, kind = row["id"].rsplit(":", 2)
            if name == symbol and kind == "split" and self._exdate(day) <= cutoff:
                changes.append((self._exdate(day), "split", number(row["amount"])))
        quantity = number(0)
        for _, kind, value in sorted(changes):
            quantity = quantity * value if kind == "split" else quantity + value
        if quantity < 0 or quantity != int(quantity):
            raise RiskError("Dividend entitlement requires operator reconciliation")
        return quantity
