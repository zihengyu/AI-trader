"""Durable open-strategy research workflow; validation never grants broker access.

Uses the fund journal and existing experiment registry. Candidate definitions,
worker inputs/results and forward receipts are append-only. Only controller code
changes validation/admission states; researcher updates affect research tasks.
"""
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .experiments import Registry
from .journal import encode
from .research_budget import ResearchBudget, ResearchBudgetSettings
from .risk import RiskError, instant, number, trading_day
from .strategy_definition import ResearchTaskUpdate, StrategyLabSettings, StrategyProposal
from .strategy_program import validate_program
from .strategy_validation import check_dataset, digest, implementation_hash


class StrategyLab:
    def __init__(self, journal, policy, settings=None, budget_settings=None):
        self.journal, self.policy = journal, copy.deepcopy(policy)
        self.settings = settings or StrategyLabSettings()
        self.policy["research_execution"] = {"initial_capital": self.settings.initial_capital_usd,
                                            "source_hash": implementation_hash()}
        self.budget = ResearchBudget(journal, budget_settings or ResearchBudgetSettings())
        self._executor, self._future = None, None
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_strategy_versions (
                    id TEXT NOT NULL, version TEXT NOT NULL, created_at TEXT NOT NULL,
                    proposer TEXT NOT NULL, proposal TEXT NOT NULL, digest TEXT NOT NULL,
                    policy TEXT NOT NULL, policy_hash TEXT NOT NULL, state TEXT NOT NULL,
                    task_state TEXT NOT NULL, PRIMARY KEY(id,version));
                CREATE TABLE IF NOT EXISTS fund_strategy_events (
                    sequence INTEGER PRIMARY KEY, id TEXT NOT NULL, version TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_strategy_datasets (
                    digest TEXT PRIMARY KEY, at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_strategy_jobs (
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, dataset TEXT NOT NULL,
                    state TEXT NOT NULL, lease TEXT, lease_until TEXT, reservation TEXT,
                    UNIQUE(strategy_id,version,kind,dataset));
                CREATE TABLE IF NOT EXISTS fund_strategy_results (
                    job_id TEXT PRIMARY KEY, at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_strategy_forward (
                    id TEXT NOT NULL, version TEXT NOT NULL, day TEXT NOT NULL,
                    at TEXT NOT NULL, dataset TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(id,version,day));
                CREATE TABLE IF NOT EXISTS fund_strategy_cohorts (
                    cutoff TEXT PRIMARY KEY, opened_at TEXT NOT NULL, members TEXT NOT NULL);
            """)
        journal.set_meta("strategy_lab_policy", {**asdict(self.settings), "policy_hash": digest(self.policy)})

    @staticmethod
    def _event(db, key, version, now, kind, body):
        db.execute("INSERT INTO fund_strategy_events(id,version,at,kind,body) VALUES(?,?,?,?,?)",
                   (key, version, instant(now).isoformat(), kind, encode(body)))

    def submit(self, proposal, now, proposer="researcher"):
        body = StrategyProposal.model_validate(proposal).model_dump()
        key, version = body["strategy_id"], body["version"]
        fingerprint, policy_hash = digest(body), digest(self.policy)
        feasibility_error = None
        try:
            state = "needs_formalization" if body["implementation"] is None else validate_program(
                body["implementation"], body["instruments"])
        except (RiskError, ValueError) as exc:
            # Retain the complete hypothesis/version when its proposed program
            # cannot yet be compiled; a syntax error is not worthless research.
            state, feasibility_error = "needs_formalization", str(exc)[:500]
        if state == "ready":
            state = "candidate"
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest,policy_hash FROM fund_strategy_versions WHERE id=? AND version=?",
                             (key, version)).fetchone()
            if old:
                if old[0] != fingerprint or old[1] != policy_hash:
                    raise RiskError("A strategy version or its execution policy cannot be rewritten")
                return key, version
            if db.execute("SELECT count(*) FROM fund_strategy_versions WHERE state NOT IN ('rejected','retired','paused') "
                          "AND task_state!='retired'").fetchone()[0] >= self.settings.max_candidates:
                raise RiskError("Strategy research capacity reached; existing evidence is retained")
            parent = body.get("parent_version")
            previous = db.execute("SELECT version FROM fund_strategy_versions WHERE id=?", (key,)).fetchall()
            if previous and (not parent or parent not in {r[0] for r in previous}) or not previous and parent:
                raise RiskError("A strategy revision needs an existing parent version")
            if previous and body["change_type"] == "new":
                raise RiskError("A changed strategy must identify its revision type")
            db.execute("INSERT INTO fund_strategy_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key, version, instant(now).isoformat(), proposer, encode(body), fingerprint,
                        encode(self.policy), policy_hash, state, "open"))
            self._event(db, key, version, now, "registered", {"state": state, "digest": fingerprint,
                                                            "feasibility_error": feasibility_error})
        self.sync_registry(now)
        return key, version

    def update_task(self, update, now):
        body = ResearchTaskUpdate.model_validate(update).model_dump()
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM fund_strategy_versions WHERE id=? AND version=?",
                              (body["strategy_id"], body["version"])).fetchone():
                raise RiskError("Research task names an unknown strategy version")
            task_state = {"continue": "open", "pause": "paused", "retire": "retired", "request_data": "open"}[body["action"]]
            db.execute("UPDATE fund_strategy_versions SET task_state=? WHERE id=? AND version=?",
                       (task_state, body["strategy_id"], body["version"]))
            # A researcher cannot promote, revoke account permissions or modify
            # a production implementation by submitting a task update.
            self._event(db, body["strategy_id"], body["version"], now, "research_task", body)

    def rows(self):
        with self.journal.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM fund_strategy_versions ORDER BY created_at,rowid")]

    def record_dataset(self, data, now):
        if sum(len(rows) for collection in ("bars", "features") for rows in data.get(collection, {}).values()) > self.settings.max_history_rows:
            raise RiskError("Strategy dataset exceeds the configured resource bound")
        fingerprint = check_dataset(data)
        if any(instant(r["at"]) > instant(now) or instant(r["available_at"]) > instant(now)
               for name in ("bars", "features") for rows in data.get(name, {}).values() for r in rows):
            raise RiskError("A research snapshot contains observations from the future")
        if any(instant(r["available_at"]) > instant(now) for r in data.get("actions", [])):
            raise RiskError("A corporate-action publication receipt is from the future")
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM fund_strategy_datasets WHERE digest=?", (fingerprint,)).fetchone():
                used = db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_datasets").fetchone()[0]
                used += db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_results").fetchone()[0]
                if used + len(encode(data).encode()) > self.settings.max_artifact_bytes:
                    raise RiskError("Strategy research artifact allowance is exhausted; audit data retained")
            db.execute("INSERT OR IGNORE INTO fund_strategy_datasets VALUES(?,?,?)",
                       (fingerprint, instant(now).isoformat(), encode(data)))
        return fingerprint

    def schedule(self, data, now):
        fingerprint = self.record_dataset(data, now)
        created = []
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in db.execute("SELECT * FROM fund_strategy_versions WHERE task_state='open' "
                                               "OR state IN ('shadow','paper_active')")]
            cutoff = max(r["day"] for values in data["bars"].values() for r in values)
            cohort = db.execute("SELECT opened_at,members FROM fund_strategy_cohorts ORDER BY cutoff DESC LIMIT 1").fetchone()
            if cohort is None:
                members = [[r["id"], r["version"]] for r in rows]
                db.execute("INSERT INTO fund_strategy_cohorts VALUES(?,?,?)",
                           (cutoff, instant(now).isoformat(), encode(members)))
            else:
                members = json.loads(cohort[1])
            for row in rows:
                if row["policy_hash"] != digest(self.policy):
                    # Historical evidence keeps its policy. It cannot authorize
                    # a strategy under a different account risk configuration.
                    continue
                body = json.loads(row["proposal"])
                if set(body["instruments"] + [body["validation"]["benchmark"]]) - data["bars"].keys():
                    continue
                if row["state"] in {"candidate", "needs_data", "awaiting_new_holdout"}:
                    kind = "historical"
                    if [row["id"], row["version"]] not in members:
                        previous_cutoff = db.execute("SELECT max(cutoff) FROM fund_strategy_cohorts").fetchone()[0]
                        days = [r["day"] for r in data["bars"][body["validation"]["benchmark"]]]
                        size = body["validation"]["final_sessions"]
                        if len(days) < size or days[-size] <= previous_cutoff:
                            db.execute("UPDATE fund_strategy_versions SET state='awaiting_new_holdout' WHERE id=? AND version=?",
                                       (row["id"], row["version"]))
                            kind = "exploratory"
                        else:
                            members = [[r["id"], r["version"]] for r in rows]
                            db.execute("INSERT OR IGNORE INTO fund_strategy_cohorts VALUES(?,?,?)",
                                       (cutoff, instant(now).isoformat(), encode(members)))
                elif row["state"] in {"shadow", "paper_active"}:
                    kind = "forward"
                    passed = db.execute("SELECT at FROM fund_strategy_events WHERE id=? AND version=? "
                                        "AND kind='historical_pass' ORDER BY sequence LIMIT 1",
                                        (row["id"], row["version"])).fetchone()
                    if not passed or not any(instant(r["open_at"]) > instant(passed[0])
                                              for r in data["bars"][body["validation"]["benchmark"]]):
                        continue
                else:
                    continue
                key = uuid4().hex
                result = db.execute("INSERT OR IGNORE INTO fund_strategy_jobs VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                                    (key, row["id"], row["version"], instant(now).isoformat(), kind, fingerprint, "queued"))
                if result.rowcount:
                    db.execute("UPDATE fund_strategy_jobs SET state='superseded' WHERE strategy_id=? AND version=? "
                               "AND kind=? AND state='queued' AND id!=?",
                               (row["id"], row["version"], kind, key))
                    created.append(key)
        return created

    def _claim(self, now):
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            day = trading_day(now)
            attempted = db.execute("SELECT count(*) FROM fund_research_reservations WHERE day=? "
                                   "AND category='validation_compute'", (day,)).fetchone()[0]
            if attempted >= self.settings.max_jobs_per_day:
                return None
            historical_attempts = db.execute("SELECT count(*) FROM fund_strategy_jobs j JOIN "
                                             "fund_research_reservations r ON r.id=j.reservation "
                                             "WHERE r.day=? AND j.kind IN ('historical','exploratory')", (day,)).fetchone()[0]
            prefer_historical = attempted > 0 and historical_attempts == 0
            row = db.execute("SELECT j.* FROM fund_strategy_jobs j JOIN fund_strategy_versions v ON "
                             "v.id=j.strategy_id AND v.version=j.version WHERE v.policy_hash=? AND (v.task_state='open' OR "
                             "v.state IN ('shadow','paper_active')) AND "
                             "((j.kind IN ('historical','exploratory') AND v.state IN ('candidate','needs_data','awaiting_new_holdout')) OR "
                             "(j.kind='forward' AND v.state IN ('shadow','paper_active'))) AND "
                             "NOT EXISTS(SELECT 1 FROM fund_strategy_jobs other WHERE other.strategy_id=j.strategy_id "
                             "AND other.version=j.version AND other.id!=j.id AND other.state='running' AND "
                             "julianday(other.lease_until)>=julianday(?)) AND "
                             "(j.state='queued' OR (j.state='running' AND julianday(j.lease_until)<julianday(?))) "
                             "ORDER BY (j.kind=? OR (?='historical' AND j.kind='exploratory')) DESC,coalesce((SELECT max(r.at) FROM fund_strategy_results r "
                             "JOIN fund_strategy_jobs done ON done.id=r.job_id WHERE done.strategy_id=j.strategy_id "
                             "AND done.version=j.version),'0000'),j.at,j.rowid LIMIT 1",
                             (digest(self.policy), instant(now).isoformat(), instant(now).isoformat(),
                              "historical" if prefer_historical else "forward",
                              "historical" if prefer_historical else "forward")).fetchone()
            if row is None:
                return None
            reservation = self.budget.reserve_in(db, now, row["strategy_id"], "validation_compute",
                                                  self.budget.settings.validation_reservation_usd)
            if reservation is None:
                return None
            lease = uuid4().hex
            until = instant(now) + timedelta(seconds=self.settings.job_timeout_seconds * 2 + 30)
            db.execute("UPDATE fund_strategy_jobs SET state='running',lease=?,lease_until=?,reservation=? WHERE id=?",
                       (lease, until.isoformat(), reservation, row["id"]))
            return {**dict(row), "lease": lease, "reservation": reservation}

    def _forward_input(self, db, row, dataset):
        records = db.execute("SELECT body FROM fund_strategy_forward WHERE id=? AND version=? ORDER BY day",
                             (row["id"], row["version"])).fetchall()
        passed = db.execute("SELECT e.at FROM fund_strategy_events e WHERE id=? AND version=? AND kind='historical_pass' "
                            "ORDER BY sequence LIMIT 1", (row["id"], row["version"])).fetchone()
        if not passed:
            raise RiskError("Forward research requires a frozen historical validation record")
        historical = db.execute("SELECT d.body FROM fund_strategy_jobs j JOIN fund_strategy_datasets d ON j.dataset=d.digest "
                                "JOIN fund_strategy_results r ON r.job_id=j.id WHERE j.strategy_id=? AND j.version=? "
                                "AND j.kind='historical' ORDER BY r.at DESC LIMIT 1", (row["id"], row["version"])).fetchone()
        seed = json.loads(historical[0])
        if records:
            # Stored forward inputs, not today's corrected copy of old history.
            frozen = json.loads(records[-1][0])["frozen_dataset_hash"]
            seed = json.loads(db.execute("SELECT body FROM fund_strategy_datasets WHERE digest=?", (frozen,)).fetchone()[0])
        for symbol, values in dataset["bars"].items():
            known = {r["day"] for r in seed["bars"].get(symbol, [])}
            last = max(known) if known else ""
            seed["bars"].setdefault(symbol, []).extend(r for r in values if r["day"] > last)
        for symbol, values in dataset.get("features", {}).items():
            known = seed.setdefault("features", {}).setdefault(symbol, [])
            last = instant(known[-1]["at"]) if known else None
            known.extend(r for r in values if last is None or instant(r["at"]) > last)
        existing = {digest(a) for a in seed.get("actions", [])}
        seed.setdefault("actions", []).extend(a for a in dataset.get("actions", []) if digest(a) not in existing)
        # Coverage cannot become true merely because a later snapshot asserts it.
        seed["coverage"] = {k: v is True and dataset.get("coverage", {}).get(k) is True
                            for k, v in seed.get("coverage", {}).items()}
        return seed, passed[0], [json.loads(r[0])["receipt"] for r in records]

    def run_one(self, now):
        now = instant(now)
        if (self.journal.root / "STOP").exists():
            return None
        job = self._claim(now)
        if job is None:
            return None
        reservation = job["reservation"]
        started = monotonic()
        with self.journal.connection() as db:
            row = dict(db.execute("SELECT * FROM fund_strategy_versions WHERE id=? AND version=?",
                                  (job["strategy_id"], job["version"])).fetchone())
            data = json.loads(db.execute("SELECT body FROM fund_strategy_datasets WHERE digest=?", (job["dataset"],)).fetchone()[0])
            trials = db.execute("SELECT count(*) FROM fund_strategy_versions").fetchone()[0]
            forward_at, prior = None, []
            if job["kind"] == "forward":
                data, forward_at, prior = self._forward_input(db, row, data)
        frozen_policy = json.loads(row["policy"])
        payload = {"proposal": json.loads(row["proposal"]), "data": data, "policy": frozen_policy,
                   "initial_capital": frozen_policy["research_execution"]["initial_capital"], "trials": trials,
                   "kind": job["kind"], "forward_after": forward_at,
                   "timeout_seconds": self.settings.job_timeout_seconds}
        try:
            result = run_worker(payload, self.settings.job_timeout_seconds)
        except Exception as exc:
            result = {"status": "worker_error", "passed": False, "error": type(exc).__name__}
        finished = now + timedelta(seconds=max(0, monotonic() - started))
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT lease,state FROM fund_strategy_jobs WHERE id=?", (job["id"],)).fetchone()
            if tuple(current) != (job["lease"], "running"):
                return None  # An expired worker may not overwrite a newer claim.
            used = db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_datasets").fetchone()[0]
            used += db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_results").fetchone()[0]
            extra_data = len(encode(data).encode()) if job["kind"] == "forward" and not db.execute(
                "SELECT 1 FROM fund_strategy_datasets WHERE digest=?", (digest(data),)).fetchone() else 0
            if used + extra_data + len(encode(result).encode()) > self.settings.max_artifact_bytes:
                result = {"status": "worker_error", "passed": False, "error": "ArtifactBudgetExceeded"}
            if job["kind"] == "forward" and result.get("series"):
                series = result["series"]
                if series[:len(prior)] != prior:
                    result = {"status": "needs_data", "passed": False, "reason": "Forward history changed; receipts retained"}
                else:
                    frozen = digest(data)
                    db.execute("INSERT OR IGNORE INTO fund_strategy_datasets VALUES(?,?,?)",
                               (frozen, finished.isoformat(), encode(data)))
                    for receipt in series[len(prior):]:
                        db.execute("INSERT INTO fund_strategy_forward VALUES(?,?,?,?,?,?)",
                                   (row["id"], row["version"], receipt["day"], finished.isoformat(), job["dataset"],
                                    encode({"receipt": receipt, "frozen_dataset_hash": frozen})))
            if result.get("status") == "worker_error":
                # Technical failures are not negative investment evidence and
                # cannot permanently consume an unopened experiment. Preserve
                # each failed lease, retry the same immutable job under budgets.
                db.execute("UPDATE fund_strategy_jobs SET state='queued',lease=NULL,lease_until=NULL WHERE id=?", (job["id"],))
                self._event(db, row["id"], row["version"], finished, "worker_failure",
                            {"job_id": job["id"], "lease": job["lease"], "result": result})
            else:
                db.execute("INSERT INTO fund_strategy_results VALUES(?,?,?)", (job["id"], finished.isoformat(), encode(result)))
                db.execute("UPDATE fund_strategy_jobs SET state='finished' WHERE id=?", (job["id"],))
            state = row["state"]
            if job["kind"] in {"historical", "exploratory"} and result.get("status") != "worker_error":
                state = "shadow" if result.get("passed") else (
                    "needs_data" if result.get("status") == "worker_error" else result.get("status", "rejected"))
            elif result.get("degraded"):
                state = "paused"
            elif result.get("passed") and self.settings.admission == "paper_library" and (
                    result.get("observations", 0) >= self.settings.min_forward_sessions):
                state = "paper_active"
            db.execute("UPDATE fund_strategy_versions SET state=? WHERE id=? AND version=?",
                       (state, row["id"], row["version"]))
            self._event(db, row["id"], row["version"], finished,
                        "historical_pass" if job["kind"] == "historical" and result.get("passed") else "validation",
                        {"job_id": job["id"], "state": state, "status": result.get("status")})
        # This is a configured compute allowance, not a measured cloud bill.
        self.budget.settle(reservation, finished, self.budget.settings.validation_reservation_usd,
                           {"job_id": job["id"], "wall_seconds": round(monotonic() - started, 6),
                            "basis": "configured per-job compute allowance"})
        self.sync_registry(finished)
        return result

    def kick(self, now):
        if self._future is not None and not self._future.done():
            return False
        if self._future is not None:
            # Surface errors instead of silently abandoning a background task.
            previous, self._future = self._future, None
            previous.result()
        with self.journal.connection() as db:
            if not db.execute("SELECT 1 FROM fund_strategy_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
                return False
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-lab")
        self._future = self._executor.submit(self._drain, instant(now))
        return True

    def _drain(self, now):
        for _ in range(self.settings.max_jobs_per_day):
            if self.run_one(now) is None:
                break

    def context(self, now, *, detail_limit=4, strategy_id=None, version=None):
        candidates = []
        rows = [row for row in self.rows() if instant(row["created_at"]) <= instant(now)
                and (strategy_id is None or row["id"] == strategy_id) and (version is None or row["version"] == version)]
        with self.journal.connection() as db:
            for index, row in enumerate(reversed(rows[-20:])):
                result_row = db.execute("SELECT r.body FROM fund_strategy_results r JOIN fund_strategy_jobs j ON "
                                        "r.job_id=j.id WHERE j.strategy_id=? AND j.version=? AND julianday(r.at)<=julianday(?) "
                                        "ORDER BY r.at DESC LIMIT 1", (row["id"], row["version"], instant(now).isoformat())).fetchone()
                result = json.loads(result_row[0]) if result_row else {}
                task = db.execute("SELECT body FROM fund_strategy_events WHERE id=? AND version=? AND kind='research_task' "
                                  "ORDER BY sequence DESC LIMIT 1", (row["id"], row["version"])).fetchone()
                # Final holdout trades, returns and parameter-level diagnostics
                # are operator-only; ordinary SQL/research capability is intact.
                public_tests = {k: v for k, v in result.get("tests", {}).items() if k != "final"}
                proposal = json.loads(row["proposal"])
                candidates.append({"id": row["id"], "version": row["version"], "digest": row["digest"],
                                   "state": row["state"], "task_state": row["task_state"],
                                   "policy_current": row["policy_hash"] == digest(self.policy),
                                   **({"proposal": proposal} if index < detail_limit or strategy_id else
                                      {"name": proposal["name"], "hypothesis": proposal["hypothesis"][:500]}),
                                   "last_task": json.loads(task[0]) if task else None,
                                   "validation": {"status": result.get("status"), "tests": public_tests,
                                                  "limitations": result.get("limitations"),
                                                  "reason": result.get("reason"),
                                                  "final_passed": result.get("tests", {}).get("final", {}).get("passed")}})
        from .strategy_admission import library
        from .strategy_competition import compare
        return {"mode": self.settings.admission, "allocation_mode": self.settings.allocation_mode,
                "admitted_library": library(self.journal, now),
                "competition": compare(self.journal, now),
                "candidates": candidates, "budget": self.budget.summary(now),
                "older_versions_omitted": max(0, len(rows) - 20),
                "authority": "Research evidence only. Existing operator mandate, order approvals and hard risk limits remain.",
                "supported_adapter": "daily_rules_v1, daily_cash_rules_v1; other mechanisms remain open feasibility tasks",
                "historical_caveat": "Public historical data/model knowledge can contaminate hypotheses; frozen forward evidence required."}

    def sync_registry(self, now):
        mapping = {"shadow": "shadow", "paper_active": "paper_active", "paused": "paused", "rejected": "rejected",
                   "retired": "retired"}
        return Registry(self.journal).observe(now, [{
            "id": "strategy:" + row["id"], "version": row["version"], "kind": "strategy",
            "supersede": False,
            "hypothesis": json.loads(row["proposal"])["hypothesis"],
            "dependencies": json.loads(row["proposal"])["data_requirements"],
            "config": {"proposal_hash": row["digest"], "policy_hash": row["policy_hash"]},
            "state": mapping.get(row["state"], "historical_test" if row["state"] not in {
                "candidate", "needs_formalization", "needs_adapter"} else "candidate"),
            "reason": row["state"], "evidence": {"task_state": row["task_state"], "account_permission": False}}
            for row in self.rows()])


def run_worker(payload, timeout):
    # A trusted interpreter, not arbitrary generated Python. The child receives
    # serialized research data only: no broker, inherited secrets or file fds.
    worker = Path(__file__).with_name("strategy_worker.py")
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "LANG") if key in os.environ}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    completed = subprocess.run([sys.executable, "-I", str(worker)], input=encode(payload), text=True,
                               capture_output=True, timeout=timeout, env=env, close_fds=True,
                               cwd=worker.parent)
    if completed.returncode or len(completed.stdout) > 32_000_000:
        raise RiskError("Strategy worker failed or exceeded its output allowance")
    return json.loads(completed.stdout)
