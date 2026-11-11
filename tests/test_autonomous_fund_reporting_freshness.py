"""Reporting regressions with synthetic prices, account journals and clocks only."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.costs import cost_summary
from scripts.autonomous_fund.datastore import DailyStore
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.status import cost_line
from tests.test_autonomous_fund_quant import NOW, default_frames, harness


@pytest.mark.parametrize("close", [
    "2026-11-05T21:00:00+00:00",  # Standard time.
    "2026-07-06T20:00:00+00:00",  # Daylight saving time.
    "2026-11-27T18:00:00+00:00",  # Thanksgiving Friday: 13:00 New York.
])
def test_daily_prices_refresh_at_the_exchange_close_without_exposing_a_forming_bar(close):
    close = datetime.fromisoformat(close)
    clock = SimpleNamespace(now=close - timedelta(seconds=1))
    day = close.astimezone(ZoneInfo("America/New_York")).date()
    index = pd.date_range(end=day + timedelta(days=1), periods=4, tz="America/New_York")
    frame = pd.DataFrame({"open": 100, "high": 103, "low": 99, "close": 102, "volume": 1e6}, index=index)
    calls = []
    source = YahooRealtimeData(now=lambda: clock.now,
                              fetch=lambda *args: calls.append(args) or frame)
    asset = Asset("ALFA")
    assert max(source.get_historical_prices(asset, 10, timestep="day").df.index.date) < day
    clock.now = close
    assert max(source.get_historical_prices(asset, 10, timestep="day").df.index.date) == day
    assert len(calls) == 2  # The still-fresh pre-close response must be fetched again.
    clock.now = close - timedelta(seconds=1)
    assert max(source.get_historical_prices(asset, 10, timestep="day").df.index.date) < day


def test_attribution_refreshes_prefetched_and_in_memory_prices_after_close(tmp_path):
    close = datetime.fromisoformat("2026-11-05T21:00:00+00:00")
    clock = SimpleNamespace(now=close - timedelta(minutes=1))
    index = pd.DatetimeIndex(["2026-11-04", "2026-11-05", "2026-11-06"], tz="America/New_York")
    def fetch(symbol, *args):
        values = [500, 505, 999] if symbol == "SPY" else [100, 102, 999]
        return pd.DataFrame({"open": values, "high": values, "low": values,
                             "close": values, "volume": 1e6}, index=index)
    source = YahooRealtimeData(now=lambda: clock.now, fetch=fetch)
    strategy = harness(tmp_path, mode="local")
    strategy.get_datetime = lambda: clock.now
    strategy.get_historical_prices = source.get_historical_prices
    strategy.daily_store = store = DailyStore(tmp_path / "daily")
    for symbol in ("ALFA", "SPY"):
        strategy._daily_closes(symbol, clock.now)  # Populate all three pre-close caches.
    strategy.journal.event("2026-11-04T21:00:00+00:00", "account", {
        "equity": "10000", "cash": "9000", "positions": {"ALFA": "10"}})
    strategy.journal.event(close, "account", {
        "equity": "10020", "cash": "9000", "positions": {"ALFA": "10"}})
    clock.now = close + timedelta(minutes=5)
    result = strategy._attribution({"session_date": "2026-11-05"}, clock.now)
    assert result["status"] == "complete" and not result["unavailable"]
    assert result["components"]["market"] == pytest.approx(.001)
    assert result["components"]["selection"] == pytest.approx(.001)
    assert result["unexplained"] == pytest.approx(0)
    # A restart reuses the completed close; an intraday reader still gets only prior sessions.
    strategy._bars = {}
    strategy.get_historical_prices = lambda *a, **k: pytest.fail("completed prices are already stored")
    assert strategy._daily_closes("ALFA", clock.now)[-1] == ("2026-11-05", 102.0)
    prior = store.bars("2026-11-05", "ALFA", strategy.quant.es_lookback_days + 5)
    assert all(str(row[0]) < "2026-11-05" for row in prior)


def test_daily_prices_use_new_york_dates_even_when_the_caller_is_already_tomorrow(tmp_path):
    strategy = harness(tmp_path)
    rows = [(datetime.fromisoformat(day).date(), 100, 1e6)
            for day in ("2026-11-04", "2026-11-05", "2026-11-06")]
    # 15:59 New York, although the caller's local date is already November 6.
    now = datetime.fromisoformat("2026-11-06T10:59:00+14:00")
    assert [row[0] for row in strategy._completed(rows, now)] == ["2026-11-04"]


def test_post_close_fallback_requests_the_entire_last_completed_session(tmp_path):
    from scripts.autonomous_fund.history import AlpacaDailyHistory
    from tests.test_autonomous_fund_history import bar, response
    strategy = harness(tmp_path, mode="local")
    strategy.get_historical_prices = lambda *a, **k: (_ for _ in ()).throw(ConnectionError())
    requests = []
    def get(url, **kwargs):
        requests.append(kwargs["params"])
        return response([bar("2026-11-04", 100), bar("2026-11-05", 102), bar("2026-11-06", 999)])
    strategy.history_fallback = AlpacaDailyHistory("synthetic-key", "synthetic-secret", get=get)
    now = datetime.fromisoformat("2026-11-05T21:05:00+00:00")
    assert strategy._daily_closes("ALFA", now)[-1] == ("2026-11-05", 102)
    assert requests[0]["end"] == "2026-11-06T00:00:00-05:00"


def test_scorecards_refresh_for_new_visible_events_and_ignore_future_events(tmp_path):
    strategy = harness(tmp_path, default_frames(), mode="local")
    first = strategy._scorecards(NOW)
    # Unrelated liveness rows must not force another price read or recomputation.
    strategy.journal.event(NOW, "heartbeat", {})
    assert strategy._scorecards(NOW) is first
    at = NOW + timedelta(minutes=1)
    idea = {"idea_id": "synthetic-idea", "symbol": "ALFA", "benchmark": "SPY", "direction": "up",
            "horizon_sessions": 1, "confidence": .6, "formed_at": at.isoformat()}
    strategy.journal.event(at, "research_idea", idea)
    assert strategy._scorecards(NOW) is first  # It is not public to this strategy clock yet.
    updated = strategy._scorecards(at)
    assert updated["ideas"]["total"] == 1 and updated is not first
    assert strategy._scorecards(at) is updated
    strategy.journal.event(at + timedelta(days=1), "research_idea", {**idea, "idea_id": "future-idea"})
    assert strategy._scorecards(at) is updated


def test_scorecards_refresh_when_the_current_session_finishes(tmp_path):
    strategy = harness(tmp_path, default_frames(), mode="local")
    before = datetime.fromisoformat("2026-11-02T20:59:59+00:00")
    first = strategy._scorecards(before)
    after = strategy._scorecards(before + timedelta(seconds=1))
    assert after is not first and after["as_of"] != first["as_of"]


def test_scorecards_refresh_on_the_next_date_even_with_the_same_close_cutoff(tmp_path):
    strategy = harness(tmp_path, default_frames(), mode="local")
    first = strategy._scorecards(datetime.fromisoformat("2026-11-02T21:05:00+00:00"))
    next_day = strategy._scorecards(datetime.fromisoformat("2026-11-03T13:00:00+00:00"))
    assert next_day is not first


def test_historical_mandate_is_rebuilt_from_its_own_events(tmp_path):
    journal = Journal(tmp_path, "synthetic-mandate-history")
    start = datetime.fromisoformat("2026-11-02T15:00:00+00:00")
    journal.apply_mandate(start, "10000", "50000", "50000", {}, False, {}, True)
    journal.apply_mandate(start + timedelta(days=1), "15000", "50000", "50000", {}, False, {}, True)
    journal.adjust_reserve(start + timedelta(days=2), "2000")
    def snapshot(at):
        return report(tmp_path, write_files=False, as_of=at)["mandate"]
    assert snapshot(start)["capital_usd"] == "10000"
    assert snapshot(start)["reserved_cash"] == "40000"
    assert snapshot(start + timedelta(days=1))["capital_usd"] == "15000"
    assert snapshot(start + timedelta(days=1))["reserved_cash"] == "35000"
    assert snapshot(start + timedelta(days=2))["reserved_cash"] == "37000"
    assert snapshot(start - timedelta(seconds=1))["capital_usd"] is None
    assert report(tmp_path, write_files=False)["mandate"]["reserved_cash"] == "37000"


def test_missing_historical_mandate_evidence_never_uses_current_metadata(tmp_path):
    journal = Journal(tmp_path, "synthetic-legacy-mandate")
    with journal.connection() as db:
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            ("mandate_capital_basis", "10000"), ("mandate_reserve_cash", "40000")])
    snapshot = report(tmp_path, write_files=False, as_of=NOW)
    assert snapshot["mandate"]["capital_usd"] is None
    assert snapshot["mandate"]["reserved_cash"] is None


def test_historical_targets_approvals_and_monitor_do_not_borrow_future_state(tmp_path):
    from tests.test_autonomous_fund_execution import LIMITS, UNIVERSE, authorize, envelope, setup
    journal, execution, _, active = setup(tmp_path)
    request = execution.request(active, envelope(), NOW)
    pending = report(tmp_path, write_files=False, as_of=NOW)
    assert pending["active_allocation"]["id"] == active["id"]
    assert pending["pending_approval_count"] == 1
    assert pending["approval_requests"][0]["effective_state"] == "pending"
    at = NOW + timedelta(minutes=1)
    authorize(tmp_path, request, at)
    approved = report(tmp_path, write_files=False, as_of=at)
    assert approved["approval_requests"][0]["state"] == "approved"
    execution.complete(active["id"], at + timedelta(seconds=1))
    assert report(tmp_path, write_files=False, as_of=at + timedelta(seconds=1))["active_allocation"]["state"] == "complete"
    execution.accept(active["decision"], at + timedelta(minutes=1), UNIVERSE, LIMITS)
    (tmp_path / "monitor.json").write_text('{"at":"2026-11-03T15:00:00+00:00","health":"healthy"}')
    assert report(tmp_path, write_files=False, as_of=NOW) == pending
    assert report(tmp_path, write_files=False, as_of=at) == approved
    assert report(tmp_path, write_files=False, as_of=NOW - timedelta(seconds=1))["approval_requests"] == []


def test_daily_document_keeps_its_original_capital_after_a_later_change(tmp_path):
    from scripts.autonomous_fund.notifications import daily_document
    from tests.test_autonomous_fund_review import CLOSE, TRADE, review_harness
    strategy, _ = review_harness(tmp_path)
    strategy.journal.apply_mandate(TRADE, "10000", "50000", "50000", {}, False, {}, True)
    strategy.after_market_closes()
    with strategy.journal.connection() as db:
        sequence = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review'").fetchone()[0]
    before = daily_document(tmp_path, sequence)
    strategy.journal.apply_mandate(CLOSE + timedelta(days=1), "15000", "50000", "50000", {}, False, {}, True)
    assert daily_document(tmp_path, sequence) == before
    assert "USD：10000" in before[1] and "USD：40000" in before[1]


def test_recent_model_cost_call_count_uses_the_same_window_as_costs():
    def call(at, cost):
        # This test prices complete usage within a time window. Missing output
        # usage now stays unknown; an observed zero must be explicit instead.
        return {"at": at.isoformat(), "kind": "agent",
                "body": {"model": "synthetic/model", "name": "manager",
                         "usage": {"prompt_tokens": 100, "completion_tokens": 0}, "cost_usd": cost}}
    rows = [call(NOW - timedelta(days=31), 2), call(NOW - timedelta(days=30), 1),
            call(NOW, None), call(NOW + timedelta(seconds=1), 9)]
    summary = cost_summary(rows, equity=10000, now=NOW)
    assert summary["calls"] == 2 and summary["window_usd"] == 1
    assert summary["unpriced_calls"] == 1 and summary["tokens"]["input_tokens"] == 200
    empty = cost_summary(rows[:1], equity=10000, now=NOW)
    assert empty["calls"] == 0 and empty["unpriced_calls"] == 0
    assert cost_line(empty) is None  # Old priced calls must never become an "unpriced" warning.
    future = call(NOW + timedelta(seconds=1), None)
    future["body"]["model"] = "synthetic/future-model"
    priced = cost_summary([rows[2], future], now=NOW, prices={"input": 1, "output": 2})
    assert priced["calls"] == 1 and priced["unpriced_calls"] == 0
    assert priced["window_usd"] == .0001  # A future model must not alter today's pricing scope.
