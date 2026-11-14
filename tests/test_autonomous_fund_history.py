"""Alpaca as the second source of completed daily bars when Yahoo fails (local mode).

A fake HTTP client and synthetic bars only; no network or credentials.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.autonomous_fund.history import AlpacaDailyHistory
from scripts.autonomous_fund.risk import RiskError


def response(bars, token=None, status=200):
    return SimpleNamespace(status_code=status, raise_for_status=lambda: None,
                           json=lambda: {"bars": bars, "next_page_token": token})


def bar(day, close):
    # Alpaca stamps a daily bar at New York midnight: 04:00Z in summer, 05:00Z after DST ends (Nov 1, 2026).
    hour = "05" if day >= "2026-11-01" else "04"
    return {"t": f"{day}T{hour}:00:00Z", "o": close - 1, "h": close + 1, "l": close - 2, "c": close, "v": 1000}


def test_pages_are_joined_today_is_excluded_and_sip_falls_back_to_iex():
    seen = []

    def get(url, headers=None, params=None, timeout=None):
        seen.append(dict(params))
        if params["feed"] == "sip":
            return response([], status=403)
        if not params.get("page_token"):
            return response([bar("2026-10-29", 100.0)], token="next")
        return response([bar("2026-10-30", 101.0), bar("2026-11-02", 102.0)])
    history = AlpacaDailyHistory("synthetic-id", "synthetic-secret", get=get)
    rows = history.bars("ALFA", 10, date(2026, 11, 2))
    assert [(r[0].isoformat(), r[1]) for r in rows] == [("2026-10-29", 100.0), ("2026-10-30", 101.0)]
    assert rows[0][3:] == (99.0, 101.0, 98.0) and history.feed == "iex"
    # Contract correction: split-only prices omit spin-off restatements; cash
    # dividends remain excluded because the book accounts for them separately.
    assert all(p["adjustment"] == "split,spin-off" for p in seen)


def test_no_bars_is_an_error_not_an_empty_history():
    history = AlpacaDailyHistory("i", "s", get=lambda url, **kw: response([]))
    with pytest.raises(RiskError):
        history.bars("ALFA", 10, date(2026, 11, 2))
    with pytest.raises(RiskError):
        AlpacaDailyHistory("", "s")


def test_strategy_uses_the_fallback_only_when_yahoo_fails_and_records_it(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    strategy.get_historical_prices = lambda asset, length, timestep="day": (_ for _ in ()).throw(
        RiskError("Yahoo market data cooling down for ALFA; no stale fallback"))
    strategy.history_fallback = SimpleNamespace(feed="sip", bars=lambda symbol, length, today: [
        (date(2026, 10, 29), 100.0, 1e6, 99.0, 101.0, 98.0), (date(2026, 10, 30), 101.0, 1e6, 100.0, 102.0, 99.0)])
    rows, error = strategy._daily_bars("ALFA", NOW)
    assert error is None and [r[1] for r in rows] == [100.0, 101.0]
    kinds = [r["kind"] for r in strategy.journal.all_events()]
    assert "risk_history_fallback" in kinds and "risk_history_unavailable" not in kinds
    strategy._bars = {}
    strategy.history_fallback = SimpleNamespace(feed="sip", bars=lambda *a: (_ for _ in ()).throw(ConnectionError()))
    rows, error = strategy._daily_bars("ALFA", NOW)
    assert rows == [] and error == "RiskError"  # Both failed: honestly unavailable.


def test_invalid_primary_closes_in_old_cache_and_fresh_response_use_the_reference(tmp_path):
    from scripts.autonomous_fund.datastore import DailyStore
    from scripts.autonomous_fund.risk import trading_day
    from tests.test_autonomous_fund_quant import NOW, harness

    strategy = harness(tmp_path, mode="local")
    strategy.daily_store = DailyStore(tmp_path / "daily")
    day = trading_day(NOW)
    dates = [date(2026, 10, 29), date(2026, 10, 30)]
    strategy.daily_store.save_bars(day, "ALFA", 10000, [(d, 0, None, None, None, None) for d in dates])
    primary_reads = []

    def primary(asset, length, timestep):
        primary_reads.append(asset.symbol)
        return SimpleNamespace(df=pd.DataFrame({"close": [float("nan"), -3]}, index=pd.to_datetime(dates)))

    strategy.get_historical_prices = primary
    strategy.history_fallback = SimpleNamespace(feed="iex", bars=lambda *args: [
        (d, 100.0, 1e6, 99.0, 101.0, 98.0) for d in dates])
    got, error = strategy._daily_bars("ALFA", NOW)
    assert primary_reads == ["ALFA"] and error is None and [row[1] for row in got] == [100.0, 100.0]
    assert strategy.daily_store.bars(day, "ALFA", 1) is None  # Invalid Yahoo history is still a cache miss.
    [event] = strategy.journal.events(("risk_history_fallback",))
    assert event["body"]["feed"] == "iex"
