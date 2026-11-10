"""Valid trades stay executable; retryable quotes and derived warnings do not halt the fund."""
import json
from datetime import datetime, timedelta, timezone

import pandas_market_calendars as mcal
import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund.execution import EXECUTION_REVISION, ExecutionState
from scripts.autonomous_fund.ledger_audit import audit
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.risk import Intent, Limits, MarketDataUnavailable, Price, number
from scripts.autonomous_fund.report import report
from .test_autonomous_fund_local import setup_book, advance, submit
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_closed_ledger import traded_book


@pytest.mark.parametrize("error", ["network", "stale_quote"])
def test_temporary_market_data_waits_and_recovers_without_operator_resume(tmp_path, error):
    if error == "stale_quote":
        clock, source, journal, broker, _ = traded_book(tmp_path)
    else:
        clock, source, journal, broker = setup_book(tmp_path)
        configure(broker)
    journal.event(clock.value, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    _, order = submit(broker, journal, clock.value, quantity=1)
    advance(clock, source)
    with journal.connection() as db:
        cash = number(db.execute("SELECT cash FROM local_account").fetchone()[0])
        fills = db.execute("SELECT count(*) FROM local_fills").fetchone()[0]
    original_bars, original_quote = source.execution_bars, source.get_quote
    if error == "network":
        source.execution_bars = lambda *a, **kw: (_ for _ in ()).throw(MarketDataUnavailable("Temporary source outage"))
    else:
        quote = source.get_quote(Asset("ALFA"))
        quote.raw_data["original_response"].timestamp = clock.value - timedelta(minutes=5)
        # Exercise the native balance read on an existing holding, without
        # replacing the broker's financial checks or its account methods.
        source.get_quote = lambda *a, **kw: quote
    broker.do_polling()
    state = json.loads((tmp_path / "status.json").read_text())
    assert state["matching_error"] is None and state["market_data_error"]
    assert broker._pull_order(order.identifier, "LocalTest").status == "new"
    with journal.connection() as db:
        assert number(db.execute("SELECT cash FROM local_account").fetchone()[0]) == cash
        assert db.execute("SELECT count(*) FROM local_fills").fetchone()[0] == fills
    # Exercise the real watchdog too: a fresh runner waiting for quotes is healthy.
    journal.event(clock.value, "iteration_finished", {})
    monitor = Monitor(tmp_path)
    assert monitor.poll(clock.value)["health"] == "healthy"
    assert not (tmp_path / "STOP").exists()
    source.execution_bars, source.get_quote = original_bars, original_quote
    broker.do_polling()  # Same fresh completed bar is still eligible; no restart/resume.
    assert broker._pull_order(order.identifier, "LocalTest").status == "fill"
    assert broker._market_data_error is None and broker._last_poll_error is None


def test_actual_cash_discrepancy_still_blocks_fills_and_requires_reconciliation(tmp_path):
    clock, source, journal, broker, _ = traded_book(tmp_path)
    journal.event(clock.value, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    _, sale = submit(broker, journal, clock.value, side="sell", limit=99)
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash='9000' WHERE id=1")
    advance(clock, source)
    broker.do_polling()
    journal.event(clock.value, "iteration_finished", {})
    assert broker._last_poll_error and "reconciliation failed" in broker._last_poll_error
    assert broker._pull_order(sale.identifier, "LocalTest").status == "new"
    monitor = Monitor(tmp_path)
    assert monitor.poll(clock.value)["health"] == "matching_error"
    assert (tmp_path / "STOP").exists()
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM local_fills").fetchone()[0] == 1
    # Improved retry behavior is restricted to data observations, never real money discrepancies.
    from scripts.autonomous_fund import stops
    assert not stops.resumable(stops.read(tmp_path))


def test_sale_does_not_wait_for_another_holdings_unavailable_mark(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    # Both positions belong to the sleeve. Fill-time attribution now enforces
    # that scope; this test isolates an unrelated symbol's quote outage.
    configure(broker, universe=("ALFA", "BRAV"))
    def order_for(symbol, side):
        limit = 101 if side == "buy" else 99
        intent = Intent(symbol, side, 1, number(limit))
        key = journal.reserve(intent, clock.value, broker.limits)
        order = Order("Continuity", Asset(symbol), 1, side, order_type="limit", limit_price=limit,
                      custom_params={"local_intent": key})
        broker.submit_order(order)
        return key, order
    for symbol in ("ALFA", "BRAV"):
        key, buy = order_for(symbol, "buy")
        advance(clock, source)
        broker.do_polling()
        journal.update(key, buy.identifier, True, clock.value)
    _, sale = order_for("BRAV", "sell")
    advance(clock, source)
    original = source.get_quote
    def quote(asset, **kwargs):
        if asset.symbol == "ALFA":
            raise MarketDataUnavailable("ALFA source temporarily unavailable")
        return original(asset, **kwargs)
    source.get_quote = quote
    broker.do_polling()
    assert broker._pull_order(sale.identifier, "Continuity").status == "fill"
    assert broker._valuation_error and broker._last_poll_error is None
    assert audit(tmp_path)["errors"] == []


def test_bad_historical_nav_does_not_block_a_correctly_accounted_sale(tmp_path):
    clock, source, journal, broker, _ = traded_book(tmp_path)
    journal.event(clock.value, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    with journal.connection() as db:
        row = db.execute("SELECT sequence,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        body = json.loads(row[1]); body["equity"] = "999999"
        db.execute("UPDATE events SET body=? WHERE sequence=?", (json.dumps(body), row[0]))
    assert audit(tmp_path)["status"] == "partial"
    monitor = Monitor(tmp_path)
    assert "999999" not in monitor._status_text(clock.value)
    from scripts.autonomous_fund.review import _events
    assert not [r for r in _events(journal, clock.value) if r["kind"] == "account"]
    from scripts.autonomous_fund.monitor import metrics_text
    assert "999999" not in metrics_text(tmp_path, {"at": str(clock.value)})
    assert broker._pull_positions("LocalTest")[0].quantity == 5
    _, sale = submit(broker, journal, clock.value, side="sell", limit=99)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(sale.identifier, "LocalTest").status == "fill"
    latest = report(tmp_path, write_files=False)["latest_account"]
    assert latest["positions"] == {} and latest["equity"] == latest["cash"] < 10000
    assert broker._last_poll_error is None and not (tmp_path / "STOP").exists()
    # The same accepted post-sale projection must reach user notifications and AI history.
    journal.event(clock.value, "iteration_finished", {})
    monitor.poll(clock.value)
    updates = [r["body"]["details"] for r in journal.all_events() if r["kind"] == "notification"
               and r["body"]["kind"] == "execution_update"]
    assert updates[-1]["account"]["positions"] == {}
    assert number(_events(journal, clock.value, kinds=("account",))[-1]["body"]["equity"]) == number(latest["equity"])


def test_one_hundred_valid_fills_have_no_accounting_false_blocks(tmp_path):
    clock, source, journal, _ = setup_book(tmp_path)
    broker = LocalPaperBroker(source, journal, Limits(daily_orders=50), connect_stream=False)
    configure(broker)
    execution = ExecutionState(journal)
    sessions = mcal.get_calendar("NYSE").schedule(start_date="2026-11-02", end_date="2026-11-16").index[:10]
    filled = 0
    for session in sessions:
        clock.value = datetime.combine(session.date(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15, minutes=30, seconds=5)
        source._cache.clear()
        for _ in range(5):
            for side, limit in (("buy", 101), ("sell", 99)):
                intent = Intent("ALFA", side, 5, number(limit))
                key = journal.reserve(intent, clock.value, broker.limits)
                quote = source.get_quote(Asset("ALFA"))
                execution.record_submission(key, "neutral-plan", intent, clock.value,
                    quote=Price(number(quote.bid), number(quote.ask), quote.timestamp, number(10000000)))
                order = Order("Continuity", Asset("ALFA"), 5, side, order_type="limit", limit_price=limit,
                              custom_params={"local_intent": key})
                broker.submit_order(order)
                advance(clock, source)
                broker.do_polling()
                assert broker._pull_order(order.identifier, "Continuity").status == "fill"
                assert broker._last_poll_error is None and broker._market_data_error is None
                journal.update(key, order.identifier, True, clock.value)
                filled += 1
    checked = audit(tmp_path)
    assert filled == checked["fills_checked"] == 100
    assert checked["status"] == "verified" and checked["errors"] == []
    assert checked["replayed"]["positions"] == {} and not (tmp_path / "STOP").exists()


@pytest.mark.parametrize("entry_limit", [None, 101])
def test_three_minute_manager_call_reprices_or_waits_at_entry_limit(tmp_path, monkeypatch, entry_limit):
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, open_journal
    from .test_autonomous_fund_local import Clock
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    started = clock.value
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "mandate": {"capital_usd": "10000"},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    journal = open_journal(tmp_path, "local", "local-virtual-account", config)
    class ThinkingRuntime(ScriptedRuntime):
        def run(self, request):
            result = super().run(request)
            if request.agent_name == "manager" and request.context.get("phase") != "postmarket_review":
                body = json.loads(result.summary)
                target = {"symbol": "ALFA", "weight": .05}
                if entry_limit is not None:
                    target["entry_limit"] = entry_limit
                body["targets"] = [target]
                clock.value += timedelta(minutes=3)
                clock.price, clock.high = 102, 102.01
                source._cache.clear()
                result.summary = json.dumps(body)
            return result
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="DelayedDecision", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": ThinkingRuntime()})
    try:
        strategy._executor._initialize()
        strategy.on_trading_iteration()
        submitted = [r for r in journal.all_events() if r["kind"] == "order_submitted"]
        assert clock.value == started + timedelta(minutes=3)
        if entry_limit is not None:
            assert submitted == []
            assert any(r["kind"] == "entry_waiting" for r in journal.all_events())
        else:
            # The native lifecycle must plan and fill against the new $102 market,
            # not the $100 observation from before the manager started thinking.
            assert len(submitted) == 1 and 102 < number(submitted[0]["body"]["limit"]) < 103
            assert datetime.fromisoformat(submitted[0]["at"]) == clock.value
            advance(clock, source)
            broker.do_polling()
            with journal.connection() as db:
                [price] = db.execute("SELECT price FROM local_fills").fetchone()
            assert 102 < number(price) < 103
        assert audit(tmp_path)["errors"] == []
    finally:
        broker._close_connection()
