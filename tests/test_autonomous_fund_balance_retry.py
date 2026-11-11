"""Native balance refresh failures must not re-read on every cached-cash access.

Uses native Strategy.update_broker_balances and the application's real local
broker path, with synthetic clocks/data and no paid inference or external APIs.
"""
from types import SimpleNamespace

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.risk import MarketDataUnavailable, RiskError
from scripts.autonomous_fund.strategy import AutonomousFund


def refresh_harness(monkeypatch, failure="market", mode="local"):
    timer, reads, logs = [100.0], [], []
    monkeypatch.setattr("scripts.autonomous_fund.strategy.monotonic", lambda: timer[0])
    strategy = object.__new__(AutonomousFund)
    strategy.mode, strategy.is_backtesting = mode, False
    strategy._quote_asset = Asset("USD", asset_type="forex")
    strategy._portfolio_value, strategy._position_value, strategy.cached_cash = 10000, 500, 9500
    strategy.last_broker_balances_update = -1e9
    strategy.logger = SimpleNamespace(info=lambda *a, **k: logs.append(a),
                                     warning=lambda *a, **k: logs.append(a), debug=lambda *a, **k: None)

    def read(*args):
        reads.append(timer[0])
        if failure == "market":
            raise MarketDataUnavailable("Synthetic history cooling down for ALFA")
        if failure == "risk":
            raise RiskError("Synthetic ledger reconciliation failed")
        if failure == "os":
            raise OSError("Synthetic unavailable storage")
        if failure == "incomplete":
            return None, 500, None
        return 9000, 1100, 10100

    strategy.broker = SimpleNamespace(_get_balances_at_broker=read)
    strategy._set_cash_position = lambda value: setattr(strategy, "cached_cash", value)
    return strategy, timer, reads, logs


@pytest.mark.parametrize("failure", ["market", "risk", "os", "incomplete"])
def test_failed_background_refresh_is_bounded_and_never_becomes_a_success(monkeypatch, failure):
    strategy, timer, reads, logs = refresh_harness(monkeypatch, failure)
    for second in range(31):
        timer[0] = 100 + second
        assert strategy.update_broker_balances(force_update=False) is False
    assert reads == [100, 115, 130]
    assert len(logs) == 3
    assert strategy.last_broker_balances_update == -1e9
    assert (strategy.cached_cash, strategy._position_value, strategy._portfolio_value) == (9500, 500, 10000)


def test_explicit_refresh_bypasses_background_retry_and_can_recover_immediately(monkeypatch):
    strategy, timer, reads, _ = refresh_harness(monkeypatch)
    assert strategy.update_broker_balances(force_update=False) is False
    timer[0] += 1
    assert strategy.update_broker_balances(force_update=True) is False
    assert reads == [100, 101]
    strategy.broker._get_balances_at_broker = lambda *a: (9000, 1100, 10100)
    assert strategy.update_broker_balances(force_update=True) is True
    assert (strategy.cached_cash, strategy._position_value, strategy._portfolio_value) == (9000, 1100, 10100)
    assert strategy.last_broker_balances_update > 0
    assert strategy._local_balance_retry_at == 0


@pytest.mark.parametrize("mode", ["paper", "backtest", None])
def test_other_runtimes_and_early_initialization_keep_native_behavior(monkeypatch, mode):
    strategy, timer, reads, _ = refresh_harness(monkeypatch, mode=mode)
    strategy._local_balance_retry_at = 1e9
    assert strategy.update_broker_balances(force_update=False) is False
    timer[0] += 1
    assert strategy.update_broker_balances(force_update=False) is False
    assert reads == [100, 101]


def test_background_retry_is_private_to_each_strategy_instance(monkeypatch):
    first, _, first_reads, _ = refresh_harness(monkeypatch)
    second, _, second_reads, _ = refresh_harness(monkeypatch)
    assert first.update_broker_balances(force_update=False) is False
    assert second.update_broker_balances(force_update=False) is False
    assert first_reads == second_reads == [100]


def test_native_local_snapshot_still_rejects_unavailable_actions_during_background_retry(tmp_path, monkeypatch):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import account_digest
    from .test_autonomous_fund_local import Clock, advance, submit
    from .test_autonomous_fund_quant import LIMITS

    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock, timer = Clock(), [100.0]
    monkeypatch.setattr("scripts.autonomous_fund.strategy.monotonic", lambda: timer[0])
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "mandate": {"capital_usd": None},
              "universe": {"ALFA": "stock"}, "cash_sweep": {"enabled": False}, "critic": {"enabled": False}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="BalanceRetry", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local",
        "account_binding": "local-virtual-account", "runtime": ScriptedRuntime()})
    try:
        strategy._executor._initialize()
        key, order = submit(broker, journal, clock())
        journal.update(key, order.identifier, False, clock())
        advance(clock, source)
        broker.do_polling()
        assert broker._pull_broker_order(order.identifier)["filled"] == 5
        strategy._snapshot()
        before = (strategy._portfolio_value, strategy.last_broker_balances_update)

        def failed_actions(symbol, interval, period):
            if interval == "1d":
                raise TimeoutError("synthetic private provider response")
            return clock.fetch(symbol, interval, period)

        source.fetch = failed_actions
        source._cache.clear()  # Only this fixture's source cache.
        strategy.last_broker_balances_update = -1e9
        assert strategy.update_broker_balances(force_update=False) is False
        assert strategy.update_broker_balances(force_update=False) is False
        with pytest.raises(MarketDataUnavailable):
            strategy._snapshot()
        assert strategy.get_cash() is None  # An explicit query must not return the retained native value.
        assert strategy._portfolio_value == before[0]
        assert strategy.last_broker_balances_update == -1e9
        # Restore this fixture's independent source; an explicit refresh need not wait 15 seconds.
        source.fetch = clock.fetch
        source._failures.clear()
        assert strategy.update_broker_balances(force_update=True) is True
        assert strategy.get_cash() == pytest.approx(broker._get_balances_at_broker(None, strategy)[0])
    finally:
        broker._close_connection()


def test_history_backoff_keeps_the_first_cause_until_recovery_without_provider_payload(monkeypatch):
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from .test_autonomous_fund_local import Clock
    clock, timer, reads = Clock(), [100.0], []
    monkeypatch.setattr("scripts.autonomous_fund.market_data.time.monotonic", lambda: timer[0])

    def unavailable(*args):
        reads.append(args)
        raise TimeoutError("private-provider-response-with-token")

    source = YahooRealtimeData(now=clock, fetch=unavailable)
    with pytest.raises(MarketDataUnavailable):
        source.get_quote(Asset("BETA"))
    first = source.diagnostics()["source_failures"][0]
    assert (first["symbol"], first["interval"], first["period"], first["error"]) == ("BETA", "1m", "5d", "TimeoutError")
    with pytest.raises(MarketDataUnavailable) as failure:
        source.get_quote(Asset("BETA"))
    assert failure.value.diagnostic["reason"] == "source_backoff"
    assert failure.value.diagnostic["last_error"] == "TimeoutError"
    assert source.diagnostics()["source_failures"][0]["failed_at"] == first["failed_at"]
    assert len(reads) == 1
    assert "private-provider-response" not in str(source.diagnostics()) + str(failure.value.diagnostic)
    timer[0] += 16
    source.fetch = clock.fetch
    assert source.get_quote(Asset("BETA")).price > 0
    assert source.diagnostics()["source_failures"] == []


def test_another_symbols_success_does_not_clear_the_failed_series_diagnostic():
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from .test_autonomous_fund_local import Clock
    clock = Clock()

    def fetch(symbol, interval, period):
        if symbol == "BETA":
            raise ConnectionError("private provider details")
        return clock.fetch(symbol, interval, period)

    source = YahooRealtimeData(now=clock, fetch=fetch)
    with pytest.raises(MarketDataUnavailable):
        source.execution_bars(Asset("BETA"))
    assert source.get_quote(Asset("GAMMA")).price > 0
    assert [row["symbol"] for row in source.diagnostics()["source_failures"]] == ["BETA"]
