"""Production polling uses response/claim time for delivery leases and backoff."""
from datetime import datetime, timedelta

import pytest

from scripts.autonomous_fund import monitor as monitor_module
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.risk import RiskError, instant
from .test_autonomous_fund_monitor import FakeTelegram, NOW, setup


def live_clock(monkeypatch):
    wall = [NOW]

    class ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0].astimezone(tz) if tz is not None else wall[0].replace(tzinfo=None)

    monkeypatch.setattr(monitor_module, "datetime", ClockedDatetime)
    return wall


def test_rate_limit_delay_starts_after_the_response_not_before_sending(tmp_path, monkeypatch):
    journal, wall = setup(tmp_path), live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})

    class Slow(FakeTelegram):
        def send(self, text):
            wall[0] += timedelta(seconds=10)
            return super().send(text)

    transport = Slow([{"ok": False, "error": "telegram_http_429", "retry_after": 30}])
    monitor = Monitor(tmp_path, transport)
    monitor.poll()
    with journal.connection() as db:
        next_at = instant(db.execute("SELECT next_at FROM fund_deliveries WHERE state='retry'").fetchone()[0])
        gate = instant(db.execute("SELECT value FROM fund_monitor_meta WHERE key='next_delivery_at'").fetchone()[0])
    assert next_at == gate == NOW + timedelta(seconds=40)
    wall[0] = NOW + timedelta(seconds=39)
    monitor.poll()
    assert len(transport.messages) == 1
    wall[0] = NOW + timedelta(seconds=40)
    monitor.poll()
    assert len(transport.messages) == 2


def test_slow_local_message_build_uses_failure_time_for_its_own_retry(tmp_path, monkeypatch):
    journal, wall = setup(tmp_path), live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)

    def unavailable(*args):
        wall[0] += timedelta(seconds=10)
        raise OSError("synthetic private provider payload")

    monitor._outgoing = unavailable
    monitor.poll()
    with journal.connection() as db:
        row = db.execute("SELECT state,next_at,last_error FROM fund_deliveries").fetchone()
        assert row["state"] == "retry" and instant(row["next_at"]) == NOW + timedelta(seconds=40)
        assert row["last_error"] == "message_unavailable_OSError"
        assert db.execute("SELECT value FROM fund_monitor_meta WHERE key='next_delivery_at'").fetchone() is None
    assert not transport.messages


def test_later_message_claim_gets_a_full_lease_after_an_earlier_slow_send(tmp_path, monkeypatch):
    journal, wall, leases = setup(tmp_path), live_clock(monkeypatch), []
    for index in range(2):
        # Distinct notices exercise both claims without the intentional duplicate suppression.
        journal.event(NOW, "notification", {"kind": "blocked", "details": f"Synthetic notice {index}"})

    class Slow(FakeTelegram):
        def send(self, text):
            wall[0] += timedelta(seconds=10)
            return super().send(text)

    monitor = Monitor(tmp_path, Slow())
    original = monitor._outgoing

    def outgoing(body, sequence, text, now):
        with journal.connection() as db:
            lease = instant(db.execute("SELECT next_at FROM fund_deliveries WHERE sequence=?", (sequence,)).fetchone()[0])
        leases.append((wall[0], lease))
        return original(body, sequence, text, now)

    monitor._outgoing = outgoing
    monitor.poll()
    assert len(leases) == 2
    assert all(lease == claimed + timedelta(seconds=60) for claimed, lease in leases)


def test_report_build_that_loses_its_claim_does_not_send_or_overwrite_newer_state(tmp_path, monkeypatch):
    journal = setup(tmp_path)
    live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    original = monitor._outgoing

    def handed_off(body, sequence, text, now):
        with journal.connection() as db:
            db.execute("UPDATE fund_deliveries SET state='sent',next_at=?,message_id='newer-ack' WHERE sequence=?",
                       ((NOW + timedelta(seconds=120)).isoformat(), sequence))
        return original(body, sequence, text, now)

    monitor._outgoing = handed_off
    monitor.poll()
    assert not transport.messages
    with journal.connection() as db:
        assert tuple(db.execute("SELECT state,message_id FROM fund_deliveries").fetchone()) == ("sent", "newer-ack")


def test_late_http_failure_cannot_overwrite_newer_ack_or_shorten_shared_backoff(tmp_path, monkeypatch):
    journal, wall = setup(tmp_path), live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})

    class HandedOff(FakeTelegram):
        def send(self, text):
            self.messages.append(text)
            wall[0] += timedelta(seconds=70)
            with journal.connection() as db:
                db.execute("UPDATE fund_deliveries SET state='sent',next_at=?,message_id='newer-ack'",
                           ((NOW + timedelta(seconds=150)).isoformat(),))
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('next_delivery_at',?)",
                           ((NOW + timedelta(seconds=300)).isoformat(),))
            return {"ok": False, "error": "telegram_http_429", "retry_after": 30}

    monitor = Monitor(tmp_path, HandedOff())
    monitor.poll()
    with journal.connection() as db:
        assert tuple(db.execute("SELECT state,message_id FROM fund_deliveries").fetchone()) == ("sent", "newer-ack")
        assert instant(db.execute("SELECT value FROM fund_monitor_meta WHERE key='next_delivery_at'").fetchone()[0]) == NOW + timedelta(seconds=300)


def test_failed_build_that_loses_its_lease_does_not_reschedule_another_sender(tmp_path, monkeypatch):
    journal = setup(tmp_path)
    live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})
    monitor = Monitor(tmp_path, FakeTelegram())

    def failed(*args):
        with journal.connection() as db:
            db.execute("UPDATE fund_deliveries SET next_at=?", ((NOW + timedelta(seconds=120)).isoformat(),))
        raise OSError("synthetic report failure")

    monitor._outgoing = failed
    monitor.poll()
    with journal.connection() as db:
        row = db.execute("SELECT state,next_at FROM fund_deliveries").fetchone()
        assert row["state"] == "sending" and instant(row["next_at"]) == NOW + timedelta(seconds=120)


def test_recipient_is_rechecked_after_report_build_before_http(tmp_path, monkeypatch):
    journal = setup(tmp_path)
    live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    original = monitor._outgoing

    def changed(body, sequence, text, now):
        with journal.connection() as db:
            db.execute("UPDATE fund_monitor_meta SET value='different-recipient-binding' WHERE key='recipient'")
        return original(body, sequence, text, now)

    monitor._outgoing = changed
    with pytest.raises(RiskError, match="recipient changed"):
        monitor.poll()
    assert not transport.messages


def test_shared_backoff_activated_during_build_is_honored_before_http(tmp_path, monkeypatch):
    journal, wall = setup(tmp_path), live_clock(monkeypatch)
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic notice"})
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    original = monitor._outgoing

    def built(body, sequence, text, now):
        with journal.connection() as db:
            db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('next_delivery_at',?)",
                       ((NOW + timedelta(seconds=120)).isoformat(),))
        return original(body, sequence, text, now)

    monitor._outgoing = built
    monitor.poll()
    assert not transport.messages
    with journal.connection() as db:
        row = db.execute("SELECT state,next_at FROM fund_deliveries").fetchone()
        assert row["state"] == "retry" and instant(row["next_at"]) == NOW + timedelta(seconds=120)
    wall[0] += timedelta(seconds=120)
    monitor.poll()
    assert len(transport.messages) == 1
