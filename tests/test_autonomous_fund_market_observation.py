"""Qualify the parallel reader with synthetic feeds and a real read-only book."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from requests import Response

from lumibot.entities import Asset
from scripts.autonomous_fund.datadesk import DataDesk
from scripts.autonomous_fund.history import ACTIONS_URL, MINUTES_URL, SNAPSHOTS_URL, AlpacaDailyHistory
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.market_observation import SourceObserver, observation_lines
from scripts.autonomous_fund.risk import Limits, MarketDataUnavailable
from scripts.autonomous_fund.strategy import configuration


NOW = datetime(2026, 11, 19, 14, 32, 5, tzinfo=timezone.utc)
NY = ZoneInfo("America/New_York")


def bar(start, price=100.05):
    return {"t": start.isoformat(), "o": 100, "h": 101, "l": 99, "c": price, "v": 1500}


def snapshot(now, *, quote_at=None, minute_start=None):
    return {
        "latestQuote": {"t": (quote_at or now - timedelta(seconds=1)).isoformat(), "bp": 100, "ap": 100.1},
        "latestTrade": {"t": (now - timedelta(seconds=2)).isoformat(), "p": 100.03},
        "minuteBar": bar(minute_start or now.replace(second=0) - timedelta(minutes=1)),
    }


class AlpacaResponses:
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []

    def __call__(self, url, *, params, headers, timeout):
        assert url in {SNAPSHOTS_URL, MINUTES_URL, ACTIONS_URL}
        assert headers == {"APCA-API-KEY-ID": "synthetic-key", "APCA-API-SECRET-KEY": "synthetic-secret"}
        assert timeout <= 10
        self.calls.append((url, params))
        value = self.payloads[url]
        if isinstance(value, Response):
            return value
        response = Response()
        response.status_code = 200
        response._content = json.dumps(value).encode()
        return response


def observer(root, fetch, clock, references=("ALFA",)):
    return SourceObserver(root, AlpacaDailyHistory("synthetic-key", "synthetic-secret", get=fetch),
                          references, Limits(), clock)


def test_yahoo_rate_limit_keeps_read_only_alpaca_and_preserves_yahoo_execution(tmp_path):
    class YFRateLimitError(Exception):
        pass

    yahoo_calls = []
    def yahoo(symbol, interval, period):
        yahoo_calls.append((symbol, interval))
        if symbol == "ALFA":
            raise YFRateLimitError()
        instant = NOW.replace(second=0) - timedelta(minutes=1) if interval == "1m" else NOW - timedelta(days=1)
        start = pd.DatetimeIndex([instant.astimezone(NY)])
        return pd.DataFrame({"Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [1500],
                             "Dividends": [.25]}, index=start)

    source = YahooRealtimeData(now=lambda: NOW, fetch=yahoo)
    with pytest.raises(MarketDataUnavailable, match="YFRateLimitError"):
        source.execution_bars(Asset("ALFA"))
    source.get_quote(Asset("BETA"))
    source.corporate_actions(Asset("BETA"))
    original_status = {"mode": "local", "runner_state": "running", "heartbeat_at": NOW.isoformat(),
                       "market_data": source.diagnostics(), "cash": "fake-account-value"}
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(original_status), encoding="utf-8")
    book = tmp_path / "state.sqlite3"
    with sqlite3.connect(book) as db:
        db.executescript("""
            CREATE TABLE local_positions(symbol TEXT, quantity INTEGER);
            INSERT INTO local_positions VALUES ('BETA', 4), ('SOLD', 0);
            CREATE TABLE local_orders(body TEXT, status TEXT);
            INSERT INTO local_orders VALUES ('{"symbol":"OPENED"}', 'partial_fill'), ('{"symbol":"OLD"}', 'filled');
            CREATE TABLE fund_decisions(body TEXT);
            INSERT INTO fund_decisions VALUES ('{"targets":[{"symbol":"NEXT"}],"alerts":[{"symbol":"WATCH"}]}');
        """)
    before = book.read_bytes()
    responses = AlpacaResponses({
        SNAPSHOTS_URL: {"ALFA": snapshot(NOW), "BETA": snapshot(NOW)},
        ACTIONS_URL: {"corporate_actions": {"cash_dividends": [
            {"id": "test-dividend", "symbol": "BETA", "rate": "0.25", "ex_date": "2026-11-18"},
        ]}},
    })
    record = observer(tmp_path, responses, lambda: NOW).poll(NOW)
    assert responses.calls[0] == (SNAPSHOTS_URL, {"symbols": "ALFA,BETA,NEXT,OPENED,WATCH", "feed": "iex"})
    assert record["phase"] == "opening"
    assert record["yahoo_failures"][0]["error_type"] == "YFRateLimitError"
    assert record["symbols"]["ALFA"]["alpaca"]["quote"]["status"] == "valid"
    assert record["symbols"]["ALFA"]["yahoo"]["minute"]["status"] == "not_observed"
    assert record["symbols"]["BETA"]["differences"] == {"same_minute_close_bps": 5.0}
    assert record["corporate_actions"]["alpaca"]["BETA"] == [{"date": "2026-11-18", "dividend": "0.25"}]
    assert record["corporate_actions"]["yahoo"]["BETA"]["actions"][0]["dividend"] == "0.25"
    assert book.read_bytes() == before
    assert json.loads(status_path.read_text()) == original_status
    persisted = next((tmp_path / "market-source-observation").glob("*.jsonl")).read_text()
    assert "synthetic-secret" not in persisted and "fake-account-value" not in persisted
    calls_at_observation = list(yahoo_calls)
    with pytest.raises(MarketDataUnavailable, match="cooling down"):
        source.execution_bars(Asset("ALFA"))
    assert yahoo_calls == calls_at_observation == [("ALFA", "1m"), ("BETA", "1m"), ("BETA", "1d")]
    summary = "\n".join(observation_lines(tmp_path, "2026-11-19"))
    assert "Yahoo 有限流证据 1 轮" in summary
    assert "Yahoo 分钟线报错期间，Alpaca 报价有效 1/1" in summary
    assert "BETA 2026-11-18 dividend alpaca 0.25" in summary
    assert "BETA 2026-11-18 dividend yahoo 0.25" in summary


def test_actual_early_close_never_uses_after_hours_minutes_or_quotes(tmp_path):
    closed = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    at = closed + timedelta(minutes=1, seconds=5)
    responses = AlpacaResponses({
        SNAPSHOTS_URL: {symbol: snapshot(at) for symbol in ("ALFA", "BETA")},
        MINUTES_URL: {"bars": {"ALFA": [bar(closed), bar(closed - timedelta(minutes=1))],
                              "BETA": [bar(closed)]}},
        ACTIONS_URL: {"corporate_actions": {}},
    })
    reader = observer(tmp_path, responses, lambda: at, references=("ALFA", "BETA"))
    assert reader.poll(datetime(2026, 11, 26, 16, tzinfo=timezone.utc)) is None  # Thanksgiving
    record = reader.poll(at)
    assert record["phase"] == "after_close"
    params = responses.calls[1][1]
    assert params["start"] == "2026-11-27T17:59:00+00:00"
    assert params["end"] == "2026-11-27T17:59:59+00:00"
    assert params["adjustment"] == "raw" and params["feed"] == "iex"
    alfa = record["symbols"]["ALFA"]["alpaca"]
    assert alfa["minute"]["end"] == closed.isoformat() and alfa["minute"]["status"] == "valid"
    assert alfa["quote"]["status"] == "outside_session"
    assert record["symbols"]["BETA"]["alpaca"]["minute"]["status"] == "missing"


def test_freshness_and_market_phase_are_independent_of_other_symbols(tmp_path):
    at = NOW + timedelta(minutes=30)
    responses = AlpacaResponses({
        SNAPSHOTS_URL: {
            "ALFA": snapshot(at, quote_at=at - timedelta(seconds=61), minute_start=at.replace(second=0)),
            "BETA": snapshot(at, quote_at=at + timedelta(seconds=1), minute_start=at.replace(second=0) - timedelta(minutes=5)),
            "GAMMA": snapshot(at),
        },
        ACTIONS_URL: {"corporate_actions": {}},
    })
    reader = observer(tmp_path, responses, lambda: at, references=("ALFA", "BETA", "GAMMA", "DELTA"))
    record = reader.poll(at)
    assert record["phase"] == "intraday"
    statuses = {symbol: (data["alpaca"]["quote"]["status"], data["alpaca"]["minute"]["status"])
                for symbol, data in record["symbols"].items()}
    assert statuses == {"ALFA": ("stale", "forming_or_future"), "BETA": ("future", "stale"),
                        "GAMMA": ("valid", "valid"), "DELTA": ("missing", "missing")}


def test_alpaca_429_honors_retry_headers_without_repeating_actions(tmp_path):
    failure = Response()
    failure.status_code = 429
    failure.headers["X-RateLimit-Reset"] = str(int((NOW + timedelta(minutes=3)).timestamp()))
    responses = AlpacaResponses({SNAPSHOTS_URL: failure, ACTIONS_URL: {"corporate_actions": {}}})
    at = [NOW]
    reader = observer(tmp_path, responses, lambda: at[0])
    first = reader.poll(at[0])
    assert first["alpaca_request"]["status"] == "rate_limited"
    assert first["corporate_actions"]["alpaca_request"]["status"] == "rate_limit_cooldown"
    at[0] += timedelta(minutes=1)
    assert reader.poll(at[0])["alpaca_request"]["status"] == "rate_limit_cooldown"
    assert len(responses.calls) == 1
    at[0] += timedelta(minutes=2)
    responses.payloads[SNAPSHOTS_URL] = {"ALFA": snapshot(at[0])}
    assert reader.poll(at[0])["alpaca_request"]["status"] == "ok"
    assert [url for url, _ in responses.calls] == [SNAPSHOTS_URL, SNAPSHOTS_URL, ACTIONS_URL]


def test_data_desk_starts_read_only_observer_even_when_other_jobs_wait(tmp_path, monkeypatch):
    stop = Event()
    responses = AlpacaResponses({SNAPSHOTS_URL: {}, ACTIONS_URL: {"corporate_actions": {}}})
    config = configuration(Path("scripts/autonomous_fund/config.json"), environ={})
    desk = DataDesk(tmp_path, config, "local", environ={"ALPACA_API_KEY": "synthetic-key",
                    "ALPACA_API_SECRET": "synthetic-secret"}, clock=lambda: NOW, alpaca_get=responses)
    called = Event()

    def sample_once(self, stop_event):
        assert stop_event is stop
        self.poll(NOW)
        called.set()
        stop_event.set()

    monkeypatch.setattr(SourceObserver, "serve", sample_once)
    def skip_jobs():
        assert called.wait(5)
    monkeypatch.setattr(desk, "tick", skip_jobs)
    desk.serve(stop)
    assert called.is_set()
    assert "1 个纽约交易日" in "\n".join(observation_lines(tmp_path))
    assert not (tmp_path / "state.sqlite3").exists()
