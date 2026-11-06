"""Large NAV histories cannot slow trade guards or bypass account/fill validation.

Count actual historical JSON reads instead of imposing machine-dependent timing
thresholds. All orders, prices and books are synthetic; no network or inference.
"""
import json

import pytest

from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.ledger_audit import audit, require_consistent
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.risk import RiskError, number
from .test_autonomous_fund_closed_ledger import traded_book
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_local import setup_book, submit, advance


def historical_marks(journal, count=2000):
    with journal.connection() as db:
        at, raw = db.execute("SELECT at,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        body = json.loads(raw)
        body["audit_speed_history_marker"] = True
        db.executemany("INSERT INTO events(at,kind,body) VALUES(?,'account',?)", [(at, json.dumps(body))] * count)
    return body


def count_history_reads(monkeypatch):
    reads = []
    original = json.loads
    def counted(body, *args, **kwargs):
        if isinstance(body, str) and '"audit_speed_history_marker"' in body:
            reads.append(1)
        return original(body, *args, **kwargs)
    monkeypatch.setattr(json, "loads", counted)
    return reads


def test_native_trade_guards_do_not_read_historical_nav_but_full_audit_does(tmp_path, monkeypatch):
    clock, source, journal, broker, _ = traded_book(tmp_path)
    historical_marks(journal)
    reads = count_history_reads(monkeypatch)
    assert broker.ledger_audit()["scope"] == "ledger"
    broker._get_balances_at_broker(None, "LocalTest")
    broker._pull_positions("LocalTest")
    _, buy = submit(broker, journal, clock.value, quantity=1)
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(buy.identifier, "LocalTest").status == "fill"
    assert reads == []  # Work on the order path must not grow with historical NAV rows.
    full = audit(tmp_path)
    assert full["scope"] == "ledger_and_observations" and full["errors"] == []
    assert len(reads) == 2000  # Explicit audit still independently checks every historical mark.


def test_repeated_fast_reads_still_detect_uncommitted_and_external_cash_changes(tmp_path):
    _, _, journal, broker, _ = traded_book(tmp_path)
    historical_marks(journal)
    for _ in range(3):
        broker._get_balances_at_broker(None, "LocalTest")
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE local_account SET cash='1'")
        with pytest.raises(RiskError, match="cash_mismatch"):
            require_consistent(db)
        db.rollback()
        assert require_consistent(db)["errors"] == []
    # A different connection's committed edit must be visible immediately too.
    with journal.connection() as external:
        external.execute("UPDATE local_account SET cash='1'")
    with pytest.raises(RiskError, match="cash_mismatch"):
        broker._get_balances_at_broker(None, "LocalTest")


def test_post_fill_money_check_still_rolls_back_the_whole_transaction(tmp_path, monkeypatch):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    original = broker._event
    def corrupt_after_fill(db, kind, body):
        original(db, kind, body)
        if kind == "fill":
            db.execute("UPDATE local_account SET cash='1'")
    monkeypatch.setattr(broker, "_event", corrupt_after_fill)
    _, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    assert "cash_mismatch" in broker._last_poll_error
    assert broker._pull_order(order.identifier, "LocalTest").status == "new"
    with journal.connection() as db:
        assert number(db.execute("SELECT cash FROM local_account").fetchone()[0]) == 10000
        assert db.execute("SELECT count(*) FROM local_fills").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM local_positions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events WHERE kind='fill'").fetchone()[0] == 0
    assert broker.ledger_audit()["errors"] == []


def test_monitor_bounds_history_reads_and_rejects_bad_latest_snapshot(tmp_path, monkeypatch):
    clock, _, journal, _, _ = traded_book(tmp_path)
    journal.event(clock.value, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    journal.event(clock.value, "iteration_finished", {})
    body = historical_marks(journal)
    bad = {**body, "equity": "999999"}
    journal.event(clock.value, "account", bad)
    reads = count_history_reads(monkeypatch)
    monitor = Monitor(tmp_path)
    assert monitor.poll(clock.value)["health"] == "healthy"
    # Recent candidate checks remain bounded; old projection history is not a
    # dependency of notification delivery or the five-second monitor heartbeat.
    assert 0 < len(reads) < 250
    updates = [r["body"]["details"] for r in journal.all_events() if r["kind"] == "notification"
               and r["body"]["kind"] == "execution_update"]
    assert updates[-1]["account"]["equity"] == body["equity"]
    assert "999999" not in (tmp_path / "metrics.prom").read_text()
    full = audit(tmp_path)
    assert full["status"] == "partial" and full["errors"] == []
    assert "account_nav_arithmetic_mismatch" in {r["code"] for r in full["warnings"]}
