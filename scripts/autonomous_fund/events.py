"""Scheduled event risk: an earnings announcement caps a single stock's weight beforehand.

A stop-loss cannot protect against an overnight gap, and earnings are the most
common scheduled cause of one. When a stock in the universe reports earnings
today or within `earnings_window_days` NYSE sessions, its weight is capped at
`earnings_max_position` until the date has passed; the risk engine sells the
excess automatically, like any other reduction. ETFs are not capped.

Dates come from a calendar source (Yahoo by default, Finnhub when
FINNHUB_API_KEY is set and `source` allows it), fetched once per symbol per day
and stored in the journal. An unknown date is never guessed: the name keeps its
normal cap and is listed as `earnings_unknown`. Only live (local/paper) runs
apply the guard: a historical run would need the calendar as it was known then.
"""
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from .risk import RiskError, number

SOURCES = ("auto", "yahoo", "finnhub")
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"


@dataclass(frozen=True)
class EventSettings:
    enabled: bool = True
    earnings_max_position: Decimal = Decimal("0.05")
    earnings_window_days: int = 1
    source: str = "auto"

    def __post_init__(self):
        object.__setattr__(self, "earnings_max_position", number(self.earnings_max_position))
        if type(self.enabled) is not bool:
            raise RiskError("events.enabled must be true/false")
        if not Decimal("0") <= self.earnings_max_position <= Decimal("1"):
            raise RiskError("events.earnings_max_position must be 0..1")
        if type(self.earnings_window_days) is not int or not 0 <= self.earnings_window_days <= 5:
            raise RiskError("events.earnings_window_days must be 0..5 NYSE sessions")
        if self.source not in SOURCES:
            raise RiskError("events.source must be one of: " + ", ".join(SOURCES))

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown events settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def yahoo_dates(symbol):
    """Upcoming earnings dates from Yahoo's calendar (unofficial, best effort)."""
    import yfinance
    calendar = yfinance.Ticker(symbol.replace(".", "-")).calendar or {}
    values = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
    if values is None:
        return []
    values = values if isinstance(values, (list, tuple)) else [values]
    return sorted({_day(v).isoformat() for v in values if v is not None})


def finnhub_dates(symbol, today, key, get):
    response = get(FINNHUB_URL, params={"symbol": symbol, "from": today.isoformat(),
                                        "to": (today + timedelta(days=100)).isoformat(), "token": key}, timeout=10)
    response.raise_for_status()
    rows = (response.json() or {}).get("earningsCalendar") or []
    return sorted({_day(row["date"]).isoformat() for row in rows if row.get("date")})


def bounded(function, seconds):
    """function() with a time limit; the Yahoo calendar call takes no timeout of its own.

    A daemon thread, so a hung request can never keep the process alive; it is
    abandoned and the symbol's date is reported unavailable.
    """
    import threading
    outcome = {}

    def target():
        try:
            outcome["value"] = function()
        except Exception as exc:  # Reported to the caller below.
            outcome["error"] = exc
    worker = threading.Thread(target=target, name="earnings-calendar", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"calendar request exceeded {seconds} seconds")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def within(event_day, today, window_days):
    """Today through the next `window_days` NYSE sessions; zero means today only."""
    event_day, today = _day(event_day), _day(today)
    if event_day < today:
        return False
    from .market_data import _session_close
    last, remaining = today, window_days
    while remaining > 0:
        last += timedelta(days=1)
        if _session_close(last) is not None:
            remaining -= 1
    return event_day <= last


class EarningsCalendar:
    def __init__(self, journal, settings, environ=None, get=None, yahoo=None, timeout_seconds=10, store=None):
        import requests
        self.journal, self.settings, self.timeout_seconds, self.store = journal, settings, timeout_seconds, store
        self.environ = os.environ if environ is None else environ
        self.get, self.yahoo = get or requests.get, yahoo or yahoo_dates
        if journal is None:  # The prefetch job: fetch() only, results go to the daily store.
            return
        with journal.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS fund_earnings (symbol TEXT NOT NULL, day TEXT NOT NULL,
                source TEXT, dates TEXT, error TEXT, PRIMARY KEY (symbol, day))""")

    def source(self):
        key = str(self.environ.get("FINNHUB_API_KEY", "")).strip()
        if self.settings.source == "finnhub" or (self.settings.source == "auto" and key):
            if not key:
                raise RiskError("events.source finnhub needs FINNHUB_API_KEY")
            return "finnhub"
        return "yahoo"

    def fetch(self, symbol, today):
        """(source, dates or None, error or None) for one symbol from the configured calendar."""
        source = dates = error = None
        try:
            source = self.source()
            dates = (finnhub_dates(symbol, today, self.environ["FINNHUB_API_KEY"], self.get)
                     if source == "finnhub" else bounded(lambda: self.yahoo(symbol), self.timeout_seconds))
        except Exception as exc:
            error = str(exc)[:200] if isinstance(exc, RiskError) else type(exc).__name__
        return source, dates, error

    def dates(self, symbols, today, may_fetch=None):
        """{symbol: {"dates": [...], "source": s} | {"unavailable": error}}; one fetch per symbol per day.

        may_fetch(): when it returns False, symbols not fetched yet today are left
        `deferred` (listed in self.deferred) for a later call instead of blocking.
        """
        today = _day(today)
        self.deferred = []
        with self.journal.connection() as db:
            stored = {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM fund_earnings WHERE day=?",
                                                               (today.isoformat(),))}
        result = {}
        prefetched = (self.store.get(today.isoformat(), "earnings") if self.store is not None else None) or {}
        for symbol in sorted(symbols):
            row = stored.get(symbol)
            item = prefetched.get(symbol)
            if item is not None and (row is None or not item.get("error")):
                # Kept current by the data desk (or a prefetch): newer than this process's own fetch.
                row = {"source": item.get("source"), "error": item.get("error"),
                       "dates": json.dumps(item["dates"]) if item.get("dates") is not None else None}
            if row is None and may_fetch is not None and not may_fetch():
                self.deferred.append(symbol)
                result[symbol] = {"unavailable": "deferred"}
                continue
            if row is None:
                source, dates, error = self.fetch(symbol, today)
                with self.journal.connection() as db:
                    db.execute("INSERT OR REPLACE INTO fund_earnings VALUES (?,?,?,?,?)",
                               (symbol, today.isoformat(), source, json.dumps(dates) if dates is not None else None,
                                error))
                row = {"source": source, "dates": json.dumps(dates) if dates is not None else None, "error": error}
            if row["error"] or row["dates"] is None:
                result[symbol] = {"unavailable": row["error"] or "no data"}
            else:
                result[symbol] = {"dates": json.loads(row["dates"]), "source": row["source"]}
        return result

    def caps(self, stocks, today, may_fetch=None):
        """(caps, upcoming, unknown): the weight cap per stock reporting inside the window."""
        caps, upcoming, unknown = {}, {}, []
        for symbol, item in self.dates(stocks, today, may_fetch).items():
            if "unavailable" in item:
                unknown.append(symbol)
                continue
            soon = [d for d in item["dates"] if within(d, today, self.settings.earnings_window_days)]
            future = [d for d in item["dates"] if _day(d) >= _day(today)]
            if future:
                upcoming[symbol] = future[0]
            if soon:
                caps[symbol] = self.settings.earnings_max_position
        return caps, upcoming, sorted(unknown)
