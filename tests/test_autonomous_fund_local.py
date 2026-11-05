"""Native local-broker integration with explicit clock/data/model substitutes."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import SessionClock, YahooRealtimeData
from scripts.autonomous_fund.risk import Intent, Limits, RiskError, number, validate_evidence


NOW = datetime(2026, 11, 2, 15, 30, 5, tzinfo=timezone.utc)


class Clock:
    value = NOW
    volume = 10000
    high = 100.01
    price = 100.0
    actions = []

    def __call__(self):
        return self.value

    def fetch(self, symbol, interval, period):
        if interval == "1m":
            index = pd.date_range(end=pd.Timestamp(self.value).floor("min"), periods=8, freq="min")
            return pd.DataFrame({"Open": self.price, "High": max(self.price, self.high),
                                 "Low": self.price - .01, "Close": self.price, "Volume": self.volume}, index=index)
        # Yahoo daily timestamps identify New York session dates, not UTC midnight.
        index = pd.bdate_range(end=self.value.date(), periods=90, tz="America/New_York")
        frame = pd.DataFrame({"Open": self.price, "High": self.price + .01, "Low": self.price - .01,
                              "Close": self.price, "Volume": 1000000, "Dividends": 0., "Stock Splits": 0.}, index=index)
        for stamp, split, dividend in self.actions:
            frame.loc[pd.Timestamp(stamp, tz="America/New_York"), ["Stock Splits", "Dividends"]] = [split, dividend]
        return frame


def setup_book(tmp_path):
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    journal = Journal(tmp_path, "test-local-book")
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    return clock, source, journal, broker


def submit(broker, journal, now, quantity=5, side="buy", limit=101):
    intent = Intent("ALFA", side, quantity, number(limit))
    key = journal.reserve(intent, now, Limits())
    order = Order("LocalTest", Asset("ALFA"), quantity, side, order_type="limit", limit_price=limit,
                  custom_params={"local_intent": key})
    broker.submit_order(order)
    return key, order


def advance(clock, source, minutes=2):
    clock.value += timedelta(minutes=minutes)
    source._cache.clear()


def test_local_data_excludes_forming_minute_and_today_daily_close(tmp_path):
    clock, source, _, _ = setup_book(tmp_path)
    asset = Asset("ALFA")
    minute = source.execution_bars(asset)
    assert minute.index[-1] + timedelta(minutes=1) <= clock.value
    assert minute.index[-1] < pd.Timestamp(clock.value).floor("min")
    daily = source.get_historical_prices(asset, 30, timestep="day").df
    assert all(day < source.get_datetime().date() for day in daily.index.date)
    quote = source.get_quote(asset)
    assert quote.raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"
    assert quote.timestamp == minute.index[-1] + timedelta(minutes=1)


def test_native_agent_indicator_can_load_its_full_history_fallback(tmp_path, monkeypatch):
    from lumibot.components.agents.builtins import _bind_get_indicator
    from lumibot.strategies import Strategy

    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("LUMIBOT_FRED_CACHE_DIR", str(tmp_path / "cache" / "fred"))
    clock, source, _, broker = setup_book(tmp_path)
    requested = []

    def fetch(symbol, interval, period):
        requested.append((interval, period))
        return clock.fetch(symbol, interval, period)

    source.fetch = fetch
    strategy = Strategy(broker=broker, name="IndicatorHistory")
    result = _bind_get_indicator(strategy, strategy.agents).function(
        symbol="ALFA", indicator="ema", parameters_json='{"length":14}')
    # Native recursive indicators ask for 10,000 observations to seed their
    # calculation; a source returns the actual available history, never padding.
    assert result["value"] == pytest.approx(clock.price)
    assert ("1d", "max") in requested
    frame = source.get_historical_prices(Asset("ALFA"), 10000, timestep="day").df
    assert 14 < len(frame) < 10000
    assert all(day < source.get_datetime().date() for day in frame.index.date)


@pytest.mark.parametrize("length", [0, -1, True, 1.5, "10000"])
def test_history_requires_a_positive_integer_length(tmp_path, length):
    _, source, _, _ = setup_book(tmp_path)
    with pytest.raises(RiskError, match="positive integer"):
        source.get_historical_prices(Asset("ALFA"), length, timestep="day")


def test_order_requires_a_new_complete_bar_and_realistic_adverse_fill(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "new"
    advance(clock, source)
    broker.do_polling()
    observed = broker._pull_order(order.identifier, "LocalTest")
    assert observed.status == "fill"
    assert observed.get_fill_price() == 100.12
    assert observed.get_fill_price() >= clock.high
    cash, value, equity = broker._get_balances_at_broker(None, "LocalTest")
    assert cash == pytest.approx(10000 - 5 * 100.12 * 1.001)
    assert value == 500
    broker.do_polling()
    assert broker._get_balances_at_broker(None, "LocalTest")[0] == cash
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM local_fills").fetchone()[0] == 1
        assert db.execute("SELECT quantity FROM local_positions").fetchone()[0] == 5
    assert (broker._trade_event_log_df.status == "fill").sum() == 1


def test_minute_volume_caps_partial_fills_without_duplicate_fee(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    clock.volume = 200  # 1% of observed volume permits two shares per minute.
    _, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "partial_fill"
    for _ in range(2):
        advance(clock, source, 1)
        broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "fill"
    from scripts.autonomous_fund.report import report
    result = report(tmp_path)
    assert result["fills_observed"] == 3
    assert result["observed_fees"] == pytest.approx(5 * 100.12 * .001)


def test_restart_recovers_order_and_cash_and_does_not_replay_fills(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key, order = submit(broker, journal, clock.value)
    # The adapter commits the order ID and reservation together, even before
    # the application's submit function has returned to save its own result.
    assert journal.pending()[0]["broker_id"] == order.identifier
    restored = LocalPaperBroker(source, Journal(tmp_path, "test-local-book"), Limits(), connect_stream=False)
    assert restored._pull_order(order.identifier, "LocalTest").status == "new"
    advance(clock, source)
    restored.do_polling()
    cash = restored._get_balances_at_broker(None, "LocalTest")[0]
    again = LocalPaperBroker(source, Journal(tmp_path, "test-local-book"), Limits(), connect_stream=False)
    again.sync_positions("LocalTest")
    again.sync_orders("LocalTest")
    assert again.get_tracked_positions("LocalTest")[0].quantity == 5
    again.do_polling()
    assert again._get_balances_at_broker(None, "LocalTest")[0] == cash


def test_crash_before_local_acceptance_is_provably_not_submitted(tmp_path):
    _, source, journal, _ = setup_book(tmp_path)
    journal.reserve(Intent("ALFA", "buy", 1, number(101)), NOW, Limits())
    LocalPaperBroker(source, Journal(tmp_path, "test-local-book"), Limits(), connect_stream=False)
    assert not journal.pending()


def test_partial_fill_restart_preserves_native_remaining_cash_and_average(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    clock.volume = 200
    _, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    restored = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    restored.sync_positions("LocalTest")
    restored.sync_orders("LocalTest")
    tracked = restored.get_tracked_order(order.identifier)
    assert sum(tx.quantity for tx in tracked.transactions) == 2
    assert tracked.cash_pending(SimpleNamespace()) == pytest.approx(-3 * 100.12)
    clock.price, clock.high = 100.03, 100.04
    advance(clock, source, 1)
    restored.do_polling()
    assert sum(tx.quantity for tx in tracked.transactions) == 4
    assert tracked.get_fill_price() == pytest.approx((2 * 100.12 + 2 * 100.15) / 4)
    assert restored._pull_positions("LocalTest")[0].quantity == 4


def test_observe_only_restart_does_not_fill_preexisting_orders(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value)
    observer = LocalPaperBroker(source, journal, Limits(), connect_stream=False, matching_enabled=False)
    advance(clock, source)
    observer.do_polling()
    assert observer._pull_order(order.identifier, "LocalTest").status == "new"
    assert not observer._pull_positions("LocalTest")
    assert observer._get_balances_at_broker(None, "LocalTest")[0] == 10000
    assert json.loads((tmp_path / "status.json").read_text())["matching_enabled"] is False


def test_local_adapter_rejects_orders_without_risk_reservation(tmp_path):
    _, _, _, broker = setup_book(tmp_path)
    with pytest.raises(RiskError, match="reservation"):
        broker.submit_order(Order("LocalTest", "ALFA", 1, "buy", order_type="limit", limit_price=101))


def test_stop_cancels_order_before_any_fill_including_cancelling_local_state(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value)
    order.status = "cancelling"
    (tmp_path / "STOP").touch()
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "canceled"
    assert broker._get_balances_at_broker(None, "LocalTest")[0] == 10000


def test_gap_outside_limit_cannot_be_reported_as_a_fill(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value, limit=100.1)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "new"
    assert not broker._pull_positions("LocalTest")


def test_buy_rechecks_drawdown_at_fill_time(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    journal.mark_equity(clock.value, 11000, Limits())
    _, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "canceled"
    assert order.status == "canceled"
    assert not broker._pull_positions("LocalTest")


def test_fill_rechecks_position_limit_and_cannot_short(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key, order = submit(broker, journal, clock.value, quantity=10)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "canceled"
    assert not broker._pull_positions("LocalTest")
    journal.update(key, order.identifier, True, clock.value)  # Strategy reconciliation.
    _, sale = submit(broker, journal, clock.value, side="sell", limit=99)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(sale.identifier, "LocalTest").status != "fill"
    assert not broker._pull_positions("LocalTest")
    assert broker._get_balances_at_broker(None, "LocalTest")[0] == 10000


def test_no_retrospective_fill_after_order_expiration(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value)
    clock.value += timedelta(days=1)
    source._cache.clear()
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "canceled"
    assert not broker._pull_positions("LocalTest")


def test_market_calendar_observes_weekend_dst_and_early_close():
    saturday = SessionClock(lambda: datetime(2026, 10, 31, 15, tzinfo=timezone.utc)).status()
    assert saturday["open"] is False
    assert saturday["next_open"].startswith("2026-11-02T14:30:00")  # DST has ended.
    early_close = SessionClock(lambda: datetime(2026, 11, 27, 18, tzinfo=timezone.utc)).status()
    assert early_close["open"] is False  # NYSE closes at 13:00 ET after Thanksgiving.


def test_live_research_does_not_require_an_exact_publication_date():
    evidence = [{"available_at": None, "observed_at": NOW.isoformat(), "estimated": False}]
    validate_evidence(evidence, NOW, False)
    with pytest.raises(RiskError):
        validate_evidence(evidence, NOW, True)


def test_report_is_readable_while_runner_holds_its_lock(tmp_path, capsys):
    from filelock import FileLock
    from scripts.autonomous_fund.run import main
    journal = Journal(tmp_path, "report-test")
    journal.event(NOW, "startup", {"mode": "local", "mock_model": False})
    with FileLock(str(tmp_path / "runner.lock")):
        main(["report", "--run-dir", str(tmp_path)])
    assert '"mode": "local"' in capsys.readouterr().out


def test_dividend_is_nonspendable_and_split_does_not_create_profit(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    old_cash = broker._get_balances_at_broker(None, "LocalTest")[0]
    clock.value += timedelta(days=1)
    clock.price, clock.high = 50, 50.01
    clock.actions = [(clock.value.date().isoformat(), 2, 1)]
    source._cache.clear()
    cash, positions_value, equity = broker._get_balances_at_broker(None, "LocalTest")
    assert cash == old_cash
    assert broker._pull_positions("LocalTest")[0].quantity == 10
    assert positions_value == 500
    assert broker.receivables() == 10
    assert equity == pytest.approx(old_cash + 500 + 10)
    broker._get_balances_at_broker(None, "LocalTest")
    assert broker.receivables() == 10


def test_source_failure_or_stale_data_cannot_fill_an_order(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value)
    stale = source.execution_bars(Asset("ALFA"))
    advance(clock, source, 10)
    source.execution_bars = lambda *args: stale
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "new"
    source.execution_bars = lambda *args: (_ for _ in ()).throw(ConnectionError("synthetic failure"))
    broker.do_polling()
    assert broker._get_balances_at_broker(None, "LocalTest")[0] == 10000


def test_pre_split_stale_price_cannot_create_an_artificial_nav_high(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    prior_quote = source.get_quote(Asset("ALFA"))
    clock.value += timedelta(days=1)
    clock.actions = [(clock.value.date().isoformat(), 2, 0)]
    source._cache.clear()
    source.get_quote = lambda *args, **kwargs: prior_quote
    with pytest.raises(RiskError, match="Price predates applied split"):
        broker._get_balances_at_broker(None, "LocalTest")


@pytest.mark.parametrize("split, expected", [(0.5, "Fractional split"), (3, "Corporate action revision")])
def test_unsupported_or_revised_corporate_action_blocks_matching(tmp_path, split, expected):
    clock, source, journal, broker = setup_book(tmp_path)
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    clock.value += timedelta(days=1)
    stamp = clock.value.date().isoformat()
    if split == 3:
        clock.actions = [(stamp, 2, 0)]
        source._cache.clear()
        broker._corporate_actions()
    clock.actions = [(stamp, split, 0)]
    source._cache.clear()
    broker.do_polling()
    assert expected in broker._last_poll_error


def test_reopened_position_does_not_receive_old_dividends(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, order.identifier, True, clock.value)
    key, order = submit(broker, journal, clock.value, side="sell", limit=99)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, order.identifier, True, clock.value)
    assert not broker._pull_positions("LocalTest")
    clock.value += timedelta(days=1)
    source._cache.clear()
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    clock.actions = [(clock.value.date().isoformat(), 0, 1)]
    source._cache.clear()
    broker._corporate_actions()
    assert broker.receivables() == 0


def test_delayed_dividend_does_not_credit_shares_bought_on_exdate(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, order.identifier, True, clock.value)
    clock.value += timedelta(days=1)
    source._cache.clear()
    submit(broker, journal, clock.value, quantity=2)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_positions("LocalTest")[0].quantity == 7
    clock.actions = [(clock.value.date().isoformat(), 0, 1)]
    source._cache.clear()
    broker._corporate_actions()
    assert broker.receivables() == 5


@pytest.mark.parametrize("seconds, budget_warning", [(360, None), (450, 902)])
def test_manager_timeout_reaches_both_schemas_and_watchdog_budget(tmp_path, monkeypatch, seconds, budget_warning):
    from scripts.autonomous_fund.review import PostmarketReview
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.settings import apply_environment
    from scripts.autonomous_fund.strategy import AutonomousFund, account_digest

    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_AGENT_MAX_RUN_ATTEMPTS", "2")
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config, _ = apply_environment({"model": "deepseek/deepseek-flash", "limits": {},
                                   "universe": {"ALFA": "stock"}},
                                  {"FUND_MANAGER_RUN_TIMEOUT_SECONDS": str(seconds)})
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account"))
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    timeouts = []

    class RecordingRuntime(ScriptedRuntime):
        def run(self, request):
            timeouts.append(request.run_timeout_seconds)
            return super().run(request)

    strategy = AutonomousFund(broker=broker, name="ManagerTimeout", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": RecordingRuntime()})
    try:
        strategy._executor._initialize()
        manager = strategy.agents["manager"]
        timeouts.clear()
        context = {"as_of": NOW.isoformat(), "eligible_universe": ["ALFA"]}
        assert manager.run(context=context).parsed is not None
        assert manager.run(context={**context, "phase": "postmarket_review", "session": {"session_date": "2026-11-02"}},
                           output_schema=PostmarketReview).parsed is not None
        assert timeouts == [seconds, seconds]
        warnings = [r["body"]["worst_case_seconds"]["manager"] for r in journal.all_events()
                    if r["kind"] == "agent_time_budget_exceeds_monitor"]
        assert warnings == ([] if budget_warning is None else [budget_warning])
    finally:
        broker._close_connection()


def test_local_mode_runs_through_native_strategy_executor_with_scripted_model(tmp_path, monkeypatch):
    from lumibot.traders import Trader
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "reasoning_effort": None, "limits": {},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    binding = hashlib.sha256(encode({"mode": "local", "account": "local-virtual-account", "config": config}).encode()).hexdigest()
    journal = Journal(tmp_path, binding)
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="LocalTest", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": ScriptedRuntime()})
    trader = Trader()
    trader.add_strategy(strategy)
    trader.run_all(run_once=True)
    assert strategy.is_backtesting is False
    assert journal.pending()
    advance(clock, source)
    # run_once closes its broker. Continue through a new connection to the same
    # durable account, as a restarted running process would.
    restored = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    restored.do_polling()
    observed = restored._pull_order(journal.pending()[0]["broker_id"], strategy.name)
    assert observed.status == "fill"


def test_exchange_hours_are_built_once_a_day_not_three_times_a_second():
    """2026-11-03: LumiBot's live loop asks for the open and close every second; each call rebuilt the exchange
    calendar and kept a CPU core busy around the clock. The cached answer must equal the uncached one."""
    from datetime import datetime, timezone
    from lumibot.brokers import Broker
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import CachedMarketHours

    class Counting:  # LumiBot's own market_hours, without an abstract broker around it.
        def __init__(self):
            self.market, self.extended_trading_minutes, self.calls = "NYSE", 0, 0

        def market_hours(self, market="NASDAQ", close=True, next=False, date=None):
            self.calls += 1
            return Broker.market_hours(self, market, close, next, date)

    class Cached(CachedMarketHours, Counting):
        pass

    plain, cached = Counting(), Cached()
    day = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    for _ in range(50):
        assert cached.market_hours(close=True, date=day) == plain.market_hours(close=True, date=day)
        cached.market_hours(close=False, date=day)
    assert cached.calls == 2 and plain.calls == 50  # One build per (open/close, day).
    assert cached.market_hours(close=True, date=day.replace(hour=23)) == cached.market_hours(close=True, date=day)
    later = cached.market_hours(close=True, date=datetime(2026, 11, 4, 15, tzinfo=timezone.utc))
    assert later > cached.market_hours(close=True, date=day) and cached.calls == 3  # A new day is built afresh.
    assert cached.market_hours(close=True) is not None  # Today, with no date given.
    assert issubclass(LocalPaperBroker, CachedMarketHours)
