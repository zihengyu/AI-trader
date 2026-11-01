"""Durable target execution and operator-only bounded approvals."""
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .journal import encode
from .risk import RiskError, instant, number, trading_day, validate_decision


class ApprovalPending(RiskError):
    pass


@dataclass(frozen=True)
class ApprovalPolicy:
    single_order: str = "0.08"
    daily_turnover: str = "0.20"
    valid_minutes: int = 30
    price_tolerance: str = "0.005"

    def __post_init__(self):
        if not (0 < number(self.single_order) <= 1 and 0 < number(self.daily_turnover) <= 2):
            raise RiskError("Invalid approval thresholds")
        if type(self.valid_minutes) is not int or not 1 <= self.valid_minutes <= 60:
            raise RiskError("Approval validity must be 1..60 minutes")
        if not 0 <= number(self.price_tolerance) <= number("0.01"):
            raise RiskError("Approval price tolerance must be at most 1%")

    @property
    def digest(self):
        return digest(asdict(self))


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


class ExecutionState:
    def __init__(self, journal, policy=None):
        self.journal = journal
        self.policy = policy or ApprovalPolicy()
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_decisions (
                    id TEXT PRIMARY KEY, accepted_at TEXT NOT NULL, until_at TEXT NOT NULL,
                    body TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_approvals (
                    id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, body TEXT NOT NULL,
                    digest TEXT NOT NULL, state TEXT NOT NULL, resolved_at TEXT);
            """)

    def latest(self, now, db=None):
        if db is None:
            with self.journal.connection() as connection:
                return self.latest(now, connection)
        rows = db.execute("SELECT * FROM fund_decisions ORDER BY rowid DESC").fetchall()
        return next((dict(row) for row in rows if instant(row["accepted_at"]) <= instant(now)), None)

    def active(self, now, universe, limits, db=None):
        row = self.latest(now, db)
        if not row or row["state"] != "active" or row["policy"] != self.policy.digest:
            return None
        if not instant(row["accepted_at"]) <= instant(now) < instant(row["until_at"]):
            return None
        body = json.loads(row["body"])
        # Only acceptance uses the five-minute response freshness rule. A
        # retained target is executable until its declared review/expiry, with
        # NEW account, quotes, liquidity and risk checks before every order.
        weights = validate_decision(body, instant(row["accepted_at"]), universe, limits)
        return {**row, "decision": body, "weights": weights}

    def accept(self, body, now, universe, limits):
        validate_decision(body, instant(now), universe, limits)
        key = uuid4().hex
        until = min(instant(body["expires_at"]), instant(body["next_review_at"]))
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE fund_decisions SET state='superseded' WHERE state='active'")
            db.execute("UPDATE fund_approvals SET state='superseded' WHERE state IN ('pending','approved')")
            db.execute("INSERT INTO fund_decisions VALUES(?,?,?,?,?,?)",
                       (key, str(now), until.isoformat(), encode(body), self.policy.digest, "active"))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                       (str(now), "decision_accepted", encode({"id": key, "until_at": until, "decision": body})))
        return key

    def complete(self, key, now):
        with self.journal.connection() as db:
            db.execute("UPDATE fund_decisions SET state='complete' WHERE id=? AND state='active'", (key,))
        self.journal.event(now, "allocation_complete", {"decision_id": key})

    @staticmethod
    def turnover(db, now):
        # Count all reserved/submitted notional, including cancelled orders.
        # Splitting a large trade into small orders cannot bypass the threshold.
        return sum((number(b["quantity"]) * number(b["limit"]) for b in
                    (json.loads(r[0]) for r in db.execute("SELECT body FROM intents WHERE day=?", (trading_day(now),)))), number(0))

    def reasons(self, db, intent, account, active, now, liquidates_all=None):
        amount = number(intent.quantity) * number(intent.limit)
        reasons = []
        if amount >= number(account.equity) * number(self.policy.single_order):
            reasons.append("single_order_at_least_8_percent" if self.policy.single_order == "0.08" else "single_order_threshold")
        if self.turnover(db, now) + amount >= number(account.equity) * number(self.policy.daily_turnover):
            reasons.append("daily_turnover_threshold")
        if liquidates_all is None:
            liquidates_all = any(number(q) > 0 for q in account.positions.values()) and not any(active["weights"].values())
        if liquidates_all:
            reasons.append("liquidate_entire_portfolio")
        return reasons

    @staticmethod
    def within(body, intent, now, active, policy):
        return (body["decision_id"] == active["id"] and body["policy"] == policy.digest and
                body["day"] == trading_day(now) and instant(body["created_at"]) <= instant(now) < instant(body["expires_at"]) and
                body["symbol"] == intent.symbol and body["side"] == intent.side and
                0 < intent.quantity <= body["max_quantity"] and number(intent.limit) <= number(body["max_limit"]) and
                (intent.side != "sell" or ("min_limit" in body and number(intent.limit) >= number(body["min_limit"]))) and
                number(intent.quantity) * number(intent.limit) <= number(body["max_notional"]))

    def prepare(self, active, intent, account, now, liquidates_all=None):
        """Commit an immutable request without reserving or sending an order."""
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            reasons = self.reasons(db, intent, account, active, now, liquidates_all)
            old = db.execute("SELECT * FROM fund_approvals WHERE decision_id=? AND state IN ('pending','approved') ORDER BY rowid DESC",
                             (active["id"],)).fetchall()
            for row in old:
                body = json.loads(row["body"])
                if (body["symbol"], body["side"]) != (intent.symbol, intent.side):
                    continue
                if self.within(body, intent, now, active, self.policy) and set(reasons) <= set(body["reasons"]):
                    return dict(row)
                db.execute("UPDATE fund_approvals SET state='superseded' WHERE id=?", (row["id"],))
            if not reasons:
                return None
            binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
            key = uuid4().hex
            maximum = number(intent.limit) * (1 + number(self.policy.price_tolerance))
            from .market_data import SessionClock
            close = instant(SessionClock(now=lambda: instant(now)).status()["close"])
            expires = min(instant(active["until_at"]), instant(now) + timedelta(minutes=self.policy.valid_minutes), close)
            body = {"id": key, "binding": binding, "decision_id": active["id"], "policy": self.policy.digest,
                    "day": trading_day(now), "created_at": instant(now).isoformat(), "expires_at": expires.isoformat(),
                    "symbol": intent.symbol, "side": intent.side, "max_quantity": intent.quantity,
                    "max_limit": str(maximum), "max_notional": str(maximum * intent.quantity),
                    "min_limit": str(number(intent.limit) * (1 - number(self.policy.price_tolerance))),
                    "observed_equity": str(account.equity), "daily_reserved_notional": str(self.turnover(db, now)),
                    "current_quantity": str(account.positions.get(intent.symbol, 0)),
                    "target_weight": str(active["weights"].get(intent.symbol, 0)),
                    "reasons": reasons, "target_portfolio": active["decision"]["targets"],
                    "rationale": active["decision"]["rationale"]}
            checksum = digest(body)
            db.execute("INSERT INTO fund_approvals VALUES(?,?,?,?,?,NULL)", (key, active["id"], encode(body), checksum, "pending"))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                "kind": "approval_required", "details": {"id": key, "digest": checksum, "symbol": intent.symbol,
                "side": intent.side, "max_quantity": intent.quantity, "max_notional": body["max_notional"],
                "expires_at": body["expires_at"], "reasons": reasons,
                "rationale": body["rationale"], "observed_equity": body["observed_equity"],
                "current_quantity": body["current_quantity"], "target_weight": body["target_weight"],
                "max_limit": body["max_limit"], "min_limit": body["min_limit"],
                "target_portfolio": body["target_portfolio"]}})))
            return {"id": key, "body": encode(body), "digest": checksum, "state": "pending"}

    def guard(self, active, intent, account, now, approval, universe, limits, liquidates_all=None):
        """Runs INSIDE the durable order reservation's transaction."""
        def check(db):
            current = self.active(now, universe, limits, db)
            if not current or current["id"] != active["id"]:
                raise RiskError("Target allocation changed or expired before reservation")
            required = self.reasons(db, intent, account, current, now, liquidates_all)
            if approval is None:
                if required:
                    raise ApprovalPending("Approval now required; refresh the allocation")
                return
            row = db.execute("SELECT * FROM fund_approvals WHERE id=?", (approval["id"],)).fetchone()
            if not row or row["state"] != "approved" or not row["resolved_at"] or instant(row["resolved_at"]) > instant(now):
                raise ApprovalPending("Waiting for explicit operator approval")
            body = json.loads(row["body"])
            binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
            if (row["digest"] != digest(body) or body["binding"] != binding or
                    not self.within(body, intent, now, current, self.policy) or not set(required) <= set(body["reasons"])):
                raise ApprovalPending("Approval is expired or outside its reviewed scope")
            db.execute("UPDATE fund_approvals SET state='consumed',resolved_at=? WHERE id=?", (str(now), row["id"]))
        return check


@contextmanager
def operator_connection(root):
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        raise RiskError("No existing account journal at the selected run directory")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def resolve_approval(root, key, checksum, approve, now):
    """Only the local operator CLI calls this; it is never an agent tool."""
    with operator_connection(root) as db:
        db.execute("BEGIN IMMEDIATE")
        startup = db.execute("SELECT body FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()
        if not startup or json.loads(startup[0]).get("mode") not in {"local", "paper"}:
            raise RiskError("Manual approval requires an existing local or paper account")
        row = db.execute("SELECT * FROM fund_approvals WHERE id=?", (key,)).fetchone()
        if not row or row["state"] not in ({"pending"} if approve else {"pending", "approved"}):
            raise RiskError("Approval is not pending")
        body = json.loads(row["body"])
        binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
        decision = db.execute("SELECT * FROM fund_decisions WHERE id=?", (row["decision_id"],)).fetchone()
        if (checksum != row["digest"] or checksum != digest(body) or body["binding"] != binding or
                not instant(body["created_at"]) <= instant(now) < instant(body["expires_at"]) or
                body["day"] != trading_day(now) or not decision or decision["state"] != "active" or
                instant(now) >= instant(decision["until_at"])):
            raise RiskError("Approval digest, account, session or validity does not match")
        if approve and (Path(root) / "STOP").exists():
            raise RiskError("STOP is active; approval cannot enable trading")
        state = "approved" if approve else "rejected"
        db.execute("UPDATE fund_approvals SET state=?,resolved_at=? WHERE id=?", (state, str(now), key))
        if not approve:
            db.execute("UPDATE fund_decisions SET state='rejected' WHERE id=?", (row["decision_id"],))
            db.execute("UPDATE fund_approvals SET state='superseded' WHERE decision_id=? AND state IN ('pending','approved')", (row["decision_id"],))
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "operator_approval", encode({
            "id": key, "digest": checksum, "state": state, "authority": "local_operator_cli"})))
    return state
