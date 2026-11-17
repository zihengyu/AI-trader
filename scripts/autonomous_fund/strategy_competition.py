"""Comparable forward evidence, never a recent-return winner-selection switch."""
import json
import math
import statistics

from .risk import instant
from .strategy_validation import metrics


def compare(journal, now):
    entries, returns = [], {}
    with journal.connection() as db:
        versions = db.execute("SELECT id,version,state,policy,proposal FROM fund_strategy_versions ORDER BY created_at DESC").fetchall()
        for key, version, state, policy, proposal in versions[:20]:
            row = db.execute("SELECT r.body FROM fund_strategy_results r JOIN fund_strategy_jobs j ON j.id=r.job_id "
                             "WHERE j.strategy_id=? AND j.version=? AND j.kind='forward' AND "
                             "julianday(r.at)<=julianday(?) ORDER BY r.at DESC LIMIT 1",
                             (key, version, instant(now).isoformat())).fetchone()
            report = json.loads(row[0]) if row else {}
            series, baseline = report.get("series") or [], report.get("reference_series") or []
            if not series or len(baseline) != len(series):
                continue
            capital = json.loads(policy)["research_execution"]["initial_capital"]
            actual, reference = metrics(series, capital), metrics(baseline, capital)
            identity = key + " / " + version
            returns[identity] = {r["day"]: value for r, value in zip(series, actual["daily_returns"])}
            entries.append({"strategy": identity, "state": state, "observations": len(series),
                            "prospective_sessions": report.get("prospective_sessions"),
                            "replayed_sessions": report.get("replayed_sessions"),
                            "net_return": actual["net_return"], "max_drawdown": actual["max_drawdown"],
                            "annual_volatility": actual["annual_volatility"],
                            "passive_reference_return": reference["net_return"],
                            "net_excess": actual["net_return"] - reference["net_return"],
                            "benchmark": json.loads(proposal)["validation"]["benchmark"],
                            "coverage_complete": report.get("coverage_complete", False),
                            "incremental_portfolio_value": "unestablished; correlation alone is not evidence of value"})
    correlations = []
    names = list(returns)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            days = sorted(returns[left].keys() & returns[right].keys())
            if len(days) < 20:
                continue
            a, b = [returns[left][d] for d in days], [returns[right][d] for d in days]
            value = statistics.correlation(a, b) if statistics.pstdev(a) > 0 and statistics.pstdev(b) > 0 else None
            correlations.append({"left": left, "right": right, "paired_sessions": len(days),
                                 "correlation": value if value is not None and math.isfinite(value) else None})
    return {"strategies": entries, "correlations": correlations,
            "meaning": "Independent forward books with predeclared passive references. Unequal periods and risk "
                       "are disclosed; no cumulative-return ranking, auto-winner promotion or alpha claim."}
