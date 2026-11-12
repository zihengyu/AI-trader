"""Capital-order authority survives reservation, acceptance and fill races.

Real local broker/order lifecycle with synthetic data; Paper tests mock the
native send boundary. No paid inference, brokerage API or Telegram operations.
"""
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund import capital
from scripts.autonomous_fund.execution import ExecutionState
from scripts.autonomous_fund.risk import Intent, MarketDataUnavailable, RiskError, number
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_local import advance, setup_book, submit


def book(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    journal.apply_mandate(clock(), "10000", 10000, 10000, {}, False, {}, True)
    configure(broker)
    key, bought = submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    journal.update(key, bought.identifier, True, clock())
    return clock, source, journal, broker, ExecutionState(journal)


def reserve(journal, broker, execution, clock, *, side="sell", request=None, purpose="allocation"):
    intent = Intent("ALFA", side, 3, number(99 if side == "sell" else 101))
    guard = (lambda db: capital.guard_redemption_sale(db, request["id"])) if request else None
    key = journal.reserve(intent, clock(), broker.limits, guard=guard)
    execution.record_submission(key, f"redemption:{request['id']}" if request else "synthetic-plan", intent, clock())
    if purpose == "protective":
        execution.mark_protective(key, clock())
    order = Order("CapitalAuthority", Asset("ALFA"), intent.quantity, side, order_type="limit",
                  limit_price=float(intent.limit), custom_params={"local_intent": key})
    return key, order


def money(journal):
    with journal.connection() as db:
        return tuple(db.execute("SELECT cash,receivables FROM local_account").fetchone()), list(
            tuple(row) for row in db.execute("SELECT * FROM local_positions ORDER BY symbol")), db.execute(
                "SELECT count(*) FROM local_fills").fetchone()[0]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_local_acceptance_rechecks_capital_authority_after_reservation(tmp_path, side):
    clock, _, journal, broker, execution = book(tmp_path)
    request = capital.request(journal, clock(), "redemption", "100") if side == "sell" else None
    key, order = reserve(journal, broker, execution, clock, side=side, request=request)
    if side == "sell":
        capital.cancel(journal, clock(), request["id"])
    else:
        capital.request(journal, clock(), "redemption", "100")
    before = money(journal)
    with pytest.raises(RiskError):
        broker.submit_order(order)
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM local_orders WHERE intent_id=?", (key,)).fetchone()[0] == 0
        assert db.execute("SELECT status FROM intents WHERE id=?", (key,)).fetchone()[0] == "terminal"
    assert money(journal) == before


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("partial", [False, True])
def test_revoked_orders_cancel_before_unavailable_action_history(tmp_path, side, partial):
    clock, source, journal, broker, execution = book(tmp_path)
    request = capital.request(journal, clock(), "redemption", "100") if side == "sell" else None
    key, order = reserve(journal, broker, execution, clock, side=side, request=request)
    broker.submit_order(order)
    if partial:
        clock.volume = 100  # One share fills; the remaining two must be cancelable.
        advance(clock, source)
        broker.do_polling()
    prefix = broker._pull_broker_order(order.identifier)["filled"]
    assert prefix == (1 if partial else 0)
    if side == "sell":
        capital.cancel(journal, clock(), request["id"])
    else:
        capital.request(journal, clock(), "redemption", "100")
    before = money(journal)
    source.corporate_actions = lambda *a, **k: (_ for _ in ()).throw(MarketDataUnavailable("Synthetic action outage"))
    broker.do_polling()
    stored = broker._pull_broker_order(order.identifier)
    assert stored["status"] == "canceled" and stored["filled"] == prefix
    assert order.is_canceled() and money(journal) == before


def test_cancel_during_minute_download_cannot_fill_a_redemption_sale(tmp_path):
    clock, source, journal, broker, execution = book(tmp_path)
    request = capital.request(journal, clock(), "redemption", "100")
    _, order = reserve(journal, broker, execution, clock, request=request)
    broker.submit_order(order)
    advance(clock, source)
    original, fired = source.fetch, []

    def fetch(symbol, interval, period):
        if interval == "1m" and not fired:
            fired.append(True)
            capital.cancel(journal, clock(), request["id"])
        return original(symbol, interval, period)

    source.fetch = fetch
    before = money(journal)
    broker.do_polling()
    assert fired and broker._pull_broker_order(order.identifier)["status"] == "canceled"
    assert money(journal) == before


def test_a_redemption_queued_after_buy_valuation_blocks_the_fill_transaction(tmp_path, monkeypatch):
    clock, source, journal, broker, execution = book(tmp_path)
    _, order = reserve(journal, broker, execution, clock, side="buy")
    broker.submit_order(order)
    advance(clock, source)
    original = broker._commit_fill

    def commit(*args, **kwargs):
        capital.request(journal, clock(), "redemption", "100")
        return original(*args, **kwargs)

    monkeypatch.setattr(broker, "_commit_fill", commit)
    before = money(journal)
    broker.do_polling()
    assert broker._pull_broker_order(order.identifier)["status"] == "canceled"
    assert money(journal) == before


@pytest.mark.parametrize("purpose", ["allocation", "protective", "redemption"])
def test_valid_redemption_and_other_risk_reducing_sales_continue(tmp_path, purpose):
    clock, source, journal, broker, execution = book(tmp_path)
    request = capital.request(journal, clock(), "redemption", "100")
    _, order = reserve(journal, broker, execution, clock,
                       request=request if purpose == "redemption" else None, purpose=purpose)
    broker.submit_order(order)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_broker_order(order.identifier)["filled"] == 3


def test_pending_subscription_does_not_block_ordinary_purchases(tmp_path):
    clock, source, journal, broker, execution = book(tmp_path)
    capital.request(journal, clock(), "subscription", "100")
    _, order = reserve(journal, broker, execution, clock, side="buy")
    broker.submit_order(order)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_broker_order(order.identifier)["filled"] == 3


def test_a_new_request_cannot_restore_an_old_redemption_orders_authority(tmp_path):
    clock, source, journal, broker, execution = book(tmp_path)
    original = capital.request(journal, clock(), "redemption", "100")
    _, order = reserve(journal, broker, execution, clock, request=original)
    broker.submit_order(order)
    capital.cancel(journal, clock(), original["id"])
    newer = capital.request(journal, clock(), "redemption", "200")
    before = money(journal)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_broker_order(order.identifier)["status"] == "canceled"
    assert capital.pending(journal) == [newer] and money(journal) == before


def test_restart_loads_revocation_from_the_journal_before_matching(tmp_path):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    clock, source, journal, broker, execution = book(tmp_path)
    request = capital.request(journal, clock(), "redemption", "100")
    _, order = reserve(journal, broker, execution, clock, request=request)
    broker.submit_order(order)
    capital.cancel(journal, clock(), request["id"])
    before = money(journal)
    restored = LocalPaperBroker(source, journal, broker.limits, connect_stream=False)
    configure(restored)
    advance(clock, source)
    restored.do_polling()
    assert restored._pull_broker_order(order.identifier)["status"] == "canceled"
    assert money(journal) == before


def test_native_redemption_path_catches_cancel_after_the_strategy_preflight(tmp_path, monkeypatch):
    from .test_autonomous_fund_observation_timing import native_snapshot
    clock, _, _, broker, strategy = native_snapshot(tmp_path, monkeypatch, capital="10000", holding=True)
    journal = strategy.journal
    try:
        request = capital.request(journal, clock(), "redemption", "8500")
        original = broker._accept_order

        def accept(order):
            capital.cancel(journal, clock(), request["id"])
            return original(order)

        monkeypatch.setattr(broker, "_accept_order", accept)
        before = money(journal)
        strategy._capital_step(strategy._snapshot(), clock())
        assert not capital.pending(journal) and not journal.pending()
        with journal.connection() as db:
            assert db.execute("SELECT count(*) FROM local_orders WHERE json_extract(body,'$.side')='sell'").fetchone()[0] == 0
        assert money(journal) == before
    finally:
        broker._close_connection()


def submission_path(tmp_path, monkeypatch, mode):
    from lumibot.brokers import Alpaca
    from lumibot.strategies import Strategy
    from .test_autonomous_fund_capital_flows import running
    strategy = running(tmp_path)
    del strategy._submit  # Exercise the real application's submission method.
    strategy.run_dir = tmp_path
    strategy.mode = mode
    if mode == "paper":
        broker = object.__new__(Alpaca)
        broker.is_paper = True
        broker.api = SimpleNamespace(_base_url="https://paper-api.alpaca.markets")
        broker.is_market_open = lambda: True
        strategy.broker = broker
    strategy.create_order = lambda asset, quantity, side, **kwargs: Order(
        "CapitalPreflight", asset, quantity, side, **kwargs)
    sends = []

    def send(self, order, **kwargs):
        sends.append(order)
        order._raw = SimpleNamespace(id="synthetic-ack")
        order.set_identifier("synthetic-ack")
        return order

    monkeypatch.setattr(Strategy, "submit_order", send)
    return strategy, sends


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_strategy_rechecks_capital_after_quote_io_before_native_send(tmp_path, monkeypatch, mode, side):
    strategy, sends = submission_path(tmp_path, monkeypatch, mode)
    request = capital.request(strategy.journal, strategy.get_datetime(), "redemption", "100") if side == "sell" else None
    original = strategy._price

    def price(*args, **kwargs):
        if request:
            capital.cancel(strategy.journal, strategy.get_datetime(), request["id"])
        else:
            capital.request(strategy.journal, strategy.get_datetime(), "redemption", "100")
        return original(*args, **kwargs)

    strategy._price = price
    intent = Intent("ALFA", side, 1, number(99 if side == "sell" else 101))
    guard = (lambda db: capital.guard_redemption_sale(db, request["id"])) if request else None
    with pytest.raises(capital.ValuationChanged):
        strategy._submit(intent, strategy.get_datetime(), f"redemption:{request['id']}" if request else "synthetic-plan",
                         kind="redemption" if request else "allocation", guard=guard)
    assert not sends and not strategy.journal.pending()


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("boundary", ["stop", "market_close", "expired", "monitor_missing", "monitor_stale", "healthy"])
def test_strategy_rechecks_execution_authority_after_quote_io(tmp_path, monkeypatch, mode, boundary):
    strategy, sends = submission_path(tmp_path, monkeypatch, mode)
    clock, original = [strategy.get_datetime()], strategy._price
    strategy.get_datetime = lambda: clock[0]
    until = clock[0] + timedelta(seconds=120)
    strategy.require_monitor = boundary.startswith("monitor") or boundary == "healthy"
    monitor = tmp_path / "monitor.json"
    if strategy.require_monitor:
        monitor.write_text(json.dumps({"at": clock[0].isoformat(), "health": "healthy", "telegram_configured": True}))

    def price(*args, **kwargs):
        if boundary == "stop":
            strategy.halt_path.touch()
        elif boundary == "market_close":
            clock[0] = clock[0].replace(hour=21, minute=0, second=0)  # NYSE closes at 21:00 UTC on this fixture date.
        elif boundary == "expired":
            clock[0] = until
        elif boundary == "monitor_missing":
            monitor.unlink()
        else:
            clock[0] += timedelta(seconds=46 if boundary == "monitor_stale" else 2)
        return original(*args, **kwargs)

    strategy._price = price
    intent = Intent("ALFA", "buy", 1, number(101))
    if boundary == "healthy":
        strategy._submit(intent, clock[0], "synthetic-plan", until=until)
        assert len(sends) == len(strategy.journal.pending()) == 1
        assert strategy.journal.pending()[0]["broker_id"] == "synthetic-ack"
    else:
        with pytest.raises(RiskError):
            strategy._submit(intent, clock[0], "synthetic-plan", until=until)
        assert not sends and not strategy.journal.pending()
        with strategy.journal.connection() as db:
            row = db.execute("SELECT status,broker_id FROM intents").fetchone()
            assert row["status"] == "terminal" and row["broker_id"] is None
            assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 1  # Preserve daily attempt accounting.
            assert db.execute("SELECT count(*) FROM events WHERE kind='order_submission_aborted'").fetchone()[0] == 1
