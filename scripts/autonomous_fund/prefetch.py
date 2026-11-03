"""One pass of the daily research downloads, by hand: `run prefetch`.

The data desk (datadesk.py, `lumibot-fund-desk.service`) runs these steps
continuously; this command runs the daily ones once, for example on a server
without the desk or to check every source before the open. It fills the
account's daily store (datastore.py) and the SEC cache:

- local mode: completed daily bars for the tradable universe, the research
  universe and the benchmarks (the same source and length the runner uses;
  paper mode keeps the broker's own data and skips this part);
- FRED series, the earnings calendar, SEC company-facts extracts.

The runner reads these first and fetches only what is missing. It never calls
a model, places an order or touches STOP, approvals or the journal.
"""
import os
import time
from datetime import datetime, timezone

from .risk import RiskError, trading_day


def fetch_bars(store, day, symbols, length, data):
    """Completed daily bars for every symbol not yet stored today: {"fetched", "already_stored", "failed"}."""
    from lumibot.entities import Asset
    from .strategy import daily_rows
    bars = {"fetched": 0, "already_stored": 0, "failed": {}}
    for symbol in symbols:
        if store.bars(day, symbol, length) is not None:
            bars["already_stored"] += 1
            continue
        try:
            rows = daily_rows(data.get_historical_prices(Asset(symbol, asset_type="stock"), length, timestep="day"))
            if not rows:
                raise RiskError("no completed daily bars")
            store.save_bars(day, symbol, length, rows)
            bars["fetched"] += 1
        except Exception as exc:
            bars["failed"][symbol] = str(exc)[:120] if isinstance(exc, RiskError) else type(exc).__name__
    return bars


def refresh_earnings(store, day, calendar, stocks, refresh=False):
    """Earnings dates per stock into the store; refresh=True fetches every stock again.

    A failed fetch never replaces dates fetched earlier: the earlier answer is kept.
    """
    today = datetime.fromisoformat(str(day)).date()
    saved = store.get(day, "earnings") or {}
    for symbol in stocks:
        if symbol in saved and not saved[symbol].get("error") and not refresh:
            continue
        source_name, dates, error = calendar.fetch(symbol, today)
        if error and symbol in saved and not saved[symbol].get("error"):
            continue
        saved[symbol] = {"source": source_name, "dates": dates, "error": error}
    store.put(day, "earnings", saved)
    return {"symbols": len(saved), "failed": sorted(s for s, v in saved.items() if v["error"])}


def prefetch(run_dir, config, mode, *, source=None, environ=None, now=None, clock=time.monotonic,
             earnings_get=None, earnings_yahoo=None, macro_get=None, sec_get=None):
    from pathlib import Path
    import pandas as pd
    from .datastore import DailyStore
    from .events import EarningsCalendar, EventSettings
    from .factors import FactorSettings
    from .macro import MacroSettings, MacroWatch
    from .risk import Limits
    from .strategy import history_length, risk_settings
    environ = os.environ if environ is None else environ
    run_dir = Path(run_dir)
    now = now or datetime.now(timezone.utc)
    day = trading_day(now)
    store = DailyStore(run_dir / "cache" / "daily")
    universe = dict(config["universe"])
    quant = risk_settings(config, set(universe), Limits(**config["limits"]))[0]
    lab = FactorSettings.from_config(config.get("factor_lab"))
    report, started = {"day": day, "mode": mode}, clock()
    stocks = sorted({s for s, kind in universe.items() if kind == "stock"} |
                    (set(lab.research_universe) if lab.enabled else set()))

    if mode == "local":
        from .market_data import YahooRealtimeData
        length = history_length(quant, lab if lab.enabled else None)
        symbols = sorted(set(universe) | set(stocks) | {quant.stress_benchmark, "SPY", "QQQ"})
        report["bars"] = fetch_bars(store, day, symbols, length, source or YahooRealtimeData(tape=None))
    else:
        report["bars"] = "skipped: paper mode keeps the broker's own daily data"

    macro = MacroSettings.from_config(config.get("macro"))
    if macro.enabled:
        context = MacroWatch(macro, environ=environ, get=macro_get, store=store).context(day)
        report["macro"] = {name: ("ok" if "unavailable" not in item else item["unavailable"])
                           for name, item in context["series"].items()}
    events = EventSettings.from_config(config.get("events"))
    if events.enabled:
        calendar = EarningsCalendar(None, events, environ=environ, get=earnings_get, yahoo=earnings_yahoo)
        report["earnings"] = refresh_earnings(store, day, calendar,
                                              sorted(s for s, kind in universe.items() if kind == "stock"))
    agent = str(environ.get("FUND_SEC_USER_AGENT", "")).strip()
    if lab.enabled and agent:
        from .fundamentals import SecFundamentals
        sec = SecFundamentals(run_dir / "cache" / "sec", agent, get=sec_get)
        sec.panels(set(stocks), pd.bdate_range(end=day, periods=5))
        report["sec"] = {"companies": len(stocks), "errors": dict(list(sec.errors.items())[:20])}
    else:
        report["sec"] = "skipped: needs the factor lab and FUND_SEC_USER_AGENT"
    store.prune(day)
    report["seconds"] = round(clock() - started, 1)
    store.put(day, "prefetch_report", report)
    return report
