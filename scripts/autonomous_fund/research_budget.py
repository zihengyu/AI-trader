"""Atomic, append-only resource reservations; never controls protective trading.

USD amounts are estimates, not provider bills. Unknown outcomes keep their full
reservation, including across a restart. Actual overruns are recorded, not hidden.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4

from .journal import encode
from .risk import RiskError, instant, number, trading_day

CATEGORIES = {"research_model", "decision_model", "validation_compute", "data"}


class ResearchBudgetExhausted(RiskError):
    """Optional research waits; account supervision and protective exits continue."""


@dataclass(frozen=True)
class ResearchBudgetSettings:
    daily_usd: str = "2"
    monthly_usd: str = "30"
    project_usd: str = "10"
    model_reservation_usd: str = "0.50"
    validation_reservation_usd: str = "0.05"

    def __post_init__(self):
        for field in self.__dataclass_fields__:
            value = number(getattr(self, field))
            if not Decimal("0") < value <= Decimal("100000"):
                raise RiskError(f"research_budget.{field} must be positive and bounded")
            object.__setattr__(self, field, str(value))

    @classmethod
    def from_config(cls, raw):
        raw = dict(raw or {})
        if set(raw) - cls.__dataclass_fields__.keys():
            raise RiskError("Unknown research budget setting")
        return cls(**raw)


def micros(value):
    amount = number(value)
    if amount < 0:
        raise RiskError("Negative research resource charge")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class ResearchBudget:
    def __init__(self, journal, settings=None):
        self.journal = journal
        self.settings = settings or ResearchBudgetSettings()
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_research_reservations (
                    id TEXT PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
                    project TEXT NOT NULL, category TEXT NOT NULL, reserved INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_research_charges (
                    id TEXT PRIMARY KEY, at TEXT NOT NULL, actual INTEGER NOT NULL, receipt TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS fund_research_budget_day ON fund_research_reservations(day);
            """)

    @staticmethod
    def _spent(db, where="1", args=()):
        return db.execute(
            "SELECT coalesce(sum(coalesce(c.actual,r.reserved)),0) "
            "FROM fund_research_reservations r LEFT JOIN fund_research_charges c ON r.id=c.id WHERE " + where,
            args).fetchone()[0]

    def reserve(self, now, project, category, amount):
        # No separate check-then-write: different workers share the same envelope.
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.reserve_in(db, now, project, category, amount)

    def reserve_in(self, db, now, project, category, amount):
        """Reserve inside the caller's write transaction, atomically with its job lease."""
        selected = self.reserve_first_in(db, now, (project,), category, amount)
        return selected[1] if selected is not None else None

    def reserve_first_in(self, db, now, projects, category, amount):
        """Select the first affordable project in caller-supplied priority order."""
        if not db.in_transaction:
            raise RiskError("Research reservations require a write transaction")
        projects = tuple(projects)
        if category not in CATEGORIES or any(not isinstance(project, str) or not 0 < len(project) <= 120
                                             for project in projects):
            raise RiskError("Invalid research resource owner or category")
        day, value = trading_day(instant(now)), micros(amount)
        if not value:
            raise RiskError("A resource reservation must be positive")
        month = day[:7]
        if (not projects or self._spent(db, "r.day=?", (day,)) + value > micros(self.settings.daily_usd)
                or self._spent(db, "r.month=?", (month,)) + value > micros(self.settings.monthly_usd)):
            return None
        project_cap = micros(self.settings.project_usd)
        for project in projects:
            if self._spent(db, "r.project=?", (project,)) + value <= project_cap:
                key = uuid4().hex
                db.execute("INSERT INTO fund_research_reservations VALUES(?,?,?,?,?,?,?)",
                           (key, instant(now).isoformat(), day, month, project, category, value))
                return project, key
        return None

    def settle(self, key, now, actual, receipt):
        """Exactly once. None deliberately leaves unknown work reserved."""
        if actual is None:
            return
        value = micros(actual)
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT at FROM fund_research_reservations WHERE id=?", (key,)).fetchone()
            if row is None or instant(now) < instant(row[0]):
                raise RiskError("Unknown or future research reservation")
            old = db.execute("SELECT actual,receipt FROM fund_research_charges WHERE id=?", (key,)).fetchone()
            body = encode(receipt)
            if old:
                if tuple(old) != (value, body):
                    raise RiskError("Research charge cannot be rewritten")
                return
            db.execute("INSERT INTO fund_research_charges VALUES(?,?,?,?)",
                       (key, instant(now).isoformat(), value, body))

    def summary(self, now):
        now = instant(now)
        day = trading_day(now)
        daily = monthly = pending = 0
        categories, projects, confirmed = {}, {}, {}
        with self.journal.connection() as db:
            rows = db.execute("SELECT r.at,r.day,r.month,r.category,r.project,r.reserved,c.at,c.actual "
                              "FROM fund_research_reservations r LEFT JOIN fund_research_charges c ON r.id=c.id "
                              "WHERE julianday(r.at)<=julianday(?)", (now.isoformat(),))
            for at, reserved_day, month, category, project, reservation, settled_at, actual in rows:
                if instant(at) > now:
                    continue
                settled = settled_at is not None and instant(settled_at) <= now
                amount = actual if settled else reservation
                daily += amount if reserved_day == day else 0
                if month == day[:7]:
                    monthly += amount
                    categories[category] = categories.get(category, 0) + amount
                projects[project] = projects.get(project, 0) + amount
                if settled:
                    confirmed[project] = confirmed.get(project, 0) + actual
                else:
                    pending += 1
            operational = db.execute("SELECT coalesce(sum(json_extract(body,'$.cost_usd')),0), "
                                     "sum(CASE WHEN json_extract(body,'$.cost_usd') IS NULL AND "
                                     "coalesce(json_extract(body,'$.cache_hit'),0)=0 AND "
                                     "coalesce(json_extract(body,'$.mock'),0)=0 THEN 1 ELSE 0 END) FROM events "
                                     "WHERE kind='agent' AND json_extract(body,'$.name')!='researcher' "
                                     "AND julianday(at)<=julianday(?)", (now.isoformat(),)).fetchone()
        return {"daily_committed_usd": daily / 1e6, "monthly_committed_usd": monthly / 1e6,
                "daily_limit_usd": self.settings.daily_usd, "monthly_limit_usd": self.settings.monthly_usd,
                "monthly_by_category": {key: value / 1e6 for key, value in categories.items()},
                "unknown_or_running_reservations": pending,
                "lifetime_by_project_usd": {key: value / 1e6 for key, value in projects.items()},
                "known_lifetime_by_project_usd": {key: value / 1e6 for key, value in confirmed.items()},
                "operational_models": {"known_cost_usd": operational[0], "unpriced_calls": operational[1] or 0,
                                       "budget_owner": "existing model_budget; outside research resource envelope"},
                "meaning": "Estimated resource commitments; unknown usage stays reserved. Not a provider bill."}
