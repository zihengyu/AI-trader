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
macro      FRED yields, curve, credit spread, VIX                 every macro_minutes (x4 outside 06:00-20:00
                                                                  New York on weekdays)
earnings   the earnings calendar                                  every earnings_minutes
bars       completed daily bars (local mode; paper mode keeps     until today's are complete (every 15
           the broker's own and the runner writes them back)      minutes), then hourly checks
snapshot   market_snapshot from those bars                        every 15 minutes, written when it changes
sec        SEC company facts due for a refresh                    every 12 hours
=========  =====================================================  ==========================================

Results go to the account's daily store (datastore.py, atomic writes) and news
to the journal's fund_news table, which the runner already reads. The status file
`cache/desk/status.json` carries a heartbeat (every 15 seconds, also during a long
job) and each job's last success, error and next run. The runner reads the
desk's data first and fetches only what is missing or stale, so a stopped desk
only means the runner fetches as it did before: it never blocks trading.

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
JOBS = ("news", "intraday", "macro", "earnings", "bars", "snapshot", "sec")


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
                          **({"error": job["error"]} if job.get("error") else {})}
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
        symbols[symbol] = item
    moved = sorted((s for s, v in symbols.items() if "change" in v and not v["stale"] and s not in gauges),
                   key=lambda s: symbols[s]["change"])
    breadth = [symbols[s]["change"] for s in moved]
    return {"as_of": now.isoformat(), "source": "Yahoo completed one-minute bars (research context, not a quote feed)",
            "gauges": {s: symbols[s] for s in gauges if s in symbols},
            "leaders": [{"symbol": s, "change": symbols[s]["change"]} for s in reversed(moved[-5:])],
            "laggards": [{"symbol": s, "change": symbols[s]["change"]} for s in moved[:5]],
            "breadth": {"up": sum(c > 0 for c in breadth), "down": sum(c < 0 for c in breadth),
                        "unchanged": sum(c == 0 for c in breadth)},
            "symbols": symbols, "unavailable": unavailable}


class DataDesk:
    def __init__(self, run_dir, config, mode, *, source=None, environ=None, clock=None, news_get=None,
                 news_yahoo=None, macro_get=None, earnings_get=None, earnings_yahoo=None, sec_get=None):
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
                      "earnings_yahoo": earnings_yahoo, "sec": sec_get}
        self.next_run, self.failures = {}, {}
        self._lock = threading.Lock()
        previous = self.status.read() or {}
        self.state = {"started_at": self.clock().isoformat(), "heartbeat_at": None, "running": None,
                      "mode": mode, "jobs": dict(previous.get("jobs") or {})}

    # ----- schedule -----------------------------------------------------------------
    def market_open(self, now):
        from .market_data import SessionClock
        return bool(SessionClock(now=lambda: now).status()["open"])

    def enabled(self, name, now):
        if name == "intraday":
            return self.market_open(now)  # The pulse describes the regular session only.
        return {"news": self.news_settings.enabled, "macro": self.macro_settings.enabled,
                "earnings": self.event_settings.enabled, "bars": self.mode == "local", "snapshot": True,
                "sec": self.lab.enabled and bool(str(self.environ.get("FUND_SEC_USER_AGENT", "")).strip())}[name]

    def interval(self, name, now, outcome=None):
        """Seconds until `name` runs again after a run with `outcome` (None when it did not run)."""
        is_open = self.market_open(now)
        local = instant(now).astimezone(NEW_YORK)
        working = local.weekday() < 5 and 6 <= local.hour < 20
        if name == "news":
            return self.news_settings.poll_seconds * (1 if is_open else 4)
        if name == "intraday":
            return self.settings.intraday_seconds
        if name == "macro":
            return self.settings.macro_minutes * 60 * (1 if working else 4)
        if name == "earnings":
            return self.settings.earnings_minutes * 60
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
            self._source = YahooRealtimeData(tape=None)
        return self._source

    def _job_news(self, now):
        if not (self.run_dir / "state.sqlite3").is_file():
            raise Idle("no account journal yet (the runner creates it on its first start)")
        if self._news is None:
            from .news import NewsWatch
            self._news = NewsWatch(_JournalAccess(self.run_dir), self.news_settings, clock=self.clock,
                                   get=self._gets["news"], yahoo=self._gets["news_yahoo"], environ=self.environ)
        symbols, held, queries = follow_list(self.run_dir, self.tradable)
        self._news.watch(symbols, held, queries)
        before = dict(self._news.stats)
        self._news.poll_once()
        return {"stored": self._news.stats["stored"] - before["stored"],
                "material": self._news.stats["material"] - before["material"],
                "sources_with_errors": sorted(self._news.stats["errors"])}

    def _job_intraday(self, now):
        from lumibot.entities import Asset
        day, frames, previous = trading_day(now), {}, {}
        for symbol in sorted(set(self.tradable) | set(GAUGES)):
            try:
                frames[symbol] = self._data().execution_bars(Asset(symbol, asset_type="stock"))
            except Exception as exc:
                frames[symbol] = exc
            rows = self.store.bars(day, symbol, 1)
            if rows:
                previous[symbol] = (str(rows[-1][0]), float(rows[-1][1]))
        pulse = intraday_pulse(frames, previous, now)
        if not pulse["symbols"]:  # Never replace the last good pulse with an empty one.
            raise RiskError("no symbol had a completed minute today: " + ", ".join(sorted(set(
                pulse["unavailable"].values())))[:150])
        self.store.put(day, "intraday", pulse)
        return {"symbols": len(pulse["symbols"]), "unavailable": len(pulse["unavailable"])}

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
            raise Idle(f"no daily bars for {benchmark} in today's store yet")
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
