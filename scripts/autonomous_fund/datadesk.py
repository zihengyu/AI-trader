"""数据员 (the data desk): an always-on process that collects and prepares the fund's research data.

The researcher and the manager should start from prepared, current data instead
of downloading it inside a model call, and new information should not wait for a
fixed daily pull. This separate process (`lumibot-fund-desk.service`, or `run
datadesk`) works continuously and incrementally:

=========  =====================================================  ==========================================
job        what                                                   when
=========  =====================================================  ==========================================
news       headlines, SEC filings, Fed releases, RSS and the       every news.poll_seconds while the market is
           AI's news_watch phrases, into the account journal      open, four times that otherwise
intraday   last completed minute, day open/high/low/volume,       every intraday_seconds while the market is
           change versus the previous close, movers, breadth      open
premarket  pre-market trades per tradable symbol and gauge:      every 15 minutes from 04:00 New York until
           last, change versus the previous close, volume        the open on trading days
global     index futures, VIX, the 10-year yield, the dollar,     every 15 minutes outside the session on
           EUR and JPY, crude, gold, Tokyo, Hong Kong, Europe     weekdays, 30 during it, 4 hours on weekends
macro      FRED yields, curve, credit spread, VIX                 every macro_minutes (x4 outside 06:00-20:00
                                                                  New York on weekdays)
calendar   scheduled US releases (CPI, jobs, GDP, PCE, PPI,       every 6 hours (needs FRED_API_KEY)
           retail sales, claims, FOMC...) for three weeks
earnings   the earnings calendar                                  every earnings_minutes
bars       completed daily bars (local mode; paper mode keeps     from 06:00 New York (corporate actions are
           the broker's own and the runner writes them back)      applied by then) until complete, then hourly
snapshot   market_snapshot from those bars                        every 15 minutes, written when it changes
sec        SEC company facts due for a refresh                    every 12 hours
=========  =====================================================  ==========================================

Results go to the account's daily store (datastore.py, atomic writes) and news
to the journal's fund_news table, which the runner already reads. The status file
`cache/desk/status.json` carries a heartbeat (every 15 seconds, also during a long
job) and each job's last success, error and next run. The runner reads the
desk's data first and fetches only what is missing or stale, so a stopped desk
only means the runner fetches as it did before: it never blocks trading.

Trading has priority on Yahoo (one server IP for both processes): the desk asks
for a fifth of the trading process's minute history, pauses between requests,
stops a round after three failures in a row, and stands back entirely while the
trading process reports its own Yahoo requests failing (cache/desk/yahoo_cooldown.json).
The service also runs at a lower CPU, I/O and out-of-memory priority.

The desk never calls a model, places an order or changes STOP, approvals or
decisions. It reads the journal only to know which symbols, holdings and
news_watch phrases to follow. Nothing is estimated or filled: a value the
sources did not provide is absent and reported as such.
"""
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .risk import RiskError, instant, trading_day

NEW_YORK = ZoneInfo("America/New_York")
ALIVE_SECONDS = 180  # A heartbeat older than this means the desk is not running.
HEARTBEAT_SECONDS = 15
STALE_QUOTE_MINUTES = 15
GAUGES = ("SPY", "QQQ", "IWM", "DIA")  # Broad market gauges for the intraday pulse.
JOBS = ("news", "intraday", "premarket", "global", "macro", "calendar", "earnings", "bars", "snapshot", "sec")
GLOBAL_MARKETS = {
    "ES=F": "S&P 500 E-mini futures", "NQ=F": "Nasdaq-100 E-mini futures", "RTY=F": "Russell 2000 E-mini futures",
    "^VIX": "CBOE VIX index", "^TNX": "CBOE 10-year Treasury yield index", "DX-Y.NYB": "US dollar index",
    "EURUSD=X": "euro in US dollars", "JPY=X": "US dollar in Japanese yen", "CL=F": "WTI crude oil futures",
    "GC=F": "gold futures", "^N225": "Nikkei 225 (Tokyo)", "^HSI": "Hang Seng (Hong Kong)",
    "^STOXX50E": "Euro Stoxx 50"}
# FRED release names (lower case, matched as substrings) of the scheduled US releases that move markets.
# FOMC meetings come from the Federal Reserve's own calendar: FRED's "FOMC Press Release" is listed
# every day (2026-11-03 rehearsal), which is a data feed, not the meeting schedule.
MAJOR_RELEASES = ("consumer price index", "employment situation", "gross domestic product",
                  "personal income and outlays", "producer price index", "monthly sales for retail",
                  "unemployment insurance weekly claims", "job openings and labor turnover", "industrial production",
                  "surveys of consumers", "new residential construction")
MAX_RELEASE_DATES = 6  # A release dated more often than this in the window is a daily feed, not an event.
FOMC_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"), start=1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})  # "Apr/May"
CALENDAR_DAYS = 21
FRED_RELEASE_DATES = "https://api.stlouisfed.org/fred/releases/dates"
SUSPECT_MOVE = 0.25  # A gap this large versus the previous close is more likely a split or bad data.
YAHOO_PAUSE_SECONDS = 0.3
MAX_FAILURES_IN_A_ROW = 3


def cooldown_path(run_dir):
    return Path(run_dir) / "cache" / "desk" / "yahoo_cooldown.json"


def yahoo_cooldown_writer(run_dir):
    """on_backoff for the trading process's Yahoo source: record until when the desk must leave Yahoo alone."""
    path, lock = cooldown_path(run_dir), threading.Lock()

    def write(until_epoch):
        with lock:
            until = datetime.fromtimestamp(until_epoch, timezone.utc)
            try:
                current = instant(json.loads(path.read_text(encoding="utf-8"))["until"])
                until = max(until, current)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f"yahoo_cooldown.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"until": until.isoformat(),
                                             "reason": "the trading process's Yahoo requests are failing"}),
                                 encoding="utf-8")
            temporary.replace(path)
    return write


def yahoo_paused_until(run_dir, now):
    """The trading process's Yahoo cooldown end while it lasts, else None."""
    try:
        until = instant(json.loads(cooldown_path(run_dir).read_text(encoding="utf-8"))["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return until if until > instant(now) else None


def yahoo_symbol(symbol):
    """Yahoo's spelling of a share class (BRK.B -> BRK-B); index, futures and FX symbols are kept as they are."""
    return symbol.replace(".", "-") if symbol.replace(".", "").isalpha() else symbol


def last_move(frame, now, meaning=None):
    """Latest bar versus the previous New York day's last bar, with the bar time and its age (no filling)."""
    frame = frame.dropna(subset=["close"])
    if frame.empty:
        raise RiskError("no observations")
    local = frame.index.tz_convert(NEW_YORK)
    last_day = local[-1].date()
    before = frame[local.date < last_day]
    last, at = float(frame["close"].iloc[-1]), frame.index[-1].to_pydatetime()
    item = {"last": round(last, 4), "bar_start": at.isoformat(),
            "age_minutes": round((instant(now) - at).total_seconds() / 60)}
    if meaning:
        item["meaning"] = meaning
    if not before.empty and float(before["close"].iloc[-1]) > 0:
        previous = float(before["close"].iloc[-1])
        item.update({"previous": round(previous, 4), "previous_date": str(before.index[-1].tz_convert(NEW_YORK).date()),
                     "change": round(last / previous - 1, 5)})
    return item


def movers(symbols, gauges=GAUGES):
    """Leaders, laggards and breadth over fresh, plausible moves (stale or suspect ones are left out)."""
    moved = sorted((s for s, v in symbols.items() if "change" in v and not v.get("stale") and not v.get("suspect")
                    and s not in gauges), key=lambda s: symbols[s]["change"])
    changes = [symbols[s]["change"] for s in moved]
    return {"leaders": [{"symbol": s, "change": symbols[s]["change"]} for s in reversed(moved[-5:])],
            "laggards": [{"symbol": s, "change": symbols[s]["change"]} for s in moved[:5]],
            "breadth": {"up": sum(c > 0 for c in changes), "down": sum(c < 0 for c in changes),
                        "unchanged": sum(c == 0 for c in changes)}}


def suspect(item, key):
    """Flag an implausible move instead of presenting it: a split or bad data, until verified."""
    if abs(item.get(key) or 0) >= SUSPECT_MOVE:
        item["suspect"] = (f"{item[key]:+.0%} versus the previous close: possibly a split, a special dividend or "
                           "bad data; verify before use")


INTRADAY_SECONDS_PER_SYMBOL = 5  # About 12 Yahoo requests a minute for the pulse.


class Idle(Exception):
    """A job with nothing to do yet (for example no account journal): neither a success nor an error."""


@dataclass(frozen=True)
class DeskSettings:
    enabled: bool = True
    intraday_seconds: int = 300
    macro_minutes: int = 60
    earnings_minutes: int = 360

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise RiskError("data_desk.enabled must be true or false")
        for key, low, high in (("intraday_seconds", 60, 3600), ("macro_minutes", 15, 1440),
                               ("earnings_minutes", 60, 1440)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise RiskError(f"data_desk.{key} must be {low}..{high}")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown data_desk settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


class DeskStatus:
    """The desk's heartbeat and per-job state, as the runner, status and the monitor read it."""

    def __init__(self, run_dir):
        self.path = Path(run_dir) / "cache" / "desk" / "status.json"

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"status.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, default=str), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _age(at, now):
        try:
            return (instant(now) - instant(at)).total_seconds()
        except (TypeError, ValueError):
            return None

    def alive(self, now=None, state=None):
        state = state if state is not None else self.read()
        age = self._age((state or {}).get("heartbeat_at"), now or datetime.now(timezone.utc))
        return age is not None and 0 <= age <= ALIVE_SECONDS

    def fresh(self, job, max_age_seconds, now=None):
        """True when the desk is running and `job` last succeeded within max_age_seconds."""
        now = now or datetime.now(timezone.utc)
        state = self.read()
        if not self.alive(now, state):
            return False
        age = self._age(((state.get("jobs") or {}).get(job) or {}).get("ok_at"), now)
        return age is not None and 0 <= age <= max_age_seconds

    def summary(self, now=None):
        """A compact view for contexts and the operator: running or not, and each dataset's age."""
        now = now or datetime.now(timezone.utc)
        state = self.read()
        if state is None:
            return {"running": False, "note": "the data desk has not run; the runner fetches its own data"}
        if state.get("disabled"):
            return {"running": False, "disabled": True, "note": "data_desk.enabled is false; the runner fetches its own data"}
        jobs = {}
        for name, job in (state.get("jobs") or {}).items():
            age = self._age(job.get("ok_at"), now)
            jobs[name] = {"updated_minutes_ago": round(age / 60, 1) if age is not None else None,
                          **({"error": job["error"]} if job.get("error") else {}),
                          **({"note": job["note"]} if job.get("note") and not job.get("error") else {})}
        return {"running": self.alive(now, state), "heartbeat_at": state.get("heartbeat_at"),
                "busy_with": state.get("running"), "datasets": jobs}


@contextmanager
def _journal_connection(run_dir):
    from .execution import operator_connection
    with operator_connection(run_dir) as db:
        yield db


class _JournalAccess:
    """Only what the news watch needs: connections to the existing account journal (never created here)."""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)

    def connection(self):
        return _journal_connection(self.run_dir)


def follow_list(run_dir, universe):
    """(symbols, held, queries) the runner follows: holdings, the newest targets, alerts and news_watch phrases."""
    from .execution import operator_connection
    symbols, held, queries = set(universe), set(), []
    with operator_connection(run_dir) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        account = db.execute("SELECT body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        if account:
            held = {s for s, q in (json.loads(account[0]).get("positions") or {}).items() if float(q)}
        if "fund_decisions" in tables:
            latest = db.execute("SELECT body FROM fund_decisions ORDER BY rowid DESC LIMIT 1").fetchone()
            if latest:
                body = json.loads(latest[0])
                symbols |= {t["symbol"] for t in body.get("targets", [])} | {a["symbol"] for a in body.get("alerts", [])}
                queries = [q["query"] for q in body.get("news_watch", [])]
    return symbols | held, held, queries


def intraday_pulse(frames, previous_closes, now, gauges=GAUGES):
    """The session so far per symbol from completed minute bars; {symbol: frame} -> summary.

    previous_closes: {symbol: (date, close)} completed daily closes (the store);
    without one, the previous session's last minute close is used and labeled.
    A symbol whose newest completed minute is older than STALE_QUOTE_MINUTES is
    reported stale rather than as current.
    """
    now = instant(now)
    today = now.astimezone(NEW_YORK).date()
    symbols, unavailable = {}, {}
    for symbol, frame in sorted(frames.items()):
        if isinstance(frame, Exception):
            unavailable[symbol] = str(frame)[:120] if isinstance(frame, RiskError) else type(frame).__name__
            continue
        dates = frame.index.tz_convert(NEW_YORK).date
        session, before = frame[dates == today], frame[dates < today]
        if session.empty:
            unavailable[symbol] = "no completed minute today"
            continue
        last_at = session.index[-1].to_pydatetime() + timedelta(minutes=1)
        previous = previous_closes.get(symbol)
        source = "daily_close"
        if previous is None and not before.empty:
            previous, source = (str(before.index[-1].date()), float(before["close"].iloc[-1])), "last_minute_close"
        last = float(session["close"].iloc[-1])
        item = {"last": round(last, 4), "at": last_at.isoformat(),
                "stale": (now - last_at).total_seconds() > STALE_QUOTE_MINUTES * 60,
                "day_open": round(float(session["open"].iloc[0]), 4),
                "day_high": round(float(session["high"].max()), 4), "day_low": round(float(session["low"].min()), 4),
                "day_volume": int(session["volume"].sum())}
        if previous is not None and previous[1] > 0:
            item.update({"previous_close": round(previous[1], 4), "previous_close_date": previous[0],
                         "previous_close_source": source, "change": round(last / previous[1] - 1, 5),
                         "gap_at_open": round(item["day_open"] / previous[1] - 1, 5)})
            suspect(item, "gap_at_open")
        symbols[symbol] = item
    return {"as_of": now.isoformat(), "source": "Yahoo completed one-minute bars (research context, not a quote feed)",
            "gauges": {s: symbols[s] for s in gauges if s in symbols}, **movers(symbols, gauges),
            "symbols": symbols, "unavailable": unavailable}


def premarket_moves(frames, previous_closes, now, gauges=GAUGES):
    """Pre-market trades today (04:00 to the open) versus the previous close; {symbol: frame} -> summary."""
    now = instant(now)
    today = now.astimezone(NEW_YORK).date()
    symbols, unavailable = {}, {}
    for symbol, frame in sorted(frames.items()):
        if isinstance(frame, Exception):
            unavailable[symbol] = str(frame)[:120] if isinstance(frame, RiskError) else type(frame).__name__
            continue
        local = frame.index.tz_convert(NEW_YORK)
        early = frame[(local.date == today) & ((local.hour < 9) | ((local.hour == 9) & (local.minute < 30)))]
        if early.empty:
            unavailable[symbol] = "no pre-market trades yet"
            continue
        last, at = float(early["close"].iloc[-1]), early.index[-1].to_pydatetime() + timedelta(minutes=1)
        item = {"last": round(last, 4), "at": at.isoformat(), "volume": int(early["volume"].sum()),
                "stale": (now - at).total_seconds() > 60 * 60}
        previous = previous_closes.get(symbol)
        if previous is not None and previous[1] > 0:
            item.update({"previous_close": round(previous[1], 4), "previous_close_date": previous[0],
                         "change": round(last / previous[1] - 1, 5)})
            suspect(item, "change")
        symbols[symbol] = item
    return {"as_of": now.isoformat(), "source": "Yahoo pre-market one-minute bars (thin trading: research context)",
            "gauges": {s: symbols[s] for s in gauges if s in symbols}, **movers(symbols, gauges),
            "symbols": symbols, "unavailable": unavailable}


def major_releases(rows, today, days=CALENDAR_DAYS):
    """The market-moving scheduled releases from FRED's release dates, soonest first.

    A release dated more than MAX_RELEASE_DATES times in the window is a daily
    data feed, not a scheduled event, and is left out whatever its name.
    """
    end = (today + timedelta(days=days)).isoformat()
    dates = {}
    for row in rows:
        name, day = str(row.get("release_name") or ""), str(row.get("date") or "")
        if name and today.isoformat() <= day <= end:
            dates.setdefault(name, {})[day] = row.get("release_id")
    major, other = [], 0
    for name, days_ in dates.items():
        if len(days_) > MAX_RELEASE_DATES or not any(key in name.lower() for key in MAJOR_RELEASES):
            other += len(days_)
            continue
        major += [{"date": day, "release": name, "release_id": key} for day, key in days_.items()]
    return sorted(major, key=lambda r: (r["date"], r["release"])), other


def fomc_meetings(html, today, days=CALENDAR_DAYS):
    """Scheduled FOMC decision days within `days` from the Federal Reserve's calendar page.

    The page lists each year's meetings as a month ("January", "Apr/May") and a
    day span ("27-28", "30-1", "16-17*" with an asterisk for a projections
    meeting). The decision is on the last day. Notation votes and unscheduled
    meetings are left out. Raises RiskError when the page has no readable year.
    """
    import re
    end = today + timedelta(days=days)
    sections = re.split(r"(\d{4}) FOMC Meetings", html)
    meetings = []
    for index in range(1, len(sections) - 1, 2):
        year, body = int(sections[index]), sections[index + 1]
        pairs = re.findall(r"fomc-meeting__month[^>]*>\s*(?:<[^>]+>\s*)*([A-Za-z/]+)\s*(?:<[^>]+>\s*)*</div>\s*"
                           r"<div[^>]*fomc-meeting__date[^>]*>\s*([^<]+)<", body)
        for month_text, day_text in pairs:
            if "notation" in day_text.lower() or "unscheduled" in day_text.lower():
                continue
            days_ = [int(d) for d in re.findall(r"\d+", day_text)]
            months = [MONTHS[m.lower()[:3]] for m in month_text.split("/") if m.lower()[:3] in MONTHS]
            if not days_ or not months:
                continue
            month = months[-1]  # "Apr/May 30-1": the decision is on the last day, in the last month.
            try:
                decision = datetime(year, month, days_[-1]).date()
            except ValueError:
                continue
            if today <= decision <= end:
                meetings.append({"date": decision.isoformat(), "release": "FOMC meeting decision"
                                 + (" with economic projections" if "*" in day_text else ""),
                                 "source": "federalreserve.gov FOMC calendar"})
    if len(sections) < 3:
        raise RiskError("the Federal Reserve's FOMC calendar page had no readable meeting year")
    return meetings


class DataDesk:
    def __init__(self, run_dir, config, mode, *, source=None, environ=None, clock=None, news_get=None,
                 news_yahoo=None, macro_get=None, earnings_get=None, earnings_yahoo=None, sec_get=None,
                 chart=None, calendar_get=None, sleep=time.sleep):
        from .datastore import DailyStore
        from .events import EventSettings
        from .factors import FactorSettings
        from .macro import MacroSettings
        from .news import NewsSettings
        from .risk import Limits
        from .strategy import history_length, risk_settings
        self.run_dir, self.config, self.mode = Path(run_dir), config, mode
        self.environ = os.environ if environ is None else environ
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.settings = DeskSettings.from_config(config.get("data_desk"))
        self.store, self.status = DailyStore(self.run_dir / "cache" / "daily"), DeskStatus(self.run_dir)
        self.universe = dict(config["universe"])
        sweep = (config.get("cash_sweep") or {}).get("symbol")
        self.tradable = sorted(s for s, kind in self.universe.items() if kind != "cash_equivalent" and s != sweep)
        self.quant = risk_settings(config, set(self.universe), Limits(**config["limits"]))[0]
        self.lab = FactorSettings.from_config(config.get("factor_lab"))
        self.length = history_length(self.quant, self.lab if self.lab.enabled else None)
        self.stocks = sorted({s for s, kind in self.universe.items() if kind == "stock"} |
                             (set(self.lab.research_universe) if self.lab.enabled else set()))
        self.news_settings = NewsSettings.from_config(config.get("news"))
        self.macro_settings = MacroSettings.from_config(config.get("macro"))
        self.event_settings = EventSettings.from_config(config.get("events"))
        self._source, self._news, self._macro, self._calendar = source, None, None, None
        self._gets = {"news": news_get, "news_yahoo": news_yahoo, "macro": macro_get, "earnings": earnings_get,
                      "earnings_yahoo": earnings_yahoo, "sec": sec_get, "chart": chart, "calendar": calendar_get}
        self._sleep, self._closes = sleep, {}
        self.next_run, self.failures = {}, {}
        self._lock = threading.Lock()
        previous = self.status.read() or {}
        self.state = {"started_at": self.clock().isoformat(), "heartbeat_at": None, "running": None,
                      "mode": mode, "jobs": dict(previous.get("jobs") or {})}

    # ----- schedule -----------------------------------------------------------------
    def _session(self, now):
        """The NYSE session at `now`; one calendar (its schedule is cached per day), asked many times a minute."""
        if getattr(self, "_calendar_clock", None) is None:
            from .market_data import SessionClock
            self._calendar_clock = SessionClock(now=lambda: self._session_at)
        self._session_at = now
        return self._calendar_clock.status()

    def market_open(self, now):
        return bool(self._session(now)["open"])

    def before_open(self, now):
        """True from 04:00 New York until the open on a trading day (the pre-market session)."""
        status = self._session(now)
        local = instant(now).astimezone(NEW_YORK)
        opens = instant(status["next_open"]).astimezone(NEW_YORK) if status.get("next_open") else None
        return (not status["open"] and opens is not None and opens.date() == local.date() and local.hour >= 4)

    def enabled(self, name, now):
        from .datastore import bars_settled
        if name == "intraday":
            return self.market_open(now)  # The pulse describes the regular session only.
        if name == "premarket":
            return self.before_open(now)
        if name == "bars":
            # Before 06:00 New York a split or dividend of the new day may not be applied yet.
            return self.mode == "local" and bars_settled(now)
        return {"news": self.news_settings.enabled, "macro": self.macro_settings.enabled, "global": True,
                "calendar": True, "earnings": self.event_settings.enabled, "snapshot": True,
                "sec": self.lab.enabled and bool(str(self.environ.get("FUND_SEC_USER_AGENT", "")).strip())}[name]

    def interval(self, name, now, outcome=None):
        """Seconds until `name` runs again after a run with `outcome` (None when it did not run)."""
        is_open = self.market_open(now)
        local = instant(now).astimezone(NEW_YORK)
        working = local.weekday() < 5 and 6 <= local.hour < 20
        if name == "news":
            return self.news_settings.poll_seconds * (1 if is_open else 4)
        if name == "intraday":
            # One minute-bar request per symbol: a wide universe stretches the pulse instead of
            # raising the Yahoo request rate (about one request every INTRADAY_SECONDS_PER_SYMBOL s).
            symbols = len(set(getattr(self, "tradable", ())) | set(GAUGES))
            return max(self.settings.intraday_seconds, round(symbols * INTRADAY_SECONDS_PER_SYMBOL))
        if name == "macro":
            return self.settings.macro_minutes * 60 * (1 if working else 4)
        if name == "earnings":
            return self.settings.earnings_minutes * 60
        if name == "premarket":
            return 900
        if name == "global":
            return 1800 if is_open else (900 if local.weekday() < 5 else 4 * 3600)
        if name == "calendar":
            return 6 * 3600
        if name == "bars":
            return 900 if (outcome or {}).get("failed") else 3600
        if name == "snapshot":
            return 900
        return 12 * 3600

    def due(self, now):
        return [name for name in JOBS if (self.next_run.get(name) is None or now >= self.next_run[name])
                and self.enabled(name, now)]

    def tick(self, now=None):
        """Run every due job once; returns {job: outcome}. One failing job never stops the others."""
        ran = {}
        for name in self.due(now or self.clock()):
            ran[name] = self.run_job(name)
        return ran

    def run_job(self, name):
        now = self.clock()
        with self._lock:
            self.state["running"] = {"job": name, "since": now.isoformat()}
        self.heartbeat()
        job = self.state["jobs"].setdefault(name, {})
        try:
            outcome = getattr(self, f"_job_{name}")(now)
            failures = 0
            job.update({"ok_at": self.clock().isoformat(), "error": None, "note": None, "result": outcome})
            wait = self.interval(name, now, outcome)
        except Idle as reason:
            failures, outcome = 0, None
            job.update({"note": str(reason), "error": None})
            wait = self.interval(name, now)
        except Exception as exc:
            failures = self.failures.get(name, 0) + 1
            job.update({"error": str(exc)[:200] if isinstance(exc, RiskError) else type(exc).__name__,
                        "failed_at": self.clock().isoformat()})
            # Back off on failure, but never wait longer than the job's normal interval.
            wait = min(self.interval(name, now), 60 * 2 ** min(failures - 1, 4))
            outcome = None
        self.failures[name] = failures
        self.next_run[name] = now + timedelta(seconds=wait)
        job["next_at"] = self.next_run[name].isoformat()
        with self._lock:
            self.state["running"] = None
        self.heartbeat()
        return outcome

    def heartbeat(self):
        with self._lock:
            self.state["heartbeat_at"] = self.clock().isoformat()
            try:
                self.status.write(self.state)
            except OSError:
                pass  # A full disk shows up as a stale heartbeat; the runner then fetches itself.

    def serve(self, stop):
        """Run until `stop` is set; the heartbeat thread keeps the status fresh during long jobs."""
        def beat():
            while not stop.wait(HEARTBEAT_SECONDS):
                self.heartbeat()
        threading.Thread(target=beat, name="desk-heartbeat", daemon=True).start()
        while not stop.is_set():
            self.tick()
            now = self.clock()
            upcoming = [t for n, t in self.next_run.items() if n != "intraday" and self.enabled(n, now)]
            pause = min([(t - now).total_seconds() for t in upcoming] + [30])  # Also notices the open.
            stop.wait(max(1.0, pause))
        self.heartbeat()

    # ----- jobs -----------------------------------------------------------------------
    def _data(self):
        if self._source is None:
            from .market_data import YahooRealtimeData
            # Also points yfinance's own cache at the writable account cache before any direct call.
            self._source = YahooRealtimeData(tape=None)
        return self._source

    def _yield_to_trading(self, now):
        """Leave Yahoo to the trading process while its own Yahoo requests are failing."""
        until = yahoo_paused_until(self.run_dir, now)
        if until is not None:
            raise Idle(f"Yahoo is failing for the trading process; the desk waits until {until.isoformat()}")

    def _requests(self, symbols, fetch):
        """{symbol: frame or exception}, paced; after three failures in a row the rest of the round is skipped."""
        frames, failures = {}, 0
        for index, symbol in enumerate(symbols):
            if failures >= MAX_FAILURES_IN_A_ROW:
                frames[symbol] = RiskError("skipped: Yahoo failed three times in a row this round")
                continue
            try:
                frames[symbol] = fetch(symbol)
                failures = 0
            except Exception as exc:
                frames[symbol], failures = exc, failures + 1
            if index < len(symbols) - 1:
                self._sleep(YAHOO_PAUSE_SECONDS)
        return frames

    def _chart(self, symbol, period, interval, prepost):
        """Yahoo bars with the pre/post-market session when asked (indices, futures, FX, pre-market trades)."""
        if self._gets["chart"] is not None:
            return self._gets["chart"](symbol, period, interval, prepost)
        self._data()
        import pandas as pd
        import yfinance as yf
        frame = yf.Ticker(symbol).history(period=period, interval=interval, prepost=prepost, auto_adjust=False,
                                          actions=False, timeout=15, raise_errors=True)
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise RiskError(f"Yahoo returned empty or undated data for {symbol}")
        return frame.rename(columns=str.lower)

    def _previous_close(self, symbol, day):
        """(date, close) of the last completed session: the store first, then one daily read per day."""
        rows = self.store.bars(day, symbol, 1)
        if rows:
            return str(rows[-1][0]), float(rows[-1][1])
        key = (day, symbol)
        if key not in self._closes:
            from lumibot.entities import Asset
            from .strategy import daily_rows
            try:
                rows = [r for r in daily_rows(self._data().get_historical_prices(
                    Asset(symbol, asset_type="stock"), 5, timestep="day")) if str(r[0]) < str(day) and r[1] == r[1]]
                self._closes = {k: v for k, v in self._closes.items() if k[0] == day}
                self._closes[key] = (str(rows[-1][0]), float(rows[-1][1])) if rows else None
            except Exception:
                return None  # Retried in a later round; the change is reported as unknown meanwhile.
        return self._closes[key]

    def _job_news(self, now):
        if not (self.run_dir / "state.sqlite3").is_file():
            raise Idle("no account journal yet (the runner creates it on its first start)")
        if self._news is None:
            from .news import NewsWatch
            self._news = NewsWatch(_JournalAccess(self.run_dir), self.news_settings, clock=self.clock,
                                   get=self._gets["news"], yahoo=self._gets["news_yahoo"], environ=self.environ)
        symbols, held, queries = follow_list(self.run_dir, self.tradable)
        self._news.watch(symbols, held, queries)
        # Yahoo headlines wait while trading needs Yahoo; the Fed, SEC, Google News and RSS continue.
        self._news.skip_sources = {"yahoo"} if yahoo_paused_until(self.run_dir, now) else set()
        before = dict(self._news.stats)
        self._news.poll_once()
        return {"stored": self._news.stats["stored"] - before["stored"],
                "material": self._news.stats["material"] - before["material"],
                "sources_with_errors": sorted(self._news.stats["errors"])}

    def _job_intraday(self, now):
        from lumibot.entities import Asset
        self._yield_to_trading(now)
        day, source = trading_day(now), self._data()
        bars = getattr(source, "session_bars", None) or source.execution_bars  # Today only: a light request.
        symbols = sorted(set(self.tradable) | set(GAUGES))
        frames = self._requests(symbols, lambda symbol: bars(Asset(symbol, asset_type="stock")))
        # Previous closes only for symbols that answered: a failing Yahoo gets no extra requests.
        previous = {s: close for s in symbols if not isinstance(frames[s], Exception)
                    and (close := self._previous_close(s, day)) is not None}
        pulse = intraday_pulse(frames, previous, now)
        if not pulse["symbols"]:  # Never replace the last good pulse with an empty one.
            raise RiskError("no symbol had a completed minute today: " + ", ".join(sorted(set(
                pulse["unavailable"].values())))[:150])
        self.store.put(day, "intraday", pulse)
        return {"symbols": len(pulse["symbols"]), "unavailable": len(pulse["unavailable"])}

    def _job_premarket(self, now):
        self._yield_to_trading(now)
        day = trading_day(now)
        symbols = sorted(set(self.tradable) | set(GAUGES))
        frames = self._requests(symbols, lambda symbol: self._chart(yahoo_symbol(symbol), "1d", "1m", True))
        previous = {s: close for s in symbols if not isinstance(frames[s], Exception)
                    and (close := self._previous_close(s, day)) is not None}
        moves = premarket_moves(frames, previous, now)
        if not moves["symbols"]:
            raise Idle("no pre-market trades yet: " + ", ".join(sorted(set(moves["unavailable"].values())))[:150])
        self.store.put(day, "premarket", moves)
        return {"symbols": len(moves["symbols"]), "unavailable": len(moves["unavailable"])}

    def _job_global(self, now):
        self._yield_to_trading(now)
        frames = self._requests(list(GLOBAL_MARKETS), lambda symbol: self._chart(symbol, "5d", "15m", True))
        markets, unavailable = {}, {}
        for symbol, frame in frames.items():
            try:
                if isinstance(frame, Exception):
                    raise frame
                markets[symbol] = last_move(frame, now, GLOBAL_MARKETS[symbol])
            except Exception as exc:
                unavailable[symbol] = str(exc)[:120] if isinstance(exc, RiskError) else type(exc).__name__
        if not markets:  # Never replace the last good view with an empty one.
            raise RiskError("no global market answered: " + ", ".join(sorted(set(unavailable.values())))[:150])
        self.store.put(trading_day(now), "global_markets", {
            "as_of": instant(now).isoformat(), "markets": markets, "unavailable": unavailable,
            "source": "Yahoo 15-minute bars including pre/post-market; change is versus the previous New York "
                      "day's last bar (futures and FX trade nearly around the clock)"})
        return {"markets": len(markets), "unavailable": len(unavailable)}

    def _job_calendar(self, now):
        key = str(self.environ.get("FRED_API_KEY", "")).strip()
        if not key:
            raise Idle("needs FRED_API_KEY (free at fred.stlouisfed.org) for the release calendar")
        import requests
        get = self._gets["calendar"] or requests.get
        today = instant(now).astimezone(NEW_YORK).date()
        rows = []
        for page in range(5):  # Hundreds of releases a week: page through the window, at most 5000 rows.
            response = get(FRED_RELEASE_DATES, timeout=20, params={
                "api_key": key, "file_type": "json", "realtime_start": today.isoformat(),
                "realtime_end": (today + timedelta(days=CALENDAR_DAYS)).isoformat(),
                "include_release_dates_with_no_data": "true", "sort_order": "asc", "order_by": "release_date",
                "limit": 1000, "offset": 1000 * page})
            response.raise_for_status()
            batch = (response.json() or {}).get("release_dates") or []
            rows += batch
            if len(batch) < 1000:
                break
        major, other = major_releases(rows, today)
        unavailable = None
        try:  # FOMC decision days from the Fed itself; a failure is disclosed, never guessed.
            page = get(FOMC_CALENDAR, timeout=20, headers={"User-Agent": "LumiBot-AutonomousFund/1.0"})
            page.raise_for_status()
            fomc = fomc_meetings(page.text, today)
        except Exception as exc:
            fomc, unavailable = [], str(exc)[:120] if isinstance(exc, RiskError) else type(exc).__name__
        releases = sorted(major + fomc, key=lambda r: (r["date"], r["release"]))
        self.store.put(trading_day(now), "econ_calendar", {
            "as_of": instant(now).isoformat(), "window_days": CALENDAR_DAYS, "releases": releases,
            "other_releases": other, "fomc_unavailable": unavailable,
            "source": "FRED release calendar (St. Louis Fed); FOMC meetings from federalreserve.gov",
            "note": "Dates only. Most US data releases come out at 08:30 New York time and FOMC statements at "
                    "14:00; check the agency's own calendar for the exact time."})
        return {"major": len(major), "fomc": len(fomc), "other": other, "fomc_unavailable": unavailable}

    def _job_macro(self, now):
        from .macro import MacroWatch
        if self._macro is None:
            self._macro = MacroWatch(self.macro_settings, environ=self.environ, get=self._gets["macro"], store=self.store)
        result = self._macro.refresh(trading_day(now))
        if all(value != "ok" for value in result.values()):
            raise RiskError("every FRED series failed: " + ", ".join(sorted(set(result.values())))[:150])
        return result

    def _job_earnings(self, now):
        from .events import EarningsCalendar
        from .prefetch import refresh_earnings
        if self._calendar is None:
            self._calendar = EarningsCalendar(None, self.event_settings, environ=self.environ,
                                              get=self._gets["earnings"], yahoo=self._gets["earnings_yahoo"])
        if self._calendar.source() == "yahoo":
            self._yield_to_trading(now)
        stocks = sorted(s for s, kind in self.universe.items() if kind == "stock")
        result = refresh_earnings(self.store, trading_day(now), self._calendar, stocks, refresh=True)
        if stocks and len(result["failed"]) == len(stocks):  # Nothing usable: an error, not a fresh calendar.
            raise RiskError(f"the earnings calendar failed for all {len(stocks)} stocks")
        return result

    def _job_bars(self, now):
        from .prefetch import fetch_bars
        symbols = sorted(set(self.universe) | set(self.stocks) | {self.quant.stress_benchmark} | set(GAUGES))
        result = fetch_bars(self.store, trading_day(now), symbols, self.length, self._data())
        self.store.prune(trading_day(now))
        if result["failed"] and not result["fetched"] and not result["already_stored"]:
            first = sorted(result["failed"].items())[0]
            raise RiskError(f"daily bars failed for all {len(symbols)} symbols (first: {first[0]}: {first[1]})"[:200])
        return result

    def _job_snapshot(self, now):
        from .snapshot import snapshot
        day, benchmark = trading_day(now), self.quant.stress_benchmark
        histories = {}
        for symbol in sorted(set(self.tradable) | {benchmark}):
            rows = self.store.bars(day, symbol, 1) or []
            histories[symbol] = [(str(d), c, v) for d, c, v, *_ in rows if c == c and c > 0]
        if not histories.get(benchmark):
            raise Idle("waiting for the day's daily bars (stored after 06:00 New York); until then the trading "
                       "process computes the snapshot itself")
        result = snapshot(histories, benchmark)
        if self.store.get(day, "market_snapshot") != json.loads(json.dumps(result, default=str)):
            self.store.put(day, "market_snapshot", result)  # Rewritten only when something changed.
        return {"symbols": len(result["symbols"]), "no_history": result["no_history"]}

    def _job_sec(self, now):
        import pandas as pd
        from .fundamentals import SecFundamentals
        sec = SecFundamentals(self.run_dir / "cache" / "sec", self.environ["FUND_SEC_USER_AGENT"], get=self._gets["sec"])
        sec.panels(set(self.stocks), pd.bdate_range(end=trading_day(now), periods=5))
        return {"companies": len(self.stocks), "errors": dict(list(sec.errors.items())[:20])}


def run(run_dir, config, mode, stop=None):
    """The service entry point: run until SIGINT/SIGTERM (or `stop`)."""
    import signal
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
    if not DeskSettings.from_config(config.get("data_desk")).enabled:
        print("data_desk.enabled is false: the data desk is off and the runner fetches its own data", flush=True)
        DeskStatus(run_dir).write({"disabled": True, "at": datetime.now(timezone.utc).isoformat()})
        stop.wait()  # Stay up quietly so a Restart=always unit does not loop.
        return 0
    desk = DataDesk(run_dir, config, mode)
    print(f"Data desk running for {mode} at {run_dir}; status in {desk.status.path}", flush=True)
    desk.serve(stop)
    return 0
