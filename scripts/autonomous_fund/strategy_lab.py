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
from .strategy_definition import ResearchTaskUpdate, StrategyLabSettings, daily_sample_requirements, proposal_body
from .strategy_program import validate_program
from .strategy_evidence import (load_frozen_dataset, load_reference, merge_forward_input, reference_signal_matches,
                                reference_signal_ready)
from .strategy_validation import (check_dataset, check_snapshot_as_of, digest, following_session, historical_input_gap, implementation_hash,
                                  planned_sessions, snapshot_limitations, source_artifact_gaps)


def has_source_artifact_receipt(db, fingerprint, now):
    row = db.execute("SELECT verified_at FROM fund_strategy_source_artifacts WHERE dataset=?", (fingerprint,)).fetchone()
    return row is not None and instant(row[0]) <= instant(now)


class StrategyLab:
    def __init__(self, journal, policy, settings=None, budget_settings=None, *, publish_policy=True):
        self.journal, self.policy = journal, copy.deepcopy(policy)
        self.settings = settings or StrategyLabSettings()
        self.policy["research_execution"] = {"initial_capital": self.settings.initial_capital_usd,
                                            "require_source_artifacts": self.settings.require_source_artifacts,
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
                CREATE INDEX IF NOT EXISTS strategy_event_version_kind ON fund_strategy_events(id,version,kind,sequence);
                CREATE TABLE IF NOT EXISTS fund_strategy_datasets (
                    digest TEXT PRIMARY KEY, at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_strategy_source_artifacts (
                    dataset TEXT PRIMARY KEY REFERENCES fund_strategy_datasets(digest), verified_at TEXT NOT NULL);
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
                CREATE TABLE IF NOT EXISTS fund_strategy_signals (
                    id TEXT NOT NULL, version TEXT NOT NULL, day TEXT NOT NULL,
                    at TEXT NOT NULL, job_id TEXT NOT NULL, dataset TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(id,version,day));
                CREATE TABLE IF NOT EXISTS fund_strategy_cohorts (
                    cutoff TEXT PRIMARY KEY, opened_at TEXT NOT NULL, members TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fund_strategy_holdouts (
                    id TEXT NOT NULL, version TEXT NOT NULL, at TEXT NOT NULL,
                    dataset TEXT NOT NULL, sessions TEXT NOT NULL, PRIMARY KEY(id,version));
            """)
        self.admission_policy = {**asdict(self.settings), "policy_hash": digest(self.policy)}
        if publish_policy:
            journal.set_meta("strategy_lab_policy", self.admission_policy)
        elif journal.get_meta("strategy_lab_policy") != self.admission_policy:
            raise RiskError("Offline research requires the current runner's frozen lab policy")

    @staticmethod
    def _event(db, key, version, now, kind, body):
        db.execute("INSERT INTO fund_strategy_events(id,version,at,kind,body) VALUES(?,?,?,?,?)",
                   (key, version, instant(now).isoformat(), kind, encode(body)))

    def submit(self, proposal, now, proposer="researcher", *, source_call_id=None):
        body = proposal_body(proposal)
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
            plan = body["validation"]
            required = daily_sample_requirements(plan)
            short = {key: required[key] for key in ("validation_sessions", "final_sessions")
                     if plan[key] < required[key]}
            if plan["method"] != "daily_forward_v1":
                state, feasibility_error = "needs_adapter", "An independent verifier for this method is needed"
            elif short:
                state = "needs_formalization"
                feasibility_error = "After purging, minimum split sizes are " + encode(short)
            else:
                state = "candidate"
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest,policy_hash FROM fund_strategy_versions WHERE id=? AND version=?",
                             (key, version)).fetchone()
            if old:
                if old[0] != fingerprint or old[1] != policy_hash:
                    raise RiskError("A strategy version or its execution policy cannot be rewritten")
                return key, version
            if db.execute("SELECT count(*) FROM fund_strategy_versions WHERE "
                          "state NOT IN ('rejected','retired','paused','forward_incomplete') "
                          "AND task_state!='retired'").fetchone()[0] >= self.settings.max_candidates:
                raise RiskError("Strategy research capacity reached; existing evidence is retained")
            parent = body.get("parent_version")
            previous = db.execute("SELECT version FROM fund_strategy_versions WHERE id=?", (key,)).fetchall()
            if previous and (not parent or parent not in {r[0] for r in previous}) or not previous and parent:
                raise RiskError("A strategy revision needs an existing parent version")
            if previous and body["change_type"] == "new":
                raise RiskError("A changed strategy must identify its revision type")
            if state == "candidate":
                try:
                    load_reference(db, body, policy_hash, now)
                except RiskError as exc:
                    state, feasibility_error = "needs_formalization", str(exc)[:500]
            db.execute("INSERT INTO fund_strategy_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key, version, instant(now).isoformat(), proposer, encode(body), fingerprint,
                        encode(self.policy), policy_hash, state, "open"))
            self._event(db, key, version, now, "registered", {"state": state, "digest": fingerprint,
                                                            "feasibility_error": feasibility_error,
                                                            "source_call_id": source_call_id})
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

    def record_dataset(self, data, now, *, artifacts_dir=None):
        fingerprint = check_snapshot_as_of(data, now, self.settings.max_history_rows)
        if artifacts_dir is not None:
            gaps = source_artifact_gaps(data, artifacts_dir)
            if gaps:
                raise RiskError("Source artifact verification failed: " + ", ".join(gaps))
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM fund_strategy_datasets WHERE digest=?", (fingerprint,)).fetchone():
                used = db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_datasets").fetchone()[0]
                used += db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_results").fetchone()[0]
                if used + len(encode(data).encode()) > self.settings.max_artifact_bytes:
                    raise RiskError("Strategy research artifact allowance is exhausted; audit data retained")
            db.execute("INSERT OR IGNORE INTO fund_strategy_datasets VALUES(?,?,?)",
                       (fingerprint, instant(now).isoformat(), encode(data)))
            if artifacts_dir is not None:
                db.execute("INSERT OR IGNORE INTO fund_strategy_source_artifacts VALUES(?,?)",
                           (fingerprint, instant(now).isoformat()))
        return fingerprint

    def _qualified_input(self, db, data, fingerprint, now):
        return not snapshot_limitations(data, require_receipts=True) and (
            not self.settings.require_source_artifacts or has_source_artifact_receipt(db, fingerprint, now))

    def schedule(self, data, now, *, artifacts_dir=None, candidate=None):
        fingerprint = self.record_dataset(data, now, artifacts_dir=artifacts_dir)
        policy_hash = digest(self.policy)
        created = []
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            complete = self._qualified_input(db, data, fingerprint, now)
            rows = [dict(r) for r in db.execute("SELECT * FROM fund_strategy_versions WHERE task_state='open' "
                                               "OR state IN ('shadow','paper_active')")]
            previous_cutoff = db.execute("SELECT max(cutoff) FROM fund_strategy_cohorts").fetchone()[0]
            new_windows = []
            for row in rows:
                if row["policy_hash"] != policy_hash or (candidate and candidate != (row["id"], row["version"])):
                    # Historical evidence keeps its policy. It cannot authorize
                    # a strategy under a different account risk configuration.
                    continue
                body = json.loads(row["proposal"])
                if row["state"] not in {"candidate", "needs_data", "awaiting_new_holdout", "shadow", "paper_active"}:
                    continue
                try:
                    reference = load_reference(db, body, policy_hash, row["created_at"])
                except RiskError as exc:
                    self._record_preflight_gap(db, row, now, {"status": "needs_formalization", "reason": str(exc)[:500]})
                    continue
                if row["state"] in {"candidate", "needs_data", "awaiting_new_holdout"}:
                    kind = "exploratory"
                    plan = body["validation"]
                    total = plan["train_sessions"] + plan["validation_sessions"] + plan["final_sessions"]
                    lock = db.execute("SELECT sessions FROM fund_strategy_holdouts WHERE id=? AND version=?",
                                      (row["id"], row["version"])).fetchone()
                    sessions = json.loads(lock[0]) if lock else None
                    if lock:
                        # Keep an eligible queued job when an exploratory or
                        # narrower snapshot arrives while it is waiting.
                        if not complete:
                            continue
                        if db.execute("SELECT 1 FROM fund_strategy_jobs WHERE strategy_id=? AND version=? "
                                      "AND kind='historical' AND state IN ('queued','running')",
                                      (row["id"], row["version"])).fetchone():
                            continue
                    # A shared provider snapshot need not contain every strategy.
                    # A scoped cache snapshot, however, is specifically for this version.
                    if candidate is None and plan["benchmark"] not in data["bars"]:
                        continue
                    if gap := historical_input_gap(body, data, self.policy, locked_sessions=sessions, reference=reference):
                        self._record_preflight_gap(db, row, now, gap)
                        continue
                    days = [r["day"] for r in data["bars"][plan["benchmark"]]]
                    if lock:
                        kind = "historical"
                    elif complete and (
                            not self.policy.get("cash_symbol") or all(arm["implementation"]["adapter"] == "daily_cash_rules_v1"
                                                                     for arm in (body, reference) if arm is not None)):
                        sessions = planned_sessions(days, total)
                        if previous_cutoff and sessions[-plan["final_sessions"]] <= previous_cutoff:
                            db.execute("UPDATE fund_strategy_versions SET state='awaiting_new_holdout' WHERE id=? AND version=?",
                                       (row["id"], row["version"]))
                            if row["state"] != "awaiting_new_holdout":
                                self._event(db, row["id"], row["version"], now, "validation",
                                            {"state": "awaiting_new_holdout", "status": "awaiting_new_holdout"})
                        else:
                            kind = "historical"
                            db.execute("INSERT INTO fund_strategy_holdouts VALUES(?,?,?,?,?)",
                                       (row["id"], row["version"], instant(now).isoformat(), fingerprint, encode(sessions)))
                            new_windows.append((row["id"], row["version"], sessions[-1]))
                elif row["state"] in {"shadow", "paper_active"}:
                    # Exploratory inputs can be kept for historical research.
                    # They must not supersede a queued forward job or become
                    # an immutable seed for later admission checks.
                    if not complete or body["validation"]["benchmark"] not in data["bars"] or (
                        any(set(arm["instruments"]) - data["bars"].keys() for arm in (body, reference) if arm is not None)
                    ):
                        continue
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
                    if kind == "historical":
                        db.execute("UPDATE fund_strategy_jobs SET state='superseded' WHERE strategy_id=? AND version=? "
                                   "AND kind='exploratory' AND state='queued'", (row["id"], row["version"]))
                    created.append(key)
            if new_windows:
                db.execute("INSERT INTO fund_strategy_cohorts VALUES(?,?,?)",
                           (max(end for _, _, end in new_windows), instant(now).isoformat(),
                            encode([[key, version] for key, version, _ in new_windows])))
        return created

    def _record_preflight_gap(self, db, row, now, gap):
        identity = row["id"], row["version"]
        if db.execute("SELECT 1 FROM fund_strategy_jobs WHERE strategy_id=? AND version=? "
                      "AND state IN ('queued','running')", identity).fetchone():
            return
        # Keep a sealed-window wait visible while the exploratory data is being filled.
        state = row["state"] if row["state"] == "awaiting_new_holdout" and gap["status"] == "needs_data" else gap["status"]
        event = {**gap, "state": state, "preflight": True}
        previous = db.execute("SELECT body FROM fund_strategy_events WHERE id=? AND version=? "
                              "AND kind IN ('validation','historical_pass') ORDER BY sequence DESC LIMIT 1", identity).fetchone()
        if previous and json.loads(previous[0]) == event:
            return
        db.execute("UPDATE fund_strategy_versions SET state=? WHERE id=? AND version=?", (state, *identity))
        self._event(db, *identity, now, "validation", event)

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
            eligible = db.execute("SELECT j.* FROM fund_strategy_jobs j JOIN fund_strategy_versions v ON "
                             "v.id=j.strategy_id AND v.version=j.version WHERE v.policy_hash=? AND (v.task_state='open' OR "
                             "v.state IN ('shadow','paper_active')) AND julianday(j.at)<=julianday(?) AND "
                             "((j.kind IN ('historical','exploratory') AND v.state IN ('candidate','needs_data','awaiting_new_holdout')) OR "
                             "(j.kind='forward' AND v.state IN ('shadow','paper_active'))) AND "
                             "NOT EXISTS(SELECT 1 FROM fund_strategy_jobs other WHERE other.strategy_id=j.strategy_id "
                             "AND other.version=j.version AND other.id!=j.id AND other.state='running' AND "
                             "julianday(other.lease_until)>=julianday(?)) AND "
                             "(j.state='queued' OR (j.state='running' AND julianday(j.lease_until)<julianday(?))) "
                             "ORDER BY coalesce((SELECT max(r.at) FROM fund_strategy_results r "
                             "JOIN fund_strategy_jobs done ON done.id=r.job_id WHERE done.strategy_id=j.strategy_id "
                             "AND done.version=j.version),'0000'),j.at,j.rowid",
                             (digest(self.policy), instant(now).isoformat(), instant(now).isoformat(),
                              instant(now).isoformat())).fetchall()
            forward_versions = {(job["strategy_id"], job["version"]) for job in eligible if job["kind"] == "forward"}
            # A missed forward deadline leaves a permanent gap. Historical work
            # gets the usual rotation when enough daily slots are still available.
            prefer_historical = (attempted > 0 and historical_attempts == 0
                                 and len(forward_versions) < self.settings.max_jobs_per_day - attempted)
            eligible.sort(key=lambda job: (job["kind"] == "forward") == prefer_historical)
            jobs = {}
            for candidate in eligible:
                jobs.setdefault(candidate["strategy_id"], candidate)
            if not jobs:
                return None
            # A lifetime project cap cannot block unrelated work. Preserve
            # queue priority among affordable jobs in this same transaction.
            selected = self.budget.reserve_first_in(db, now, jobs, "validation_compute",
                                                    self.budget.settings.validation_reservation_usd)
            if selected is None:
                return None
            project, reservation = selected
            row = jobs[project]
            lease = uuid4().hex
            until = instant(now) + timedelta(seconds=self.settings.job_timeout_seconds * 2 + 30)
            db.execute("UPDATE fund_strategy_jobs SET state='running',lease=?,lease_until=?,reservation=? WHERE id=?",
                       (lease, until.isoformat(), reservation, row["id"]))
            return {**dict(row), "lease": lease, "reservation": reservation}

    def _forward_input(self, db, row, dataset):
        records = db.execute("SELECT body FROM fund_strategy_forward WHERE id=? AND version=? ORDER BY day",
                             (row["id"], row["version"])).fetchall()
        passed = db.execute("SELECT e.at,e.body FROM fund_strategy_events e WHERE id=? AND version=? AND kind='historical_pass' "
                            "ORDER BY sequence LIMIT 1", (row["id"], row["version"])).fetchone()
        if not passed:
            raise RiskError("Forward research requires a frozen historical validation record")
        historical = db.execute("SELECT j.dataset FROM fund_strategy_jobs j "
                                "JOIN fund_strategy_results r ON r.job_id=j.id WHERE j.strategy_id=? AND j.version=? "
                                "AND j.kind='historical' AND j.id=?",
                                (row["id"], row["version"], json.loads(passed[1])["job_id"])).fetchone()
        if historical is None:
            raise RiskError("The frozen historical job or its source snapshot is missing")
        receipts = [json.loads(r[0]) for r in records]
        # Use the saved prefix and incoming snapshot, including legacy full prefixes.
        base = receipts[-1]["frozen_dataset_hash"] if receipts else historical[0]
        seed = merge_forward_input(load_frozen_dataset(db, base), dataset)
        return seed, passed[0], receipts, json.loads(passed[1])["forward_plan"], base

    @staticmethod
    def _save_next_signal(db, row, job, result, last_bar, finished, dataset, *, reference=None):
        signal = result.get("next_signal")
        if not signal or not signal["as_of"] or instant(signal["as_of"]) != instant(last_bar["at"]):
            return
        day, opened = following_session(last_bar["day"])
        if not instant(last_bar["available_at"]) <= finished < opened:
            return
        if reference is not None:
            control = result.get("next_strategy_reference_signal")
            if not reference_signal_ready(control, reference, last_bar["at"]):
                return
            signal = {**signal, "strategy_reference": control}
        db.execute("INSERT OR IGNORE INTO fund_strategy_signals VALUES(?,?,?,?,?,?,?)",
                   (row["id"], row["version"], day, finished.isoformat(), job["id"], dataset,
                    encode({**signal, "open_at": opened.isoformat(), "proposal_digest": row["digest"],
                            "policy_hash": row["policy_hash"]})))

    @staticmethod
    def _forward_evidence(db, row, series, bars, *, reference=None, reference_series=()):
        signals = {day: (instant(at), json.loads(body)) for day, at, body in db.execute(
            "SELECT day,at,body FROM fund_strategy_signals WHERE id=? AND version=?", (row["id"], row["version"]))}
        opens = {bar["day"]: instant(bar["open_at"]) for bar in bars}
        controls = {receipt["day"]: receipt for receipt in reference_series}
        prospective = set()
        for session in series:
            day = session["day"]
            saved = signals.get(day)
            if saved is None or not session.get("signal_as_of"):
                continue
            recorded, signal = saved
            opened = opens[day]
            if (instant(signal["as_of"]) == instant(session["signal_as_of"]) <= recorded < opened
                    and instant(signal["open_at"]) == opened
                    and signal["raw_targets"] == session["raw_signal"]
                    and (reference is None or reference_signal_matches(signal.get("strategy_reference") or {},
                                                                       controls.get(day, {}), reference))
                    and signal["proposal_digest"] == row["digest"] and signal["policy_hash"] == row["policy_hash"]):
                prospective.add(day)
        return prospective

    def _forward_qualification(self, db, row, job, result, prospective, finished):
        qualification = result["qualification"]
        prior = db.execute("SELECT body FROM fund_strategy_events WHERE id=? AND version=? "
                           "AND kind='forward_qualification' ORDER BY sequence LIMIT 1",
                           (row["id"], row["version"])).fetchone()
        if prior:
            return json.loads(prior[0])
        if qualification["status"] == "collecting":
            return qualification
        prefix = result["series"][:qualification["required_sessions"]]
        if any(item["day"] not in prospective for item in prefix):
            qualification = {**qualification, "status": "incomplete"}
        qualification = {**qualification, "job_id": job["id"]}
        self._event(db, row["id"], row["version"], finished, "forward_qualification", qualification)
        return qualification

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
            data = load_frozen_dataset(db, job["dataset"])
            qualified_input = digest(data) == job["dataset"] and self._qualified_input(db, data, job["dataset"], now)
            trials = sum(instant(created) <= now for (created,) in db.execute("SELECT created_at FROM fund_strategy_versions"))
            holdout = db.execute("SELECT sessions FROM fund_strategy_holdouts WHERE id=? AND version=?",
                                 (row["id"], row["version"])).fetchone()
            forward_at, prior, forward_plan, input_issue, forward_base = None, [], None, None, None
            proposal = json.loads(row["proposal"])
            identity = proposal["validation"].get("reference_strategy")
            reference, reference_issue = None, None
            try:
                reference = load_reference(db, proposal, row["policy_hash"], row["created_at"])
            except RiskError as exc:
                reference_issue = str(exc)[:500]
            if job["kind"] == "forward" and qualified_input:
                try:
                    data, forward_at, prior, forward_plan, forward_base = self._forward_input(db, row, data)
                except RiskError as exc:
                    input_issue = str(exc)
                except (KeyError, TypeError, ValueError):
                    input_issue = "Frozen forward qualification plan is unavailable; revalidation is required"
        frozen_policy = json.loads(row["policy"])
        benchmark_bars = data["bars"][proposal["validation"]["benchmark"]]
        payload = {"proposal": proposal, "data": data, "policy": frozen_policy,
                   "initial_capital": frozen_policy["research_execution"]["initial_capital"], "trials": trials,
                   "kind": job["kind"], "forward_after": forward_at,
                   "forward_plan": forward_plan,
                   "locked_sessions": json.loads(holdout[0]) if job["kind"] == "historical" and holdout else None,
                   "timeout_seconds": self.settings.job_timeout_seconds}
        if reference is not None:
            payload["reference"] = reference
        try:
            if reference_issue:
                result = {"status": "needs_formalization", "passed": False, "reason": reference_issue}
            elif job["kind"] in {"historical", "forward"} and (input_issue or not qualified_input):
                result = {"status": "needs_data", "passed": False,
                          "reason": input_issue or "Snapshot lacks eligible bound source evidence"}
            else:
                result = run_worker(payload, self.settings.job_timeout_seconds)
                if (job["kind"] == "exploratory" and self.settings.require_source_artifacts
                        and not qualified_input and result.get("status") != "worker_error"):
                    result.setdefault("limitations", []).append("source_artifacts_unchecked")
                    if result["status"] == "awaiting_new_holdout":
                        result["status"] = "needs_data"
        except Exception as exc:
            result = {"status": "worker_error", "passed": False, "error": type(exc).__name__}
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            finished = now + timedelta(seconds=max(0, monotonic() - started))
            current = db.execute("SELECT lease,state FROM fund_strategy_jobs WHERE id=?", (job["id"],)).fetchone()
            if tuple(current) != (job["lease"], "running"):
                return None  # An expired worker may not overwrite a newer claim.
            if job["kind"] == "forward" and result.get("series") and result.get("coverage_complete") is not True:
                # Recheck at the write boundary for older queued jobs or
                # a merged seed with incomplete attestations.
                result = {"status": "needs_data", "passed": False,
                          "reason": "Forward data coverage is incomplete; receipts retained"}
            used = db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_datasets").fetchone()[0]
            used += db.execute("SELECT coalesce(sum(length(body)),0) FROM fund_strategy_results").fetchone()[0]
            frozen = digest(data) if forward_base and result.get("series") else None
            link = encode({"forward_base": forward_base, "forward_source": job["dataset"]})
            extra_data = len(link.encode()) if frozen and not db.execute(
                "SELECT 1 FROM fund_strategy_datasets WHERE digest=?", (frozen,)).fetchone() else 0
            if used + extra_data + len(encode(result).encode()) > self.settings.max_artifact_bytes:
                result = {"status": "worker_error", "passed": False, "error": "ArtifactBudgetExceeded"}
            if job["kind"] == "forward" and result.get("series"):
                series = result["series"]
                controls = result.get("strategy_reference_series") or []
                if (series[:len(prior)] != [item["receipt"] for item in prior] or identity is not None and (
                    [item["day"] for item in controls] != [item["day"] for item in series]
                    or controls[:len(prior)] != [item.get("strategy_reference_receipt") for item in prior]
                )):
                    result = {"status": "needs_data", "passed": False, "reason": "Forward history changed; receipts retained"}
                else:
                    prospective = self._forward_evidence(db, row, series, benchmark_bars, reference=identity,
                                                         reference_series=controls)
                    result["statistical_passed"] = result["passed"]
                    result["prospective_sessions"] = len(prospective)
                    result["replayed_sessions"] = len(series) - len(prospective)
                    qualification = self._forward_qualification(db, row, job, result, prospective, finished)
                    result["qualification"] = qualification
                    result["passed"] = (result["passed"] and qualification["status"] == "passed"
                                        and not result["replayed_sessions"] and not result["degraded"])
                    db.execute("INSERT OR IGNORE INTO fund_strategy_datasets VALUES(?,?,?)",
                               (frozen, finished.isoformat(), link))
                    for index in range(len(prior), len(series)):
                        receipt = series[index]
                        evidence = "prospective" if receipt["day"] in prospective else "replay"
                        observation = {"receipt": receipt, "frozen_dataset_hash": frozen, "evidence": evidence}
                        if identity is not None:
                            observation["strategy_reference_receipt"] = controls[index]
                        db.execute("INSERT INTO fund_strategy_forward VALUES(?,?,?,?,?,?)",
                                   (row["id"], row["version"], receipt["day"], finished.isoformat(), job["dataset"],
                                    encode(observation)))
                    if not result["degraded"]:
                        self._save_next_signal(db, row, job, result, benchmark_bars[-1],
                                               now + timedelta(seconds=monotonic() - started), frozen, reference=identity)
            elif job["kind"] == "historical" and result.get("passed"):
                self._save_next_signal(db, row, job, result, benchmark_bars[-1],
                                       now + timedelta(seconds=monotonic() - started), job["dataset"], reference=identity)
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
                state = "shadow" if result.get("passed") else result.get("status", "rejected")
            elif result.get("status") == "needs_formalization":
                state = "needs_formalization"
            elif result.get("degraded"):
                state = "paused"
            elif job["kind"] == "forward" and (result.get("qualification") or {}).get("status") == "failed":
                state = "rejected"
            elif job["kind"] == "forward" and (result.get("qualification") or {}).get("status") == "incomplete":
                state = "forward_incomplete"
            elif job["kind"] == "forward" and result.get("passed") and self.settings.admission == "paper_library" and (
                    result.get("observations", 0) >= self.settings.min_forward_sessions):
                current = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
                if current and json.loads(current[0]) == self.admission_policy:
                    state = "paper_active"
            db.execute("UPDATE fund_strategy_versions SET state=? WHERE id=? AND version=?",
                       (state, row["id"], row["version"]))
            event = {"job_id": job["id"], "state": state, "status": result.get("status")}
            historical_pass = job["kind"] == "historical" and result.get("passed")
            if historical_pass:
                required = daily_sample_requirements(proposal["validation"])["forward_sessions"]
                event["forward_plan"] = {"required_sessions": max(required, self.settings.min_forward_sessions),
                                         "trials": trials, "trial_scope": "registered_versions_at_freeze"}
            self._event(db, row["id"], row["version"], finished,
                        "historical_pass" if historical_pass else "validation", event)
        # This is a configured compute allowance, not a measured cloud bill.
        self.budget.settle(reservation, finished, self.budget.settings.validation_reservation_usd,
                           {"job_id": job["id"], "wall_seconds": round(monotonic() - started, 6),
                            "basis": "configured per-job compute allowance"})
        self.sync_registry(finished)
        return result

    def _has_pending_jobs(self):
        with self.journal.connection() as db:
            return bool(db.execute("SELECT 1 FROM fund_strategy_jobs j JOIN fund_strategy_versions v "
                "ON v.id=j.strategy_id AND v.version=j.version WHERE j.state IN ('queued','running') "
                "AND v.policy_hash=? AND (v.task_state='open' OR v.state IN ('shadow','paper_active')) LIMIT 1",
                (self.admission_policy["policy_hash"],)).fetchone())

    def kick(self, now, *, prepare=None):
        if self._future is not None and not self._future.done():
            return False
        if self._future is not None:
            # Surface errors instead of silently abandoning a background task.
            previous, self._future = self._future, None
            previous.result()
        if prepare is None and not self._has_pending_jobs():
            return False
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-lab")
        self._future = self._executor.submit(self._drain, instant(now), monotonic(), prepare=prepare)
        return True

    def _drain(self, now, started=None, *, prepare=None):
        started = monotonic() if started is None else started
        try:
            if prepare is not None:
                prepare(now + timedelta(seconds=monotonic() - started))
        finally:
            if prepare is None or self._has_pending_jobs():
                for _ in range(self.settings.max_jobs_per_day):
                    # Each claim uses its actual elapsed time within the batch.
                    if self.run_one(now + timedelta(seconds=monotonic() - started)) is None:
                        break

    def context(self, now, *, detail_limit=4, strategy_id=None, version=None):
        candidates = []
        now = instant(now)
        rows = [row for row in self.rows() if instant(row["created_at"]) <= now
                and (strategy_id is None or row["id"] == strategy_id) and (version is None or row["version"] == version)]
        with self.journal.connection() as db:
            for index, row in enumerate(reversed(rows[-20:])):
                results = db.execute("SELECT r.at,r.body FROM fund_strategy_results r JOIN fund_strategy_jobs j ON "
                                        "r.job_id=j.id WHERE j.strategy_id=? AND j.version=? AND julianday(r.at)<=julianday(?) "
                                        "ORDER BY julianday(r.at) DESC,r.rowid DESC", (row["id"], row["version"], now.isoformat()))
                result = next((json.loads(body) for at, body in results if instant(at) <= now), {})
                history = db.execute("SELECT at,kind,body FROM fund_strategy_events WHERE id=? AND version=? "
                                     "AND kind IN ('registered','historical_pass','validation','research_task') "
                                     "AND julianday(at)<=julianday(?) ORDER BY julianday(at) DESC,sequence DESC",
                                     (row["id"], row["version"], now.isoformat()))
                events = {}
                for at, kind, body in history:
                    if instant(at) <= now:
                        category = "validation" if kind == "historical_pass" else kind
                        if category not in events:
                            events[category] = json.loads(body)
                registration, task = events.get("registered", {}), events.get("research_task")
                state = events.get("validation", registration).get("state", "unavailable")
                gap = events.get("validation", {})
                if gap.get("preflight"):
                    result = gap
                task_state = {"pause": "paused", "retire": "retired"}.get(task["action"] if task else None, "open")
                # Final holdout trades, returns and parameter-level diagnostics
                # are operator-only; ordinary SQL/research capability is intact.
                public_tests = {k: v for k, v in result.get("tests", {}).items() if k != "final"}
                proposal = json.loads(row["proposal"])
                candidates.append({"id": row["id"], "version": row["version"], "digest": row["digest"],
                                   "state": state, "task_state": task_state,
                                   "policy_current": row["policy_hash"] == digest(self.policy),
                                   **({"proposal": proposal} if index < detail_limit or strategy_id else
                                      {"name": proposal["name"], "hypothesis": proposal["hypothesis"][:500]}),
                                   "last_task": task, "source_call_id": registration.get("source_call_id"),
                                   "feasibility_error": registration.get("feasibility_error"),
                                   "validation": {"status": result.get("status"), "tests": public_tests,
                                                  "limitations": result.get("limitations"),
                                                  "qualification": {k: v for k, v in (result.get("qualification") or {}).items()
                                                                    if k in {"status", "required_sessions", "start", "end", "trials"}},
                                                  "regimes": result.get("regimes"),
                                                  "prospective_sessions": result.get("prospective_sessions"),
                                                  "replayed_sessions": result.get("replayed_sessions"),
                                                  **({key: result[key] for key in ("missing_symbols", "missing_feature",
                                                                                   "observed_sessions", "required_sessions")
                                                      if key in result}),
                                                  "reason": result.get("reason")}})
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
                   "forward_incomplete": "paused",
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


def load_snapshot(path):
    with path.open("rb") as source:
        raw = source.read(32_000_001)
    if len(raw) > 32_000_000:
        raise RiskError("Strategy snapshot exceeds the 32 MB input allowance")
    snapshot = json.loads(raw)
    if not isinstance(snapshot, dict):
        raise RiskError("A strategy snapshot must be a JSON object")
    return snapshot


def run_worker(payload, timeout):
    # A trusted interpreter, not arbitrary generated Python. The child receives
    # serialized research data only: no broker, inherited secrets or file fds.
    worker = Path(__file__).with_name("strategy_worker.py")
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "LANG") if key in os.environ}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    completed = subprocess.run([sys.executable, "-I", str(worker)], input=encode(payload).encode("utf-8"),
                               capture_output=True, timeout=timeout, env=env, close_fds=True,
                               cwd=worker.parent)
    if completed.returncode or len(completed.stdout) > 32_000_000:
        raise RiskError("Strategy worker failed or exceeded its output allowance")
    return json.loads(completed.stdout)
