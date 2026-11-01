"""Live Yahoo adapter for the local simulator; no brokerage credentials."""
import threading
import time
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

from lumibot.data_sources import DataSource
from lumibot.entities import Bars, Quote

from .risk import RiskError, instant, number


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

    def __init__(self, *, now=None, fetch=None, spread_bps=10):
        super().__init__(delay=0)
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

    def diagnostics(self):
        with self._request_lock:
            return {**self._stats, "cached_series": len(self._cache), "max_parallel_requests": 2,
                    "feed": "completed_minute_bars_not_ticks"}

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
        # A five-second broker poll does not require another identical history
        # download. Source timestamps, never cache age, still gate execution.
        ttl = (15 if self.session.status()["open"] else 300) if interval == "1m" else 900
        with self._request_lock:
            lock = self._key_locks.setdefault(key, threading.RLock())
        # Single flight per series; never hold a global lock during network I/O.
        with lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < ttl:
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
                self._failures[key] = (count, time.monotonic() + min(300, 15 * 2 ** min(count - 1, 5)))
                self._stats["failures"] += 1
                if isinstance(exc, RiskError):
                    raise
                raise RiskError(f"Yahoo market data unavailable for {symbol} ({type(exc).__name__})") from None
            self._failures.pop(key, None)
            self._cache[key] = (time.monotonic(), frame.copy())
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
            frame = frame[frame.index.date < now.date()]
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

    def corporate_actions(self, asset, since=None):
        period = "max" if since and instant(since) < self.get_datetime() - timedelta(days=150) else "6mo"
        self._history(asset, "1d", period)
        return list(self._actions.get((asset.symbol, period), []))

    def get_quote(self, asset, quote=None, exchange=None):
        frame = self.execution_bars(asset)
        start, last = frame.index[-1], frame.iloc[-1]
        end = start + pd.Timedelta(minutes=1)
        price = number(last.close)
        return Quote(asset, price=float(price), bid=float(price * (1 - self.spread / 2)),
                     ask=float(price * (1 + self.spread / 2)), timestamp=end,
                     volume=float(last.volume), raw_data={
                         "original_response": SimpleNamespace(timestamp=end), "source": self.SOURCE,
                         "price_kind": "completed_minute_close_with_spread_proxy", "bar_start": start.isoformat(),
                         "spread_bps": str(self.spread * 10000)})

    def get_last_price(self, asset, quote=None, exchange=None, **kwargs):
        return self.get_quote(asset).price

    def get_chains(self, asset, quote=None):
        raise NotImplementedError("Derivatives are outside this simulator")
