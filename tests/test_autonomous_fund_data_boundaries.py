"""Trading-calendar and external-data regressions with synthetic responses only."""
from datetime import date
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.events import within
from scripts.autonomous_fund.history import AlpacaDailyHistory
from scripts.autonomous_fund.macro import SERIES, MacroSettings, MacroWatch, parse_csv, regime, summarize
from scripts.autonomous_fund.risk import RiskError


@pytest.mark.parametrize("today, event, window, expected", [
    ("2026-10-31", "2026-11-02", 0, False),
    ("2026-10-31", "2026-10-31", 0, True),
    ("2026-11-01", "2026-11-02", 1, True),
    ("2026-11-01", "2026-11-03", 1, False),
    ("2026-09-04", "2026-09-08", 1, True),
    ("2026-09-04", "2026-09-09", 1, False),
    ("2026-11-25", "2026-11-27", 1, True),
    ("2026-11-25", "2026-11-30", 1, False),
    ("2026-11-27", "2026-11-30", 1, True),
    ("2026-10-30", "2026-11-02", 1, True),
    ("2026-10-30", "2026-11-03", 1, False),
    ("2026-10-30", "2026-10-29", 5, False),
])
def test_earnings_window_counts_actual_sessions_after_today(today, event, window, expected):
    assert within(event, date.fromisoformat(today), window) is expected


def test_earnings_caps_include_the_next_session_across_a_holiday(tmp_path):
    from scripts.autonomous_fund.events import EarningsCalendar, EventSettings
    from scripts.autonomous_fund.journal import Journal
    dates = {"ALFA": ["2026-09-08"], "BETA": ["2026-09-09"]}
    calendar = EarningsCalendar(Journal(tmp_path, "synthetic-calendar-account"), EventSettings(), environ={},
                                yahoo=lambda symbol: dates[symbol])
    caps, upcoming, unknown = calendar.caps(set(dates), date(2026, 9, 4))
    assert set(caps) == {"ALFA"} and unknown == []
    assert upcoming == {symbol: values[0] for symbol, values in dates.items()}


class UnwritableStore:
    def version(self, *args):
        return None

    def put(self, *args):
        raise OSError("synthetic cache publication failure")


def test_valid_macro_data_remains_usable_when_the_optional_cache_cannot_be_written():
    watch = MacroWatch(MacroSettings(), environ={}, store=UnwritableStore())
    calls = []
    watch._fetch = lambda name, day: calls.append(name) or [("2026-11-02", 4.0)]
    for _ in range(2):
        context = watch.context("2026-11-03")
        assert all(value["latest"] == 4.0 for value in context["series"].values())
        assert context["deferred"] == []
    assert calls == list(SERIES)  # A failed cache write does not force another network request.


def test_real_macro_fetch_failures_remain_unavailable_and_budget_deferrals_do_not_fetch():
    calls = []
    watch = MacroWatch(MacroSettings(), environ={}, store=UnwritableStore())

    def fail(name, today):
        calls.append(name)
        raise ConnectionError("synthetic source outage")
    watch._fetch = fail
    deferred = watch.context("2026-11-03", may_fetch=lambda: False)
    assert set(deferred["deferred"]) == set(SERIES) and calls == []
    failed = watch.context("2026-11-03")
    assert all(value["unavailable"] == "ConnectionError" for value in failed["series"].values())


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", "1e1000"])
def test_fred_csv_excludes_nonfinite_cells_without_losing_finite_observations(invalid):
    rows = parse_csv(f"observation_date,X\n2026-10-30,4.0\n2026-11-02,{invalid}\n")
    assert rows == [("2026-10-30", 4.0)]


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
def test_fred_api_excludes_nonfinite_cells(invalid):
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"observations": [
        {"date": "2026-10-30", "value": "4.0"}, {"date": "2026-11-02", "value": invalid}]})
    watch = MacroWatch(MacroSettings(), environ={"FRED_API_KEY": "synthetic-key"}, get=lambda *a, **k: response)
    assert watch._fetch("DGS10", date(2026, 11, 3)) == [("2026-10-30", 4.0)]


def test_invalid_cached_macro_values_are_unknown_instead_of_false_calm_flags(tmp_path):
    from scripts.autonomous_fund.datastore import DailyStore
    store = DailyStore(tmp_path)
    for name in SERIES:
        store.put("2026-11-03", f"fred_{name}", [["2026-11-02", float("nan")]])
    watch = MacroWatch(MacroSettings(), environ={}, store=store)
    context = watch.context("2026-11-03", may_fetch=lambda: False)
    assert all("unavailable" in value for value in context["series"].values())
    assert context["regime"]["volatility_elevated"] is None
    assert context["regime"]["credit_stress"] is None


@pytest.mark.parametrize("broken", [[["2026-11-02", float("nan")]], 42])
def test_broken_macro_cache_is_refetched_once_when_budget_allows(tmp_path, broken):
    from scripts.autonomous_fund.datastore import DailyStore
    store = DailyStore(tmp_path)
    for name in SERIES:
        store.put("2026-11-03", f"fred_{name}", broken)
    watch = MacroWatch(MacroSettings(), environ={}, store=store)
    calls = []
    watch._fetch = lambda name, day: calls.append(name) or [("2026-11-02", 4.0)]
    for _ in range(2):
        result = watch.context("2026-11-03")
        assert all(value["latest"] == 4.0 for value in result["series"].values())
    assert calls == list(SERIES)


def test_zero_and_negative_finite_macro_values_remain_valid_and_future_values_are_excluded():
    rows = parse_csv("DATE,X\n2026-10-29,-0.25\n2026-10-30,0\n2026-11-03,99\n")
    assert rows == [("2026-10-29", -.25), ("2026-10-30", 0), ("2026-11-03", 99)]
    assert summarize(rows, "2026-11-03")["latest"] == 0
    assert regime({"T10Y2Y": summarize(rows, "2026-10-30")})["curve_inverted"] is True


def test_an_all_invalid_macro_refresh_is_not_reported_as_success():
    response = SimpleNamespace(raise_for_status=lambda: None,
                               text="observation_date,X\n2026-11-02,NaN\n")
    watch = MacroWatch(MacroSettings(), environ={}, get=lambda *a, **k: response)
    outcomes = watch.refresh("2026-11-03")
    assert set(outcomes) == set(SERIES) and all(value != "ok" for value in outcomes.values())


def test_the_data_desk_does_not_mark_all_invalid_macro_data_fresh(tmp_path):
    from tests.test_autonomous_fund_datadesk import DataDesk, NOW, config
    response = SimpleNamespace(raise_for_status=lambda: None,
                               text="observation_date,X\n2026-11-02,NaN\n")
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: NOW, environ={},
                    macro_get=lambda *a, **k: response)
    assert desk.run_job("macro") is None
    job = desk.status.read()["jobs"]["macro"]
    assert job["error"] and not job.get("ok_at")
    assert desk.store.get("2026-11-03", "fred_DGS10") is None


def bar(stamp="2026-10-30T04:00:00Z", close=100):
    return {"t": stamp, "c": close, "v": 1000, "o": 99, "h": 101, "l": 98}


def history(rows):
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"bars": rows})
    return AlpacaDailyHistory("synthetic-id", "synthetic-secret", get=lambda *a, **k: response)


@pytest.mark.parametrize("stamp", ["2026-10-30T12:00:00", "2026-10-30"])
def test_alpaca_history_does_not_invent_a_timezone_for_naive_timestamps(stamp):
    with pytest.raises(RiskError, match="timezone"):
        history([bar(stamp=stamp)]).bars("ALFA", 10, date(2026, 11, 3))


@pytest.mark.parametrize("close", [float("nan"), float("inf"), float("-inf"), 0, -1, True])
def test_unusable_alpaca_closes_are_not_returned_as_risk_history(close):
    with pytest.raises(RiskError):
        history([bar(close=close)]).bars("ALFA", 10, date(2026, 11, 3))


def test_valid_source_offsets_are_preserved_and_future_prices_are_never_used():
    rows = history([bar(stamp="2026-10-30T00:00:00-04:00"),
                    bar(stamp="2026-11-03T05:00:00Z", close=float("inf"))]).bars("ALFA", 10, date(2026, 11, 3))
    assert len(rows) == 1 and rows[0][0] == date(2026, 10, 30) and rows[0][1] == 100


def test_one_malformed_fallback_symbol_does_not_borrow_or_block_another_symbols_history(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, harness
    strategy = harness(tmp_path)

    def primary(*args, **kwargs):
        raise RiskError("synthetic primary-source outage")

    def get(url, **kwargs):
        rows = [bar(close=float("inf") if "/ALFA/" in url else 100)]
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"bars": rows})

    strategy.get_historical_prices = primary
    strategy.history_fallback = AlpacaDailyHistory("synthetic-id", "synthetic-secret", get=get)
    missing, error = strategy._daily_bars("ALFA", NOW)
    other, other_error = strategy._daily_bars("BETA", NOW)
    assert missing == [] and error is not None
    assert other and other_error is None and other[0][1] == 100
