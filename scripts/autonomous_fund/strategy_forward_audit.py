"""Read-only audit of the first frozen prospective window for one strategy version."""
import json
import math
from collections import Counter
from datetime import date, timedelta
from itertools import takewhile

from .costs import scripted, unpriced_reason
from .risk import RiskError, instant, number, trading_day
from .strategy_competition import _risk_matched
from .strategy_evidence import reference_signal_matches, reference_signal_ready, signal_matches_receipt
from .strategy_validation import _daily_sessions, metrics, signal_cutoff


def _window(frozen, required):
    if type(required) is not int or not 1 <= required <= 10000:
        raise RiskError("Frozen forward window has an invalid length")
    day = date.fromisoformat(trading_day(frozen))
    sessions = _daily_sessions((day - timedelta(days=14)).isoformat(),
                               (day + timedelta(days=required * 2 + 30)).isoformat())
    window = [(session, opened, closed, previous[2]) for previous, (session, opened, closed)
              in zip(sessions, sessions[1:]) if opened > frozen][:required]
    if len(window) != required:
        raise RiskError("Exchange calendar cannot resolve the frozen forward window")
    return window


def _event(db, key, version, kind, cutoff):
    rows = db.execute("SELECT at,body FROM fund_strategy_events WHERE id=? AND version=? AND kind=? "
                      "AND julianday(at)<=julianday(?) ORDER BY sequence", (key, version, kind, cutoff.isoformat()))
    return next(((instant(at), json.loads(body)) for at, body in rows if instant(at) <= cutoff), (None, {}))


def _paired_result(db, key, version, cutoff, *, checkpoint_job=None, strategy_reference=False):
    rows = db.execute("SELECT r.at,r.body FROM fund_strategy_results r JOIN fund_strategy_jobs j ON j.id=r.job_id "
                      "WHERE j.strategy_id=? AND j.version=? AND j.kind='forward' AND julianday(r.at)<=julianday(?) "
                      "AND (? IS NULL OR r.job_id=?) ORDER BY julianday(r.at) DESC,r.rowid DESC",
                      (key, version, cutoff.isoformat(), checkpoint_job, checkpoint_job))
    for at, body in rows:
        if instant(at) > cutoff:
            continue
        try:
            result = json.loads(body)
            own, reference = result.get("series"), result.get("reference_series")
            if result.get("coverage_complete") is not True or not own or not reference:
                continue
            days = [row["day"] for row in own]
            if days == sorted(set(days)) and days == [row["day"] for row in reference]:
                if strategy_reference:
                    control = result.get("strategy_reference_series") or []
                    if days != [row["day"] for row in control]:
                        continue
                    return instant(at), dict(zip(days, zip(own, reference, control)))
                return instant(at), dict(zip(days, zip(own, reference)))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return None, {}


def _originating_call(db, key, version, cutoff):
    _, registration = _event(db, key, version, "registered", cutoff)
    call = registration.get("source_call_id")
    if not call:
        return {"status": "unlinked"}
    rows = db.execute("SELECT at,body FROM events WHERE kind='agent' AND json_valid(body) AND "
                      "json_extract(body,'$.call_id')=? AND json_extract(body,'$.name')='researcher' "
                      "AND julianday(at)<=julianday(?) ORDER BY sequence DESC", (call, cutoff.isoformat()))
    for at, body in rows:
        if instant(at) > cutoff:
            continue
        receipt = json.loads(body)
        cost = 0 if scripted(receipt) or receipt.get("cache_hit") else receipt.get("cost_usd")
        known = type(cost) in {int, float} and math.isfinite(cost) and cost >= 0
        return {"call_id": call, "status": "known" if known else "unpriced",
                "reported_call_cost_usd": cost if known else None,
                "unpriced_reason": unpriced_reason(receipt) or "invalid_cost_receipt" if not known else None}
    return {"call_id": call, "status": "call_receipt_unavailable"}


def _signal_valid(at, body, opened, expected_cutoff, proposal_digest, policy_hash):
    return (at is not None and expected_cutoff <= at < opened and instant(body["as_of"]) == expected_cutoff
            and instant(body["open_at"]) == opened and body.get("proposal_digest") == proposal_digest
            and body.get("policy_hash") == policy_hash and isinstance(body.get("raw_targets"), dict))


def _source_status(db, dataset, at, require_verification):
    if at is None:
        return None
    row = db.execute("SELECT at FROM fund_strategy_datasets WHERE digest=?", (dataset,)).fetchone()
    if not row or instant(row[0]) > at:
        return "snapshot_unavailable"
    if require_verification:
        from .strategy_lab import has_source_artifact_receipt

        return "artifact_bytes_matched" if has_source_artifact_receipt(db, dataset, at) else "artifact_check_unavailable"
    return "snapshot_recorded"


def _session_state(opened, closed, cutoff, signal_ok, recorded_forward, forward_body, pair, result_at, signal,
                   strategy_reference):
    if cutoff < opened:
        return "ready_for_open" if signal_ok else "awaiting_signal"
    if recorded_forward is not None:
        receipt = forward_body["receipt"]
        if recorded_forward < closed or receipt["day"] != trading_day(opened):
            return "inconsistent_evidence"
        if forward_body.get("evidence") == "replay":
            return "replayed"
        if (forward_body.get("evidence") != "prospective" or not signal_ok
                or not signal_matches_receipt(signal, receipt)
                or instant(receipt["signal_as_of"]) != instant(signal["as_of"])):
            return "inconsistent_evidence"
        if pair is None:
            return "paired_result_unavailable"
        if strategy_reference is not None:
            control = forward_body.get("strategy_reference_receipt") or {}
            if control != pair[2] or not reference_signal_matches(signal.get("strategy_reference") or {}, control,
                                                                  strategy_reference):
                return "inconsistent_evidence"
        return "prospective" if recorded_forward <= result_at and receipt == pair[0] else "inconsistent_evidence"
    if not signal_ok:
        return "invalid_signal" if signal else "missed_signal"
    return "awaiting_close" if cutoff < closed else "awaiting_observation"


def _economics(pairs, days, capital):
    if not days:
        return {"status": "no_contiguous_prospective_evidence", "sessions": 0}
    strategy, reference, *extra = map(list, zip(*(pairs[day] for day in days)))

    def summary(series):
        report = metrics(series, capital)
        if not math.isfinite(report["net_return"]):
            raise RiskError("Nonfinite economic return")
        return {"net_return": report["net_return"], "max_drawdown": report["max_drawdown"],
                "estimated_trading_fees_usd": str(sum((number(trade["trade_cost"]) for row in series
                                                      for trade in row["trades"]), number(0))),
                "modeled_operating_cost_usd": str(number(series[-1]["equity"]) - number(series[-1]["net_economic_equity"]))}

    try:
        own, passive = summary(strategy), summary(reference)
        result = {"status": "observed", "sessions": len(days), "through": days[-1], "strategy": own,
                  "passive_reference": passive, "net_difference": own["net_return"] - passive["net_return"],
                  "risk_matched": _risk_matched(strategy, reference, days)}
        if extra:
            control = summary(extra[0])
            result["strategy_reference"] = {**control, "net_difference": own["net_return"] - control["net_return"]}
        return result
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return {"status": "unreadable_economic_evidence", "sessions": len(days)}


def audit_forward(db, key, version, cutoff):
    cutoff = instant(cutoff)
    row = db.execute("SELECT created_at,proposal,policy,digest,policy_hash FROM fund_strategy_versions "
                     "WHERE id=? AND version=?", (key, version)).fetchone()
    if not row or instant(row[0]) > cutoff:
        raise RiskError("Unknown strategy version at the selected cutoff in this account")
    proposal, policy = json.loads(row[1]), json.loads(row[2])
    plan = proposal["validation"]
    reference = {"symbol": plan["benchmark"], "weight": plan["benchmark_weight"], "kind": "static_no_ai"}
    audit = {"strategy_id": key, "version": version, "as_of": cutoff.isoformat(), "proposal_digest": row[3],
             "policy_hash": row[4], "passive_reference": reference,
             "originating_research_call": _originating_call(db, key, version, cutoff)}
    strategy_reference = plan.get("reference_strategy")
    if strategy_reference is not None:
        audit["strategy_reference"] = strategy_reference
    frozen, precommitment = _event(db, key, version, "historical_pass", cutoff)
    if frozen is None:
        return {**audit, "status": "awaiting_historical_pass"}
    forward_plan = precommitment.get("forward_plan")
    if not isinstance(forward_plan, dict) or "required_sessions" not in forward_plan:
        return {**audit, "status": "missing_frozen_forward_plan", "historical_pass_at": frozen.isoformat()}
    window = _window(frozen, forward_plan["required_sessions"])
    checkpoint_at, checkpoint = _event(db, key, version, "forward_qualification", cutoff)
    checkpoint_job = checkpoint.get("job_id")
    result_at, paired = (None, {}) if checkpoint_at and not checkpoint_job else _paired_result(
        db, key, version, cutoff, checkpoint_job=checkpoint_job, strategy_reference=strategy_reference is not None)
    execution = policy["research_execution"]
    requires_artifacts = execution.get("require_source_artifacts", False)
    args = key, version, window[0][0], window[-1][0], cutoff.isoformat()
    signals = {day: (instant(at), body, job, dataset, source, result) for day, at, body, job, dataset, source, result
               in db.execute("SELECT s.day,s.at,s.body,s.job_id,s.dataset,j.dataset,r.at FROM fund_strategy_signals s "
                             "LEFT JOIN fund_strategy_jobs j ON j.id=s.job_id AND j.strategy_id=s.id AND j.version=s.version "
                             "LEFT JOIN fund_strategy_results r ON r.job_id=j.id "
                             "WHERE s.id=? AND s.version=? AND s.day BETWEEN ? AND ? AND julianday(s.at)<=julianday(?)",
                             args) if instant(at) <= cutoff}
    observations = {day: (instant(at), body, dataset) for day, at, body, dataset in db.execute(
        "SELECT day,at,body,dataset FROM fund_strategy_forward WHERE id=? AND version=? AND day BETWEEN ? AND ? "
        "AND julianday(at)<=julianday(?)", args) if instant(at) <= cutoff}
    rows = []
    for day, opened, closed, previous_close in window:
        expected_cutoff = instant(signal_cutoff(previous_close, plan))
        signal_at, raw_signal, job, signal_dataset, signal_source, signal_result = signals.get(day, (None,) * 6)
        recorded, raw_observation, input_dataset = observations.get(day, (None, None, None))
        try:
            signal = json.loads(raw_signal) if raw_signal else {}
            observation = json.loads(raw_observation) if raw_observation else {}
            signal_source_status = _source_status(db, signal_source, signal_at, requires_artifacts)
            observation_source_status = _source_status(db, input_dataset, recorded, requires_artifacts)
            source_states = {"artifact_bytes_matched", "snapshot_recorded"}
            signal_ok = (_signal_valid(signal_at, signal, opened, expected_cutoff, row[3], row[4])
                         and signal_source_status in source_states and signal_result is not None
                         and instant(signal_result) <= signal_at
                         and _source_status(db, signal_dataset, signal_at, False) == "snapshot_recorded")
            if signal_ok and strategy_reference is not None:
                signal_ok = reference_signal_ready(signal.get("strategy_reference"), strategy_reference, expected_cutoff)
            status = _session_state(opened, closed, cutoff, signal_ok, recorded, observation,
                                    paired.get(day), result_at, signal, strategy_reference)
            if status == "prospective" and observation_source_status not in source_states:
                status = "inconsistent_evidence"
        except (AttributeError, KeyError, TypeError, ValueError):
            status = "inconsistent_evidence"
            signal_source_status = observation_source_status = None
        rows.append({"session": day, "open_at": opened.isoformat(), "close_at": closed.isoformat(), "status": status,
                     "signal_at": signal_at.isoformat() if signal_at else None, "signal_job_id": job,
                     "signal_dataset": signal_dataset, "signal_source_dataset": signal_source,
                     "signal_source_status": signal_source_status,
                     "observation_at": recorded.isoformat() if recorded else None,
                     "observation_source_dataset": input_dataset, "observation_source_status": observation_source_status})
        if plan.get("signal_delay_minutes"):
            rows[-1]["signal_cutoff"] = expected_cutoff.isoformat()
        if opened > cutoff:
            break

    prefix = [row["session"] for row in takewhile(lambda row: row["status"] == "prospective", rows)]
    first_gap = next((row for row in rows if row["status"] in {"missed_signal", "invalid_signal", "replayed",
                                                              "inconsistent_evidence", "paired_result_unavailable"}), None)
    status = checkpoint.get("status", "window_elapsed_without_checkpoint" if window[-1][2] <= cutoff else "collecting")
    return {**audit, "status": status, "historical_pass_at": frozen.isoformat(),
            "source_artifacts_required": requires_artifacts, "window_evidence_complete": len(prefix) == len(window),
            "window": {"first_session": window[0][0], "last_session": window[-1][0], "required_sessions": len(window),
                       "opened_sessions": sum(opened <= cutoff for _, opened, _, _ in window),
                       "closed_sessions": sum(closed <= cutoff for _, _, closed, _ in window)},
            "checkpoint": {**checkpoint, "recorded_at": checkpoint_at.isoformat()} if checkpoint_at else None,
            "counts": dict(Counter(row["status"] for row in rows)), "first_evidence_gap": first_gap,
            "sessions": rows, "economic_comparison": _economics(paired, prefix, execution["initial_capital"])}
