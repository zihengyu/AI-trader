"""Scheduled prefetch into the daily store, and the runner reading it before any network.

Synthetic frames, fake HTTP clients and neutral symbols only; no network.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.datastore import DailyStore

DAY = "2026-11-03"
NOW = datetime(2026, 11, 3, 12, 15, tzinfo=timezone.utc)  # 07:15 New York.


def frame(days=600, include_today=True):
    index = pd.bdate_range(end="2026-11-03" if include_today else "2026-11-02", periods=days, tz="America/New_York")
    close = 100 * np.cumprod(1 + np.random.default_rng(1).normal(0, .01, len(index)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * .99, "close": close, "volume": 1e6},
                        index=index)


def test_the_store_keeps_completed_sessions_only_and_requires_the_length(tmp_path):
    store = DailyStore(tmp_path)
    rows = [(date(2026, 11, 2), 101.0, 1e6, 100.0, 102.0, 99.0), (date(2026, 11, 3), 103.0, 5e5, 101.0, 104.0, 100.0)]
    store.save_bars(DAY, "ALFA", 504, rows)
    assert store.bars(DAY, "ALFA", 504) == [rows[0]]  # Today's forming bar is never stored.
    assert store.bars(DAY, "ALFA", 600) is None  # Fetched for a shorter history: fetch again.
    assert store.bars("2026-11-04", "ALFA", 504) is None  # Each trading day is fetched fresh.
    for day in ("2026-10-29", "2026-10-30", "2026-11-02", DAY):
        store.put(day, "marker", {"day": day})
    store.prune(DAY, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-11-02", DAY]


def config(**sections):
    return {"model": "deepseek/deepseek-flash", "limits": {},
            "universe": {"ALFA": "stock", "BETA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"},
            "factor_lab": {"research_universe": ["WIDE"]}, **sections}


def test_prefetch_fills_bars_fred_and_earnings_once(tmp_path):
    from scripts.autonomous_fund.prefetch import prefetch
    fetched = []
    source = SimpleNamespace(get_historical_prices=lambda asset, length, timestep="day": fetched.append(
        asset.symbol) or SimpleNamespace(df=frame()))
    fred_calls = []

    def fred(url, params=None, timeout=None, headers=None):
        fred_calls.append(params["id"])
        return SimpleNamespace(raise_for_status=lambda: None, text="observation_date,X\n2026-10-30,4.0\n")
    report = prefetch(tmp_path, config(), "local", source=source, environ={}, now=NOW, macro_get=fred,
                      earnings_yahoo=lambda s: ["2026-11-04"] if s == "ALFA" else [])
    assert report["bars"]["fetched"] == 5 and sorted(fetched) == ["ALFA", "BETA", "QQQ", "SPY", "WIDE"]
    assert set(report["macro"].values()) == {"ok"} and report["earnings"]["symbols"] == 2
    store = DailyStore(tmp_path / "cache" / "daily")
    assert store.get(DAY, "earnings")["ALFA"]["dates"] == ["2026-11-04"]
    again = prefetch(tmp_path, config(), "local", source=source, environ={}, now=NOW, macro_get=fred,
                     earnings_yahoo=lambda s: [])
    assert again["bars"] == {"fetched": 0, "already_stored": 5, "failed": {}} and len(fetched) == 5
    assert len(fred_calls) == 6  # FRED series were read from the store the second time.


def test_paper_prefetch_leaves_daily_bars_to_the_broker(tmp_path):
    from scripts.autonomous_fund.prefetch import prefetch
    report = prefetch(tmp_path, config(macro={"enabled": False}, events={"enabled": False}), "paper", environ={},
                      now=NOW)
    assert report["bars"].startswith("skipped") and report["sec"].startswith("skipped")


def test_the_runner_reads_prefetched_bars_and_writes_back_what_it_fetched(tmp_path):
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path, mode="local")
    strategy.daily_store = store = DailyStore(tmp_path / "daily")
    from scripts.autonomous_fund.strategy import daily_rows, history_length
    length = history_length(strategy.quant)
    store.save_bars(DAY, "ALFA", length, daily_rows(SimpleNamespace(df=frame())))
    calls = []
    strategy.get_historical_prices = lambda asset, length, timestep="day": calls.append(asset.symbol) or SimpleNamespace(
        df=frame())
    rows, error = strategy._daily_bars("ALFA", NOW)
    assert error is None and rows and calls == []  # Prefetched: no network.
    strategy._daily_bars("BETA", NOW)
    assert calls == ["BETA"] and store.bars(DAY, "BETA", length) is not None  # Fetched once, kept for restarts.
    strategy._bars = {}
    strategy._daily_bars("BETA", NOW)
    assert calls == ["BETA"]  # A "restart" reads it back.


def test_prefetched_fred_and_earnings_need_no_network_even_without_budget(tmp_path):
    from scripts.autonomous_fund.events import EarningsCalendar, EventSettings
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.macro import SERIES, MacroSettings, MacroWatch
    store = DailyStore(tmp_path)
    for name in SERIES:
        store.put(DAY, f"fred_{name}", [["2026-10-30", 4.0]])
    store.put(DAY, "earnings", {"ALFA": {"source": "yahoo", "dates": [DAY], "error": None}})

    def offline(*args, **kwargs):
        raise AssertionError("no network expected")
    context = MacroWatch(MacroSettings(), environ={}, get=offline, store=store).context(DAY, may_fetch=lambda: False)
    assert not context["deferred"] and context["series"]["DGS10"]["latest"] == 4.0
    calendar = EarningsCalendar(Journal(tmp_path / "book", "synthetic-prefetch"), EventSettings(), environ={},
                                yahoo=offline, store=store)
    caps, upcoming, unknown = calendar.caps({"ALFA"}, date(2026, 11, 3), may_fetch=lambda: False)
    assert "ALFA" in caps and unknown == [] and not calendar.deferred
