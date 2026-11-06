"""Independent read-only watches, marked accounts and confirmed-fill notices; no network."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.notifications import readable
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.risk import RiskError, number, validate_decision
from scripts.autonomous_fund.strategy import AutonomousFund, Mandate
from .test_autonomous_fund_autonomy import NOW, LIMITS, UNIVERSE, accept, account, at, decision_body, quote, watching
from .test_autonomous_fund_local import advance, setup_book, submit
from .test_autonomous_fund_monitor import FakeTelegram, heartbeat


@pytest.mark.parametrize("symbol", ["^RISK", "MACRO.TEST"])
def test_read_only_alerts_outside_the_tradable_universe_do_not_expand_order_permissions(symbol):
    body = decision_body([{"symbol": "ALFA", "weight": .05}],
                         [{"symbol": symbol, "above": 25, "reason": "Reassess a market observation"}])
    assert validate_decision(body, NOW, UNIVERSE, LIMITS) == {"ALFA": number(.05)}
    body["targets"] = [{"symbol": symbol, "weight": .05}]
    with pytest.raises(RiskError, match="ineligible"):
        validate_decision(body, NOW, UNIVERSE, LIMITS)


def test_index_alert_uses_fresh_observation_without_trade_liquidity_history(tmp_path):
    strategy = watching(tmp_path, {"ALFA": quote(100)})
    strategy.mode = "local"
    strategy.get_quote = lambda asset: SimpleNamespace(
        price=26, bid=None, ask=None, raw_data={"original_response": SimpleNamespace(timestamp=NOW)})
    historical = strategy.get_historical_prices
    def history(asset, *args, **kwargs):
        if asset.symbol == "^RISK":
            pytest.fail("Read-only alerts need no liquidity history")
        return historical(asset, *args, **kwargs)
    strategy.get_historical_prices = history
    strategy._alert_price = lambda symbol: AutonomousFund._alert_price(strategy, symbol)
    strategy.next_review = at(90)
    accept(strategy, [{"symbol": "ALFA", "weight": .05}],
           [{"symbol": "^RISK", "above": 25, "reason": "Observed risk increased"}], when=at(-30))
    assert strategy._watch(account(), NOW) is False
    triggers = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "review_triggered"]
    assert triggers[-1]["reason"] == "price_alert"
    assert triggers[-1]["symbol"] == "^RISK"
    assert strategy.next_review == NOW and not strategy.journal.pending()


@pytest.mark.parametrize("stamp,price", [(NOW - timedelta(seconds=61), 26),
                                         (NOW + timedelta(seconds=1), 26), (None, 26), (NOW, 0)])
def test_alert_observations_cannot_use_stale_future_undated_or_nonpositive_quotes(tmp_path, stamp, price):
    strategy = watching(tmp_path, {})
    strategy.mode = "local"
    strategy.get_quote = lambda asset: SimpleNamespace(
        price=price, raw_data={"original_response": SimpleNamespace(timestamp=stamp)})
    with pytest.raises(RiskError):
        AutonomousFund._alert_price(strategy, "^RISK")


def configure(broker, universe=("ALFA",), mandate=None, sweep=None):
    broker.configure_valuation(universe=universe, mandate=mandate or Mandate(capital_usd=None),
                               sweep=sweep, interval_seconds=30, benchmark_symbols=())


def test_local_poll_marks_held_positions_without_running_a_strategy_or_model(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    _, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    first = report(tmp_path, write_files=False)["latest_account"]
    # Native quantities can serialize as 5.0; compare their financial values.
    assert {symbol: number(qty) for symbol, qty in first["positions"].items()} == {"ALFA": 5}
    assert first["equity"] == pytest.approx(first["cash"] + 500)
    clock.price, clock.high = 103, 103.1
    advance(clock, source, 1)
    broker.do_polling()
    later = report(tmp_path, write_files=False)["latest_account"]
    assert later["equity"] == pytest.approx(first["cash"] + 515)
    assert later["as_of"] != first["as_of"]
    assert later["position_average_prices"]["ALFA"] == "100.12"
    assert broker._pull_order(order.identifier, "LocalTest").status == "fill"
    count = len([row for row in journal.all_events() if row["kind"] == "account"])
    broker.do_polling()
    assert len([row for row in journal.all_events() if row["kind"] == "account"]) == count
    assert not [row for row in journal.all_events() if row["kind"] == "agent"]


def test_independent_valuation_discloses_stale_marks_and_recovers_without_retimestamping_old_equity(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    previous = report(tmp_path, write_files=False)["latest_account"]
    original = source.get_quote
    source.get_quote = lambda asset: SimpleNamespace(price=200, timestamp=clock.value - timedelta(minutes=5))
    advance(clock, source, 1)
    broker.do_polling()
    assert report(tmp_path, write_files=False)["latest_account"] == previous
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["valuation_error"]
    source.get_quote = original
    clock.price, clock.high = 104, 104.1
    advance(clock, source, 1)
    broker.do_polling()
    assert report(tmp_path, write_files=False)["latest_account"]["equity"] == pytest.approx(previous["cash"] + 520)


def test_independent_marks_preserve_reserved_capital_and_operator_positions(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash='9900'")
        for symbol in ("ALFA", "LEGACY"):
            db.execute("INSERT INTO local_positions VALUES(?,?,?,?)", (symbol, 1, "90", str(clock.value)))
    journal.apply_mandate(clock.value, "1000", number(9900), number(10000), {"ALFA": 1}, False, {"LEGACY": 1}, False)
    configure(broker, mandate=Mandate(capital_usd="1000", allow_external_holdings=True))
    broker.do_polling()
    observed = report(tmp_path, write_files=False)["latest_account"]
    assert observed["cash"] == 900 and observed["equity"] == 1000
    assert {symbol: number(qty) for symbol, qty in observed["positions"].items()} == {"ALFA": 1}
    assert "LEGACY" not in observed["position_average_prices"]
    assert set(observed["valuation"]["quotes"]) == {"ALFA"}


def test_independent_marks_keep_the_cash_sweep_separate_from_risk_positions(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep
    clock, source, journal, broker = setup_book(tmp_path)
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash='1000'")
        for symbol, quantity in (("ALFA", 10), ("BILLS", 80)):
            db.execute("INSERT INTO local_positions VALUES(?,?,?,?)", (symbol, quantity, "90", str(clock.value)))
    configure(broker, universe=("ALFA", "BILLS"), sweep=CashSweep("BILLS"))
    broker.do_polling()
    observed = report(tmp_path, write_files=False)["latest_account"]
    assert observed["cash"] == 9000 and observed["equity"] == 10000
    assert {symbol: number(qty) for symbol, qty in observed["positions"].items()} == {"ALFA": 10}
    assert {symbol: number(qty) for symbol, qty in observed["cash_equivalents"].items()} == {"BILLS": 80}


def test_unlimited_mandate_does_not_reuse_a_previous_capital_reserve(tmp_path):
    _, _, journal, broker = setup_book(tmp_path)
    # Removing the capital limit must agree with the normal strategy snapshot,
    # even when the earlier mandate's audit metadata remains in the journal.
    journal.set_meta("mandate_reserve_cash", 9000)
    configure(broker, mandate=Mandate(capital_usd=None))
    broker.do_polling()
    observed = report(tmp_path, write_files=False)["latest_account"]
    assert observed["cash"] == observed["equity"] == 10000


def test_buy_and_sell_fees_are_both_deducted_once_and_included_in_cash_and_equity(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key, purchase = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, purchase.identifier, True, clock.value)
    _, sale = submit(broker, journal, clock.value, side="sell", limit=99)
    advance(clock, source)
    broker.do_polling()
    buy = broker._pull_order(purchase.identifier, "LocalTest")
    sell = broker._pull_order(sale.identifier, "LocalTest")
    expected_fees = (number(buy.avg_fill_price) + number(sell.avg_fill_price)) * 5 * number("0.001")
    expected_cash = number(10000) + (number(sell.avg_fill_price) - number(buy.avg_fill_price)) * 5 - expected_fees
    cash, value, equity = broker._get_balances_at_broker(None, "LocalTest")
    assert cash == pytest.approx(float(expected_cash)) and value == 0 and equity == cash
    assert report(tmp_path, write_files=False)["observed_fees"] == pytest.approx(float(expected_fees))
    broker.do_polling()
    assert broker._get_balances_at_broker(None, "LocalTest")[0] == cash


@pytest.mark.parametrize("snapshot_age", [60, 0])
def test_fill_update_waits_for_a_post_fill_account_and_survives_monitor_restart(tmp_path, snapshot_age):
    from .test_autonomous_fund_monitor import setup
    journal = setup(tmp_path)
    journal.event(NOW - timedelta(seconds=snapshot_age), "account", {
        "cash": "9500", "equity": "10000", "positions": {"ALFA": "5"},
        "as_of": str(NOW - timedelta(seconds=snapshot_age))})
    journal.event(NOW, "fill", {"broker_id": "synthetic-sale", "symbol": "ALFA", "side": "sell",
                                 "quantity": "5", "price": "101", "trade_cost": "0.505"})
    heartbeat(tmp_path, NOW)
    journal.event(NOW, "iteration_finished", {})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(NOW)
    assert not transport.messages
    later = NOW + timedelta(seconds=5)
    journal.event(later, "account", {"cash": "10004.495", "equity": "10004.495", "positions": {}, "as_of": str(later)})
    heartbeat(tmp_path, later)
    monitor.poll(later)
    assert len(transport.messages) == 1
    assert "成交" in transport.messages[0] and "暂无证券持仓" in transport.messages[0]
    assert "卖出 ALFA：5 股" in transport.messages[0] and "$10,004.50" in transport.messages[0]
    Monitor(tmp_path, transport).poll(later + timedelta(seconds=1))
    assert len(transport.messages) == 1


def test_target_notice_explicitly_describes_a_plan_before_the_fill():
    text = readable("decision_changed", {"at": str(NOW), "changes": [{"symbol": "ALFA", "from": ".05", "to": "0"}],
                                          "cash_weight": "1"})
    assert "计划卖出／减仓" in text and "成交更新" in text


def test_confirmed_fill_is_disclosed_during_snapshot_outage_then_followed_by_the_updated_account(tmp_path):
    from .test_autonomous_fund_monitor import setup
    journal = setup(tmp_path)
    journal.event(NOW, "fill", {"broker_id": "synthetic-buy", "symbol": "ALFA", "side": "buy",
                                 "quantity": "1", "price": "100", "trade_cost": "0.1"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    later = NOW + timedelta(seconds=61)
    heartbeat(tmp_path, later)
    journal.event(later, "iteration_finished", {})
    monitor.poll(later)
    assert len(transport.messages) == 1
    assert "账户快照待更新" in transport.messages[0] and "买入 ALFA：1 股" in transport.messages[0]
    assert "暂无证券持仓" not in transport.messages[0]
    Monitor(tmp_path, transport).poll(later + timedelta(seconds=1))
    assert len(transport.messages) == 1
    journal.event(later, "account", {"as_of": str(later), "cash": "9899.9", "equity": "9999.9", "positions": {"ALFA": 1}})
    monitor.poll(later + timedelta(seconds=2))
    assert len(transport.messages) == 2 and "ALFA：1 股" in transport.messages[-1]


def test_book_change_during_quote_fetch_cannot_publish_mixed_account_state(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    with journal.connection() as db:
        db.execute("INSERT INTO local_positions VALUES('ALFA',1,'90',?)", (str(clock.value),))
    original = source.get_quote
    def change_book(asset):
        with journal.connection() as db:
            db.execute("UPDATE local_account SET cash='9800'")
        return original(asset)
    source.get_quote = change_book
    broker.do_polling()
    assert not [r for r in journal.all_events() if r["kind"] == "account"]
    assert "Book changed" in broker._valuation_error
    source.get_quote = original
    broker.do_polling()  # Changed book bypasses the normal thirty-second interval.
    assert report(tmp_path, write_files=False)["latest_account"]["equity"] == 9900


@pytest.mark.parametrize("unavailable", [False, True])
def test_live_decision_refreshes_the_notice_account_after_the_model_returns(tmp_path, unavailable):
    from dataclasses import replace
    from .test_autonomous_fund_manager_independence import live_manager
    strategy, calls = live_manager(tmp_path)
    old = account()
    fresh = replace(old, cash=number(9990), equity=number(9990), as_of=NOW + timedelta(seconds=10))
    def snapshot():
        if unavailable:
            raise ConnectionError("synthetic unavailable account")
        return fresh
    strategy._snapshot = snapshot
    notices = []
    strategy._announce_decision = lambda decision, now, **kw: notices.append(kw["account"])
    strategy._decide(old, NOW, False)
    assert calls and notices == [None if unavailable else fresh]


def test_native_manager_wait_does_not_hold_up_local_valuation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.risk import Limits
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import account_digest
    from .test_autonomous_fund_local import Clock
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    entered, release = Event(), Event()
    class WaitingRuntime(ScriptedRuntime):
        def run(self, request):
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test did not release the synthetic model")
            return super().run(request)
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "mandate": {"capital_usd": None},
              "universe": {"ALFA": "stock"}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="IndependentMarks", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": WaitingRuntime()})
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        strategy._executor._initialize()
        submit(broker, journal, clock.value)
        advance(clock, source)
        broker.do_polling()
        previous = report(tmp_path, write_files=False)["latest_account"]
        future = pool.submit(strategy.agents["manager"].run, context={
            "as_of": clock.value.isoformat(), "eligible_universe": ["ALFA"]})
        assert entered.wait(5)
        clock.price, clock.high = 102, 102.1
        advance(clock, source, 1)
        broker.do_polling()
        observed = report(tmp_path, write_files=False)["latest_account"]
        assert observed["equity"] == pytest.approx(previous["cash"] + 510)
        assert not future.done()  # Account changed while the actual agent call remains in flight.
        release.set()
        assert future.result(timeout=10).parsed is not None
    finally:
        release.set()
        pool.shutdown(wait=True)
        broker._close_connection()
