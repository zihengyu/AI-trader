"""The ledger replay's cost and memory stay proportional to the book, and it runs only where it guards something.

Counts work (JSON parses, replays, SQL searches) instead of machine-dependent
timings. All orders, prices and books are synthetic; no network or inference.
"""
import json
import sqlite3
import tracemalloc
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

import pytest

from scripts.autonomous_fund import ledger_audit
from scripts.autonomous_fund.journal import Journal
from .test_autonomous_fund_closed_ledger import CLOSE, closing_feed, traded_book
from .test_autonomous_fund_live_observations import configure
from .test_autonomous_fund_local import advance, setup_book, submit

NEW_YORK = ZoneInfo("America/New_York")


def synthetic_book(path, fills=120, dividends=24):
    """A consistent book with periodic dividends; the receivables are computed here, independently."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE intents (id TEXT PRIMARY KEY, day TEXT NOT NULL, body TEXT NOT NULL, broker_id TEXT,
                              status TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
                             body TEXT NOT NULL);
        CREATE INDEX events_kind_sequence ON events(kind, sequence);
        CREATE TABLE local_account (id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT NOT NULL, receivables TEXT NOT NULL);
        CREATE TABLE local_positions (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, average TEXT NOT NULL,
                                      opened_at TEXT NOT NULL);
        CREATE TABLE local_orders (id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL, strategy TEXT NOT NULL,
            body TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
            filled INTEGER NOT NULL DEFAULT 0, notional TEXT NOT NULL DEFAULT '0', fee TEXT NOT NULL DEFAULT '0',
            last_bar TEXT);
        CREATE TABLE local_fills (order_id TEXT NOT NULL, bar_start TEXT NOT NULL, quantity INTEGER NOT NULL,
            price TEXT NOT NULL, fee TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(order_id,bar_start));
        CREATE TABLE local_actions (id TEXT PRIMARY KEY, amount TEXT NOT NULL);
    """)
    start, cash = datetime(2026, 1, 5, 15, tzinfo=timezone.utc), Decimal(1000000)

    def event(at, kind, body):
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(at), kind, json.dumps(body)))

    event(start, "local_ledger_origin", {"cash": str(cash), "receivables": "0", "positions": {}})
    costs = {"fee_rate": "0.001", "spread_bps": "2", "slippage_bps": "5", "volume_fraction": "0.01"}
    symbols, held, average, history = ["ALFA", "BETA", "GAMA"], {}, {}, []
    for i in range(fills):
        at, symbol = start + timedelta(hours=6 * i), symbols[i % 3]
        side = "sell" if held.get(symbol, 0) >= 10 and i % 4 == 3 else "buy"
        high = Decimal(100 + i % 5)
        friction = Decimal(5) / 10000 + Decimal(2) / 20000  # Slippage plus half the spread, as recorded below.
        price = ((high * (1 + friction)).quantize(Decimal("0.01"), ROUND_CEILING) if side == "buy" else
                 (high * (1 - friction)).quantize(Decimal("0.01"), ROUND_FLOOR))  # The bar's low equals its high.
        fee = price * 10 * Decimal("0.001")
        body = {"symbol": symbol, "side": side, "quantity": 10, "limit": str(price + (1 if side == "buy" else -1))}
        db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?)", (f"i{i}", str(at.date()), json.dumps(body),
                                                              f"local-i{i}", "terminal", str(at)))
        db.execute("INSERT INTO local_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            f"local-i{i}", f"i{i}", "S", json.dumps(body), str(at), str(at + timedelta(hours=2)), "fill", 10,
            str(price * 10), str(fee), str(at)))
        observed = at + timedelta(minutes=2)
        db.execute("INSERT INTO local_fills VALUES(?,?,?,?,?,?)", (f"local-i{i}", str(at), 10, str(price), str(fee),
                                                                  str(observed)))
        event(observed, "fill", {"broker_id": f"local-i{i}", "symbol": symbol, "side": side, "quantity": "10",
              "price": str(price), "trade_cost": str(fee), "bar_start": str(at), "execution_assumptions": costs,
              "bar": {"open": str(high), "high": str(high), "low": str(high), "close": str(high), "volume": "100000"}})
        if side == "buy":
            average[symbol] = (held.get(symbol, 0) * average.get(symbol, 0) + 10 * price) / (held.get(symbol, 0) + 10)
            held[symbol] = held.get(symbol, 0) + 10
            cash -= 10 * price + fee
        else:
            held[symbol] -= 10
            cash += 10 * price - fee
        history.append((observed, symbol, 10 if side == "buy" else -10))
    receivables = Decimal(0)
    for d in range(dividends):
        symbol, day = symbols[d % 3], (start + timedelta(days=d + 1)).date().isoformat()
        cutoff = datetime.combine(datetime.fromisoformat(day).date(), time(), tzinfo=NEW_YORK)
        entitled = sum(change for at, name, change in history if name == symbol and at < cutoff)
        db.execute("INSERT INTO local_actions VALUES(?,?)", (f"{symbol}:{day}:dividend", "0.25"))
        event(f"{day}T21:00:00+00:00", "local_corporate_action", {"symbol": symbol, "date": day, "dividend": "0.25",
              "kind": "dividend", "entitled_quantity": str(entitled)})
        receivables += entitled * Decimal("0.25")
    db.execute("INSERT INTO local_account VALUES(1,?,?)", (str(cash), str(receivables)))
    for symbol, quantity in held.items():
        if quantity:
            db.execute("INSERT INTO local_positions VALUES(?,?,?,?)", (symbol, quantity, str(average[symbol]),
                                                                      str(start)))
    db.commit()
    return db


def counted_parses(monkeypatch):
    calls = []
    original = json.loads
    monkeypatch.setattr(json, "loads", lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs))
    return calls


def test_the_replay_parses_each_record_once_however_many_dividends_there_are(tmp_path, monkeypatch):
    fills, dividends = 120, 24
    db = synthetic_book(tmp_path / "book.sqlite3", fills, dividends)
    db.execute("BEGIN")
    calls = counted_parses(monkeypatch)
    result = ledger_audit.require_consistent(db)
    assert result["errors"] == [] and result["fills_checked"] == fills
    # Orders, intents and fill events once each, plus the dividend and origin events. Before, every
    # dividend re-parsed every fill's order (here 24 x 120 extra parses).
    assert len(calls) <= 3 * fills + dividends + 10
    db.rollback()
    db.execute("UPDATE local_account SET receivables='0'")  # The dividend replay is still checked.
    db.commit()
    db.execute("BEGIN")
    with pytest.raises(Exception, match="receivables_mismatch"):
        ledger_audit.require_consistent(db)


def test_nav_marks_are_checked_one_at_a_time_not_held_in_memory(tmp_path):
    db = synthetic_book(tmp_path / "book.sqlite3", fills=6, dividends=0)
    at = "2026-01-05T15:00:00+00:00"
    mark = json.dumps({"cash": "1", "equity": "1", "positions": {}, "as_of": at, "padding": "x" * 2000})
    db.executemany("INSERT INTO events(at,kind,body) VALUES(?,'account',?)", [(at, mark)] * 4000)
    db.execute("INSERT INTO events(at,kind,body) VALUES(?,'account',?)", (at, json.dumps({"cash": "x"})))
    db.commit()
    db.execute("BEGIN")
    tracemalloc.start()
    try:
        result = ledger_audit.audit_connection(db)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result["warnings"] == [{"code": "invalid_account_observation", "sequence": result["invalid_account_sequences"][0]}]
    assert peak < 4_000_000  # The 4,000 marks hold about 8 MB of text; keeping them all needed more than twice that.


def test_the_agent_context_audit_reads_each_nav_mark_once_and_equals_the_full_audit(tmp_path, monkeypatch):
    _, _, journal, broker, _ = traded_book(tmp_path)

    def add_marks(count, broken=False):
        with journal.connection() as db:
            at, raw = db.execute("SELECT at,body FROM events WHERE kind='account' AND body NOT LIKE "
                                 "'%cost_test_marker%' ORDER BY sequence DESC LIMIT 1").fetchone()  # A valid mark.
            body = {**json.loads(raw), "cost_test_marker": True}
            rows = [(at, json.dumps(body))] * count
            if broken:
                rows.append((at, json.dumps({**body, "equity": str(number_plus_one(body["equity"]))})))
            db.executemany("INSERT INTO events(at,kind,body) VALUES(?,'account',?)", rows)

    def full():
        with journal.connection() as db:
            db.execute("BEGIN")
            return ledger_audit.audit_connection(db)

    add_marks(50, broken=True)
    reads = []
    original = json.loads
    monkeypatch.setattr(json, "loads", lambda body, *a, **k: (
        reads.append(1) if isinstance(body, str) and '"cost_test_marker"' in body else None) or original(body, *a, **k))
    first = broker.ledger_audit(strict=False)
    assert len(reads) == 51 and first["warnings"]  # Every mark once, including the inconsistent one.
    monkeypatch.setattr(json, "loads", original)
    assert first == full()
    monkeypatch.setattr(json, "loads", lambda body, *a, **k: (
        reads.append(1) if isinstance(body, str) and '"cost_test_marker"' in body else None) or original(body, *a, **k))
    reads.clear()
    assert broker.ledger_audit(strict=False) == first and reads == []  # Nothing new: no history re-read.
    add_marks(5, broken=True)
    second = broker.ledger_audit(strict=False)
    assert len(reads) == 6 and len(second["invalid_account_sequences"]) == 2
    monkeypatch.setattr(json, "loads", original)
    assert second == full()
    assert broker.ledger_audit()["scope"] == "ledger"  # Trade guards keep their own scope.


def number_plus_one(value):
    return Decimal(str(value)) + 1


def count_replays(monkeypatch):
    calls = []
    original = ledger_audit.require_consistent
    monkeypatch.setattr(ledger_audit, "require_consistent", lambda db: calls.append(1) or original(db))
    return calls


def test_a_balance_read_replays_the_ledger_once(tmp_path, monkeypatch):
    _, _, _, broker, _ = traded_book(tmp_path)
    calls = count_replays(monkeypatch)
    broker._get_balances_at_broker(None, "LocalTest")
    assert len(calls) == 1
    broker._pull_positions("LocalTest")  # The native hook keeps its own check.
    assert len(calls) == 2


def test_a_poll_replays_the_ledger_only_when_a_valuation_is_due(tmp_path, monkeypatch):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)  # A 30-second valuation interval.
    broker.do_polling()
    calls = count_replays(monkeypatch)
    for _ in range(4):  # Polls every 5 seconds; the book is unchanged and no valuation is due.
        clock.value += timedelta(seconds=5)
        broker.do_polling()
    assert calls == []
    clock.value += timedelta(seconds=11)
    source._cache.clear()
    marks = sum(row["kind"] == "account" for row in journal.all_events())
    broker.do_polling()
    assert len(calls) == 1 and sum(row["kind"] == "account" for row in journal.all_events()) == marks + 1


def test_a_bar_beyond_the_limit_is_consumed_without_valuation_or_replay(tmp_path, monkeypatch):
    clock, source, journal, broker = setup_book(tmp_path)
    _, order = submit(broker, journal, clock.value, limit=100.1)  # Below the bar's high plus friction.
    advance(clock, source)
    calls, valued = count_replays(monkeypatch), []
    original = broker._read_balances
    monkeypatch.setattr(broker, "_read_balances", lambda *a, **k: valued.append(1) or original(*a, **k))
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "new"
    assert calls == [] and valued == []
    with journal.connection() as db:
        consumed = db.execute("SELECT last_bar FROM local_orders").fetchone()[0]
    assert consumed == str(source.execution_bars(order.asset).index[-1].isoformat())
    broker.do_polling()  # The consumed bar is not looked at again.
    assert calls == [] and valued == []
    clock.high = clock.price  # A later bar inside the limit still fills through the full path.
    advance(clock, source)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "fill"
    assert valued and len(calls) >= 2  # The fill was valued and replayed before and after the write.


@contextmanager
def traced_statements(monkeypatch):
    statements, original = [], Journal.connection

    @contextmanager
    def connection(self):
        with original(self) as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(Journal, "connection", connection)
    yield statements


def test_the_saved_closing_valuation_is_searched_once_per_close(tmp_path, monkeypatch):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    original = source.fetch
    clock.value = CLOSE + timedelta(seconds=5)
    source.fetch = lambda *args: original(*args).loc[lambda frame: frame.index < CLOSE - timedelta(minutes=1)]
    with traced_statements(monkeypatch) as statements:
        for _ in range(6):  # The closing minute is missing for these polls.
            broker.do_polling()
            clock.value += timedelta(seconds=5)
        searches = [s for s in statements if "$.valuation.session_close" in s]
        assert len(searches) == 1 and broker._valuation_error
        closing_feed(clock, source)
        clock.value += timedelta(seconds=31)
        broker.do_polling()
        published = [row for row in journal.all_events() if row["kind"] == "account"][-1]
        assert published["body"]["valuation"]["session_close"] == str(CLOSE)
        statements.clear()
        for _ in range(3):
            clock.value += timedelta(seconds=5)
            broker.do_polling()
        assert not [s for s in statements if "$.valuation.session_close" in s or "local_fills" in s]


def test_the_monitor_replays_the_ledger_only_when_fills_are_new(tmp_path, monkeypatch):
    from scripts.autonomous_fund.execution import operator_connection
    from scripts.autonomous_fund.monitor import Monitor
    _, _, journal, _, _ = traded_book(tmp_path)
    journal.event(datetime.now(timezone.utc), "startup", {"mode": "local"})
    monitor = Monitor(tmp_path)
    calls = []
    original = ledger_audit.audit_connection
    monkeypatch.setattr(ledger_audit, "audit_connection", lambda db, **k: calls.append(1) or original(db, **k))
    now = datetime.now(timezone.utc) + timedelta(days=1)
    with operator_connection(tmp_path) as db:
        monitor._queue_fill_updates(db, now)  # The traded fill is announced (or skipped as old) once.
    assert len(calls) == 1
    with operator_connection(tmp_path) as db:
        monitor._queue_fill_updates(db, now)
    assert len(calls) == 1  # No new fill: no replay.
