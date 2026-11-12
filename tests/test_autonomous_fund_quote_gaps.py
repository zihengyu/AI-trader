"""Incomplete provider rows cannot poison usable bars or manufacture fills.

Synthetic prices and clocks only; assertions exercise the local adapter and lifecycle hooks.
"""
from datetime import datetime, timedelta

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.log_errors import error_records
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.strategy import AutonomousFund
from scripts.autonomous_fund.risk import MarketDataUnavailable, ObservedPrice, RiskError, number
from .test_autonomous_fund_local import Clock, advance, setup_book, submit
from .test_autonomous_fund_quant import LIMITS, NOW, account
from .test_autonomous_fund_autonomy import accept, watching
from .test_autonomous_fund_review import events, review_harness


@pytest.mark.parametrize("interval", ["1m", "1d"])
@pytest.mark.parametrize("field,value", [
    ("Close", float("nan")), ("Open", float("inf")), ("High", float("-inf")),
    ("Low", 0), ("Volume", float("nan")), ("Volume", -1), ("High", 90),
])
def test_one_broken_completed_row_preserves_other_source_rows(interval, field, value):
    clock = Clock()
    raw = clock.fetch("ALFA", interval, "5d")
    bad_at = raw.index[-4]
    raw.loc[bad_at, field] = value
    raw.attrs["yahoo_last_trade"] = {"price": 102, "as_of": clock() - timedelta(seconds=1)}
    source = YahooRealtimeData(now=clock, fetch=lambda *a: raw)
    actual = source._history(Asset("ALFA"), interval, "5d")
    assert bad_at not in actual.index
    assert actual.loc[raw.index[-3], "close"] == 100
    assert actual.attrs["yahoo_last_trade"] == raw.attrs["yahoo_last_trade"]
    diagnostics = source.diagnostics()
    assert diagnostics["discarded_rows"] == 1
    assert diagnostics["last_rejected_bar"]["symbol"] == "ALFA"
    assert diagnostics["last_rejected_bar"]["fields"]
    # The adapter displays New York time; compare the instant across timezones.
    assert datetime.fromisoformat(diagnostics["last_rejected_bar"]["as_of"]) == bad_at


def test_bad_latest_minute_does_not_retimestamp_an_older_mark():
    clock = Clock()
    raw = clock.fetch("BETA", "1m", "5d")
    raw.loc[raw.index[-2], "Close"] = float("nan")
    source = YahooRealtimeData(now=clock, fetch=lambda *a: raw)
    quote = source.get_quote(Asset("BETA"))
    from scripts.autonomous_fund.risk import observed_quote
    with pytest.raises(MarketDataUnavailable) as failure:
        observed_quote(quote, clock(), LIMITS)
    assert failure.value.diagnostic["reason"] == "stale"
    assert failure.value.diagnostic["age_seconds"] == 65
    assert failure.value.diagnostic["source_as_of"] == quote.timestamp.isoformat()


def test_invalid_bar_never_fills_but_next_valid_bar_can_fill_same_order(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock())
    original = source.fetch
    advance(clock, source)

    def missing_latest(symbol, interval, period):
        raw = original(symbol, interval, period)
        if interval == "1m":
            raw.loc[raw.index[-2], "Volume"] = float("nan")
        return raw

    source.fetch = missing_latest
    broker.do_polling()
    assert broker._pull_broker_order(order.identifier)["filled"] == 0
    assert not [r for r in journal.all_events() if r["kind"] == "local_fill"]
    source.fetch = original
    advance(clock, source, minutes=1)
    broker.do_polling()
    assert broker._pull_broker_order(order.identifier)["filled"] == 5


def test_invalid_corporate_action_remains_a_blocker_with_symbol_and_field():
    clock = Clock()
    raw = clock.fetch("GAMMA", "1d", "6mo")
    raw.loc[raw.index[-3], "Stock Splits"] = float("nan")
    source = YahooRealtimeData(now=clock, fetch=lambda *a: raw)
    with pytest.raises(MarketDataUnavailable, match="GAMMA.*split"):
        source.corporate_actions(Asset("GAMMA"), None)


@pytest.mark.parametrize("delta,price,reason", [(61, 100, "stale"), (-1, 100, "future"),
                                               (0, 0, "nonpositive"), (0, float("nan"), "invalid_price")])
def test_observation_rejection_preserves_exact_clock_evidence(delta, price, reason):
    stamp = NOW - timedelta(seconds=delta)
    with pytest.raises(MarketDataUnavailable) as failure:
        ObservedPrice(price, stamp).validate(NOW, LIMITS)
    detail = failure.value.diagnostic
    assert detail["reason"] == reason
    assert detail["source_as_of"] == stamp.isoformat()
    assert detail["checked_at"] == NOW.isoformat()
    assert detail["age_seconds"] == delta
    assert detail["max_age_seconds"] == LIMITS.quote_max_seconds


def test_repeated_alert_rejection_records_bounded_diagnostics_then_can_trigger(tmp_path):
    strategy = watching(tmp_path, {})
    # Exercise the real notification dedupe, rather than the harness's collector.
    strategy._notify_fund = lambda kind, detail: AutonomousFund._notify_fund(strategy, kind, detail)
    strategy.log_message = lambda *a, **k: None
    strategy._notices = {}
    strategy._alert_price = lambda symbol: ObservedPrice(105, NOW - timedelta(seconds=75)).validate(NOW, LIMITS)
    accept(strategy, [], [{"symbol": "BETA", "above": 104, "reason": "Synthetic level"}])
    strategy._watch(account(), NOW)
    strategy._watch(account(), NOW)
    rejected = events(strategy, "price_observation_rejected")
    assert len(rejected) == 1
    assert rejected[0]["symbol"] == "BETA" and rejected[0]["reason"] == "stale"
    assert rejected[0]["age_seconds"] == 75
    assert len(strategy.execution.alerts()) == 1
    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    strategy._watch(account(), NOW)
    assert len(events(strategy, "price_alert")) == 1
    assert not strategy.execution.alerts() and not strategy.journal.pending()


def test_close_reports_the_risk_reason_and_safe_location_while_review_still_runs(tmp_path):
    strategy, calls = review_harness(tmp_path)

    def failed_snapshot():
        raise RiskError("Synthetic account reconciliation failed")

    strategy._snapshot = failed_snapshot
    strategy.after_market_closes()
    assert len(calls) == 2
    assert events(strategy, "notification")[0] == {
        "kind": "report_error", "details": "Synthetic account reconciliation failed"}
    detail = events(strategy, "operation_error")[0]
    assert detail["phase"] == "closing_snapshot"
    assert detail["frames"][-1]["function"] == "failed_snapshot"


def test_missing_close_is_reported_as_pending_without_claiming_a_closing_nav(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy._snapshot = lambda: (_ for _ in ()).throw(MarketDataUnavailable("Closing minute unavailable"))
    strategy.after_market_closes()
    assert len(calls) == 2
    assert events(strategy, "notification")[0]["kind"] == "closing_valuation_pending"
    assert not events(strategy, "account")[-1].get("valuation", {}).get("session_close")


def test_errors_command_includes_market_data_and_close_diagnostics_logged_at_info():
    kinds = ["price_alert_unavailable", "price_observation_rejected", "local_market_data_pending",
             "closing_valuation_pending", "operation_error", "report_error"]
    rows = [{"MESSAGE": f"INFO | FUND {kind}: synthetic"} for kind in kinds]
    rows.append({"MESSAGE": "INFO | FUND local_fill: synthetic"})
    selected = error_records(rows)
    assert len(selected) == len(kinds)
    assert all(any(f"FUND {kind}:" in line for line in selected) for kind in kinds)


def test_all_invalid_bars_report_the_symbol_and_field_without_fabricating_a_quote():
    clock = Clock()
    raw = clock.fetch("DELTA", "1m", "5d")
    raw["Volume"] = float("nan")
    source = YahooRealtimeData(now=clock, fetch=lambda *a: raw)
    with pytest.raises(MarketDataUnavailable, match="DELTA.*volume"):
        source.get_quote(Asset("DELTA"))
    assert source.diagnostics()["failures"] == 1


def test_slow_download_uses_receipt_time_for_completed_bar_cutoff():
    clock = Clock()

    def slow_fetch(symbol, interval, period):
        clock.value += timedelta(seconds=65)
        return clock.fetch(symbol, interval, period)

    source = YahooRealtimeData(now=clock, fetch=slow_fetch)
    quote = source.get_quote(Asset("ALFA"))
    from scripts.autonomous_fund.risk import observed_quote
    assert observed_quote(quote, clock(), LIMITS).as_of == clock().replace(second=0, microsecond=0)


def test_per_symbol_diagnostic_identifies_missing_stream_and_stale_chart_and_is_bounded():
    from .test_autonomous_fund_freshness_recovery import inert_tape
    clock = Clock()

    def fetch(symbol, interval, period):
        raw = clock.fetch(symbol, interval, period)
        raw.attrs["yahoo_last_trade"] = {"price": 100, "as_of": clock() - timedelta(minutes=15)}
        return raw

    source = YahooRealtimeData(now=clock, fetch=fetch, tape=inert_tape())
    for i in range(61):
        source.get_quote(Asset(f"S{i}"))
    diagnostics = source.diagnostics()["quotes_by_symbol"]
    assert len(diagnostics) == 60 and "S0" not in diagnostics
    detail = diagnostics["S60"]
    assert detail["stream_as_of"] is None
    assert detail["chart_age_seconds"] == 900
    assert detail["price_kind"] == "completed_minute_close_with_spread_proxy"
    assert detail["age_seconds"] == 5


@pytest.mark.parametrize("capital", [None, "10000"])
def test_native_closing_hook_waits_for_valid_final_bar_then_background_valuation_recovers(tmp_path, monkeypatch, capital):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import account_digest
    from .test_autonomous_fund_closed_ledger import CLOSE

    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "universe": {"ALFA": "stock"},
              "cash_sweep": {"enabled": False}, "critic": {"enabled": False},
              "mandate": {"capital_usd": capital, "local_account_usd": "20000"}}
    journal = Journal(tmp_path, account_digest("local", "local-virtual-account", config))
    broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False, starting_cash="20000")
    strategy = AutonomousFund(broker=broker, name="ClosingRecovery", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local",
        "account_binding": "local-virtual-account", "runtime": ScriptedRuntime()})
    try:
        strategy._executor._initialize()
        strategy._snapshot()  # Initialize the real capital-sleeve baseline when configured.
        key, order = submit(broker, journal, clock())
        journal.update(key, order.identifier, False, clock())
        advance(clock, source)
        broker.do_polling()
        assert broker._pull_broker_order(order.identifier)["filled"] == 5
        strategy._snapshot()
        original = source.fetch

        def broken_close(symbol, interval, period):
            raw = original(symbol, interval, period)
            if interval == "1m":
                raw.loc[raw.index == CLOSE - timedelta(minutes=1), "Close"] = float("nan")
            else:
                # A missing minute alone now recovers from a real daily close.
                # This fixture exercises pending valuation with neither source.
                raw = raw[raw.index.date < CLOSE.date()]
            return raw

        clock.value = CLOSE + timedelta(seconds=1)
        source.fetch = broken_close
        source._cache.clear()  # This fixture's private source cache only.
        strategy.after_market_closes()
        assert any(row["kind"] == "closing_valuation_pending" for row in events(strategy, "notification"))
        assert not any(row.get("valuation", {}).get("session_close") == str(CLOSE) for row in events(strategy, "account"))
        assert events(strategy, "postmarket_review")
        source.fetch = original
        advance(clock, source, minutes=1)
        broker.do_polling()
        assert broker._valuation_error is None
        closing = [row for row in events(strategy, "account") if row.get("valuation", {}).get("session_close") == str(CLOSE)]
        assert len(closing) == 1 and closing[0]["as_of"] == str(CLOSE)
    finally:
        broker._close_connection()
