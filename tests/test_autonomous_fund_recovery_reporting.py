"""Transient history recovery and truthful account reports; synthetic data only."""
import csv
from datetime import timedelta

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.report import report
from tests.test_autonomous_fund_quant import NOW, default_frames, harness


def mark(journal, at, equity, benchmark=None):
    journal.event(at, "account", {"as_of": str(at), "equity": str(equity), "cash": str(equity), "positions": {}})
    if benchmark is not None:
        journal.event(at, "benchmark", {"SPY": str(benchmark), "QQQ": str(benchmark)})


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_live_history_failures_recover_after_bounded_backoff(tmp_path, monkeypatch, mode, failure):
    import scripts.autonomous_fund.strategy as module
    ticks, calls = [100.0], []
    monkeypatch.setattr(module, "monotonic", lambda: ticks[0])
    strategy = harness(tmp_path, default_frames(), mode=mode)
    original = strategy.get_historical_prices
    def fetch(asset, *args, **kwargs):
        calls.append(asset.symbol)
        if len(calls) == 1:
            if failure == "exception":
                raise ConnectionError("synthetic outage")
            return None
        return original(asset, *args, **kwargs)
    strategy.get_historical_prices = fetch
    assert strategy._daily_closes("ALFA", NOW) == []
    for _ in range(5):
        assert strategy._daily_closes("ALFA", NOW) == []
    assert calls == ["ALFA"]  # An iteration loop must not hammer the failed source.
    ticks[0] += 60
    assert strategy._daily_closes("ALFA", NOW + timedelta(minutes=1))
    assert calls == ["ALFA", "ALFA"]
    strategy._daily_closes("ALFA", NOW + timedelta(minutes=2))
    assert calls == ["ALFA", "ALFA"]  # Successful histories still reuse the daily cache.


def test_cached_risk_and_snapshot_refresh_after_a_source_recovers(tmp_path, monkeypatch):
    import scripts.autonomous_fund.strategy as module
    ticks, unavailable = [100.0], [True]
    monkeypatch.setattr(module, "monotonic", lambda: ticks[0])
    strategy = harness(tmp_path, default_frames(), mode="local")
    original = strategy.get_historical_prices
    def fetch(asset, *args, **kwargs):
        if asset.symbol == "ALFA" and unavailable[0]:
            raise ConnectionError("synthetic outage")
        return original(asset, *args, **kwargs)
    strategy.get_historical_prices = fetch
    first = strategy._risk_estimate({"ALFA"}, NOW)
    snapshot = strategy._market_snapshot(NOW)
    assert "ALFA" in first.missing and "ALFA" in snapshot["no_history"]
    unavailable[0] = False
    ticks[0] += 60
    later = strategy._risk_estimate({"ALFA"}, NOW + timedelta(minutes=1))
    assert "ALFA" not in later.missing and later is not first
    assert "ALFA" in strategy._market_snapshot(NOW + timedelta(minutes=1))["symbols"]


def test_replay_history_retries_do_not_depend_on_wall_clock(tmp_path, monkeypatch):
    import scripts.autonomous_fund.strategy as module
    ticks, calls = [100.0], []
    monkeypatch.setattr(module, "monotonic", lambda: ticks[0])
    strategy = harness(tmp_path, mode="mock")
    def fetch(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("synthetic outage")
    strategy.get_historical_prices = fetch
    assert strategy._daily_closes("ALFA", NOW) == []
    ticks[0] += 600
    assert strategy._daily_closes("ALFA", NOW) == [] and calls == [1]


def test_reports_group_by_new_york_date_and_preserve_chronological_account_marks(tmp_path):
    journal = Journal(tmp_path, "synthetic-new-york-book")
    mark(journal, "2026-11-02T23:00:00+00:00", 10000, 500)
    mark(journal, "2026-11-03T09:00:00+08:00", 10100, 505)  # Still November 2 in New York.
    assert report(tmp_path, write_files=False)["observations"] == 1
    mark(journal, "2026-11-03T21:00:00+00:00", 10200, 510)
    # Backfilled older evidence must not become today's last account observation.
    mark(journal, "2026-11-02T22:00:00+00:00", 9900, 495)
    result = report(tmp_path)
    assert result["observations"] == 2
    assert result["starting_equity"] == 9900 and result["ending_equity"] == 10200
    assert result["latest_account"]["equity"] == 10200
    assert result["return"] == pytest.approx(10200 / 9900 - 1)
    with (tmp_path / "equity.csv").open() as stream:
        curve = list(csv.DictReader(stream))
    assert [float(row["equity"]) for row in curve] == [10100, 10200]
    assert float(curve[1]["daily_return"]) == pytest.approx(10200 / 10100 - 1)


def test_benchmarks_require_the_same_start_and_end_account_observations(tmp_path):
    journal = Journal(tmp_path, "synthetic-aligned-benchmark")
    mark(journal, "2026-11-02T15:00:00+00:00", 10000, 500)
    mark(journal, "2026-11-02T21:00:00+00:00", 10100, 510)
    mark(journal, "2026-11-03T15:00:00+00:00", 11000, 550)
    mark(journal, "2026-11-03T21:00:00+00:00", 12000)
    assert report(tmp_path, write_files=False)["benchmark_price_returns"]["SPY"] is None
    # Equivalent offsets identify the same observation, not a different benchmark window.
    journal.event("2026-11-04T05:00:00+08:00", "benchmark", {"SPY": "600", "QQQ": "600"})
    result = report(tmp_path, write_files=False)
    assert result["benchmark_price_returns"]["SPY"] == pytest.approx(.2)
    assert result["return"] == pytest.approx(.2)


def test_benchmark_comparison_preserves_operator_model_rates(tmp_path):
    journal = Journal(tmp_path, "synthetic-price-tables-book")
    mark(journal, NOW, 10000, 500)
    mark(journal, NOW + timedelta(days=1), 11000, 550)
    journal.event(NOW, "agent", {"name": "manager", "model": "synthetic/model",
                                  "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})
    result = report(tmp_path, write_files=False, as_of=NOW + timedelta(days=1),
                    prices={"synthetic/model": {"input": 1, "output": 2}})
    assert result["benchmark_price_returns"]["SPY"] == pytest.approx(.1)
    assert result["model_cost_estimate_usd"] == 1
    assert result["model_costs"]["window_usd"] == 1 and result["model_costs"]["unpriced_calls"] == 0


@pytest.mark.parametrize("equities", [[0], [0, 0], [10000, 0], [0, 10000], [10000, 0, 11000]])
def test_zero_equity_reports_stay_readable_without_inventing_returns(tmp_path, equities):
    journal = Journal(tmp_path, "synthetic-zero-equity-book")
    for index, equity in enumerate(equities):
        mark(journal, NOW + timedelta(days=index), equity)
    result = report(tmp_path)
    assert result["latest_account"]["equity"] == equities[-1]
    if equities[0] == 0:
        assert result["return"] is None
    if max(equities) == 0:
        assert result["max_observed_drawdown"] is None
    elif equities[0] > 0 and 0 in equities:
        assert result["max_observed_drawdown"] == 1
    if 0 in equities[:-1]:
        assert result["daily_sharpe_zero_risk_free"] is None
