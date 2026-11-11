"""Observation receipt time, independent benchmarks and deferred notices.

Synthetic native local accounts and fake notification transport only.
"""
from datetime import timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.risk import Intent, MarketDataUnavailable, instant, number
from scripts.autonomous_fund.run import ScriptedRuntime
from scripts.autonomous_fund.strategy import AutonomousFund, account_digest
from .test_autonomous_fund_local import Clock, advance, submit
from .test_autonomous_fund_monitor import FakeTelegram, NOW as MONITOR_NOW, heartbeat, setup
from .test_autonomous_fund_quant import LIMITS


def rows(journal, kind):
    with journal.connection() as db:
        return [dict(row) for row in db.execute("SELECT sequence,at,body FROM events WHERE kind=? ORDER BY sequence", (kind,))]


def native_snapshot(tmp_path, monkeypatch, *, capital=None, holding=False, universe=("ALFA",)):
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "universe": {symbol: "stock" for symbol in universe},
              "mandate": {"capital_usd": capital, "local_account_usd": "20000"},
              "cash_sweep": {"enabled": False}, "critic": {"enabled": False}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False, starting_cash="20000")
    strategy = AutonomousFund(broker=broker, name="ObservationTiming", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local",
        "account_binding": "local-virtual-account", "runtime": ScriptedRuntime()})
    strategy._executor._initialize()
    strategy._snapshot()
    if holding:
        key, bought = submit(broker, journal, clock())
        advance(clock, source)
        broker.do_polling()
        journal.update(key, bought.identifier, True, clock())
        strategy._snapshot()
    return clock, source, journal, broker, strategy


@pytest.mark.parametrize("quote_missing", [False, True])
def test_paper_receipt_time_is_after_reads_even_when_all_benchmarks_are_unavailable(tmp_path, quote_missing):
    from .test_autonomous_fund_autonomy import snapshot_harness
    strategy, _ = snapshot_harness(tmp_path, 10000, 10000, {}, {"capital_usd": None})
    clock = Clock()
    before = clock()
    strategy.mode, strategy.get_datetime = "paper", clock
    strategy._orders = lambda: []

    def balances(*args):
        clock.value += timedelta(seconds=7)
        return 10000, 0, 10000

    def quote(*args):
        if quote_missing:
            raise MarketDataUnavailable("Synthetic benchmark source unavailable")
        return SimpleNamespace(price=100, raw_data={"original_response": SimpleNamespace(timestamp=clock())})

    strategy.broker = SimpleNamespace(_get_balances_at_broker=balances, _pull_positions=lambda *a: [])
    strategy.get_quote = quote
    account = strategy._snapshot()
    assert account.as_of == clock() == before + timedelta(seconds=7)
    assert account.equity == 10000
    assert instant(rows(strategy.journal, "account")[-1]["at"]) == clock()
    if quote_missing:
        assert not rows(strategy.journal, "benchmark")


@pytest.mark.parametrize("origin", ["strategy", "poller"])
def test_held_benchmark_reuses_the_same_valid_mark_without_a_duplicate_read(tmp_path, monkeypatch, origin):
    clock, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch, universe=("ALFA", "SPY"))
    key = journal.reserve(Intent("SPY", "buy", 1, number(101)), clock(), broker.limits)
    order = Order("ObservedBenchmark", Asset("SPY"), 1, "buy", order_type="limit", limit_price=101,
                  custom_params={"local_intent": key})
    broker.submit_order(order)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, order.identifier, True, clock())
    original, calls = source.get_quote, []

    def quote(asset, **kwargs):
        calls.append(asset.symbol)
        if asset.symbol == "SPY" and calls.count("SPY") > 1:
            raise MarketDataUnavailable("Synthetic duplicate benchmark read unavailable")
        return original(asset, **kwargs)

    source.get_quote = quote
    try:
        if origin == "strategy":
            strategy._snapshot()
        else:
            broker._next_valuation = None
            broker._publish_valuation()
        assert calls.count("SPY") == 1
        assert "SPY" in json.loads(rows(journal, "benchmark")[-1]["body"])
        body = json.loads(rows(journal, "account")[-1]["body"])
        assert instant(body["benchmark_observations"]["SPY"]["as_of"]) == instant(body["valuation"]["quotes"]["SPY"]["as_of"])
    finally:
        broker._close_connection()


@pytest.mark.parametrize("capital", [None, "10000"])
def test_slow_account_read_is_not_visible_before_the_source_evidence_arrived(tmp_path, monkeypatch, capital):
    clock, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch, capital=capital, holding=True)
    before, original, slow = clock(), source.fetch, [True]
    previous = report(tmp_path, write_files=False)["latest_account"]
    source._cache.clear()  # Only this fixture's private source.

    def fetch(symbol, interval, period):
        if symbol == "ALFA" and interval == "1m" and slow[0]:
            slow[0] = False
            clock.value += timedelta(seconds=7)
            raw = original(symbol, interval, period)
            raw.attrs["yahoo_last_trade"] = {"price": 110, "as_of": clock() - timedelta(seconds=1)}
            return raw
        return original(symbol, interval, period)

    source.fetch = fetch
    try:
        account = strategy._snapshot()
        last = rows(journal, "account")[-1]
        body = json.loads(last["body"])
        assert account.as_of == instant(last["at"]) == instant(body["as_of"]) == clock()
        assert account.as_of > before
        assert all(instant(mark["as_of"]) <= account.as_of for mark in body["valuation"]["quotes"].values())
        historical = report(tmp_path, write_files=False, as_of=before + timedelta(seconds=1))
        assert historical["latest_account"] == previous
    finally:
        broker._close_connection()


@pytest.mark.parametrize("origin", ["strategy", "poller"])
def test_benchmark_that_ages_during_later_io_is_omitted_without_losing_the_account(tmp_path, monkeypatch, origin):
    clock, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch)
    original = source.get_quote

    def quote(asset, **kwargs):
        actual = original(asset, **kwargs)
        if asset.symbol == "SPY":
            actual.timestamp = actual.raw_data["original_response"].timestamp = clock() - timedelta(seconds=59)
        elif asset.symbol == "QQQ":
            clock.value += timedelta(seconds=2)
        return actual

    source.get_quote = quote
    try:
        if origin == "strategy":
            account = strategy._snapshot()
            assert account.equity == 20000
        else:
            broker._next_valuation = None
            broker._publish_valuation()
        benchmarks = json.loads(rows(journal, "benchmark")[-1]["body"])
        assert "QQQ" in benchmarks and "SPY" not in benchmarks
        body = json.loads(rows(journal, "account")[-1]["body"])
        assert "SPY" not in body.get("benchmark_observations", {})
        assert body["benchmark_observations"]["QQQ"]["as_of"]
    finally:
        broker._close_connection()


@pytest.mark.parametrize("bad", ["missing", "future", "stale", "nonpositive"])
def test_snapshot_benchmark_uses_source_evidence_and_preserves_other_symbol_success(tmp_path, monkeypatch, bad):
    clock, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch)
    original = source.get_quote

    def quote(asset, **kwargs):
        actual = original(asset, **kwargs)
        if asset.symbol == "QQQ":
            if bad == "nonpositive":
                actual.price = 0
            else:
                actual.timestamp = clock()  # Receipt time cannot rescue invalid provider time.
                actual.raw_data["original_response"].timestamp = (None if bad == "missing" else
                    clock() + timedelta(seconds=1) if bad == "future" else clock() - timedelta(seconds=61))
        return actual

    source.get_quote = quote
    try:
        strategy._snapshot()
        benchmarks = json.loads(rows(journal, "benchmark")[-1]["body"])
        assert "SPY" in benchmarks and "QQQ" not in benchmarks
        assert rows(journal, "benchmark_unavailable")
    finally:
        broker._close_connection()


def test_one_unavailable_benchmark_does_not_discard_another_fresh_benchmark(tmp_path, monkeypatch):
    _, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch)
    original, before_count = source.get_quote, len(rows(journal, "benchmark"))

    def quote(asset, **kwargs):
        if asset.symbol == "QQQ":
            raise MarketDataUnavailable("Synthetic independent quote failure")
        return original(asset, **kwargs)

    source.get_quote = quote
    try:
        strategy._snapshot()
        assert len(rows(journal, "benchmark")) == before_count + 1
        assert "SPY" in json.loads(rows(journal, "benchmark")[-1]["body"])
    finally:
        broker._close_connection()


def test_snapshot_does_not_publish_a_holding_that_expired_during_optional_benchmark_io(tmp_path, monkeypatch):
    clock, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch, holding=True)
    original, before_count = source.get_quote, len(rows(journal, "account"))

    def quote(asset, **kwargs):
        actual = original(asset, **kwargs)
        if asset.symbol == "ALFA":
            actual.timestamp = actual.raw_data["original_response"].timestamp = clock() - timedelta(seconds=59)
        elif asset.symbol == "QQQ":
            clock.value += timedelta(seconds=2)
        return actual

    source.get_quote = quote
    try:
        with pytest.raises(MarketDataUnavailable):
            strategy._snapshot()
        assert len(rows(journal, "account")) == before_count
    finally:
        broker._close_connection()


@pytest.mark.parametrize("seconds", [30, 300])
def test_future_notice_is_deferred_without_blocking_ready_notices_and_survives_restart(tmp_path, seconds):
    journal = setup(tmp_path)
    future = MONITOR_NOW + timedelta(seconds=seconds, microseconds=1)
    journal.event(future.astimezone(ZoneInfo("America/New_York")), "notification", {"kind": "blocked", "details": "Synthetic future"})
    journal.event(MONITOR_NOW, "notification", {"kind": "blocked", "details": "Synthetic ready"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(MONITOR_NOW)
    assert len(transport.messages) == 1 and "Synthetic ready" in transport.messages[0]
    with journal.connection() as db:
        row = db.execute("SELECT state,next_at FROM fund_deliveries ORDER BY sequence LIMIT 1").fetchone()
        assert row["state"] == "pending" and instant(row["next_at"]) == future
    heartbeat(tmp_path, future - timedelta(microseconds=1))
    Monitor(tmp_path, transport).poll(future - timedelta(microseconds=1))
    assert len(transport.messages) == 1
    heartbeat(tmp_path, future)
    Monitor(tmp_path, transport).poll(future)
    assert len(transport.messages) == 2 and "Synthetic future" in transport.messages[-1]


def test_future_app_notice_cannot_hide_a_current_watchdog_stop_notice(tmp_path):
    journal = setup(tmp_path)
    journal.event(MONITOR_NOW + timedelta(minutes=5), "notification", {"kind": "blocked", "details": "Synthetic future"})
    heartbeat(tmp_path, MONITOR_NOW + timedelta(minutes=5))
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(MONITOR_NOW)
    assert (tmp_path / "STOP").exists()
    assert any("监控停止交易" in message for message in transport.messages)
    assert not any("Synthetic future" in message for message in transport.messages)


def test_future_duplicate_cannot_suppress_an_observation_that_is_already_due(tmp_path):
    journal = setup(tmp_path)
    notice = {"kind": "blocked", "details": "Synthetic repeated condition"}
    journal.event(MONITOR_NOW + timedelta(minutes=5), "notification", notice)
    journal.event(MONITOR_NOW, "notification", notice)
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(MONITOR_NOW)
    assert len(transport.messages) == 1


def test_normal_past_duplicate_suppression_is_preserved(tmp_path):
    journal = setup(tmp_path)
    notice = {"kind": "blocked", "details": "Synthetic repeated condition"}
    journal.event(MONITOR_NOW - timedelta(seconds=1), "notification", notice)
    journal.event(MONITOR_NOW, "notification", notice)
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(MONITOR_NOW)
    assert len(transport.messages) == 1


@pytest.mark.parametrize("stamp", ["invalid-time", "2026-11-01T17:00:00"])
def test_malformed_notice_timestamp_cannot_block_a_later_ready_notice(tmp_path, stamp):
    journal = setup(tmp_path)
    journal.event(stamp, "notification", {"kind": "blocked", "details": "Synthetic bad date"})
    journal.event(MONITOR_NOW, "notification", {"kind": "blocked", "details": "Synthetic ready"})
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(MONITOR_NOW)
    assert len(transport.messages) == 1 and "Synthetic ready" in transport.messages[0]
    with journal.connection() as db:
        assert tuple(db.execute("SELECT state,last_error FROM fund_deliveries ORDER BY sequence LIMIT 1").fetchone()) == (
            "suppressed", "malformed_notification_timestamp")
        assert db.execute("SELECT at FROM events WHERE kind='notification' ORDER BY sequence LIMIT 1").fetchone()[0] == stamp


@pytest.mark.parametrize("same_body", [False, True])
def test_unusable_old_duplicate_timestamp_cannot_block_or_suppress_new_notice(tmp_path, same_body):
    journal = setup(tmp_path)
    notice = {"kind": "blocked", "details": "Synthetic original"}
    journal.event(MONITOR_NOW, "notification", notice)
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(MONITOR_NOW)
    with journal.connection() as db:
        db.execute("UPDATE events SET at='invalid-time' WHERE kind='notification'")
    journal.event(MONITOR_NOW, "notification", notice if same_body else {"kind": "blocked", "details": "Synthetic different"})
    monitor.poll(MONITOR_NOW)
    assert len(transport.messages) == 2
