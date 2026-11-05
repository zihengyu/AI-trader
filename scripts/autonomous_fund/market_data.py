"""Live Yahoo adapter for the local simulator; no brokerage credentials."""
import threading
import time
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

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

from .risk import RiskError, instant, number


@lru_cache(maxsize=32)
def _session_close(day):
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    return schedule.iloc[0].market_close if not schedule.empty else None


def completed_daily_cutoff(now):
    """Exclusive New York date bound: today's bar becomes usable at its actual exchange close."""
    now = instant(now)
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    close = _session_close(day)
    return day + timedelta(days=1) if close is not None and now >= close else day


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
            self._schedule = self.calendar.schedule(start_date=day, end_date=day + timedelta(days=14))
            self._day = day
        for row in self._schedule.itertuples():
            if row.market_open <= now < row.market_close:
                return {"open": True, "next_open": row.market_open.isoformat(), "close": row.market_close.isoformat()}
            if row.market_open > now:
                return {"open": False, "next_open": row.market_open.isoformat(), "close": row.market_close.isoformat()}
        raise RiskError("NYSE calendar has no next session")


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

    A background thread keeps one websocket subscription for every symbol that
    has been quoted. Ticks keep their SOURCE time; a consumer decides freshness.
    Failures reconnect with backoff and are reported, never hidden: when no
    fresh tick exists the caller falls back to completed minute bars.
    """
    URL = "wss://streamer.finance.yahoo.com/?version=2"

    def __init__(self, connect=None, clock=time.time, max_symbols=60):
        self.connect = connect or self._connect
        self.clock, self.max_symbols = clock, max_symbols
        self._ticks, self._wanted, self._subscribed = {}, set(), set()
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread = None
        self.state = {"connected": False, "messages": 0, "errors": 0, "reconnects": 0, "last_error": None}

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
                if len(self._wanted) < self.max_symbols:
                    self._wanted.add(symbol)
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
            return {**self.state, "subscribed": len(self._subscribed), "symbols_with_ticks": len(self._ticks)}

    def handle(self, raw):
        """Decode one streamer frame (JSON wrapping base64 protobuf PricingData)."""
        import base64
        import json as _json
        from yfinance.pricing_pb2 import PricingData
        message = PricingData()
        message.ParseFromString(base64.b64decode(_json.loads(raw)["message"]))
        if not message.id or not message.price > 0 or not message.time:
            return
        with self._lock:
            wanted = set(self._wanted)
        symbol = next((s for s in wanted if self.yahoo(s) == message.id), message.id.replace("-", "."))
        stamp = datetime.fromtimestamp(int(message.time) / 1000, timezone.utc)
        row = {"price": number(str(message.price)), "time": stamp, "received": self.clock(),
               "bid": number(str(message.bid)) if message.bid > 0 else None,
               "ask": number(str(message.ask)) if message.ask > 0 else None,
               "day_volume": int(message.day_volume)}
        with self._lock:
            old = self._ticks.get(symbol)
            if old is None or stamp >= old["time"]:  # Never let an out-of-order frame roll the tape back.
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
                        new = self._wanted - self._subscribed
                    if new:
                        client.send(_json.dumps({"subscribe": sorted(self.yahoo(s) for s in new)}))
                        with self._lock:
                            self._subscribed |= new
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
    """Completed minute OHLCV, explicitly NOT an exchange bid/ask quote feed.

    Quotes use a disclosed spread proxy around the last completed bar's close.
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
        self._slots = threading.BoundedSemaphore(2)
        self._stats = {"requests": 0, "cache_hits": 0, "failures": 0, "backoff_blocks": 0}
        self.name = "yahoo-local-simulation"
        # Optional live tape: quotes prefer a fresh regular-session trade.
        self.tape, self.live_seconds = tape, live_seconds
        self._quote_kinds = {"live": 0, "completed_bar": 0}

    def diagnostics(self):
        with self._request_lock:
            result = {**self._stats, "cached_series": len(self._cache), "max_parallel_requests": 2,
                      "feed": "live_trades_with_completed_bar_fallback" if self.tape else "completed_minute_bars_not_ticks",
                      "quotes_by_kind": dict(self._quote_kinds)}
        if self.tape:
            result["live_tape"] = self.tape.diagnostics()
        return result

    @staticmethod
    def _fetch(symbol, interval, period):
        import yfinance as yf
        # Yahoo's native spelling for share classes; no strategy-specific ticker rule.
        ticker = yf.Ticker(symbol.replace(".", "-"))
        return ticker.history(period=period, interval=interval, auto_adjust=False,
                              actions=True, prepost=False, timeout=15, raise_errors=True)

    def get_datetime(self, adjust_for_delay=False):
        return self.to_default_timezone(instant(self.now()))

    @staticmethod
    def _symbol(asset):
        if str(asset.asset_type) != "stock":
            raise RiskError("Local market data accepts US stocks/ETFs only")
        return asset.symbol

    def _history(self, asset, interval, period):
        symbol, now = self._symbol(asset), self.get_datetime()
        key = (symbol, interval, period)
        cutoff = completed_daily_cutoff(now) if interval == "1d" else None
        # A five-second broker poll does not require another identical history
        # download. Source timestamps, never cache age, still gate execution.
        ttl = (15 if self.session.status()["open"] else 300) if interval == "1m" else 900
        with self._request_lock:
            lock = self._key_locks.setdefault(key, threading.RLock())
        # Single flight per series; never hold a global lock during network I/O.
        with lock:
            cached = self._cache.get(key)
            if cached and cached[2] == cutoff and time.monotonic() - cached[0] < ttl:
                self._stats["cache_hits"] += 1
                return cached[1].copy()
            failure = self._failures.get(key)
            if failure and time.monotonic() < failure[1]:
                self._stats["backoff_blocks"] += 1
                raise RiskError(f"Yahoo market data cooling down for {symbol}; no stale fallback")
            try:
                with self._slots:
                    self._stats["requests"] += 1
                    frame = self._download(symbol, interval, period, now)
            except Exception as exc:
                count = (failure[0] if failure else 0) + 1
                delay = min(300, 15 * 2 ** min(count - 1, 5))
                self._failures[key] = (count, time.monotonic() + delay)
                self._stats["failures"] += 1
                if self.on_backoff is not None:
                    try:
                        self.on_backoff(time.time() + delay)
                    except Exception:
                        pass  # Advisory only; never changes this request's outcome.
                if isinstance(exc, RiskError):
                    raise
                raise RiskError(f"Yahoo market data unavailable for {symbol} ({type(exc).__name__})") from None
            self._failures.pop(key, None)
            self._cache[key] = (time.monotonic(), frame.copy(), cutoff)
            return frame

    def _download(self, symbol, interval, period, now):
        frame = self.fetch(symbol, interval, period).copy()
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise RiskError(f"Yahoo returned empty or undated data for {symbol}")
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise RiskError("Market history timestamps must be unique and ordered")
        frame = frame.rename(columns=str.lower).rename(columns={"dividends": "dividend", "stock splits": "split"})
        frame.index = frame.index.tz_convert("America/New_York")
        if interval == "1d":
            actions = []
            for stamp, row in frame[frame.index.date <= now.date()].iterrows():
                split, dividend = number(row.get("split", 0)), number(row.get("dividend", 0))
                if split < 0 or dividend < 0:
                    raise RiskError("Invalid corporate action")
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
            raise RiskError("No completed market observations")
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if any(not number(value).is_finite() for value in frame[column]):
                raise RiskError("Non-finite market data")
        if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame.volume < 0).any():
            raise RiskError("Invalid prices or negative volume")
        if (frame.low > frame[["open", "close"]].min(axis=1)).any() or (frame.high < frame[["open", "close"]].max(axis=1)).any():
            raise RiskError("Invalid market OHLC envelope")
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

    def execution_bars(self, asset):
        return self._history(asset, "1m", "5d")

    def session_bars(self, asset):
        """Today's completed minutes only: a fifth of execution_bars' download, for research views."""
        return self._history(asset, "1m", "1d")

    def corporate_actions(self, asset, since=None):
        period = "max" if since and instant(since) < self.get_datetime() - timedelta(days=150) else "6mo"
        self._history(asset, "1d", period)
        return list(self._actions.get((asset.symbol, period), []))

    def get_quote(self, asset, quote=None, exchange=None):
        frame = self.execution_bars(asset)
        start, last = frame.index[-1], frame.iloc[-1]
        end = start + pd.Timedelta(minutes=1)
        if self.tape is not None:
            self.tape.watch([self._symbol(asset)])
            live = self._live_quote(asset, end, last)
            if live is not None:
                return live
        self._quote_kinds["completed_bar"] += 1
        price = number(last.close)
        return Quote(asset, price=float(price), bid=float(price * (1 - self.spread / 2)),
                     ask=float(price * (1 + self.spread / 2)), timestamp=end,
                     volume=float(last.volume), raw_data={
                         "original_response": SimpleNamespace(timestamp=end), "source": self.SOURCE,
                         "price_kind": "completed_minute_close_with_spread_proxy", "bar_start": start.isoformat(),
                         "spread_bps": str(self.spread * 10000)})

    def _live_quote(self, asset, bar_end, last):
        """A streamed trade inside the open regular session, newer than the last bar and fresh."""
        tick = self.tape.tick(self._symbol(asset))
        now, session = self.get_datetime(), self.session.status()
        if not tick or not session["open"]:
            return None
        stamp = tick["time"]
        age = (now - stamp).total_seconds()
        if not 0 <= age <= self.live_seconds or stamp <= bar_end or stamp < instant(session["next_open"]):
            return None
        price, bid, ask = tick["price"], tick["bid"], tick["ask"]
        if bid and ask and bid <= price <= ask and ask / bid <= number("1.02"):
            kind = "live_trade_with_streamed_bid_ask"
        else:
            bid, ask = price * (1 - self.spread / 2), price * (1 + self.spread / 2)
            kind = "live_trade_with_spread_proxy"
        self._quote_kinds["live"] += 1
        return Quote(asset, price=float(price), bid=float(bid), ask=float(ask), timestamp=stamp,
                     volume=float(last.volume), raw_data={
                         "original_response": SimpleNamespace(timestamp=stamp), "source": "YAHOO_STREAM_UNOFFICIAL",
                         "price_kind": kind, "spread_bps": str(self.spread * 10000),
                         "day_volume": tick["day_volume"], "received_epoch": tick["received"]})

    def get_last_price(self, asset, quote=None, exchange=None, **kwargs):
        return self.get_quote(asset).price

    def get_chains(self, asset, quote=None):
        raise NotImplementedError("Derivatives are outside this simulator")
