"""Durable submission reservations, restart state and append-only audit events."""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .risk import RiskError, number, trading_day


def encode(value):
    return json.dumps(value, default=str, ensure_ascii=False, allow_nan=False)


class Journal:
    def __init__(self, root, binding):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, body TEXT NOT NULL,
                    broker_id TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL);
            """)
            old = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
            if old and old[0] != binding:
                raise RiskError("State belongs to another mode, account or configuration")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('binding', ?)", (binding,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, at, kind, body):
        with self.connection() as db:
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(at), kind, encode(body)))

    def reserve(self, intent, now, limits, guard=None):
        """Commit BEFORE contacting the broker. Ambiguous calls remain reserved."""
        key = uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            unresolved = db.execute("SELECT 1 FROM intents WHERE status != 'terminal'").fetchone()
            count = db.execute("SELECT count(*) FROM intents WHERE day=?", (trading_day(now),)).fetchone()[0]
            if unresolved or count >= limits.daily_orders:
                raise RiskError("Unresolved intent or daily submission limit reached")
            if guard:
                guard(db)
            db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?)", (
                key, trading_day(now), encode(asdict(intent)), None, "reserved", str(now)))
        self.event(now, "intent_reserved", {"id": key, **asdict(intent)})
        return key

    def update(self, key, broker_id, terminal, now):
        with self.connection() as db:
            db.execute("UPDATE intents SET broker_id=?,status=?,updated_at=? WHERE id=?", (
                str(broker_id) if broker_id else None, "terminal" if terminal else "pending", str(now), key))

    def pending(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM intents WHERE status != 'terminal'")]

    def confirm_initial_paper_account(self, account):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM meta WHERE key='initial_paper_equity'").fetchone():
                if account.positions or account.open_orders or abs(number(account.equity) - 10000) > number("0.01"):
                    raise RiskError("First paper start requires an empty dedicated $10,000 account with no open orders")
                db.execute("INSERT INTO meta VALUES ('initial_paper_equity', ?)", (str(account.equity),))

    def mark_equity(self, now, equity, limits):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = dict(db.execute("SELECT key,value FROM meta"))
            peak = max(number(equity), number(rows.get("peak", equity)))
            breached = number(equity) <= peak * (1 - limits.drawdown)
            paused = rows.get("drawdown_paused") == "true" or breached
            for key, value in (("peak", str(peak)), ("drawdown_paused", str(paused).lower())):
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        if breached and rows.get("drawdown_paused") != "true":
            self.event(now, "drawdown_pause", {"equity": equity, "peak": peak})
        return paused

    def export(self):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY sequence")]
        # SQLite is authoritative; these are rebuildable human-readable exports.
        with (self.root / "events.jsonl").open("w", encoding="utf-8") as out:
            for row in rows:
                row["body"] = json.loads(row["body"])
                out.write(encode(row) + "\n")
        return rows
