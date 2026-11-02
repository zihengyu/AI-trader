"""Durable target execution and exception-based, plan-level operator approvals.

Approval principle (quantitative pre-trade control, not per-order notional):
- Hard limits are enforced by code and can never be approved away.
- Risk-reducing orders (all sales in this long-only account) never wait.
- Purchases execute automatically inside a daily risk-increase envelope:
  gross exposure may rise by `auto_daily_exposure_increase` above the session
  start and observed purchase cash may reach `auto_daily_buy_turnover` of equity.
- Only purchases beyond that envelope, or any purchase during benchmark market
  stress, need ONE operator approval covering the whole current plan, bounded by
  the hard envelope, a price collar and an expiry.
"""
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .journal import encode
from .risk import RiskError, decision_levels, instant, number, trading_day, validate_decision

# Runner/monitor contract version: a monitor refuses older runners.
EXECUTION_REVISION = 3
TOLERANCE = number("0.002")


class ApprovalPending(RiskError):
    pass


def _section(cls, section):
    section = dict(section or {})
    unknown = set(section) - set(cls.__dataclass_fields__)
    if unknown:
        raise RiskError(f"Unknown {cls.__name__} settings: " + ", ".join(sorted(unknown)))
    return cls(**section)


@dataclass(frozen=True)
class ApprovalPolicy:
    auto_daily_exposure_increase: str = "0.25"
    max_daily_exposure_increase: str = "0.50"
    auto_daily_buy_turnover: str = "0.40"
    max_daily_buy_turnover: str = "1.00"
    stress_requires_approval: bool = True
    valid_minutes: int = 60
    price_collar: str = "0.02"

    def __post_init__(self):
        auto, hard = number(self.auto_daily_exposure_increase), number(self.max_daily_exposure_increase)
        if not 0 <= auto <= hard <= 1:
            raise RiskError("Exposure increase envelope must satisfy 0 <= auto <= max <= 1")
        auto, hard = number(self.auto_daily_buy_turnover), number(self.max_daily_buy_turnover)
        if not 0 <= auto <= hard <= 3:
            raise RiskError("Purchase turnover envelope must satisfy 0 <= auto <= max <= 3")
        if type(self.stress_requires_approval) is not bool:
            raise RiskError("stress_requires_approval must be true or false")
        if type(self.valid_minutes) is not int or not 1 <= self.valid_minutes <= 240:
            raise RiskError("Approval validity must be 1..240 minutes")
        if not 0 <= number(self.price_collar) <= number("0.05"):
            raise RiskError("Approval price collar must be at most 5%")

    @classmethod
    def from_config(cls, section):
        return _section(cls, section)

    @property
    def digest(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class ExecutionPolicy:
    reprice_after_seconds: int = 180
    max_buy_chase: str = "0.0075"
    report_interval_seconds: int = 300
    review_on_move: str = "0.03"
    iteration_seconds: int = 60
    protective_limit_buffer: str = "0.005"
    protective_reprice_seconds: int = 60

    def __post_init__(self):
        if not 0 <= number(self.protective_limit_buffer) <= number("0.05"):
            raise RiskError("protective_limit_buffer must be at most 5%")
        if type(self.protective_reprice_seconds) is not int or not 15 <= self.protective_reprice_seconds <= 3600:
            raise RiskError("protective_reprice_seconds must be 15..3600")
        if type(self.iteration_seconds) is not int or not 15 <= self.iteration_seconds <= 300:
            raise RiskError("iteration_seconds must be 15..300")
        if not 0 <= number(self.review_on_move) <= number("0.5"):
            raise RiskError("review_on_move must be between 0 and 50%")
        if type(self.reprice_after_seconds) is not int or not 30 <= self.reprice_after_seconds <= 3600:
            raise RiskError("reprice_after_seconds must be 30..3600")
        if not 0 <= number(self.max_buy_chase) <= number("0.05"):
            raise RiskError("max_buy_chase must be at most 5%")
        if type(self.report_interval_seconds) is not int or not 0 <= self.report_interval_seconds <= 3600:
            raise RiskError("report_interval_seconds must be 0..3600")

    @classmethod
    def from_config(cls, section):
        return _section(cls, section)


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


class ExecutionState:
    def __init__(self, journal, policy=None, risk_digest=""):
        self.journal = journal
        self.policy = policy or ApprovalPolicy()
        # Decisions bind to the complete approval + quantitative risk policy:
        # a policy change retires old targets instead of executing them under new rules.
        self.digest = digest({"approval": asdict(self.policy), "risk": risk_digest})
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_decisions (
                    id TEXT PRIMARY KEY, accepted_at TEXT NOT NULL, until_at TEXT NOT NULL,
                    body TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_approvals (
                    id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, body TEXT NOT NULL,
                    digest TEXT NOT NULL, state TEXT NOT NULL, resolved_at TEXT);
                CREATE TABLE IF NOT EXISTS fund_orders (
                    intent_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    side TEXT NOT NULL, submitted_at TEXT NOT NULL, reference TEXT NOT NULL);
                -- Standing AI levels (stop / take-profit / entry) of the latest AI decision.
                CREATE TABLE IF NOT EXISTS fund_levels (
                    symbol TEXT PRIMARY KEY, decision_id TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL, set_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_deferred_levels (
                    symbol TEXT PRIMARY KEY, decision_id TEXT NOT NULL, body TEXT NOT NULL, set_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    body TEXT NOT NULL, state TEXT NOT NULL, set_at TEXT NOT NULL);
                -- Deterministic caps applied to one decision after a protective exit.
                CREATE TABLE IF NOT EXISTS fund_overrides (
                    decision_id TEXT NOT NULL, symbol TEXT NOT NULL, max_weight TEXT NOT NULL,
                    reason TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(decision_id, symbol));
                CREATE TABLE IF NOT EXISTS fund_protective_orders (intent_id TEXT PRIMARY KEY, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_refs (
                    decision_id TEXT NOT NULL, symbol TEXT NOT NULL, price TEXT NOT NULL,
                    at TEXT NOT NULL, PRIMARY KEY(decision_id, symbol));
            """)

    def latest(self, now, db=None):
        if db is None:
            with self.journal.connection() as connection:
                return self.latest(now, connection)
        # Newest first and stop at the first match: no full-history read every minute.
        for row in db.execute("SELECT * FROM fund_decisions ORDER BY rowid DESC"):
            if instant(row["accepted_at"]) <= instant(now):
                return dict(row)
        return None

    def active(self, now, universe, limits, db=None):
        row = self.latest(now, db)
        if not row or row["state"] != "active" or row["policy"] != self.digest:
            return None
        if not instant(row["accepted_at"]) <= instant(now) < instant(row["until_at"]):
            return None
        body = json.loads(row["body"])
        # Only acceptance uses the five-minute response freshness rule. A
        # retained target is executable until its declared review/expiry, with
        # NEW account, quotes, liquidity and risk checks before every order.
        weights = validate_decision(body, instant(row["accepted_at"]), universe, limits)
        return {**row, "decision": body, "weights": weights}

    def accept(self, body, now, universe, limits, min_review=None):
        validate_decision(body, instant(now), universe, limits)
        key = uuid4().hex
        review = instant(body["next_review_at"])
        if min_review is not None:
            review = max(review, instant(now) + min_review)
        until = min(instant(body["expires_at"]), review)
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE fund_decisions SET state='superseded' WHERE state='active'")
            db.execute("UPDATE fund_approvals SET state='superseded' WHERE state IN ('pending','approved')")
            db.execute("INSERT INTO fund_decisions VALUES(?,?,?,?,?,?)",
                       (key, str(now), until.isoformat(), encode(body), self.digest, "active"))
            # The newest AI decision replaces every standing level and alert, except a
            # protective exit already in progress: it completes first, and the new
            # decision's levels for that symbol take effect when it does.
            exiting = {row[0] for row in db.execute("SELECT symbol FROM fund_levels WHERE state='triggered'")}
            db.execute("DELETE FROM fund_levels WHERE state!='triggered'")
            db.execute("DELETE FROM fund_deferred_levels")
            for symbol, levels in decision_levels(body).items():
                if symbol in exiting:
                    db.execute("INSERT INTO fund_deferred_levels VALUES(?,?,?,?)", (symbol, key, encode(levels), str(now)))
                else:
                    db.execute("INSERT INTO fund_levels VALUES(?,?,?,?,?)", (symbol, key, encode(levels), "active", str(now)))
            db.execute("UPDATE fund_alerts SET state='superseded' WHERE state='active'")
            for alert in body.get("alerts") or []:
                db.execute("INSERT INTO fund_alerts(decision_id,symbol,body,state,set_at) VALUES(?,?,?,?,?)",
                           (key, alert["symbol"], encode(alert), "active", str(now)))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                       (str(now), "decision_accepted", encode({"id": key, "until_at": until, "decision": body})))
        return key

    def complete(self, key, now):
        with self.journal.connection() as db:
            db.execute("UPDATE fund_decisions SET state='complete' WHERE id=? AND state='active'", (key,))
        self.journal.event(now, "allocation_complete", {"decision_id": key})

    def levels(self):
        """Standing levels; a triggered level stays until its protective exit completes."""
        with self.journal.connection() as db:
            return {row["symbol"]: {**json.loads(row["body"]), "decision_id": row["decision_id"], "state": row["state"]}
                    for row in db.execute("SELECT * FROM fund_levels WHERE state IN ('active','triggered')")}

    def set_protective_target(self, symbol, weight):
        with self.journal.connection() as db:
            row = db.execute("SELECT body FROM fund_levels WHERE symbol=?", (symbol,)).fetchone()
            if row:
                body = {**json.loads(row[0]), "protective_target": str(weight)}
                db.execute("UPDATE fund_levels SET body=? WHERE symbol=?", (encode(body), symbol))

    def finish_level(self, symbol):
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE fund_levels SET state='done' WHERE symbol=? AND state='triggered'", (symbol,))
            deferred = db.execute("SELECT * FROM fund_deferred_levels WHERE symbol=?", (symbol,)).fetchone()
            if deferred:
                db.execute("INSERT OR REPLACE INTO fund_levels VALUES(?,?,?,?,?)", (
                    symbol, deferred["decision_id"], deferred["body"], "active", deferred["set_at"]))
                db.execute("DELETE FROM fund_deferred_levels WHERE symbol=?", (symbol,))

    def exiting(self):
        """Symbols with a protective exit in progress: no purchase until it completes."""
        with self.journal.connection() as db:
            return {row[0] for row in db.execute("SELECT symbol FROM fund_levels WHERE state='triggered'")}

    def trigger_level(self, symbol, kind, now, detail):
        with self.journal.connection() as db:
            changed = db.execute("UPDATE fund_levels SET state='triggered' WHERE symbol=? AND state='active'",
                                 (symbol,)).rowcount
            if changed:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "protective_trigger", encode({
                    "symbol": symbol, "trigger": kind, **detail})))
        return bool(changed)

    def alerts(self):
        with self.journal.connection() as db:
            return [{**json.loads(row["body"]), "id": row["id"], "decision_id": row["decision_id"]}
                    for row in db.execute("SELECT * FROM fund_alerts WHERE state='active' ORDER BY id")]

    def trigger_alert(self, alert_id, now, detail):
        with self.journal.connection() as db:
            changed = db.execute("UPDATE fund_alerts SET state='triggered' WHERE id=? AND state='active'",
                                 (alert_id,)).rowcount
            if changed:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "price_alert", encode(detail)))
        return bool(changed)

    def override(self, decision_id, symbol, max_weight, reason, now):
        with self.journal.connection() as db:
            old = db.execute("SELECT max_weight FROM fund_overrides WHERE decision_id=? AND symbol=?",
                             (decision_id, symbol)).fetchone()
            value = min(number(max_weight), number(old[0])) if old else number(max_weight)
            db.execute("INSERT OR REPLACE INTO fund_overrides VALUES(?,?,?,?,?)",
                       (decision_id, symbol, str(value), reason, str(now)))

    def overrides(self, decision_id):
        with self.journal.connection() as db:
            return {row["symbol"]: number(row["max_weight"]) for row in
                    db.execute("SELECT symbol,max_weight FROM fund_overrides WHERE decision_id=?", (decision_id,))}

    def reference(self, decision_id, symbol, price, now):
        """First observed price of a symbol under a decision (for move-triggered reviews)."""
        with self.journal.connection() as db:
            db.execute("INSERT OR IGNORE INTO fund_refs VALUES(?,?,?,?)", (decision_id, symbol, str(price), str(now)))
            return number(db.execute("SELECT price FROM fund_refs WHERE decision_id=? AND symbol=?",
                                     (decision_id, symbol)).fetchone()[0])

    # Execution-quality memory: arrival price and submission time per order.
    def record_submission(self, intent_id, decision_id, intent, now):
        with self.journal.connection() as db:
            db.execute("INSERT OR REPLACE INTO fund_orders VALUES(?,?,?,?,?,?)", (
                intent_id, decision_id, intent.symbol, intent.side, str(now), str(intent.limit)))

    def mark_protective(self, intent_id, now):
        with self.journal.connection() as db:
            db.execute("INSERT OR IGNORE INTO fund_protective_orders VALUES(?,?)", (intent_id, str(now)))

    def is_protective(self, intent_id):
        with self.journal.connection() as db:
            return db.execute("SELECT 1 FROM fund_protective_orders WHERE intent_id=?", (intent_id,)).fetchone() is not None

    def submitted_at(self, intent_id):
        with self.journal.connection() as db:
            row = db.execute("SELECT submitted_at FROM fund_orders WHERE intent_id=?", (intent_id,)).fetchone()
        return instant(row[0]) if row else None

    def arrival(self, decision_id, symbol, side):
        with self.journal.connection() as db:
            row = db.execute("SELECT reference FROM fund_orders WHERE decision_id=? AND symbol=? AND side=? "
                             "ORDER BY rowid LIMIT 1", (decision_id, symbol, side)).fetchone()
        return number(row[0]) if row else None

    @staticmethod
    def covers(body, intent, now, active, policy_digest, requirement):
        """Does an approved plan envelope cover this exact purchase?"""
        buys = body.get("buys", {})
        scope = buys.get(intent.symbol)
        return bool(
            body.get("decision_id") == active["id"] and body.get("policy") == policy_digest and
            body.get("day") == trading_day(now) and
            instant(body["created_at"]) <= instant(now) < instant(body["expires_at"]) and
            intent.side == "buy" and scope is not None and 0 < intent.quantity and
            number(intent.limit) <= number(scope["max_limit"]) and
            number(requirement["weight_after"]) <= number(scope["max_weight"]) + TOLERANCE and
            number(requirement["exposure_after"]) <= number(body["max_exposure_after"]) + TOLERANCE and
            number(requirement["buy_turnover_after"]) <= number(body["max_buy_turnover_usd"]) and
            set(requirement["reasons"]) <= set(body["reasons"]))

    def approved(self, active, intent, now, requirement, db=None):
        if db is None:
            with self.journal.connection() as connection:
                return self.approved(active, intent, now, requirement, connection)
        binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
        rows = db.execute("SELECT * FROM fund_approvals WHERE decision_id=? AND state='approved' ORDER BY rowid DESC",
                          (active["id"],)).fetchall()
        for row in rows:
            body = json.loads(row["body"])
            if (row["digest"] == digest(body) and body.get("binding") == binding and row["resolved_at"] and
                    instant(row["resolved_at"]) <= instant(now) and
                    self.covers(body, intent, now, active, self.digest, requirement)):
                return dict(row)
        return None

    def request(self, active, envelope, now):
        """Commit (or reuse) one immutable plan request; never reserves or sends orders."""
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM fund_approvals WHERE decision_id=? AND state IN ('pending','approved') "
                             "ORDER BY rowid DESC", (active["id"],)).fetchall()
            for row in old:
                body = json.loads(row["body"])
                if (body.get("buys") is not None and instant(now) < instant(body["expires_at"]) and
                        body.get("day") == trading_day(now) and set(envelope["reasons"]) <= set(body["reasons"]) and
                        # covers() also checks these: reusing a plan that cannot cover would stall silently.
                        number(envelope["max_exposure_after"]) <= number(body["max_exposure_after"]) + TOLERANCE and
                        number(envelope["max_buy_turnover_usd"]) <= number(body["max_buy_turnover_usd"]) and
                        all(symbol in body["buys"] and
                            number(scope["max_weight"]) <= number(body["buys"][symbol]["max_weight"]) + TOLERANCE and
                            number(scope["reference_limit"]) <= number(body["buys"][symbol]["max_limit"])
                            for symbol, scope in envelope["buys"].items())):
                    return dict(row)
                db.execute("UPDATE fund_approvals SET state='superseded' WHERE id=?", (row["id"],))
            binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
            key = uuid4().hex
            from .market_data import SessionClock
            close = instant(SessionClock(now=lambda: instant(now)).status()["close"])
            expires = min(instant(active["until_at"]), instant(now) + timedelta(minutes=self.policy.valid_minutes), close)
            collar = 1 + number(self.policy.price_collar)
            buys = {symbol: {**scope, "max_limit": str((number(scope["reference_limit"]) * collar).quantize(number("0.01")))}
                    for symbol, scope in envelope["buys"].items()}
            body = {"id": key, "binding": binding, "decision_id": active["id"], "policy": self.digest,
                    "day": trading_day(now), "created_at": instant(now).isoformat(), "expires_at": expires.isoformat(),
                    "kind": "plan", **envelope, "buys": buys,
                    "target_portfolio": active["decision"]["targets"], "rationale": active["decision"]["rationale"]}
            checksum = digest(body)
            db.execute("INSERT INTO fund_approvals VALUES(?,?,?,?,?,NULL)", (key, active["id"], encode(body), checksum, "pending"))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                "kind": "approval_required", "details": {
                    "id": key, "digest": checksum, "kind": "plan", "expires_at": body["expires_at"],
                    "reasons": body["reasons"], "buys": buys, "observed_equity": body["observed_equity"],
                    "start_exposure": body["start_exposure"], "current_exposure": body["current_exposure"],
                    "max_exposure_after": body["max_exposure_after"],
                    "max_buy_turnover_usd": body["max_buy_turnover_usd"],
                    "auto_envelope": body["auto_envelope"], "stress": body.get("stress"),
                    "risk": body.get("risk"), "target_portfolio": body["target_portfolio"],
                    "rationale": body["rationale"]}})))
            return {"id": key, "body": encode(body), "digest": checksum, "state": "pending"}

    def guard(self, active, intent, now, requirement, universe, limits):
        """Runs INSIDE the durable order reservation's transaction."""
        def check(db):
            current = self.active(now, universe, limits, db)
            if not current or current["id"] != active["id"]:
                raise RiskError("Target allocation changed or expired before reservation")
            if requirement is None:
                return
            row = self.approved(current, intent, now, requirement, db)
            if row is None:
                raise ApprovalPending("Purchase beyond the automatic risk envelope waits for plan approval")
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "approval_used", encode({
                "approval_id": row["id"], "symbol": intent.symbol, "quantity": intent.quantity,
                "limit": str(intent.limit), **{k: str(v) for k, v in requirement.items() if k != "reasons"},
                "reasons": requirement["reasons"]})))
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


def approval_code(key):
    """Short, human-typable reference shown in Telegram (first 6 hex digits)."""
    return str(key)[:6].upper()


def resolve_approval(root, key, checksum, approve, now, authority="local_operator_cli"):
    """Operator CLI or the authenticated Telegram reply channel; never an agent tool."""
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
            "id": key, "digest": checksum, "state": state, "authority": authority})))
    return state
