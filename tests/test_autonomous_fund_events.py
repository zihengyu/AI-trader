"""Earnings-date event risk and FRED macro context.

Neutral symbols, synthetic calendars and synthetic FRED responses only; no network.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.events import EarningsCalendar, EventSettings, within
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.macro import MacroSettings, MacroWatch, parse_csv, regime, summarize
from scripts.autonomous_fund.risk import RiskError, number

FRIDAY = date(2026, 10, 30)


def test_window_covers_today_and_the_next_business_days_only():
    assert within("2026-10-30", FRIDAY, 1) and within("2026-11-02", FRIDAY, 1)  # Friday -> Monday.
    assert not within("2026-11-03", FRIDAY, 1) and not within("2026-10-29", FRIDAY, 1)
    assert within("2026-10-30", FRIDAY, 0) and not within("2026-11-02", FRIDAY, 0)


def calendar(tmp_path, dates=None, error=None, environ=None, get=None):
    calls = []

    def yahoo(symbol):
        calls.append(symbol)
        if error:
            raise error
        return (dates or {}).get(symbol, [])
    book = EarningsCalendar(Journal(tmp_path, "synthetic-events"), EventSettings(), environ=environ or {},
                            get=get, yahoo=yahoo)
    return book, calls


def test_a_stock_reporting_inside_the_window_is_capped_and_fetched_once_per_day(tmp_path):
    book, calls = calendar(tmp_path, {"ALFA": ["2026-11-02"], "BETA": ["2026-12-01"]})
    caps, upcoming, unknown = book.caps({"ALFA", "BETA"}, FRIDAY)
    assert caps == {"ALFA": number("0.05")} and upcoming == {"ALFA": "2026-11-02", "BETA": "2026-12-01"}
    assert unknown == []
    book.caps({"ALFA", "BETA"}, FRIDAY)
    assert sorted(calls) == ["ALFA", "BETA"]  # Persisted for the day.


def test_an_unknown_date_is_never_guessed(tmp_path):
    book, _ = calendar(tmp_path, error=ConnectionError("down"))
    caps, upcoming, unknown = book.caps({"ALFA"}, FRIDAY)
    assert caps == {} and upcoming == {} and unknown == ["ALFA"]


def test_finnhub_is_used_when_its_key_is_configured(tmp_path):
    seen = {}

    def get(url, params=None, timeout=None):
        seen.update(params)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"earningsCalendar": [{"date": "2026-10-30", "symbol": "ALFA"}]})
    book, calls = calendar(tmp_path, environ={"FINNHUB_API_KEY": "synthetic-key"}, get=get)
    caps, _, _ = book.caps({"ALFA"}, FRIDAY)
    assert caps == {"ALFA": number("0.05")} and calls == [] and seen["token"] == "synthetic-key"


def test_event_settings_validate():
    with pytest.raises(RiskError):
        EventSettings.from_config({"earnings_max_position": "1.5"})
    with pytest.raises(RiskError):
        EventSettings.from_config({"source": "somewhere"})
    with pytest.raises(RiskError):
        EventSettings.from_config({"unknown": 1})


def test_strategy_caps_a_reporting_stock_but_not_an_etf_and_only_in_live_modes(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, accept, account, default_frames, harness, quote
    for mode, expected in (("local", True), ("mock", False)):
        strategy = harness(tmp_path / mode, default_frames(), mode=mode)
        strategy.config = {"universe": {"ALFA": "stock", "SPY": "unlevered_etf", "BETA": "stock",
                                        "GAMMA": "stock", "DELTA": "stock"}}
        strategy.earnings = EarningsCalendar(strategy.journal, EventSettings(), environ={},
                                             yahoo=lambda s: [NOW.date().isoformat()] if s in {"ALFA", "SPY"} else [])
        strategy.journal.observe_session(NOW, account())
        active = accept(strategy, [("ALFA", .09), ("SPY", .09)])
        result = strategy._candidates(active, account(), {"ALFA": quote(), "SPY": quote()}, NOW)
        weights = result["weights"]
        if expected:
            assert weights["ALFA"] <= number("0.05") and "earnings:ALFA" in result["overlay"]["binding"]
            assert weights["SPY"] > number("0.05")  # ETFs carry no single-company earnings gap.
        else:
            assert weights["ALFA"] > number("0.05")  # Historical runs do not use today's calendar.


# ----- FRED macro context -----------------------------------------------------------------

CSV = "observation_date,DGS10\n2026-10-27,4.10\n2026-10-28,.\n2026-10-29,4.20\n2026-10-30,4.30\n"


def test_fred_csv_parsing_skips_missing_values_and_accepts_either_header():
    assert parse_csv(CSV) == [("2026-10-27", 4.10), ("2026-10-29", 4.20), ("2026-10-30", 4.30)]
    assert parse_csv("DATE,X\n2026-01-02,1\n") == [("2026-01-02", 1.0)]


def test_macro_summary_uses_only_observations_before_the_as_of_day():
    summary = summarize(parse_csv(CSV), "2026-10-30")
    assert summary["latest"] == 4.20 and summary["date"] == "2026-10-29"  # The 30th is not known yet.
    assert summarize([], "2026-10-30") == {"unavailable": "no observation before the as-of day"}


def test_regime_flags_and_failures_are_explicit():
    flags = regime({"T10Y2Y": {"latest": -0.2}, "BAMLH0A0HYM2": {"latest": 4.0, "change_20_obs": 1.2},
                    "VIXCLS": {"latest": 18.0}, "DGS3MO": {"latest": 4.1}})
    assert flags["curve_inverted"] and flags["credit_stress"] and not flags["volatility_elevated"]
    assert flags["cash_yield_pct"] == 4.1
    assert regime({})["curve_inverted"] is None

    def get(url, params=None, timeout=None, headers=None):
        raise ConnectionError("blocked")
    context = MacroWatch(MacroSettings(), environ={}, get=get).context("2026-10-30")
    assert all("unavailable" in item for item in context["series"].values())


def test_macro_uses_the_official_api_with_a_key_and_fetches_once_per_day():
    calls = []

    def get(url, params=None, timeout=None, headers=None):
        calls.append((url, params.get("series_id") or params.get("id")))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"observations": [
            {"date": "2026-10-28", "value": "4.0"}, {"date": "2026-10-29", "value": "."}]})
    watch = MacroWatch(MacroSettings(), environ={"FRED_API_KEY": "synthetic"}, get=get)
    first = watch.context("2026-10-30")
    assert first["series"]["DGS10"]["latest"] == 4.0
    watch.context("2026-10-30")
    assert len(calls) == 6 and all("api.stlouisfed.org" in url for url, _ in calls)


def test_strategy_exposes_macro_context_once_per_day(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)
    calls = []
    strategy.macro = SimpleNamespace(context=lambda day: calls.append(day) or {"regime": {}, "as_of_day": day})
    first = strategy._macro_context(NOW)
    assert first["as_of_day"] == "2026-11-02" and strategy._macro_context(NOW) is first and len(calls) == 1


def test_a_hung_calendar_request_is_bounded_and_reported_unknown(tmp_path):
    import threading
    release = threading.Event()
    book = EarningsCalendar(Journal(tmp_path, "synthetic-hang"), EventSettings(), environ={},
                            yahoo=lambda symbol: release.wait(30) or [], timeout_seconds=0.2)
    caps, upcoming, unknown = book.caps({"ALFA"}, FRIDAY)
    release.set()
    assert caps == {} and unknown == ["ALFA"]


def test_the_earnings_calendar_is_fetched_once_per_day_and_reused(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path, mode="local")
    strategy.config = {"universe": {"ALFA": "stock"}}
    strategy.universe = frozenset({"ALFA"})
    calls = []
    strategy.earnings = EarningsCalendar(strategy.journal, EventSettings(), environ={},
                                         yahoo=lambda s: calls.append(s) or [NOW.date().isoformat()])
    first = strategy._earnings_caps(NOW)
    assert first["caps"] == {"ALFA": number("0.05")} and strategy._earnings_caps(NOW) is first and calls == ["ALFA"]
