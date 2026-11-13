"""Live Yahoo adapter for the local simulator; no brokerage credentials."""
import threading
import time
import os
from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import numpy as np

from lumibot.data_sources import DataSource


class CachedMarketHours:
    """Broker mixin: one exchange-calendar build per UTC day instead of three a second.

    LumiBot's live loop checks the lifecycle hooks every second, asking
    `market_close_time()` twice and `market_open_time()` once; each answer
    rebuilt a full exchange calendar (holiday rules included) in
    `Broker.market_hours`. That alone kept a CPU core busy around the clock on a
    small server (2026-11-03). The answer depends only on the date passed, or
    today's UTC date, which is also the cache key, so the result is identical.
    """

    def market_hours(self, market="NASDAQ", close=True, next=False, date=None):
        moment = date if date is not None else datetime.now(timezone.utc)
        key = (getattr(self, "market", None) or market, bool(close), bool(next), pd.Timestamp(moment).date())
        cache = self.__dict__.setdefault("_market_hours_cache", {})
        if key not in cache:
            if len(cache) >= 32:
                cache.clear()  # Old days are never asked again; keep the cache small.
            cache[key] = super().market_hours(market=market, close=close, next=next, date=date)
        return cache[key]
from lumibot.entities import Bars, Quote

from .risk import MarketDataUnavailable, NoCompletedMinute, RiskError, instant, number
from .session_calendar import _session_bounds, _session_close, regular_session_open, completed_daily_cutoff


class SessionClock:
    def __init__(self, now=None):
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calendar = mcal.get_calendar("NYSE")
        self._schedule = None
        self._day = None

    def status(self):
        now = instant(self.now())
        day = now.astimezone(ZoneInfo("America/New_York")).date()
        if self._day != day:
            self._schedule = self.calendar.schedule(start_date=day - timedelta(days=14), end_date=day + timedelta(days=14))
            self._day = day
        for row in self._schedule.itertuples():
            if row.market_open <= now < row.market_close:
                return {"open": True, "next_open": row.market_open.isoformat(), "close": row.market_close.isoformat()}
            if row.market_open > now:
                return {"open": False, "next_open": row.market_open.isoformat(), "close": row.market_close.isoformat()}
        raise RiskError("NYSE calendar has no next session")

    def last_close(self):
        self.status()
        closes = self._schedule.market_close[self._schedule.market_close <= instant(self.now())]
        if closes.empty:
            raise RiskError("NYSE calendar has no completed session")
        return closes.iloc[-1].to_pydatetime()


class MarketDataSettings:
    """`market_data` section: optional live streaming quotes for local mode."""
    FIELDS = {"live_stream", "live_seconds"}

    def __init__(self, live_stream=False, live_seconds=60):
        if type(live_stream) is not bool:
            raise RiskError("market_data.live_stream must be true or false")
        if type(live_seconds) is not int or not 5 <= live_seconds <= 60:
            raise RiskError("market_data.live_seconds must be 5..60")
        self.live_stream, self.live_seconds = live_stream, live_seconds

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - cls.FIELDS
        if unknown:
            raise RiskError("Unknown market_data settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


class LiveTape:
    """Best-effort Yahoo streaming last trades (unofficial endpoint, no SLA).

    A background thread subscribes the most recently requested symbols within
    its capacity, retiring older subscriptions rather than ignoring new watches.
    Ticks keep their SOURCE time; a consumer decides freshness.
    Failures reconnect with backoff and are reported, never hidden: when no
    fresh tick exists the caller falls back to completed minute bars.
    """
    URL = "wss://streamer.finance.yahoo.com/?version=2"

    def __init__(self, connect=None, clock=time.time, max_symbols=60):
        if type(max_symbols) is not int or max_symbols < 1:
            raise RiskError("Stream capacity must be a positive integer")
        self.connect = connect or self._connect
        self.clock, self.max_symbols = clock, max_symbols
        self._ticks, self._wanted, self._subscribed = {}, OrderedDict(), set()
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread = None
        self.state = {"connected": False, "messages": 0, "errors": 0, "reconnects": 0, "last_error": None,
                      "subscription_evictions": 0}

    @classmethod
    def _connect(cls):
        from websockets.sync.client import connect
        return connect(cls.URL, open_timeout=10, close_timeout=2, max_size=2 ** 20)

    @staticmethod
    def yahoo(symbol):
        return symbol.replace(".", "-")

    def watch(self, symbols):
        with self._lock:
            for symbol in symbols:
                self._wanted[symbol] = None
                self._wanted.move_to_end(symbol)
                if len(self._wanted) > self.max_symbols:
                    retired, _ = self._wanted.popitem(last=False)
                    self._ticks.pop(retired, None)
                    self.state["subscription_evictions"] += 1
            running = self._thread is not None and self._thread.is_alive()
            if not running and not self._stop.is_set():
                # Under the lock: concurrent quote requests never start two streams.
                self._thread = threading.Thread(target=self._run, name="yahoo-live-tape", daemon=True)
                self._thread.start()

    def tick(self, symbol):
        with self._lock:
            row = self._ticks.get(symbol)
            return dict(row) if row else None

    def close(self):
        self._stop.set()

    def diagnostics(self):
        with self._lock:
            return {**self.state, "capacity": self.max_symbols, "requested": len(self._wanted),
                    "subscribed": len(self._subscribed), "symbols_with_ticks": len(self._ticks)}

    def handle(self, raw):
        """Decode one streamer frame (JSON wrapping base64 protobuf PricingData)."""
        import base64
        import json as _json
        from yfinance.pricing_pb2 import PricingData
        message = PricingData()
        message.ParseFromString(base64.b64decode(_json.loads(raw)["message"]))
        if not message.id or not message.price > 0 or not message.time:
            return
        stamp = datetime.fromtimestamp(int(message.time) / 1000, timezone.utc)
        row = {"price": number(str(message.price)), "time": stamp, "received": self.clock(),
               "bid": number(str(message.bid)) if message.bid > 0 else None,
               "ask": number(str(message.ask)) if message.ask > 0 else None,
               "day_volume": int(message.day_volume)}
        with self._lock:
            symbols = [s for s in self._wanted if self.yahoo(s) == message.id]
            if not symbols:
                return  # A late frame for a retired subscription must not refill the cache.
            for symbol in symbols:
                old = self._ticks.get(symbol)
                if old is None or stamp >= old["time"]:  # Out-of-order frames cannot roll the tape back.
                    self._ticks[symbol] = row
            self.state["messages"] += 1

    def _run(self):
        import json as _json
        failures = 0
        while not self._stop.is_set():
            client = None
            try:
                client = self.connect()
                with self._lock:
                    self.state["connected"], self._subscribed = True, set()
                while not self._stop.is_set():
                    with self._lock:
                        wanted = set(self._wanted)
                        # Multiple public spellings can share one wire subscription.
                        # Retiring one alias must not unsubscribe another active alias.
                        wanted_wire = {self.yahoo(s) for s in wanted}
                        subscribed_wire = {self.yahoo(s) for s in self._subscribed}
                        retired, new = subscribed_wire - wanted_wire, wanted_wire - subscribed_wire
                    if retired:
                        client.send(_json.dumps({"unsubscribe": sorted(retired)}))
                    if new:
                        client.send(_json.dumps({"subscribe": sorted(new)}))
                    with self._lock:
                        self._subscribed = wanted
                    try:
                        raw = client.recv(timeout=5)
                    except TimeoutError:
                        continue
                    self.handle(raw)
                    failures = 0
            except Exception as exc:
                failures += 1
                with self._lock:
                    self.state.update(connected=False, last_error=type(exc).__name__,
                                      errors=self.state["errors"] + 1, reconnects=self.state["reconnects"] + 1)
                self._stop.wait(min(60, 2 ** min(failures, 6)))
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass


class YahooRealtimeData(DataSource):
    """Fresh last trades and completed minute OHLCV, with a disclosed spread proxy.

    The local broker fills only against a NEW completed bar after submission.
    """
    SOURCE = "YAHOO_LOCAL_SIMULATION"
    MIN_TIMESTEP = "minute"
    IS_BACKTESTING_DATA_SOURCE = False
    TIMESTEP_MAPPING = [
        {"timestep": "day", "representations": ["1d", "day"]},
        {"timestep": "minute", "representations": ["1m", "minute"]},
    ]

    def __init__(self, *, now=None, fetch=None, spread_bps=10, tape=None, live_seconds=60, on_backoff=None):
        super().__init__(delay=0)
        # on_backoff(until_epoch): told when Yahoo starts failing, so the data desk can yield to trading.
        self.on_backoff = on_backoff
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.session = SessionClock(self.now)
        self.spread = number(spread_bps) / 10000
        if not 0 <= self.spread <= number("0.01"):
            raise RiskError("Spread proxy must be between zero and 100 basis points")
        self.fetch = fetch or self._fetch
        if fetch is None:
            import yfinance as yf
            cache_dir = Path(os.environ.get("LUMIBOT_CACHE_FOLDER", "artifacts/autonomous-fund/cache")) / "yfinance-live"
            cache_dir.mkdir(parents=True, exist_ok=True)
            yf.set_tz_cache_location(str(cache_dir.resolve()))
        self._cache = {}
        self._actions = {}
        self._request_lock = threading.RLock()
        self._key_locks = {}
        self._failures = {}
        self._source_errors = OrderedDict()
        self._slots = threading.BoundedSemaphore(2)
        self._stats = {"requests": 0, "cache_hits": 0, "failures": 0, "backoff_blocks": 0, "discarded_rows": 0}
        self._last_rejected_bar = None
        self.name = "yahoo-local-simulation"
        # Optional live tape: quotes prefer a fresh regular-session trade.
        self.tape, self.live_seconds = tape, live_seconds
        self._quote_kinds = {"live": 0, "completed_bar": 0}
        self._quote_observations = OrderedDict()
        self._chart_trades = OrderedDict()
        self._closing_quotes = OrderedDict()

    def diagnostics(self):
        with self._request_lock:
            result = {**self._stats, "cached_series": len(self._cache), "max_parallel_requests": 2,
                      "feed": ("stream_and_chart_last_trade_with_completed_bar_fallback" if self.tape
                               else "chart_last_trade_with_completed_bar_fallback"),
                      "quotes_by_kind": dict(self._quote_kinds),
                      "quotes_by_symbol": {s: dict(row) for s, row in self._quote_observations.items()},
                      "source_failures": [{**row, "retry_in_seconds": max(0, round(self._failures[key][1] - time.monotonic(), 1))}
                                          for key, row in self._source_errors.items() if key in self._failures]}
            if self._last_rejected_bar is not None:
                result["last_rejected_bar"] = dict(self._last_rejected_bar)
        if self.tape:
            result["live_tape"] = self.tape.diagnostics()
        return result

    @staticmethod
    def _fetch(symbol, interval, period):
        import yfinance as yf
        # Yahoo's native spelling for share classes; no strategy-specific ticker rule.
        ticker = yf.Ticker(symbol.replace(".", "-"))
        frame = ticker.history(period=period, interval=interval, auto_adjust=False,
                               actions=True, prepost=False, timeout=15, raise_errors=True)
        if interval == "1m":
            try:
                # The same chart response has a last trade even when its completed
                # minute rows lag or the symbol produces no websocket ticks. Read
                # only these keys; other metadata keys can trigger lazy downloads.
                metadata = ticker.get_history_metadata()
                frame.attrs["yahoo_last_trade"] = {"price": metadata.get("regularMarketPrice"),
                                                   "as_of": metadata.get("regularMarketTime")}
            except Exception:
                pass  # Optional quote evidence must not discard usable OHLCV.
        return frame

    def get_datetime(self, adjust_for_delay=False):
        return self.to_default_timezone(instant(self.now()))

    @staticmethod
    def _symbol(asset):
        if str(asset.asset_type) != "stock":
            raise RiskError("Local market data accepts US stocks/ETFs only")
        return asset.symbol

    def _history(self, asset, interval, period, required_through=None):
        symbol, now = self._symbol(asset), self.get_datetime()
        key = (symbol, interval, period)
        opened = self.session.status()["open"]
        cutoff = (completed_daily_cutoff(now) if interval == "1d" else
                  now.replace(second=0, microsecond=0) if opened else None)
        # A five-second broker poll does not require another identical history
        # download. Source timestamps, never cache age, still gate execution.
        ttl = (15 if opened else 300) if interval == "1m" else 900
        with self._request_lock:
            lock = self._key_locks.setdefault(key, threading.RLock())
        # Single flight per series; never hold a global lock during network I/O.
        with lock:
            cached = self._cache.get(key)
            through = ((cached[1].index[-1] + timedelta(minutes=1) if interval == "1m" else
                        cached[1].index[-1].date()) if cached else None)
            if cached and interval == "1m" and opened and through < cutoff:
                ttl = 5  # Retry a lagging source, bounded across quote consumers.
            # At a new minute, a cached mark can cross the freshness limit
            # before its 15-second TTL expires. Re-read actual source evidence;
            # the source stamp and the independent 60-second guard never move.
            if (cached and cached[2] == cutoff and time.monotonic() - cached[0] < ttl
                    and (required_through is None or (through is not None and through >= required_through))):
                self._stats["cache_hits"] += 1
                return cached[1].copy()
            failure = self._failures.get(key)
            if failure and time.monotonic() < failure[1]:
                self._stats["backoff_blocks"] += 1
                previous = self._source_errors.get(key, {})
                raise MarketDataUnavailable(f"Yahoo market data cooling down for {symbol}; no stale fallback", diagnostic={
                    "reason": "source_backoff", "symbol": symbol, "interval": interval, "period": period,
                    "checked_at": now.isoformat(), "last_error": previous.get("error"),
                    "retry_after_seconds": max(0, failure[1] - time.monotonic())})
            try:
                with self._slots:
                    self._stats["requests"] += 1
                    frame = self._download(symbol, interval, period, now)
            except NoCompletedMinute:
                with self._request_lock:
                    self._failures.pop(key, None)
                    self._source_errors.pop(key, None)
                raise
            except Exception as exc:
                count = (failure[0] if failure else 0) + 1
                delay = min(300, 15 * 2 ** min(count - 1, 5))
                error = str(exc) if isinstance(exc, RiskError) else type(exc).__name__
                diagnostic = {"reason": "source_unavailable", "symbol": symbol, "interval": interval,
                              "period": period, "failed_at": self.get_datetime().isoformat(), "error": error,
                              "consecutive_failures": count}
                with self._request_lock:
                    self._failures[key] = (count, time.monotonic() + delay)
                    self._stats["failures"] += 1
                    self._source_errors[key] = diagnostic
                    self._source_errors.move_to_end(key)
                    while len(self._source_errors) > 60:
                        self._source_errors.popitem(last=False)
                if self.on_backoff is not None:
                    try:
                        self.on_backoff(time.time() + delay)
                    except Exception:
                        pass  # Advisory only; never changes this request's outcome.
                if isinstance(exc, RiskError):
                    raise MarketDataUnavailable(str(exc), diagnostic=diagnostic) from None
                raise MarketDataUnavailable(f"Yahoo market data unavailable for {symbol} ({type(exc).__name__})",
                                            diagnostic=diagnostic) from None
            with self._request_lock:
                self._failures.pop(key, None)
                self._source_errors.pop(key, None)
            self._cache[key] = (time.monotonic(), frame.copy(), cutoff)
            return frame

    def _download(self, symbol, interval, period, now):
        frame = self.fetch(symbol, interval, period).copy()
        # Network and slot waits may cross a minute/close boundary. Trim forming
        # bars against the receipt clock, never an earlier request's clock.
        now = self.get_datetime()
        if interval == "1m" and isinstance(frame, pd.DataFrame):
            # Last-trade evidence is independent of OHLCV completeness. Keep it
            # before rejecting missing/forming/broken bars, with its source time.
            self._retain_chart_trade(symbol, frame, now)
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise RiskError(f"Yahoo returned empty or undated data for {symbol}")
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise RiskError("Market history timestamps must be unique and ordered")
        frame = frame.rename(columns=str.lower).rename(columns={"dividends": "dividend", "stock splits": "split"})
        frame.index = frame.index.tz_convert("America/New_York")
        if interval == "1d":
            actions = []
            for stamp, row in frame[frame.index.date <= now.date()].iterrows():
                values = {}
                for field in ("split", "dividend"):
                    try:
                        values[field] = number(row.get(field, 0))
                        if values[field] < 0:
                            raise RiskError("Negative action")
                    except RiskError:
                        raise RiskError(f"Invalid corporate action for {symbol}: {field} at {stamp.isoformat()}") from None
                split, dividend = values["split"], values["dividend"]
                if split or dividend:
                    actions.append({"date": stamp.date().isoformat(), "split": str(split), "dividend": str(dividend)})
            self._actions[(symbol, period)] = actions
        required = ["open", "high", "low", "close", "volume"]
        if any(name not in frame for name in required):
            raise RiskError("Market history is missing OHLCV columns")
        # Source timestamps identify starts. Do not expose a forming close.
        if interval == "1m":
            frame = frame[frame.index + pd.Timedelta(minutes=1) <= now]
        else:
            frame = frame[frame.index.date < completed_daily_cutoff(now)]
        if frame.empty:
            if interval == "1m" and period == "1d":
                raise NoCompletedMinute("No completed market observations")
            raise RiskError("No completed market observations")
        valid = pd.Series(True, index=frame.index)
        fields_valid = {}
        for column in required:
            booleans = frame[column].map(lambda v: isinstance(v, (bool, np.bool_)))
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
            fields_valid[column] = np.isfinite(frame[column]) & ~booleans & (frame[column] >= 0 if column == "volume" else frame[column] > 0)
            valid &= fields_valid[column]
        fields_valid["ohlc_envelope"] = ((frame.low <= frame[["open", "close"]].min(axis=1)) &
                                          (frame.high >= frame[["open", "close"]].max(axis=1)))
        valid &= fields_valid["ohlc_envelope"]
        if not valid.all():
            bad_at = frame.index[~valid][-1]
            rejected_fields = [name for name, good in fields_valid.items() if not good.loc[bad_at]]
            with self._request_lock:
                self._stats["discarded_rows"] += int((~valid).sum())
                self._last_rejected_bar = {"symbol": symbol, "interval": interval, "as_of": bad_at.isoformat(),
                                           "reason": "invalid_ohlcv", "discarded_rows": int((~valid).sum()),
                                           "fields": rejected_fields}
            # A broken row is a gap, never a zero-volume or forward-filled bar.
            # Other real bars remain usable, with their own original timestamps.
            frame = frame.loc[valid].copy()
            if frame.empty:
                fields = ",".join(rejected_fields)
                raise RiskError(f"No valid completed OHLCV observations for {symbol} ({interval}); invalid fields: {fields}")
        return frame

    def get_historical_prices(self, asset, length, timestep="minute", timeshift=None, quote=None,
                              exchange=None, include_after_hours=False, **kwargs):
        # Native recursive indicators request 10,000 bars for warmup. Return
        # however many completed source bars exist instead of rejecting that
        # request or manufacturing enough rows to satisfy it.
        if type(length) is not int or length < 1:
            raise RiskError("History length must be a positive integer")
        if timestep not in {"day", "minute", "1d", "1m"}:
            raise RiskError("Local source supports completed daily and minute bars")
        daily = timestep in {"day", "1d"}
        period = ("6mo" if length <= 100 else "max") if daily else "5d"
        frame = self._history(asset, "1d" if daily else "1m", period)
        if timeshift:
            if not isinstance(timeshift, timedelta):
                raise RiskError("History timeshift must be a timedelta")
            frame = frame[frame.index <= self.get_datetime() - timeshift]
        return Bars(frame.tail(length).copy(), self.SOURCE, asset)

    def execution_bars(self, asset, *, required_through=None):
        return self._history(asset, "1m", "5d", required_through=required_through)

    def get_closing_quote(self, asset, closed_at):
        """A completed session's unadjusted close, from its final minute or its daily bar.

        Daily evidence is independent of intraday coverage. It is admitted only
        after the actual exchange close, for exactly that session's date. Never
        relabel an earlier minute, a prior daily close or an extended-hours tick.
        """
        closed_at = instant(closed_at)
        day = closed_at.astimezone(ZoneInfo("America/New_York")).date()
        now = self.get_datetime()
        if _session_close(day) != closed_at or now < closed_at:
            raise MarketDataUnavailable("Closing price requires an already completed exchange session")
        key = (self._symbol(asset), closed_at)
        with self._request_lock:
            cached = self._closing_quotes.get(key)
            if cached is not None:
                self._closing_quotes.move_to_end(key)
                return deepcopy(cached)
        row, start, kind = None, None, None
        try:
            minute = self.execution_bars(asset, required_through=closed_at)
            exact = minute[minute.index + timedelta(minutes=1) == closed_at]
            if not exact.empty:
                row, start, kind = exact.iloc[-1], exact.index[-1], "completed_minute_close"
        except MarketDataUnavailable:
            pass  # A real daily close can remain available during minute backoff.
        if row is None:
            try:
                daily = self._history(asset, "1d", "6mo", required_through=day)
                exact = daily[daily.index.date == day]
                if not exact.empty:
                    row, start, kind = exact.iloc[-1], exact.index[-1], "completed_daily_close"
            except MarketDataUnavailable:
                pass
        if row is None:
            raise MarketDataUnavailable(
                f"Closing minute unavailable for {asset.symbol} at {closed_at.isoformat()}; "
                "completed daily close also unavailable; retaining the last observed valuation",
                diagnostic={"reason": "closing_price_unavailable", "symbol": asset.symbol,
                            "session_close": closed_at.isoformat(), "checked_at": self.get_datetime().isoformat()})
        price = float(number(row.close))
        result = Quote(asset, price=price, bid=price, ask=price, timestamp=closed_at, volume=float(row.volume),
                       raw_data={"original_response": SimpleNamespace(timestamp=closed_at), "source": self.SOURCE,
                                 "price_kind": kind, "bar_start": start.isoformat(),
                                 "session_close": closed_at.isoformat(), "observed_at": self.get_datetime().isoformat()})
        if kind == "completed_daily_close":
            # The forced daily refresh may reveal a split/dividend that the
            # earlier action check did not yet see. Keep the same response's
            # normalized actions with its unadjusted close for reconciliation.
            result.raw_data["corporate_actions"] = deepcopy(self._actions.get((asset.symbol, "6mo"), []))
        with self._request_lock:
            # A valid recovered close is frozen just like its published NAV;
            # repeated native balance reads need not retry missing minutes all night.
            retained = self._closing_quotes.setdefault(key, deepcopy(result))
            self._closing_quotes.move_to_end(key)
            while len(self._closing_quotes) > 60:
                self._closing_quotes.popitem(last=False)
        return deepcopy(retained)

    def session_bars(self, asset):
        """Today's completed minutes only: a fifth of execution_bars' download, for research views."""
        return self._history(asset, "1m", "1d")

    def corporate_actions(self, asset, since=None):
        period = "max" if since and instant(since) < self.get_datetime() - timedelta(days=150) else "6mo"
        self._history(asset, "1d", period)
        return list(self._actions.get((asset.symbol, period), []))

    def history_actions(self, asset, length):
        """Actions from the same Yahoo series used by a daily research-history request."""
        period = "6mo" if length <= 100 else "max"
        self._history(asset, "1d", period)
        return list(self._actions.get((asset.symbol, period), []))

    def get_quote(self, asset, quote=None, exchange=None):
        if self.tape is not None:
            # A failed history request must not prevent a new watch from subscribing.
            self.tape.watch([self._symbol(asset)])
        try:
            frame = self.execution_bars(asset)
        except MarketDataUnavailable:
            # Observing a real fresh trade does not require minute history. Matching
            # still uses execution_bars independently and cannot manufacture a fill.
            live = self._live_quote(asset, None, None) if self.tape is not None else None
            if live is not None:
                return self._remember_quote(asset, live)
            chart = self._chart_quote(asset, None, None)
            if chart is not None:
                return self._remember_quote(asset, chart)
            raise
        start, last = frame.index[-1], frame.iloc[-1]
        end = start + pd.Timedelta(minutes=1)
        if self.tape is not None:
            live = self._live_quote(asset, end, last)
            if live is not None:
                return self._remember_quote(asset, live, frame)
        chart = self._chart_quote(asset, frame, end)
        if chart is not None:
            return self._remember_quote(asset, chart, frame)
        self._quote_kinds["completed_bar"] += 1
        price = number(last.close)
        result = Quote(asset, price=float(price), bid=float(price * (1 - self.spread / 2)),
                     ask=float(price * (1 + self.spread / 2)), timestamp=end,
                     volume=float(last.volume), raw_data={
                         "original_response": SimpleNamespace(timestamp=end), "source": self.SOURCE,
                         "price_kind": "completed_minute_close_with_spread_proxy", "bar_start": start.isoformat(),
                         "spread_bps": str(self.spread * 10000)})
        return self._remember_quote(asset, result, frame)

    @staticmethod
    def _trade_stamp(raw_stamp):
        return (instant(raw_stamp) if isinstance(raw_stamp, (datetime, str)) else
                datetime.fromtimestamp(float(number(raw_stamp)), timezone.utc))

    def _parse_chart_trade(self, trade):
        if not isinstance(trade, dict):
            return None
        try:
            price, stamp = number(trade.get("price")), self._trade_stamp(trade.get("as_of"))
            if price > 0 and np.isfinite(float(price)):
                return {"price": price, "as_of": stamp}
        except (ValueError, TypeError, OverflowError):
            pass
        return None

    def _retain_chart_trade(self, symbol, frame, now):
        trade = self._parse_chart_trade(frame.attrs.get("yahoo_last_trade"))
        session = self.session.status()
        if (trade is None or not session["open"]
                or not 0 <= (now - trade["as_of"]).total_seconds() <= self.live_seconds
                or trade["as_of"] < instant(session["next_open"])):
            return
        with self._request_lock:
            previous = self._chart_trades.get(symbol)
            if previous is None or trade["as_of"] >= previous["as_of"]:
                self._chart_trades[symbol] = trade
            self._chart_trades.move_to_end(symbol)
            while len(self._chart_trades) > 60:
                self._chart_trades.popitem(last=False)

    def _remember_quote(self, asset, quote, frame=None):
        """Bounded per-symbol source clocks: totals cannot explain a missing stream symbol."""
        now = self.get_datetime()
        stamp = quote.raw_data["original_response"].timestamp
        detail = {"price_kind": quote.raw_data["price_kind"], "source_as_of": stamp.isoformat(),
                  "checked_at": now.isoformat(), "age_seconds": (now - stamp).total_seconds(),
                  "stream_as_of": None, "chart_as_of": None}
        tick = self.tape.tick(self._symbol(asset)) if self.tape is not None else None
        if tick:
            detail.update(stream_as_of=tick["time"].isoformat(), stream_age_seconds=(now - tick["time"]).total_seconds())
        trade = frame.attrs.get("yahoo_last_trade") if frame is not None else None
        if quote.raw_data["price_kind"] == "chart_last_trade_with_spread_proxy":
            trade = {"as_of": stamp}
        elif trade is None:
            with self._request_lock:
                trade = self._chart_trades.get(asset.symbol)
        if isinstance(trade, dict):
            try:
                chart_stamp = self._trade_stamp(trade.get("as_of"))
                detail.update(chart_as_of=chart_stamp.isoformat(), chart_age_seconds=(now - chart_stamp).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass  # Never serialize an invalid provider field or guess its timestamp.
        with self._request_lock:
            self._quote_observations[asset.symbol] = detail
            self._quote_observations.move_to_end(asset.symbol)
            while len(self._quote_observations) > 60:
                self._quote_observations.popitem(last=False)
        return quote

    def _chart_quote(self, asset, frame, bar_end):
        """Use the chart's own last-trade price/time pair, never a forming bar's close."""
        now, session = self.get_datetime(), self.session.status()
        if not session["open"]:
            return None
        with self._request_lock:
            candidates = [self._chart_trades.get(asset.symbol)]
        if frame is not None:
            candidates.append(frame.attrs.get("yahoo_last_trade"))
        usable = []
        for candidate in candidates:
            trade = self._parse_chart_trade(candidate)
            if (trade is not None and 0 <= (now - trade["as_of"]).total_seconds() <= self.live_seconds
                    and (bar_end is None or trade["as_of"] > bar_end)
                    and trade["as_of"] >= instant(session["next_open"])):
                usable.append(trade)
        if not usable:
            return None
        latest = max(usable, key=lambda trade: trade["as_of"])
        price, stamp = latest["price"], latest["as_of"]
        self._quote_kinds["chart_quote"] = self._quote_kinds.get("chart_quote", 0) + 1
        return Quote(asset, price=float(price), bid=float(price * (1 - self.spread / 2)),
                     ask=float(price * (1 + self.spread / 2)), timestamp=stamp, volume=None, raw_data={
                         "original_response": SimpleNamespace(timestamp=stamp), "source": "YAHOO_CHART_UNOFFICIAL",
                         "price_kind": "chart_last_trade_with_spread_proxy", "spread_bps": str(self.spread * 10000)})

    def _live_quote(self, asset, bar_end, last):
        """A fresh regular-session trade, newer than the last completed bar when available."""
        tick = self.tape.tick(self._symbol(asset))
        now, session = self.get_datetime(), self.session.status()
        if not tick or not session["open"]:
            return None
        stamp = tick["time"]
        age = (now - stamp).total_seconds()
        if (not 0 <= age <= self.live_seconds or (bar_end is not None and stamp <= bar_end)
                or stamp < instant(session["next_open"])):
            return None
        price, bid, ask = tick["price"], tick["bid"], tick["ask"]
        if bid and ask and bid <= price <= ask and ask / bid <= number("1.02"):
            kind = "live_trade_with_streamed_bid_ask"
        else:
            bid, ask = price * (1 - self.spread / 2), price * (1 + self.spread / 2)
            kind = "live_trade_with_spread_proxy"
        self._quote_kinds["live"] += 1
        return Quote(asset, price=float(price), bid=float(bid), ask=float(ask), timestamp=stamp,
                     volume=float(last.volume) if last is not None else None, raw_data={
                         "original_response": SimpleNamespace(timestamp=stamp), "source": "YAHOO_STREAM_UNOFFICIAL",
                         "price_kind": kind, "spread_bps": str(self.spread * 10000),
                         "day_volume": tick["day_volume"], "received_epoch": tick["received"]})

    def get_last_price(self, asset, quote=None, exchange=None, **kwargs):
        return self.get_quote(asset).price

    def get_chains(self, asset, quote=None):
        raise NotImplementedError("Derivatives are outside this simulator")
