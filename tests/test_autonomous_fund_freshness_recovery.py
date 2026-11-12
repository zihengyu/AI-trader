"""Freshness recovery uses new source/model evidence, never rewritten timestamps.

Neutral symbols, synthetic clocks and scripted answers only; no network or inference.
"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.market_data import LiveTape, YahooRealtimeData
from scripts.autonomous_fund.risk import MarketDataUnavailable, RiskError, number, observed_quote
from scripts.autonomous_fund.strategy import AutonomousFund, Decision
from .test_autonomous_fund_autonomy import accept, watching
from .test_autonomous_fund_decision_correction import answer, events, fund, valid
from .test_autonomous_fund_local import Clock
from .test_autonomous_fund_quant import LIMITS, NOW, account, decision, quote
from .test_autonomous_fund_realtime import frame


def inert_tape(capacity=60):
    tape = LiveTape(connect=lambda: None, max_symbols=capacity)
    tape._stop.set()  # Subscription changes are exercised without starting a real websocket.
    return tape


def test_capacity_admits_later_symbols_and_keeps_recently_requested_symbols():
    tape = inert_tape(2)
    tape.watch(["ALFA", "BETA"])
    tape.handle(frame("ALFA", 100, NOW))
    tape.watch(["ALFA"])  # It is still being observed; BETA is now least recent.
    tape.watch(["GAMMA"])
    assert set(tape._wanted) == {"ALFA", "GAMMA"}
    tape.handle(frame("GAMMA", 105, NOW))
    assert tape.tick("GAMMA")["price"] == 105
    assert tape.tick("ALFA")["price"] == 100
    tape.handle(frame("BETA", 99, NOW))  # A late frame after eviction must not repopulate the cache.
    assert tape.tick("BETA") is None
    assert tape.diagnostics()["subscription_evictions"] == 1


def test_stream_sends_unsubscribe_before_subscribing_the_replacement():
    sent, tape = [], LiveTape(max_symbols=2)
    tape.watch = lambda symbols: None  # Seed first subscriptions without starting a thread.
    tape._wanted = {"ALFA", "BETA"}

    class Client:
        reads = 0

        def send(self, text):
            sent.append(json.loads(text))

        def recv(self, timeout):
            self.reads += 1
            if self.reads == 1:
                tape._wanted = {"ALFA", "GAMMA"}
            else:
                tape.close()
            raise TimeoutError

        def close(self):
            pass

    tape.connect = Client
    tape._run()
    assert sent == [{"subscribe": ["ALFA", "BETA"]}, {"unsubscribe": ["BETA"]},
                    {"subscribe": ["GAMMA"]}]
    assert tape._subscribed == {"ALFA", "GAMMA"}


def test_retiring_one_share_class_spelling_keeps_the_other_subscribed():
    sent, tape = [], LiveTape(max_symbols=2)
    tape._wanted = {"SHARE.A", "SHARE-A"}

    class Client:
        reads = 0

        def send(self, text):
            sent.append(json.loads(text))

        def recv(self, timeout):
            self.reads += 1
            if self.reads == 1:
                tape.handle(frame("SHARE-A", 100, NOW))
                assert tape.tick("SHARE.A")["price"] == tape.tick("SHARE-A")["price"] == 100
                tape._wanted = {"SHARE-A", "BETA"}
            else:
                tape.close()
            raise TimeoutError

        def close(self):
            pass

    tape.connect = Client
    tape._run()
    assert sent == [{"subscribe": ["SHARE-A"]}, {"subscribe": ["BETA"]}]


def test_history_failure_still_subscribes_the_symbol_and_allows_a_fresh_stream_quote():
    clock, tape = Clock(), inert_tape()

    def unavailable(*args):
        raise TimeoutError("synthetic minute endpoint failure")

    source = YahooRealtimeData(now=clock, fetch=unavailable, tape=tape)
    with pytest.raises(MarketDataUnavailable):
        source.get_quote(Asset("GAMMA"))
    assert "GAMMA" in tape._wanted
    stamp = clock() - timedelta(seconds=2)
    tape.handle(frame("GAMMA", 105, stamp))
    actual = source.get_quote(Asset("GAMMA"))  # History is still cooling down.
    assert observed_quote(actual, clock(), LIMITS).as_of == stamp
    assert actual.volume is None  # No fabricated minute volume or bar for matching.
    with pytest.raises(MarketDataUnavailable):
        source.execution_bars(Asset("GAMMA"))


@pytest.mark.parametrize("seconds", [-1, 61])
def test_history_failure_cannot_be_masked_by_a_future_or_stale_tick(seconds):
    clock, tape = Clock(), inert_tape()
    tape.watch(["GAMMA"])
    tape.handle(frame("GAMMA", 105, clock() - timedelta(seconds=seconds)))
    source = YahooRealtimeData(now=clock, fetch=lambda *a: (_ for _ in ()).throw(TimeoutError()), tape=tape)
    with pytest.raises(MarketDataUnavailable):
        source.get_quote(Asset("GAMMA"))


@pytest.mark.parametrize("formatted_time", [False, True])
def test_yahoo_fetch_retains_the_last_trade_price_and_time_from_the_same_response(monkeypatch, formatted_time):
    from collections.abc import Mapping
    import yfinance as yf
    clock, calls = Clock(), []
    stamp = clock() - timedelta(seconds=2)
    source_stamp = stamp if formatted_time else int(stamp.timestamp())

    class Metadata(Mapping):
        def __getitem__(self, key):
            return {"regularMarketPrice": 105, "regularMarketTime": source_stamp}[key]

        def __iter__(self):
            raise AssertionError("Copying all metadata would force lazy network-backed fields")

        def __len__(self):
            return 2

    class Ticker:
        def __init__(self, symbol):
            calls.append(symbol)

        def history(self, **kwargs):
            calls.append(kwargs)
            return clock.fetch("SHARE.A", "1m", "5d")

        def get_history_metadata(self):
            return Metadata()

    monkeypatch.setattr(yf, "Ticker", Ticker)
    data = YahooRealtimeData._fetch("SHARE.A", "1m", "5d")
    assert calls[0] == "SHARE-A" and calls[1]["prepost"] is False
    assert data.attrs["yahoo_last_trade"] == {"price": 105, "as_of": source_stamp}


def test_optional_metadata_failure_keeps_valid_minute_history(monkeypatch):
    import yfinance as yf
    clock = Clock()

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            return clock.fetch("ALFA", "1m", "5d")

        def get_history_metadata(self):
            raise OSError("synthetic optional metadata failure")

    monkeypatch.setattr(yf, "Ticker", Ticker)
    data = YahooRealtimeData._fetch("ALFA", "1m", "5d")
    assert not data.empty and "yahoo_last_trade" not in data.attrs


def chart_source(clock, trade, tape=None, *, delayed=True):
    def fetch(symbol, interval, period):
        data = clock.fetch(symbol, interval, period)
        if interval == "1m":
            if delayed:
                data.index -= timedelta(minutes=3)
            data.attrs["yahoo_last_trade"] = trade
        return data
    return YahooRealtimeData(now=clock, fetch=fetch, tape=tape)


@pytest.mark.parametrize("stream_enabled", [False, True])
def test_a_symbol_without_ticks_can_use_a_fresh_chart_last_trade(stream_enabled):
    clock = Clock()
    stamp = clock() - timedelta(seconds=2)
    tape = inert_tape() if stream_enabled else None
    source = chart_source(clock, {"price": 105, "as_of": int(stamp.timestamp())}, tape)
    actual = source.get_quote(Asset("GAMMA"))
    assert actual.raw_data["price_kind"] == "chart_last_trade_with_spread_proxy"
    assert actual.volume is None and actual.price == 105
    assert observed_quote(actual, clock(), LIMITS).as_of == stamp
    assert source.diagnostics()["quotes_by_kind"]["chart_quote"] == 1
    assert source.execution_bars(Asset("GAMMA")).iloc[-1].close == 100  # Matching bars remain unaltered.


@pytest.mark.parametrize("bad", ["stale", "future", "missing_time", "naive", "zero", "nonfinite"])
def test_chart_quote_fallback_still_requires_real_fresh_positive_source_evidence(bad):
    clock = Clock()
    trade = {"price": 105, "as_of": clock() - timedelta(seconds=2)}
    if bad in {"stale", "future"}:
        trade["as_of"] = clock() + timedelta(seconds=-61 if bad == "stale" else 1)
    elif bad == "missing_time":
        trade.pop("as_of")
    elif bad == "naive":
        trade["as_of"] = clock().replace(tzinfo=None)
    else:
        trade["price"] = 0 if bad == "zero" else float("nan")
    source = chart_source(clock, trade)
    actual = source.get_quote(Asset("GAMMA"))
    assert actual.raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"
    with pytest.raises(RiskError):
        observed_quote(actual, clock(), LIMITS)


def test_cached_chart_trade_is_not_retimestamped_and_cannot_outlive_freshness():
    clock = Clock()
    stamp = clock() - timedelta(seconds=2)
    source = chart_source(clock, {"price": 105, "as_of": stamp})
    actual = source.get_quote(Asset("GAMMA"))
    assert observed_quote(actual, clock(), LIMITS).as_of == stamp
    clock.value += timedelta(seconds=70)  # Cache receipt age is still short; source age is not.
    with pytest.raises(RiskError):
        observed_quote(source.get_quote(Asset("GAMMA")), clock(), LIMITS)
    # Crossing a minute boundary now refreshes source evidence even before the
    # normal receipt-cache TTL expires. The returned old trade still expires.
    assert source.diagnostics()["requests"] == 2


def test_an_older_chart_quote_cannot_replace_a_more_recent_completed_bar():
    clock = Clock()
    source = chart_source(clock, {"price": 105, "as_of": clock() - timedelta(seconds=10)}, delayed=False)
    assert source.get_quote(Asset("GAMMA")).raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"


def test_fresh_stream_ticks_still_take_priority_over_chart_metadata():
    clock, tape = Clock(), inert_tape()
    tape.watch(["GAMMA"])
    tape.handle(frame("GAMMA", 106, clock() - timedelta(seconds=1)))
    source = chart_source(clock, {"price": 105, "as_of": clock() - timedelta(seconds=2)}, tape)
    actual = source.get_quote(Asset("GAMMA"))
    assert actual.price == 106 and actual.raw_data["price_kind"] == "live_trade_with_spread_proxy"
    assert source.diagnostics()["quotes_by_kind"]["live"] == 1
    assert not source.diagnostics()["quotes_by_kind"].get("chart_quote")


def test_chart_metadata_does_not_enable_after_hours_quotes():
    clock = Clock()
    clock.value += timedelta(hours=8)  # After the regular session.
    source = chart_source(clock, {"price": 105, "as_of": clock() - timedelta(seconds=2)})
    assert not source.session.status()["open"]
    assert source.get_quote(Asset("GAMMA")).raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_trading_quote_is_compared_with_the_clock_after_source_io(tmp_path, mode):
    strategy = watching(tmp_path, {})
    clock = Clock()
    strategy.mode, strategy.get_datetime = mode, clock
    strategy.get_historical_prices = lambda *a, **k: SimpleNamespace(
        df=clock.fetch("ALFA", "1d", "6mo").rename(columns=str.lower))

    def fetch_quote(asset):
        clock.value += timedelta(seconds=5)
        return SimpleNamespace(bid=100, ask=100.1, raw_data={
            "original_response": SimpleNamespace(timestamp=clock())})

    strategy.get_quote = fetch_quote
    actual = AutonomousFund._price(strategy, "ALFA")
    assert actual.as_of == clock()


@pytest.mark.parametrize("valid_refresh", [True, False])
def test_alert_refreshes_a_quote_that_aged_during_the_same_watch(tmp_path, valid_refresh):
    strategy = watching(tmp_path, {})
    clock, reads = [NOW], []
    strategy.mode, strategy.get_datetime = "local", lambda: clock[0]

    def price(symbol, record=True):
        if symbol == "BETA":
            clock[0] += timedelta(seconds=75)
        return quote(100) if symbol == "ALFA" else SimpleNamespace(bid=100, ask=100, as_of=clock[0])

    def refresh(asset):
        reads.append(asset.symbol)
        stamp = clock[0] - timedelta(seconds=0 if valid_refresh else 61)
        return SimpleNamespace(price=105, raw_data={"original_response": SimpleNamespace(timestamp=stamp)})

    strategy._price, strategy.get_quote = price, refresh
    strategy._alert_price = lambda symbol: AutonomousFund._alert_price(strategy, symbol)
    accept(strategy, [{"symbol": "ALFA", "weight": .05}, {"symbol": "BETA", "weight": .05}],
           [{"symbol": "ALFA", "above": 104, "reason": "Synthetic observation"}])
    assert strategy._watch(account(), NOW) is False
    assert reads == ["ALFA"]
    triggers = events(strategy, "review_triggered")
    assert bool(triggers) is valid_refresh
    assert bool(strategy.execution.alerts()) is not valid_refresh
    assert not strategy.journal.pending()


def live_fund(tmp_path, answers):
    strategy, calls = fund(tmp_path, answers)
    strategy.mode = "local"
    strategy._snapshot = account
    return strategy, calls


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("reason", ["stale", "future", "expired"])
def test_live_timing_failure_gets_one_new_assessment_before_acceptance(tmp_path, reason, mode):
    bad = decision([("ALFA", .05)])
    if reason == "stale":
        bad["as_of"] = (NOW - timedelta(seconds=301)).isoformat()
    elif reason == "future":
        bad["as_of"] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        bad["as_of"], bad["expires_at"] = (NOW - timedelta(seconds=2)).isoformat(), NOW.isoformat()
    strategy, calls = live_fund(tmp_path, [answer(json.dumps(bad), Decision), valid()])
    strategy.mode = mode
    strategy._decide(account(), NOW, False)
    managers = [c for c in calls if c["name"] == "manager"]
    assert len(managers) == 2 and managers[1]["purpose"] == "decision_correction"
    correction = managers[1]["context"]["decision_timing_correction"]
    assert correction["reason"] == reason and correction["as_of"] == bad["as_of"]
    assert events(strategy, "decision_time_retry")[0]["diagnostic"] == correction
    assert len(events(strategy, "decision_accepted")) == 1
    assert events(strategy, "decision_accepted")[0]["decision"]["as_of"] == NOW.isoformat()
    assert len([a for a in events(strategy, "agent") if a["name"] == "manager"]) == 2


@pytest.mark.parametrize("blocker", ["stop", "calls", "cost", "historical", "overweight"])
def test_correction_does_not_bypass_stops_budgets_risk_or_historical_rules(tmp_path, blocker):
    bad = decision([("ALFA", .9 if blocker == "overweight" else .05)])
    if blocker != "overweight":
        bad["as_of"] = (NOW - timedelta(seconds=301)).isoformat()
    strategy, calls = live_fund(tmp_path, [answer(json.dumps(bad), Decision), valid()])
    if blocker == "stop":
        original = strategy._agent_call

        def stop_during_answer(name, **kwargs):
            result = original(name, **kwargs)
            strategy.halt_path.touch()
            return result
        # STOP arrives during the first call; a pre-existing STOP now skips it.
        strategy._agent_call = stop_during_answer
    elif blocker == "calls":
        original = strategy.journal.counter
        strategy.journal.counter = lambda name, when, add=0: (
            strategy.model_budget.max_agent_runs_per_day - strategy.model_budget.reserved_review_runs
            if name == "agent_runs" and calls else original(name, when, add))
    elif blocker == "cost":
        strategy._cost_exhausted = lambda now, step: step == "decision_correction"
    elif blocker == "historical":
        strategy.mode = "mock"
    with pytest.raises(RiskError):
        strategy._decide(account(), NOW, False)
    assert len([c for c in calls if c["name"] == "manager"]) == 1
    assert not events(strategy, "decision_time_retry") and not events(strategy, "decision_accepted")
    assert strategy.journal.get_meta("decision_failures") == 1


def test_format_and_timing_failures_share_one_correction_limit(tmp_path):
    from .test_autonomous_fund_decision_correction import PROSE
    bad = decision([]) | {"as_of": (NOW - timedelta(seconds=301)).isoformat()}
    strategy, calls = live_fund(tmp_path, [answer(PROSE, Decision), answer(json.dumps(bad), Decision)])
    with pytest.raises(RiskError, match="also after one format correction"):
        strategy._decide(account(), NOW, False)
    assert len(calls) == 2 and not events(strategy, "decision_accepted")


def test_a_second_timing_failure_still_blocks_without_a_third_call(tmp_path):
    bad = answer(json.dumps(decision([]) | {"as_of": (NOW - timedelta(seconds=301)).isoformat()}), Decision)
    strategy, calls = live_fund(tmp_path, [bad, bad, valid()])
    with pytest.raises(RiskError, match="also after one timing correction"):
        strategy._decide(account(), NOW, False)
    assert len(calls) == 2 and not events(strategy, "decision_accepted")
    assert strategy.journal.get_meta("decision_failures") == 1


def test_slow_model_gets_a_new_assessment_with_a_refreshed_account_and_clock(tmp_path):
    from dataclasses import asdict, replace
    strategy, calls = live_fund(tmp_path, [])
    clock, snapshots = [NOW], []
    strategy.get_datetime = lambda: clock[0]
    actual_calls = []

    def call(name, **kwargs):
        context = kwargs["context"]
        actual_calls.append(context)
        made = context["as_of"]
        clock[0] += timedelta(seconds=305 if len(actual_calls) == 1 else 10)
        return answer(json.dumps(decision([]) | {
            "as_of": made, "expires_at": (clock[0] + timedelta(hours=1)).isoformat(),
            "next_review_at": (clock[0] + timedelta(minutes=30)).isoformat()}), Decision)

    def snapshot():
        clock[0] += timedelta(seconds=2)
        fresh = replace(account(), cash=number(9900), equity=number(9900), as_of=clock[0])
        snapshots.append(fresh)
        return fresh

    strategy._agent_call, strategy._snapshot = call, snapshot
    strategy._decide(account(), NOW, False)
    assert len(actual_calls) == 2
    correction = actual_calls[1]
    assert correction["as_of"] == (NOW + timedelta(seconds=307)).isoformat()
    assert correction["account"] == asdict(snapshots[0])
    assert actual_calls[0]["account"]["as_of"] == NOW
    assert correction["decision_timing_correction"]["age_seconds"] == 305
    assert correction["decision_timing_correction"]["request_as_of"] == NOW.isoformat()
    [accepted] = events(strategy, "decision_accepted")
    assert accepted["decision"]["as_of"] == correction["as_of"]
    assert not events(strategy, "decision_backoff")


@pytest.mark.parametrize("control", ["stop", "calls", "cost"])
def test_controls_are_rechecked_after_correction_preparation(tmp_path, control):
    bad = answer(json.dumps(decision([]) | {"as_of": (NOW - timedelta(seconds=301)).isoformat()}), Decision)
    strategy, calls = live_fund(tmp_path, [bad, valid()])

    def snapshot():
        if control == "stop":
            strategy.halt_path.touch()
        elif control == "calls":
            strategy.journal.counter("agent_runs", NOW, strategy.model_budget.max_agent_runs_per_day)
        else:
            strategy._cost_exhausted = lambda now, step: True
        return account()

    strategy._snapshot = snapshot
    with pytest.raises(RiskError):
        strategy._decide(account(), NOW, False)
    assert len(calls) == 1 and not events(strategy, "decision_time_retry")
    assert not events(strategy, "decision_accepted")


@pytest.mark.parametrize("evidence", ["stream", "chart_quote"])
def test_native_local_strategy_alert_recovers_with_fresh_source_evidence(tmp_path, monkeypatch, evidence):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import account_digest
    from scripts.autonomous_fund.risk import Account, number
    from .test_autonomous_fund_autonomy import decision_body

    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock, tape, chart_ready = Clock(), inert_tape(2 if evidence == "stream" else 60), [False]

    def fetch(symbol, interval, period):
        data = clock.fetch(symbol, interval, period)
        if interval == "1m":
            data.index -= timedelta(minutes=3)  # Delayed minute feed; it cannot trigger the alert.
            if chart_ready[0]:
                data.attrs["yahoo_last_trade"] = {"price": 105, "as_of": clock() - timedelta(seconds=2)}
        return data

    source = YahooRealtimeData(now=clock, fetch=fetch, tape=tape)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "mandate": {"capital_usd": None},
              "universe": {s: "stock" for s in ("ALFA", "BETA", "GAMMA")}, "critic": {"enabled": False}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="SubscriptionRecovery", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": ScriptedRuntime()})
    try:
        strategy._executor._initialize()
        tape.watch(["ALFA", "BETA"])
        body = decision_body([], [{"symbol": "GAMMA", "above": 104, "reason": "Synthetic source recovery"}],
                             when=clock())
        strategy.execution.accept(body, clock(), strategy.universe, LIMITS)
        with pytest.raises(RiskError):
            strategy._alert_price("GAMMA")  # First read subscribes the later symbol, but cannot invent a quote.
        if evidence == "stream":
            assert set(tape._wanted) == {"BETA", "GAMMA"}
        else:
            # Native initialization also quotes benchmarks and the cash sweep.
            assert {"ALFA", "BETA", "GAMMA"} <= set(tape._wanted)
            assert len(tape._wanted) < tape.max_symbols
        stamp = clock() - timedelta(seconds=2)
        if evidence == "stream":
            tape.handle(frame("GAMMA", 105, stamp))
        else:
            chart_ready[0] = True
            source._cache.pop(("GAMMA", "1m", "5d"))  # Only this test's synthetic series, never a shared cache.
        assert strategy._watch(Account(number(10000), number(10000), {}, clock()), clock()) is False
        [trigger] = events(strategy, "price_alert")
        assert trigger["source_as_of"] == str(stamp)
        assert not strategy.execution.alerts() and not journal.pending()
        assert not events(strategy, "agent_call_started")
    finally:
        broker._close_connection()
