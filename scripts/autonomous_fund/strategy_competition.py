"""Same-session comparisons for independently recorded strategy books."""
import json
import math
import statistics
from datetime import date, timedelta
from itertools import takewhile

from .risk import instant
from .scorecard import MIN_BETA_OBSERVATIONS, beta, completed_sessions
from .strategy_validation import metrics


def _risk_matched(series, baseline, days):
    # The first session may begin at an opening fill. Twenty close-to-close
    # calibration returns therefore need 21 closes from the forward book.
    calibration = MIN_BETA_OBSERVATIONS + 1
    result = {"status": "insufficient_prospective_sessions", "prospective_prefix_sessions": len(days),
              "calibration_returns_required": MIN_BETA_OBSERVATIONS}
    if len(days) <= calibration:
        return result
    own = [(day, row["net_economic_equity"]) for day, row in zip(days, series)]
    passive = [(day, row["net_economic_equity"]) for day, row in zip(days, baseline)]
    fit_days = days[:calibration]
    try:
        fitted = beta(own[:calibration], passive[:calibration], fit_days[-1], sessions=fit_days)
    except ArithmeticError:
        fitted = None
    result.update(calibration_start=fit_days[0], calibration_end=fit_days[-1],
                  evaluation_start=days[calibration], evaluation_end=days[-1],
                  evaluation_sessions=len(days) - calibration)
    if fitted is None or not math.isfinite(fitted):
        return {**result, "status": "undefined_benchmark_beta"}
    matched = [fitted * (float(b[1]) / float(a[1]) - 1)
               for a, b in zip(passive[calibration - 1:], passive[calibration:])]
    if any(not math.isfinite(value) or value <= -1 for value in matched):
        return {**result, "status": "unusable_scaled_reference", "beta_to_passive_reference": fitted}
    scaled = math.prod(1 + value for value in matched) - 1
    actual = float(own[-1][1]) / float(own[calibration - 1][1]) - 1
    reference = float(passive[-1][1]) / float(passive[calibration - 1][1]) - 1
    if not all(map(math.isfinite, (scaled, actual, reference, actual - scaled))):
        return {**result, "status": "unusable_scaled_reference", "beta_to_passive_reference": fitted}
    return {**result, "status": "estimated" if len(matched) >= MIN_BETA_OBSERVATIONS else "limited_sample",
            "beta_to_passive_reference": fitted, "strategy_net_return": actual, "passive_net_return": reference,
            "beta_scaled_reference_return": scaled, "net_difference_at_frozen_beta": actual - scaled}


def compare_saved(db, now):
    entries, returns, unavailable = [], {}, []
    now = instant(now)
    cutoff = now.isoformat()
    versions = db.execute("SELECT id,version,created_at,policy,proposal FROM fund_strategy_versions "
                          "WHERE julianday(created_at)<=julianday(?) ORDER BY julianday(created_at) DESC,rowid DESC LIMIT 20",
                          (cutoff,)).fetchall()
    for key, version, created_at, policy, proposal in versions:
        if instant(created_at) > now:
            continue
        receipts = db.execute("SELECT r.at,r.job_id FROM fund_strategy_results r JOIN fund_strategy_jobs j ON r.job_id=j.id "
                              "WHERE j.strategy_id=? AND j.version=? AND j.kind='forward' AND "
                              "julianday(r.at)<=julianday(?) ORDER BY julianday(r.at) DESC,r.rowid DESC",
                              (key, version, cutoff))
        # SQLite's date functions round to milliseconds. Check the full
        # timestamps before projecting an archived result into an as-of report.
        row = next((row for row in receipts if instant(row[0]) <= now), None)
        if row is None:
            continue
        identity = key + " / " + version
        try:
            report = json.loads(db.execute("SELECT body FROM fund_strategy_results WHERE job_id=?", (row[1],)).fetchone()[0])
            series, baseline = report.get("series") or [], report.get("reference_series") or []
            if not series:
                unavailable.append({"strategy": identity, "reason": report.get("status") or "missing_forward_series"})
                continue
            days = [item["day"] for item in series]
            if days != [item["day"] for item in baseline] or days != sorted(set(days)):
                unavailable.append({"strategy": identity, "reason": "unpaired_forward_sessions"})
                continue
            # A common endpoint after a gap cannot represent a one-session move.
            end = date.fromisoformat(days[-1]) + timedelta(days=1)
            expected = completed_sessions({"study": ((days[0], 1),)}, end.isoformat())
            if tuple(days) != expected:
                unavailable.append({"strategy": identity, "reason": "nonconsecutive_exchange_sessions"})
                continue
            capital = json.loads(policy)["research_execution"]["initial_capital"]
            actual, reference = metrics(series, capital), metrics(baseline, capital)
            benchmark = json.loads(proposal)["validation"]["benchmark"]
            net_excess = actual["net_return"] - reference["net_return"]
            numbers = [actual["net_return"], reference["net_return"], net_excess, actual["max_drawdown"],
                       *actual["daily_returns"], *reference["daily_returns"]]
            if actual["annual_volatility"] is not None:
                numbers.append(actual["annual_volatility"])
            if not all(map(math.isfinite, numbers)):
                raise ValueError("Nonfinite comparison")
        except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
            unavailable.append({"strategy": identity, "reason": "unreadable_forward_evidence"})
            continue
        proven = {}
        if report.get("coverage_complete") is True:
            proven = {day: equity for day, at, equity in db.execute(
                "SELECT day,at,CASE WHEN json_valid(body) THEN json_extract(body,'$.receipt.net_economic_equity') END "
                "FROM fund_strategy_forward WHERE id=? AND version=? AND julianday(at)<=julianday(?) AND "
                "(CASE WHEN json_valid(body) THEN json_extract(body,'$.evidence') END)='prospective'",
                (key, version, row[0])) if instant(at) <= instant(row[0])}
        # A replayed session changes the book carried into the next day, even
        # when a later signal has a timely receipt.
        prefix = [item["day"] for item in takewhile(
            lambda item: proven.get(item["day"]) == item["net_economic_equity"], series)]
        returns[(key, version)] = dict(zip(prefix[1:], actual["daily_returns"][1:]))
        states = db.execute("SELECT at,CASE WHEN json_valid(body) THEN json_extract(body,'$.state') END "
                            "FROM fund_strategy_events WHERE id=? AND version=? "
                            "AND kind IN ('registered','validation','historical_pass') AND julianday(at)<=julianday(?) "
                            "ORDER BY julianday(at) DESC,sequence DESC", (key, version, cutoff))
        state = next((value for at, value in states if instant(at) <= now), None)
        entries.append({"strategy": identity, "strategy_id": key, "version": version,
                        "state": state, "observations": len(series),
                        "start_day": days[0], "end_day": days[-1],
                        "return_basis": "prospective" if len(prefix) == len(series) else "includes_unconfirmed_sessions",
                        "prospective_sessions": report.get("prospective_sessions"),
                        "replayed_sessions": report.get("replayed_sessions"),
                        "net_return": actual["net_return"], "max_drawdown": actual["max_drawdown"],
                        "annual_volatility": actual["annual_volatility"],
                        "passive_reference_return": reference["net_return"],
                        "net_excess": net_excess,
                        "benchmark": benchmark, "coverage_complete": report.get("coverage_complete", False),
                        "risk_matched": _risk_matched(series, baseline, prefix),
                        "incremental_portfolio_value": "unestablished; requires a portfolio with and without the strategy"})
    correlations = []
    names = list(returns)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            days = sorted(returns[left].keys() & returns[right].keys())
            if len(days) < MIN_BETA_OBSERVATIONS:
                continue
            a, b = [returns[left][d] for d in days], [returns[right][d] for d in days]
            value = statistics.correlation(a, b) if statistics.pstdev(a) > 0 and statistics.pstdev(b) > 0 else None
            correlations.append({"left": " / ".join(left), "right": " / ".join(right), "paired_sessions": len(days),
                                 "correlation": value if value is not None and math.isfinite(value) else None})
    return {"strategies": entries, "correlations": correlations, "unavailable": unavailable,
            "meaning": "Whole series metrics can include replay. Correlation uses common close-to-close sessions "
                       "from uninterrupted prospective prefixes. The risk comparison freezes beta to the net passive "
                       "book before scoring. The scaled reference assumes zero return on residual cash and has no "
                       "extra reweighting or financing costs. It estimates exposure to one reference, with "
                       "portfolio contribution and order authority determined independently."}


def compare(journal, now):
    with journal.connection() as db:
        return compare_saved(db, now)
