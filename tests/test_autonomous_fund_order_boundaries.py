"""Local order boundaries use durable state despite source failures and races.

Native broker/order callbacks, synthetic prices, no brokerage API calls.
"""
from datetime import timedelta
import json
import threading

import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund.risk import Intent, MarketDataUnavailable, RiskError, instant, number
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_local import advance, setup_book, submit


@pytest.mark.parametrize("reason", ["stop", "expired"])
@pytest.mark.parametrize("partial", [False, True])
def test_order_cancellation_does_not_wait_for_corporate_action_history(tmp_path, reason, partial):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    key, initial = submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    journal.update(key, initial.identifier, True, clock())
    _, pending = submit(broker, journal, clock(), quantity=4)
    if partial:
        clock.volume = 200
        advance(clock, source)
        broker.do_polling()
    before = broker._pull_broker_order(pending.identifier)
    assert before["filled"] == (2 if partial else 0)
    with journal.connection() as db:
        cash = db.execute("SELECT cash FROM local_account").fetchone()[0]
        fills = db.execute("SELECT count(*) FROM local_fills").fetchone()[0]
    source.corporate_actions = lambda *a, **k: (_ for _ in ()).throw(MarketDataUnavailable("Synthetic action history outage"))
    if reason == "stop":
        (tmp_path / "STOP").touch()
    else:
        clock.value = instant(before["expires_at"])
    broker.do_polling()
    after = broker._pull_broker_order(pending.identifier)
    assert after["status"] == "canceled" and after["filled"] == before["filled"]
    assert pending.is_canceled()
    with journal.connection() as db:
        assert db.execute("SELECT cash FROM local_account").fetchone()[0] == cash
        assert db.execute("SELECT count(*) FROM local_fills").fetchone()[0] == fills


def test_unexpired_order_is_not_canceled_merely_because_history_is_unavailable(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    _, pending = submit(broker, journal, clock())
    source.corporate_actions = lambda *a, **k: (_ for _ in ()).throw(MarketDataUnavailable("Synthetic history outage"))
    broker.do_polling()
    assert broker._pull_broker_order(pending.identifier)["status"] == "new"


@pytest.mark.parametrize("boundary", ["market_close", "decision_expiry", "expired_before", "stop", "disabled", "monitor_stale", "monitor_missing"])
def test_submission_rechecks_authority_after_transaction_wait_and_releases_known_non_submission(tmp_path, monkeypatch, boundary):
    from scripts.autonomous_fund import ledger_audit
    clock, source, journal, broker = setup_book(tmp_path)
    expiry = clock() + timedelta(seconds=-1 if boundary == "expired_before" else 120)
    if boundary.startswith("monitor"):
        broker.require_monitor = True
        if boundary == "monitor_stale":
            (tmp_path / "monitor.json").write_text(json.dumps({"at": clock().isoformat(), "health": "healthy", "telegram_configured": True}))
    key = journal.reserve(Intent("ALFA", "buy", 1, number(101)), clock(), broker.limits)
    order = Order("SubmissionBoundary", Asset("ALFA"), 1, "buy", order_type="limit", limit_price=101,
                  custom_params={"local_intent": key, "decision_expires_at": expiry.isoformat()})
    original = ledger_audit.require_consistent

    def check(db):
        result = original(db)
        if boundary == "market_close":
            clock.value = instant(source.session.status()["close"])
        elif boundary == "decision_expiry":
            clock.value = expiry
        elif boundary == "stop":
            (tmp_path / "STOP").touch()
        elif boundary == "disabled":
            broker.matching_enabled = False
        elif boundary == "monitor_stale":
            clock.value += timedelta(seconds=46)
        return result

    monkeypatch.setattr(ledger_audit, "require_consistent", check)
    with pytest.raises(RiskError):
        broker.submit_order(order)
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM local_orders").fetchone()[0] == 0
        row = db.execute("SELECT status,broker_id FROM intents WHERE id=?", (key,)).fetchone()
        assert row["status"] == "terminal" and row["broker_id"] is None
        assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 1  # Keep the attempt and daily budget history.
        assert db.execute("SELECT count(*) FROM events WHERE kind='local_order_rejected'").fetchone()[0] == 1
    assert not journal.pending()


def test_accepted_submission_records_the_actual_acceptance_time_after_checks(tmp_path, monkeypatch):
    from scripts.autonomous_fund import ledger_audit
    clock, source, journal, broker = setup_book(tmp_path)
    start = clock()
    original = ledger_audit.require_consistent

    def check(db):
        result = original(db)
        clock.value += timedelta(seconds=65)
        return result

    monkeypatch.setattr(ledger_audit, "require_consistent", check)
    _, accepted = submit(broker, journal, clock(), quantity=1)
    row = broker._pull_broker_order(accepted.identifier)
    assert instant(row["created_at"]) == start + timedelta(seconds=65)
    assert row["status"] == "new"
    monkeypatch.setattr(ledger_audit, "require_consistent", original)
    clock.value = start + timedelta(minutes=2)
    source._cache.clear()  # Only this fixture's source data.
    broker.do_polling()
    assert broker._pull_broker_order(accepted.identifier)["filled"] == 0  # This bar began before actual acceptance.
    advance(clock, source, minutes=1)
    broker.do_polling()
    assert broker._pull_broker_order(accepted.identifier)["filled"] == 1


@pytest.mark.parametrize("volume,filled,status", [(200, 2, "canceled"), (10000, 5, "fill")])
def test_cancel_waits_for_committed_fill_callback_to_keep_native_and_durable_state_in_order(tmp_path, volume, filled, status):
    clock, _, journal, broker = setup_book(tmp_path)
    clock.volume = volume
    _, order = submit(broker, journal, clock())
    advance(clock, broker.data_source)
    committed, release, cancel_started, cancel_done = (threading.Event() for _ in range(4))
    errors = []
    original = broker._process_trade_event

    def callback(stored, event, **kwargs):
        if event in {broker.FILLED_ORDER, broker.PARTIALLY_FILLED_ORDER}:
            committed.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic callback gate timed out")
        return original(stored, event, **kwargs)

    def cancel():
        cancel_started.set()
        try:
            broker.cancel_order(order)
        except Exception as exc:
            errors.append(exc)
        finally:
            cancel_done.set()

    broker._process_trade_event = callback
    polling = threading.Thread(target=broker.do_polling, daemon=True)
    canceling = threading.Thread(target=cancel, daemon=True)
    polling.start()
    try:
        assert committed.wait(5)
        assert broker._pull_broker_order(order.identifier)["filled"] == filled
        canceling.start()
        assert cancel_started.wait(5)
        assert not cancel_done.wait(.3), "Cancellation overtook an already committed fill callback"
    finally:
        release.set()
        polling.join(5)
        if canceling.ident is not None:
            canceling.join(5)
    assert not polling.is_alive() and not canceling.is_alive() and not errors
    assert broker._pull_broker_order(order.identifier)["status"] == status
    assert str(order.status) == status
    with journal.connection() as db:
        assert db.execute("SELECT sum(quantity) FROM local_fills").fetchone()[0] == filled


def test_stop_cancellation_is_reentrant_inside_the_polling_lock(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    clock.volume = 200
    _, order = submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    (tmp_path / "STOP").touch()
    worker = threading.Thread(target=broker.do_polling, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert order.is_canceled() and broker._pull_broker_order(order.identifier)["filled"] == 2


def test_fill_publication_cannot_overtake_new_order_publication(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key = journal.reserve(Intent("ALFA", "buy", 5, number(101)), clock(), broker.limits)
    order = Order("PublicationOrder", Asset("ALFA"), 5, "buy", order_type="limit", limit_price=101,
                  custom_params={"local_intent": key})
    accepted, release, poll_started, poll_done = (threading.Event() for _ in range(4))
    original, errors = broker._process_trade_event, []

    def callback(stored, event, **kwargs):
        if event == broker.NEW_ORDER:
            accepted.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic acceptance gate timed out")
        return original(stored, event, **kwargs)

    def accept():
        try:
            broker.submit_order(order)
        except Exception as exc:
            errors.append(exc)

    def poll():
        poll_started.set()
        try:
            broker.do_polling()
        finally:
            poll_done.set()

    broker._process_trade_event = callback
    submission = threading.Thread(target=accept, daemon=True)
    polling = threading.Thread(target=poll, daemon=True)
    submission.start()
    try:
        assert accepted.wait(5)
        advance(clock, source)
        polling.start()
        assert poll_started.wait(5)
        assert not poll_done.wait(.3), "A fill overtook NEW_ORDER publication"
    finally:
        release.set()
        submission.join(5)
        if polling.ident is not None:
            polling.join(5)
    assert not submission.is_alive() and not polling.is_alive() and not errors
    assert broker._pull_broker_order(order.identifier)["status"] == str(order.status) == "fill"
    with journal.connection() as db:
        assert db.execute("SELECT sum(quantity) FROM local_fills").fetchone()[0] == 5


def test_cancellation_completes_while_another_threads_holding_quote_is_blocked(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    key, initial = submit(broker, journal, clock())
    advance(clock, source)
    broker.do_polling()
    journal.update(key, initial.identifier, True, clock())
    _, pending = submit(broker, journal, clock(), quantity=1)
    advance(clock, source)
    reading, release, cancel_done = (threading.Event() for _ in range(3))
    original, errors = source.get_quote, []

    def slow_quote(asset, **kwargs):
        if not reading.is_set():
            reading.set()
            if not release.wait(5):
                raise TimeoutError("Synthetic quote gate timed out")
        return original(asset, **kwargs)

    def cancel():
        try:
            broker.cancel_order(pending)
        except Exception as exc:
            errors.append(exc)
        finally:
            cancel_done.set()

    source.get_quote = slow_quote
    polling = threading.Thread(target=broker.do_polling, daemon=True)
    canceling = threading.Thread(target=cancel, daemon=True)
    polling.start()
    try:
        assert reading.wait(5)
        canceling.start()
        assert cancel_done.wait(2), "Cancellation waited for unrelated source I/O"
        assert pending.is_canceled()
    finally:
        release.set()
        polling.join(5)
        if canceling.ident is not None:
            canceling.join(5)
    assert not polling.is_alive() and not canceling.is_alive() and not errors
    assert broker._pull_broker_order(pending.identifier)["status"] == "canceled"
    assert broker._pull_broker_order(pending.identifier)["filled"] == 0
