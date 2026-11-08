"""Durable submission reservations, restart state and append-only audit events."""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .risk import RiskError, number, trading_day


def encode(value):
    return json.dumps(value, default=str, ensure_ascii=False, allow_nan=False)


# A notification's own kind, as an indexed expression (see events_notification_kind).
# Lookups must use this exact text to use the index. json_valid keeps a malformed
# body from failing its own insert. The monitor otherwise re-read every notification
# body on each 10-second poll to find the few of one kind.
NOTIFICATION_KIND = "(CASE WHEN json_valid(body) THEN json_extract(body,'$.kind') END)"


def notification_body(text):
    """A stored notification body as a dict, or None when it is not a JSON object.

    Readers that scan notices skip such a row instead of failing on it: one
    corrupt row must not stop supervision or later notices.
    """
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def event_body(kind, text):
    """Project an unreadable notice as a diagnostic; ledger and research evidence stay strict.

    The raw notice remains in SQLite. Its sequence/time are retained by readers,
    so exports expose the damage without blocking every subsequent event.
    """
    if kind == "notification":
        body = notification_body(text)
        return body if body is not None else {
            "kind": "malformed_notification_body", "details": {"reason": "Stored notice is not a JSON object"}}
    return json.loads(text)


class Journal:
    def __init__(self, root, binding, legacy_bindings=(), migrate_any_legacy=None):
        """Open the account journal bound to `binding`.

        `legacy_bindings` are exact earlier digests that provably name the same
        account; `migrate_any_legacy(db)` may approve a migration when the
        journal itself is the account (local virtual book). Any other mismatch
        still refuses to borrow another account's latches, orders or peak.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.connection() as db:
            # WAL lets the monitor read while the runner writes; it persists in the file.
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, body TEXT NOT NULL,
                    broker_id TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_kind_sequence ON events(kind, sequence);
                CREATE INDEX IF NOT EXISTS events_notification_kind ON events(%s, sequence) WHERE kind='notification';
                CREATE INDEX IF NOT EXISTS intents_day ON intents(day);
            """ % NOTIFICATION_KIND)
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
            if old and old[0] != binding:
                if old[0] in legacy_bindings or (migrate_any_legacy and migrate_any_legacy(db)):
                    db.execute("UPDATE meta SET value=? WHERE key='binding'", (binding,))
                    db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (
                        datetime.now(timezone.utc).isoformat(), "binding_migrated", encode({"from": old[0], "to": binding})))
                else:
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

    def notification_with_meta(self, at, kind, details, metadata):
        """Publish the durable outbox entry and its deduplication baseline together."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                       (str(at), "notification", encode({"kind": kind, "details": details})))
            db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)",
                           [(key, encode(value)) for key, value in metadata.items()])

    @staticmethod
    def blocked(db, intent, now, cap):
        """True when an unresolved intent exists or the side's daily budget is spent.

        Purchases count every submission of the trading day, so heavy selling
        never frees room to buy. Sales count only sales: purchases and their
        reprices can never use up the budget that de-risking and protective
        exits need on the day they matter most. Both remain hard caps.
        """
        if db.execute("SELECT 1 FROM intents WHERE status != 'terminal'").fetchone():
            return True
        bodies = [row[0] for row in db.execute("SELECT body FROM intents WHERE day=?", (trading_day(now),))]
        if intent.side == "sell":
            bodies = [body for body in bodies if json.loads(body)["side"] == "sell"]
        return len(bodies) >= cap

    def reserve(self, intent, now, limits, guard=None, max_submissions=None):
        """Commit BEFORE contacting the broker. Ambiguous calls remain reserved."""
        key = uuid4().hex
        cap = limits.daily_orders if max_submissions is None else max_submissions
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.blocked(db, intent, now, cap):
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

    def observe_session(self, now, account, daily_loss_limit=None):
        """Per New York trading day: start equity/exposure and observed cash spent.

        The start equity is the last account observation before this trading
        day (normally the prior close); the start exposure is the first
        observation of the day. Cash spent is the sum of observed cash
        decreases, i.e. purchases and their costs, from real account snapshots.
        Nothing is estimated when a snapshot is missing.
        """
        day = trading_day(now)
        equity, cash = number(account.equity), number(account.cash)
        exposure = (equity - cash - number(account.receivables)) / equity if equity > 0 else number(0)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = dict(db.execute("SELECT key,value FROM meta WHERE key LIKE 'session_%'"))
            if rows.get("session_day") != day:
                start = number(rows.get("session_last_equity", equity))
                rows = {"session_day": day, "session_start_equity": str(start),
                        "session_start_exposure": str(exposure), "session_cash_spent": "0",
                        "session_loss_paused": "false"}
            else:
                spent = number(rows["session_cash_spent"]) + max(number(0), number(rows["session_last_cash"]) - cash)
                rows["session_cash_spent"] = str(spent)
            rows["session_last_cash"], rows["session_last_equity"] = str(cash), str(equity)
            start = number(rows["session_start_equity"])
            loss = 1 - equity / start if start > 0 else number(0)
            newly_paused = False
            if daily_loss_limit is not None and loss >= number(daily_loss_limit) and rows["session_loss_paused"] != "true":
                rows["session_loss_paused"], newly_paused = "true", True
            for key, value in rows.items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
            peak = db.execute("SELECT value FROM meta WHERE key='peak'").fetchone()
        if newly_paused:
            self.event(now, "daily_loss_pause", {"day": day, "loss": str(loss), "start_equity": str(start)})
        return self.session(now, peak=number(peak[0]) if peak else equity, equity=equity)

    def session(self, now, peak=None, equity=None):
        with self.connection() as db:
            rows = dict(db.execute("SELECT key,value FROM meta WHERE key LIKE 'session_%' OR key IN ('peak','drawdown_paused')"))
        if rows.get("session_day") != trading_day(now):
            return None
        equity = number(equity if equity is not None else rows["session_last_equity"])
        peak = number(peak if peak is not None else rows.get("peak", equity))
        start = number(rows["session_start_equity"])
        return {"day": rows["session_day"], "start_equity": start,
                "start_exposure": number(rows["session_start_exposure"]),
                "cash_spent": number(rows["session_cash_spent"]),
                "day_loss": 1 - equity / start if start > 0 else number(0),
                "loss_paused": rows["session_loss_paused"] == "true",
                "drawdown": max(number(0), 1 - equity / peak) if peak > 0 else number(0),
                "drawdown_latched": rows.get("drawdown_paused") == "true"}

    def reserve_cash(self):
        """Account cash excluded from the AI's capital sleeve (0 when no mandate)."""
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='mandate_reserve_cash'").fetchone()
        return number(row[0]) if row else number(0)

    def apply_mandate(self, now, capital, cash, sleeve_value, holdings, open_orders, external, clean_start):
        """Carve the operator's capital sleeve out of the account; return reserved cash.

        First start: reserve = (cash + sleeve holdings value) - capital, so the AI
        starts with exactly `capital`. A later capital change moves cash between
        reserve and sleeve. External holdings (outside the universe) are frozen
        at their first observed quantities; any change needs operator review.
        Nothing is inferred: an account smaller than the mandate is refused.
        """
        capital = number(capital)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = dict(db.execute("SELECT key,value FROM meta WHERE key LIKE 'mandate_%'"))
            event = None
            if "mandate_reserve_cash" not in rows:
                if clean_start and (holdings or open_orders):
                    raise RiskError("First mandate start requires no eligible-universe positions or open orders")
                reserve = number(sleeve_value) - capital
                if reserve < 0:
                    raise RiskError("Account value is below the mandate capital")
                if reserve > number(cash):
                    raise RiskError("Existing eligible positions exceed the mandate capital; reduce them or raise capital_usd")
                rows = {"mandate_reserve_cash": str(reserve), "mandate_capital_basis": str(capital),
                        "mandate_external": encode({k: str(v) for k, v in sorted(external.items())})}
                event = ("mandate_initialized", {"capital_usd": str(capital), "reserved_cash": str(reserve),
                                                 "external_holdings": rows["mandate_external"]})
            elif number(rows["mandate_capital_basis"]) != capital:
                delta = capital - number(rows["mandate_capital_basis"])
                reserve = number(rows["mandate_reserve_cash"]) - delta
                if reserve < 0:
                    raise RiskError("Raised mandate capital exceeds the account's reserved cash")
                if reserve > number(cash):
                    raise RiskError("Lowered mandate capital exceeds the sleeve's cash; reduce positions first")
                event = ("mandate_capital_changed", {"from": rows["mandate_capital_basis"], "to": str(capital),
                                                     "reserved_cash": str(reserve)})
                rows.update(mandate_reserve_cash=str(reserve), mandate_capital_basis=str(capital))
            baseline = {k: number(v) for k, v in json.loads(rows["mandate_external"]).items()}
            if baseline != {k: number(v) for k, v in external.items()}:
                raise RiskError("External holdings changed; review the account and run adjust-reserve to re-baseline")
            for key, value in rows.items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
            if event:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), event[0], encode(event[1])))
        return number(rows["mandate_reserve_cash"])

    def adjust_reserve(self, now, amount, external=None):
        """Operator-only: record a deposit (+) / withdrawal (-) of reserved cash, or re-baseline externals."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='mandate_reserve_cash'").fetchone()
            if not row:
                raise RiskError("No mandate reserve is recorded for this account yet")
            reserve = number(row[0]) + number(amount)
            if reserve < 0:
                raise RiskError("Reserve cannot become negative; the sleeve's capital is not withdrawable here")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('mandate_reserve_cash',?)", (str(reserve),))
            if external is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('mandate_external',?)",
                           (encode({k: str(v) for k, v in sorted(external.items())}),))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "operator_reserve_adjusted", encode({
                "amount": str(amount), "reserved_cash": str(reserve), "external_rebaselined": external is not None})))
        return reserve

    def counter(self, key, now, increment=0):
        """Per-trading-day durable counter, e.g. model runs; survives restarts."""
        day = trading_day(now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key=?", (f"counter:{key}",)).fetchone()
            saved = json.loads(row[0]) if row else {}
            count = saved.get("count", 0) if saved.get("day") == day else 0
            if increment:
                count += increment
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"counter:{key}", encode({"day": day, "count": count})))
        return count

    def get_meta(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))

    def export(self):
        """Append new events to the rebuildable JSONL export; rebuild if it was altered."""
        target = self.root / "events.jsonl"
        with self.connection() as db:
            state = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('jsonl_sequence','jsonl_bytes')"))
        last, size = int(state.get("jsonl_sequence", 0)), int(state.get("jsonl_bytes", -1))
        append = last > 0 and target.is_file() and target.stat().st_size == size
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence",
                                                (last if append else 0,))]
        # SQLite is authoritative; these are rebuildable human-readable exports.
        with target.open("a" if append else "w", encoding="utf-8") as out:
            for row in rows:
                row["body"] = event_body(row["kind"], row["body"])
                out.write(encode(row) + "\n")
        if rows or not append:
            with self.connection() as db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('jsonl_sequence',?)", (str(rows[-1]["sequence"] if rows else last),))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('jsonl_bytes',?)", (str(target.stat().st_size),))
        return rows

    def all_events(self):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY sequence")]
        for row in rows:
            row["body"] = event_body(row["kind"], row["body"])
        return rows

    def events(self, kinds=None, *, after=0, latest=None):
        """all_events() restricted to `kinds` and sequences after `after`; `latest` keeps only the last n.

        A long-running account holds a NAV mark and a benchmark about every 30
        seconds of market time, so reading every event is for small books only.
        """
        where, arguments = ["sequence>?"], [after]
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            arguments += list(kinds)
        sql = "SELECT * FROM events WHERE " + " AND ".join(where)
        if latest is not None:
            sql = f"SELECT * FROM ({sql} ORDER BY sequence DESC LIMIT ?) ORDER BY sequence"
            arguments.append(int(latest))
        else:
            sql += " ORDER BY sequence"
        with self.connection() as db:
            rows = [dict(r) for r in db.execute(sql, arguments)]
        for row in rows:
            row["body"] = event_body(row["kind"], row["body"])
        return rows

    def recent_trades(self, as_of):
        from .holdings import recent_trades
        with self.connection() as db:
            rows = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
                "SELECT at,kind,body FROM events WHERE kind IN ('fill','fill_reconciled') ORDER BY sequence")]
        return recent_trades(rows, as_of)
