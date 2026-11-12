"""Minute-boundary refresh and independently completed closing-price recovery.

Synthetic clocks, frames and native local-book paths; no network or inference.
"""
from datetime import timedelta

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.risk import MarketDataUnavailable, number, observed_quote
from .test_autonomous_fund_closed_ledger import CLOSE
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_local import Clock, advance, setup_book, submit
from .test_autonomous_fund_quant import LIMITS


@pytest.mark.parametrize("symbol", ["ALFA", "BETA"])
def test_minute_boundary_does_not_reuse_a_quote_that_just_became_stale(monkeypatch, symbol):
    clock, timer, reads = Clock(), [100.0], []
    clock.value = clock().replace(second=59, microsecond=700000)
    monkeypatch.setattr("scripts.autonomous_fund.market_data.time.monotonic", lambda: timer[0])

    def fetch(*args):
        reads.append(args)
        return clock.fetch(*args)

    source = YahooRealtimeData(now=clock, fetch=fetch)
    first = observed_quote(source.get_quote(Asset(symbol)), clock(), LIMITS)
    clock.value += timedelta(seconds=.8)
    timer[0] += .8  # The normal 15-second cache TTL has not expired.
    current = observed_quote(source.get_quote(Asset(symbol)), clock(), LIMITS)
    assert current.as_of == first.as_of + timedelta(minutes=1)
    assert (clock() - current.as_of).total_seconds() == pytest.approx(.5)
    assert len(reads) == 2


def test_a_lagging_provider_is_retried_without_a_same_second_request_storm(monkeypatch):
    clock, timer, reads = Clock(), [100.0], []
    clock.value = clock().replace(second=59, microsecond=0)
    fixed = clock.fetch("ALFA", "1m", "5d")
    fixed = fixed[fixed.index + timedelta(minutes=1) <= clock()]
    monkeypatch.setattr("scripts.autonomous_fund.market_data.time.monotonic", lambda: timer[0])

    def fetch(*args):
        reads.append(args)
        return fixed.copy()

    source = YahooRealtimeData(now=clock, fetch=fetch)
    source.get_quote(Asset("ALFA"))
    clock.value += timedelta(seconds=2)
    timer[0] += 2
    for _ in range(5):
        with pytest.raises(MarketDataUnavailable):
            observed_quote(source.get_quote(Asset("ALFA")), clock(), LIMITS)
    assert len(reads) == 2
    timer[0] += 5
    clock.value += timedelta(seconds=5)
    with pytest.raises(MarketDataUnavailable):
        observed_quote(source.get_quote(Asset("ALFA")), clock(), LIMITS)
    assert len(reads) == 3


def daily_only(clock, *, daily_kind="valid"):
    def fetch(symbol, interval, period):
        frame = clock.fetch(symbol, interval, period)
        if interval == "1m":
            return frame[frame.index < CLOSE - timedelta(minutes=1)]
        if daily_kind == "previous":
            return frame[frame.index.date < CLOSE.date()]
        if daily_kind == "invalid":
            frame.loc[frame.index.date == CLOSE.date(), "Close"] = float("nan")
        return frame
    return fetch


def test_missing_final_minute_can_use_the_actual_completed_daily_close(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    clock.value = CLOSE + timedelta(seconds=1)
    clock.price = 103
    source.fetch = daily_only(clock)
    broker.do_polling()
    assert broker._valuation_error is None
    closing = [row["body"] for row in journal.all_events() if row["kind"] == "account"
               and row["body"].get("valuation", {}).get("session_close") == str(CLOSE)]
    assert len(closing) == 1
    mark = closing[0]["valuation"]["quotes"]["ALFA"]
    assert mark["price"] == "103.0" and mark["as_of"] == str(CLOSE)
    assert mark["price_kind"] == "completed_daily_close"
    assert mark["observed_at"] != mark["as_of"]
    assert number(closing[0]["equity"]) == number(103 * 5) + number(closing[0]["cash"])


@pytest.mark.parametrize("bad", ["previous", "invalid", "future_close", "wrong_boundary"])
def test_daily_recovery_never_borrows_a_wrong_session_or_uncompleted_price(bad):
    clock = Clock()
    clock.value = CLOSE + timedelta(seconds=1)
    source = YahooRealtimeData(now=clock, fetch=daily_only(clock, daily_kind=bad))
    target = CLOSE
    if bad == "future_close":
        target += timedelta(days=1)
    elif bad == "wrong_boundary":
        target -= timedelta(minutes=1)
    with pytest.raises(MarketDataUnavailable):
        source.get_closing_quote(Asset("BETA"), target)


def test_missing_daily_close_is_refetched_during_recovery_without_waiting_fifteen_minutes(monkeypatch):
    clock, timer = Clock(), [100.0]
    clock.value = CLOSE + timedelta(seconds=1)
    monkeypatch.setattr("scripts.autonomous_fund.market_data.time.monotonic", lambda: timer[0])
    source = YahooRealtimeData(now=clock, fetch=daily_only(clock, daily_kind="previous"))
    with pytest.raises(MarketDataUnavailable):
        source.get_closing_quote(Asset("GAMMA"), CLOSE)
    timer[0] += 31
    clock.value += timedelta(seconds=31)
    source.fetch = daily_only(clock)
    actual = source.get_closing_quote(Asset("GAMMA"), CLOSE)
    assert actual.raw_data["price_kind"] == "completed_daily_close" and actual.timestamp == CLOSE


@pytest.mark.parametrize("closed", ["2026-11-27T18:00:00+00:00", "2026-07-01T20:00:00+00:00"])
def test_daily_close_recovery_respects_early_closes_and_daylight_saving(closed):
    from scripts.autonomous_fund.risk import instant
    target, clock = instant(closed), Clock()
    clock.value = target + timedelta(seconds=1)

    def fetch(symbol, interval, period):
        if interval == "1m":
            raise TimeoutError("Synthetic missing intraday endpoint")
        return clock.fetch(symbol, interval, period)

    source = YahooRealtimeData(now=clock, fetch=fetch)
    actual = source.get_closing_quote(Asset("DELTA"), target)
    assert actual.timestamp == target and actual.raw_data["price_kind"] == "completed_daily_close"
    with pytest.raises(MarketDataUnavailable):
        source.execution_bars(Asset("DELTA"))  # A valuation close never supplies fabricated fill bars.


def test_stale_quote_diagnostic_retains_its_kind_and_telegram_describes_alert_only():
    from scripts.autonomous_fund.notifications import readable
    clock = Clock()
    raw = clock.fetch("ALFA", "1m", "5d")
    raw = raw[raw.index < clock() - timedelta(minutes=2)]
    source = YahooRealtimeData(now=clock, fetch=lambda *args: raw)
    with pytest.raises(MarketDataUnavailable) as caught:
        observed_quote(source.get_quote(Asset("ALFA")), clock(), LIMITS)
    assert caught.value.diagnostic["price_kind"] == "completed_minute_close_with_spread_proxy"
    message = readable("price_alert_unavailable", {"symbol": "ALFA", "error": str(caught.value)})
    assert "价位提醒等待有效报价" in message and "后续自动重试" in message
    assert "停止交易" not in message


@pytest.mark.parametrize("kind", ["subscription", "redemption"])
def test_recovered_daily_close_cannot_apply_a_queued_after_hours_capital_flow(tmp_path, monkeypatch, kind):
    from scripts.autonomous_fund import capital
    from .test_autonomous_fund_observation_timing import native_snapshot
    clock, source, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch, capital="10000", holding=True)
    try:
        queued = capital.request(journal, clock(), kind, "100")
        before = journal.reserve_cash()
        clock.value = CLOSE + timedelta(seconds=31)
        source.fetch = daily_only(clock)
        broker.do_polling()
        assert broker._valuation_error is None
        assert broker._last_valuation["valuation"]["quotes"]["ALFA"]["price_kind"] == "completed_daily_close"
        account = strategy._snapshot()
        assert strategy._capital_step(account, clock()) is False
        assert capital.pending(journal) == [queued]
        assert journal.reserve_cash() == before
        assert not any(row["kind"] == "capital_flow" for row in journal.all_events())
    finally:
        broker._close_connection()


def test_recovered_close_is_reused_without_retrying_missing_minutes_or_retiming_evidence():
    clock, reads = Clock(), []
    clock.value = CLOSE + timedelta(seconds=1)
    original = daily_only(clock)

    def fetch(*args):
        reads.append(args)
        return original(*args)

    source = YahooRealtimeData(now=clock, fetch=fetch)
    first = source.get_closing_quote(Asset("ALFA"), CLOSE)
    assert len(reads) == 2
    observed = first.raw_data["observed_at"]
    # Caller changes cannot alter a saved close or its retained original receipt.
    first.raw_data["observed_at"] = "changed by caller"
    clock.value += timedelta(hours=1)
    clock.price = 900
    actual = source.get_closing_quote(Asset("ALFA"), CLOSE)
    assert actual.price == 100 and actual.raw_data["observed_at"] == observed
    assert len(reads) == 2


def test_a_short_live_trade_age_does_not_poll_an_on_time_minute_bar_more_often(monkeypatch):
    clock, timer = Clock(), [100.0]
    monkeypatch.setattr("scripts.autonomous_fund.market_data.time.monotonic", lambda: timer[0])
    source = YahooRealtimeData(now=clock, fetch=clock.fetch, live_seconds=5)
    source.get_quote(Asset("BETA"))
    clock.value += timedelta(seconds=6)
    timer[0] += 6
    observed_quote(source.get_quote(Asset("BETA")), clock(), LIMITS)
    assert source.diagnostics()["requests"] == 1  # Fresh bar, same minute, normal 15-second TTL.


@pytest.mark.parametrize("kind", ["split", "dividend"])
def test_actions_first_seen_in_the_recovered_daily_response_are_applied_before_freezing_nav(tmp_path, kind):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.risk import instant
    clock = Clock()
    clock.value = instant("2026-04-16T15:30:05+00:00")  # Older lot: action history uses period=max.
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    journal = Journal(tmp_path, "synthetic-closing-actions")
    broker = LocalPaperBroker(source, journal, LIMITS, connect_stream=False)
    configure(broker)
    submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    clock.value = CLOSE + timedelta(seconds=1)
    clock.price = 50 if kind == "split" else 100
    clock.actions = [("2026-11-02", 2 if kind == "split" else 0, .5 if kind == "dividend" else 0)]
    reads = []

    def delayed_action(symbol, interval, period):
        raw = clock.fetch(symbol, interval, period)
        if interval == "1m":
            return raw[raw.index < CLOSE - timedelta(minutes=1)]
        reads.append((symbol, period))
        # The earlier period=max corporate-action response is lagging. The
        # independent closing-price request sees the newly published action.
        return raw[raw.index.date < CLOSE.date()] if period == "max" else raw

    source.fetch = delayed_action
    broker.do_polling()
    assert broker._valuation_error and "reconciled book" in broker._valuation_error
    assert not any(row["body"].get("valuation", {}).get("session_close") == str(CLOSE)
                   for row in journal.all_events() if row["kind"] == "account")
    clock.value += timedelta(seconds=31)
    broker.do_polling()
    assert broker._valuation_error is None
    body = broker._last_valuation
    assert number(body["positions"]["ALFA"]) == (10 if kind == "split" else 5)
    assert number(body["receivables"]) == (0 if kind == "split" else number("2.5"))
    assert number(body["equity"]) == number(body["cash"]) + number(500) + number(body["receivables"])
    assert len([row for row in journal.all_events() if row["kind"] == "local_corporate_action"]) == 1
    assert ("ALFA", "max") in reads and ("ALFA", "6mo") in reads
