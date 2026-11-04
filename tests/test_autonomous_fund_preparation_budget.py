"""Optional data work never stalls a live trading iteration.

Reproduces the 2026-11-02 server failure: after a midday restart the first
decision rebuilt the factor research panel (research universe history, SEC
filings), the earnings calendar and FRED context synchronously. The iteration
recorded no progress event for more than the monitor's 180 seconds, the monitor
wrote an iteration_stalled STOP, every restart lost the in-memory caches and
stalled again until automatic resumes were exhausted.

Synthetic symbols, a fake clock and fake fetches only; no network.
"""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund import strategy as strategy_module
from scripts.autonomous_fund.events import EarningsCalendar, EventSettings
from scripts.autonomous_fund.factors import FactorLab, FactorSettings
from scripts.autonomous_fund.macro import MacroSettings, MacroWatch
from tests.test_autonomous_fund_quant import NOW, harness

RESEARCH = [f"R{i:02d}" for i in range(20)]


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(strategy_module, "monotonic", fake)
    return fake


def slow_strategy(tmp_path, clock, mode="local"):
    days = pd.bdate_range(end=NOW.date() - pd.Timedelta(days=1), periods=300, tz="America/New_York")
    frame = pd.DataFrame({"close": 100 * np.cumprod(1 + np.random.default_rng(1).normal(0, .01, len(days))),
                          "volume": 1e6}, index=days)
    strategy = harness(tmp_path, mode=mode)
    strategy.universe = frozenset({"ALFA"})
    strategy.config = {"universe": {"ALFA": "stock"}}
    strategy.factor_lab = FactorLab(strategy.journal, FactorSettings(research_universe=RESEARCH))
    strategy._factor_cache = strategy._factor_context_cache = None
    fetched = []

    def download(asset, length, timestep="day"):
        clock.value += 10  # Each history download takes ten seconds.
        fetched.append(asset.symbol)
        return SimpleNamespace(df=frame)
    strategy.get_historical_prices = download
    return strategy, fetched


def test_factor_research_stops_at_the_iteration_budget_and_resumes_next_iteration(tmp_path, clock):
    strategy, fetched = slow_strategy(tmp_path, clock)
    strategy._begin_preparation()
    first = strategy._factor_context(NOW)
    assert clock.value - 1000 <= strategy_module.PREPARATION_BUDGET_SECONDS + 10  # Bounded.
    assert first["warming"]["pending"] > 0 and len(fetched) < len(RESEARCH) + 1
    strategy._end_preparation()
    for _ in range(10):
        strategy._begin_preparation()
        context = strategy._factor_context(NOW)
        strategy._end_preparation()
        if "warming" not in context:
            break
    assert "warming" not in context and sorted(set(fetched)) == sorted(set(RESEARCH) | {"ALFA"})
    assert len(fetched) == len(set(fetched))  # Progress is kept: no symbol downloaded twice.


def test_outside_a_live_iteration_there_is_no_budget(tmp_path, clock):
    strategy, fetched = slow_strategy(tmp_path, clock)
    context = strategy._factor_context(NOW)  # Pre-open and post-close hooks: complete in one call.
    assert "warming" not in context and len(fetched) == len(RESEARCH) + 1


def test_only_live_modes_set_a_budget(tmp_path, clock):
    live, _ = slow_strategy(tmp_path / "live", clock)
    replay, _ = slow_strategy(tmp_path / "replay", clock, mode="mock")
    live._begin_preparation()
    replay._begin_preparation()
    assert live._preparation_left() is not None and replay._preparation_left() is None  # Replays stay deterministic.


def test_the_earnings_calendar_and_fred_defer_instead_of_blocking(tmp_path, clock):
    strategy, _ = slow_strategy(tmp_path, clock)
    stocks = {f"S{i}" for i in range(12)}
    strategy.universe = frozenset(stocks)
    strategy.config = {"universe": {s: "stock" for s in stocks}}

    def calendar(symbol):
        clock.value += 10
        return []
    strategy.earnings = EarningsCalendar(strategy.journal, EventSettings(), environ={}, yahoo=calendar)
    strategy._begin_preparation()
    first = strategy._earnings_caps(NOW)
    assert first["deferred"] and clock.value - 1000 <= strategy_module.PREPARATION_BUDGET_SECONDS + 10
    strategy._end_preparation()
    for _ in range(5):
        strategy._begin_preparation()
        result = strategy._earnings_caps(NOW)
        strategy._end_preparation()
    assert not result["deferred"] and result["unknown"] == []  # Completed, then cached for the day.

    def fred(url, params=None, timeout=None, headers=None):
        clock.value += 20
        return SimpleNamespace(raise_for_status=lambda: None, text="observation_date,X\n2026-10-30,1.0\n")
    # Preparation belongs to the app context, independently of native FRED tools.
    strategy.macro_watch = MacroWatch(MacroSettings(), environ={}, get=fred)
    strategy._begin_preparation()
    partial = strategy._macro_context(NOW)
    assert any(item.get("unavailable") == "deferred" for item in partial["series"].values())
    strategy._end_preparation()
    strategy._begin_preparation()
    complete = strategy._macro_context(NOW)
    strategy._end_preparation()
    assert not any(item.get("unavailable") == "deferred" for item in complete["series"].values())


def test_sec_downloads_are_deferred_without_losing_cached_companies(tmp_path):
    from datetime import date
    from scripts.autonomous_fund.fundamentals import SecFundamentals
    from tests.test_autonomous_fund_factor_research import payload
    downloads = []

    def get(url, headers=None, timeout=None):
        downloads.append(url)
        body = ({str(i): {"cik_str": 100 + i, "ticker": s, "title": s} for i, s in enumerate(["AAA", "BBB", "CCC"])}
                if "company_tickers" in url else payload())
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    client = SecFundamentals(tmp_path, "Synthetic Operator ops@example.com", get=get, sleep=lambda s: None,
                             today=lambda: date(2026, 3, 1))
    index = pd.bdate_range("2026-02-09", "2026-02-13")
    allowed = iter([True, False])  # Time for one company only.
    first = client.panels({"AAA", "BBB", "CCC"}, index, may_download=lambda: next(allowed, False))
    assert client.deferred == ["BBB", "CCC"] and first["earnings"]["AAA"].iloc[-1] == 46
    second = client.panels({"AAA", "BBB", "CCC"}, index, may_download=lambda: True)
    assert client.deferred == [] and second["earnings"]["CCC"].iloc[-1] == 46
    assert sum("companyfacts" in url for url in downloads) == 3  # AAA was not downloaded twice.


def test_stress_histories_defer_but_the_benchmark_is_always_fetched(tmp_path, clock):
    days = pd.bdate_range("2019-12-02", "2020-06-30", tz="America/New_York")
    frame = pd.DataFrame({"close": np.linspace(100, 70, len(days))}, index=days)
    strategy = harness(tmp_path, mode="local", quant={"stress_scenarios": {"covid_2020": ["2020-02-19", "2020-03-23"]}})
    fetched = []

    def download(asset, length, timestep="day"):
        clock.value += 30
        fetched.append(asset.symbol)
        return SimpleNamespace(df=frame)
    strategy.get_historical_prices = download
    clock.value += strategy_module.PREPARATION_BUDGET_SECONDS + 1
    strategy._begin_preparation()
    clock.value += strategy_module.PREPARATION_BUDGET_SECONDS + 1  # The budget is already spent.
    result = strategy._scenario_returns({"ALFA", "BETA"}, NOW)
    assert fetched == ["SPY"] and result["covid_2020"]["benchmark"] is not None
    assert sorted(strategy._scenario_deferred) == ["ALFA", "BETA"]
    strategy._end_preparation()
    strategy._scenario_returns({"ALFA", "BETA"}, NOW)
    assert sorted(fetched) == ["ALFA", "BETA", "SPY"] and strategy._scenario_deferred == []
