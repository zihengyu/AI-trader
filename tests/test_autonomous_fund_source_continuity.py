"""Independent quote recovery and complete source-time checks on native fills.

All prices, symbols, clocks and provider responses are synthetic.
"""
from datetime import timedelta

import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.risk import Intent, MarketDataUnavailable, number, observed_quote
from .test_autonomous_fund_freshness_recovery import inert_tape
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_local import Clock, advance, setup_book, submit
from .test_autonomous_fund_quant import LIMITS
from .test_autonomous_fund_realtime import frame as stream_frame


def broken_minutes(clock, kind="nan", *, stamp=None, price=105):
    raw = clock.fetch("ALFA", "1m", "5d")
    if kind == "empty":
        raw = raw.iloc[:0]
    elif kind == "forming":
        raw = raw.tail(1)
    else:
        raw["Close"] = float("nan")
    raw.attrs["yahoo_last_trade"] = {"price": price, "as_of": stamp}
    return raw


@pytest.mark.parametrize("kind", ["empty", "forming", "nan"])
@pytest.mark.parametrize("streaming", [False, True])
def test_fresh_chart_trade_survives_unavailable_bars_but_cannot_supply_a_fill_bar(kind, streaming):
    clock, reads = Clock(), []
    stamp = clock() - timedelta(seconds=1)

    def fetch(*args):
        reads.append(args)
        return broken_minutes(clock, kind, stamp=stamp)

    source = YahooRealtimeData(now=clock, fetch=fetch, tape=inert_tape() if streaming else None)
    actual = source.get_quote(Asset("ALFA"))
    mark = observed_quote(actual, clock(), LIMITS)
    assert mark.price == 105 and mark.as_of == stamp
    assert actual.volume is None
    assert actual.raw_data["price_kind"] == "chart_last_trade_with_spread_proxy"
    assert source.diagnostics()["quotes_by_symbol"]["ALFA"]["chart_as_of"] == stamp.isoformat()
    with pytest.raises(MarketDataUnavailable):
        source.execution_bars(Asset("ALFA"))
    assert len(reads) == 1 and source.diagnostics()["source_failures"]


@pytest.mark.parametrize("bad", ["missing", "naive", "future", "stale", "zero", "nonfinite", "closed"])
def test_independent_chart_recovery_still_requires_real_regular_session_evidence(bad):
    clock = Clock()
    if bad == "closed":
        clock.value = clock().replace(hour=22)
    stamp, price = clock() - timedelta(seconds=1), 105
    if bad == "missing":
        stamp = None
    elif bad == "naive":
        stamp = stamp.replace(tzinfo=None)
    elif bad == "future":
        stamp = clock() + timedelta(seconds=1)
    elif bad == "stale":
        stamp = clock() - timedelta(seconds=61)
    elif bad == "zero":
        price = 0
    elif bad == "nonfinite":
        price = float("inf")
    source = YahooRealtimeData(now=clock, fetch=lambda *a: broken_minutes(clock, stamp=stamp, price=price))
    with pytest.raises(MarketDataUnavailable):
        source.get_quote(Asset("ALFA"))


def test_retained_chart_evidence_ages_without_being_retimed_during_backoff():
    clock, reads = Clock(), []
    stamp = clock() - timedelta(seconds=1)

    def fetch(*args):
        reads.append(args)
        return broken_minutes(clock, stamp=stamp)

    source = YahooRealtimeData(now=clock, fetch=fetch)
    assert observed_quote(source.get_quote(Asset("ALFA")), clock(), LIMITS).as_of == stamp
    clock.value += timedelta(seconds=10)
    assert observed_quote(source.get_quote(Asset("ALFA")), clock(), LIMITS).as_of == stamp
    clock.value += timedelta(seconds=51)
    with pytest.raises(MarketDataUnavailable):
        source.get_quote(Asset("ALFA"))
    assert len(reads) == 1


def test_stream_priority_is_preserved_when_chart_evidence_and_bars_disagree():
    clock, tape = Clock(), inert_tape()
    tape.watch(["ALFA"])
    tape.handle(stream_frame("ALFA", 106, clock() - timedelta(seconds=1)))
    source = YahooRealtimeData(now=clock, tape=tape,
        fetch=lambda *a: broken_minutes(clock, stamp=clock() - timedelta(seconds=2)))
    actual = source.get_quote(Asset("ALFA"))
    assert actual.price == 106 and actual.raw_data["source"] == "YAHOO_STREAM_UNOFFICIAL"


def buy(journal, broker, clock, symbol, quantity=5):
    key = journal.reserve(Intent(symbol, "buy", quantity, number(101)), clock(), broker.limits)
    order = Order("SourceChecks", Asset(symbol), quantity, "buy", order_type="limit", limit_price=101,
                  custom_params={"local_intent": key})
    broker.submit_order(order)
    return key, order


@pytest.mark.parametrize("bad", ["missing_time", "stale_time", "future_time", "zero_ask", "nonfinite_ask", "crossed"])
def test_second_holding_quote_cannot_borrow_receipt_time_or_invalid_ask(tmp_path, bad):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    key, first = submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    journal.update(key, first.identifier, True, clock())
    _, pending = submit(broker, journal, clock(), quantity=1)
    advance(clock, source)
    original, reads = source.get_quote, []

    def quote(asset, **kwargs):
        actual = original(asset, **kwargs)
        reads.append(asset.symbol)
        if len(reads) == 2:  # The balance's mark passed; this is the risk-capacity mark.
            actual.timestamp = clock()  # Must not substitute for the retained source time.
            if bad.endswith("time"):
                actual.raw_data["original_response"].timestamp = (None if bad == "missing_time" else
                    clock() + timedelta(seconds=1) if bad == "future_time" else clock() - timedelta(seconds=61))
            else:
                actual.ask = {"zero_ask": 0, "nonfinite_ask": float("nan"), "crossed": 99}[bad]
        return actual

    source.get_quote = quote
    broker.do_polling()
    assert broker._pull_broker_order(pending.identifier)["filled"] == 0
    assert broker._market_data_error and broker._last_poll_error is None
    source.get_quote = original
    broker.do_polling()
    assert broker._pull_broker_order(pending.identifier)["filled"] == 1


@pytest.mark.parametrize("delay_at", ["quotes", "ledger"])
def test_earlier_holding_mark_is_rechecked_after_later_work(tmp_path, monkeypatch, delay_at):
    from scripts.autonomous_fund import ledger_audit
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker, universe=("ALFA", "BETA"))
    for symbol in ("ALFA", "BETA"):
        key, first = buy(journal, broker, clock, symbol)
        advance(clock, source)
        broker.do_polling()
        assert broker._pull_broker_order(first.identifier)["filled"] == 5
        journal.update(key, first.identifier, True, clock())
    _, pending = buy(journal, broker, clock, "ALFA", quantity=1)
    advance(clock, source)
    original, reads = source.get_quote, []

    def quote(asset, **kwargs):
        actual = original(asset, **kwargs)
        reads.append(asset.symbol)
        if len(reads) == 3:
            actual.timestamp = actual.raw_data["original_response"].timestamp = clock() - timedelta(seconds=59)
        elif len(reads) == 4 and delay_at == "quotes":
            clock.value += timedelta(seconds=2)
        return actual

    original_check = ledger_audit.require_consistent
    if delay_at == "ledger":
        def delayed_check(db):
            clock.value += timedelta(seconds=2)
            return original_check(db)
        monkeypatch.setattr(ledger_audit, "require_consistent", delayed_check)
    source.get_quote = quote
    broker.do_polling()
    assert broker._pull_broker_order(pending.identifier)["filled"] == 0
    assert broker._pull_broker_order(pending.identifier)["last_bar"] is None
    assert broker._market_data_error and broker._last_poll_error is None
    source.get_quote = original
    monkeypatch.setattr(ledger_audit, "require_consistent", original_check)
    broker.do_polling()
    assert broker._pull_broker_order(pending.identifier)["filled"] == 1


@pytest.mark.parametrize("update", ["older", "future", "invalid_price"])
def test_later_bad_chart_response_cannot_replace_retained_fresh_source_evidence(update):
    clock = Clock()
    stamp = clock() - timedelta(seconds=1)
    source = YahooRealtimeData(now=clock, fetch=lambda *a: broken_minutes(clock, stamp=stamp))
    assert source.get_quote(Asset("ALFA")).price == 105
    source._failures.clear()  # Only this fixture's transport state; no shared cache.
    second_stamp = clock() - timedelta(seconds=5) if update == "older" else clock() + timedelta(seconds=1)
    second_price = float("nan") if update == "invalid_price" else 110
    source.fetch = lambda *a: broken_minutes(clock, stamp=second_stamp, price=second_price)
    actual = source.get_quote(Asset("ALFA"))
    assert actual.price == 105 and observed_quote(actual, clock(), LIMITS).as_of == stamp


def test_independent_chart_evidence_never_leaks_between_symbols():
    clock = Clock()

    def fetch(symbol, *args):
        if symbol == "BETA":
            raise TimeoutError("synthetic endpoint unavailable")
        return broken_minutes(clock, stamp=clock() - timedelta(seconds=1))

    source = YahooRealtimeData(now=clock, fetch=fetch)
    assert source.get_quote(Asset("ALFA")).price == 105
    with pytest.raises(MarketDataUnavailable):
        source.get_quote(Asset("BETA"))


def test_real_local_matcher_still_waits_for_a_bar_when_chart_trade_is_available(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    _, pending = submit(broker, journal, clock())
    advance(clock, source)
    original = source.fetch

    def fetch(symbol, interval, period):
        if interval == "1m":
            return broken_minutes(clock, stamp=clock() - timedelta(seconds=1), price=100)
        return original(symbol, interval, period)

    source.fetch = fetch
    broker.do_polling()
    assert source.get_quote(Asset("ALFA")).price == 100
    assert broker._pull_broker_order(pending.identifier)["filled"] == 0
    assert broker._market_data_error and broker._last_poll_error is None
    source.fetch = original
    source._failures.clear()  # Permit this fixture's recovered source to answer now.
    broker.do_polling()
    assert broker._pull_broker_order(pending.identifier)["filled"] == 5
