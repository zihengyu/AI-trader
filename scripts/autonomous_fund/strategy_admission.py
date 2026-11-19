"""Account-level library authorization, independent of scientific tests and order approvals.

Called inside the existing acceptance, submission and local-fill transactions.
Existing operator mandates retain their behavior unless validated_only is selected.
"""
import json
import sqlite3
from datetime import timedelta

from .risk import RiskError, instant, number
from .session_calendar import completed_daily_cutoff
from .strategy_definition import StrategyAllocation
from .strategy_evidence import reference_signal_ready
from .strategy_validation import following_session


def latest_required_session(now):
    from .scorecard import _exchange_sessions
    cutoff = completed_daily_cutoff(now)
    values = _exchange_sessions(str(cutoff - timedelta(days=12)), str(cutoff))
    return max((day for day in values if day < str(cutoff)), default=None)


def library_entry(db, key, version, now, policy):
    row = db.execute("SELECT * FROM fund_strategy_versions WHERE id=? AND version=?", (key, version)).fetchone()
    if not row or row["state"] != "paper_active" or row["policy_hash"] != policy["policy_hash"]:
        raise RiskError("Strategy version is not admitted under the current account policy")
    evidence = db.execute("SELECT r.body,r.at FROM fund_strategy_results r JOIN fund_strategy_jobs j ON r.job_id=j.id "
                          "WHERE j.strategy_id=? AND j.version=? AND j.kind='forward' ORDER BY r.at DESC LIMIT 1",
                          (key, version)).fetchone()
    result = json.loads(evidence[0]) if evidence else {}
    series = result.get("series") or []
    if (not result.get("passed") or result.get("degraded") or not series or
            instant(evidence[1]) > instant(now) or series[-1]["day"] != latest_required_session(now)):
        raise RiskError("Strategy forward evidence is missing, stale, future or degraded")
    if (result.get("qualification") or {}).get("status") != "passed":
        raise RiskError("Strategy has no successful frozen forward qualification")
    if not result.get("coverage_complete"):
        raise RiskError("Strategy forward data coverage is incomplete")
    if result.get("replayed_sessions") != 0 or result.get("prospective_sessions") != len(series):
        raise RiskError("Strategy forward history lacks timely signal receipts")
    next_day, opened = following_session(series[-1]["day"])
    signal = db.execute("SELECT at,body FROM fund_strategy_signals WHERE id=? AND version=? AND day=?",
                        (key, version, next_day)).fetchone()
    if not signal or instant(signal[0]) > instant(now) or instant(signal[0]) >= opened:
        raise RiskError("Strategy allocation lacks a recorded next session signal")
    bound = json.loads(signal[1])
    if (bound["proposal_digest"] != row["digest"] or bound["policy_hash"] != policy["policy_hash"]
            or bound["targets"] != result["next_targets"]):
        raise RiskError("Strategy allocation does not match its recorded target")
    plan = json.loads(row["proposal"])["validation"]
    if reference := plan.get("reference_strategy"):
        control = bound.get("strategy_reference")
        if (not reference_signal_ready(control, reference, bound.get("as_of"))
                or control != result.get("next_strategy_reference_signal")):
            raise RiskError("Strategy allocation lacks its frozen comparison signal")
    return {"strategy_id": key, "version": version, "digest": row["digest"],
            "next_targets": result["next_targets"], "observations": result["observations"],
            "as_of": evidence[1], "purpose": plan["purpose"]}


def verify_allocations(db, decision, now, *, purchase=False):
    if not purchase and not decision.get("strategy_allocations"):
        return  # Protective reductions need no new library permission.
    try:
        _verify_allocations(db, decision, now, purchase=purchase)
    except (sqlite3.DatabaseError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RiskError):
            raise
        # Incomplete restores and malformed receipts must reach the same order
        # denial path as missing evidence, rather than a raw database traceback.
        raise RiskError("Strategy library evidence is unavailable") from None


def _verify_allocations(db, decision, now, *, purchase):
    saved = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
    policy = json.loads(saved[0]) if saved else {}
    if not isinstance(policy, dict):
        raise RiskError("Strategy library policy is unavailable")
    raw = decision.get("strategy_allocations") or []
    if not raw:
        if purchase and policy.get("allocation_mode") == "validated_only":
            raise RiskError("New purchases require an admitted strategy allocation")
        return  # Existing mandate decisions and protective reductions remain compatible.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_strategy_versions'").fetchone():
        raise RiskError("Strategy library evidence is unavailable")
    if not policy.get("enabled") or policy.get("admission") != "paper_library":
        raise RiskError("Research-only strategies cannot authorize portfolio allocations")
    allocations = [StrategyAllocation.model_validate(row).model_dump() for row in raw]
    identities = [(r["strategy_id"], r["version"]) for r in allocations]
    if len(set(identities)) != len(identities) or sum(number(r["capital_weight"]) for r in allocations) > 1:
        raise RiskError("Duplicate or overallocated strategy capital")
    caps = {}
    for allocation in allocations:
        entry = library_entry(db, allocation["strategy_id"], allocation["version"], now, policy)
        if entry["digest"] != allocation["digest"]:
            raise RiskError("Strategy artifact digest does not match its approved version")
        for symbol, weight in entry["next_targets"].items():
            caps[symbol] = caps.get(symbol, number(0)) + number(weight) * number(allocation["capital_weight"])
    for target in decision["targets"]:
        if number(target["weight"]) > caps.get(target["symbol"], number(0)) + number("0.000001"):
            raise RiskError("Portfolio target exceeds admitted strategy capital or signal")


def library(journal, now):
    with journal.connection() as db:
        saved = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
        policy = json.loads(saved[0]) if saved else {}
        if not policy.get("enabled") or policy.get("admission") != "paper_library":
            return []
        entries = []
        for key, version in db.execute("SELECT id,version FROM fund_strategy_versions WHERE state='paper_active'"):
            try:
                entries.append(library_entry(db, key, version, now, policy))
            except (sqlite3.DatabaseError, KeyError, TypeError, ValueError):
                continue
        return entries
