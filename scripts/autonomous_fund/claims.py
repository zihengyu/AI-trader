"""Verify quantitative decision claims against the snapshot sent to the model.

The manager's rationale cites numbers (a stock's momentum rank, its volatility,
beta, distance from a moving average or from its high, its liquidity). Each such
claim it relies on is also submitted as a structured `factual_claims` entry and
checked deterministically against the same market snapshot the model saw, at
decision time and without a second market read. Two forms, combinable:

- a rank among the same-date eligible names: minimum_percentile and/or
  maximum_percentile (100 = the highest value; 80 = at least the 80th percentile);
- the value itself: minimum and/or maximum (fractions, not percent, as in the snapshot).

A claim is correct only when every bound it states holds. Missing evidence (a
deferred snapshot, a stale or absent row, fewer than MIN_PEERS names for a rank)
is `unavailable`, and a claim stating no bound is `no_bound`: neither is counted
as right or wrong.
"""
from collections.abc import Mapping
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

METRICS = ("ret_1d", "ret_5d", "ret_20d", "ret_60d", "vol_20d", "vol_60d", "beta_60d", "corr_60d",
           "pct_from_ma50", "pct_from_ma200", "pct_from_high_252d", "adv_20d_usd")


class SnapshotClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(min_length=1, max_length=12)
    metric: Literal[METRICS]
    minimum_percentile: int | None = Field(default=None, ge=1, le=100)
    maximum_percentile: int | None = Field(default=None, ge=1, le=100)
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None


MomentumClaim = SnapshotClaim  # The earlier name: momentum ranks are one case of a snapshot claim.
MIN_PEERS = 5
BOUNDS = ("minimum_percentile", "maximum_percentile", "minimum", "maximum")


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def check_claims(claims, snapshot, universe):
    """Persist verifiable results as seen at decision time, without a second market read."""
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    day = snapshot.get("as_of_close")
    rows = snapshot.get("symbols")
    rows = rows if isinstance(rows, Mapping) else {}
    pools = {}
    results, seen = [], set()
    for raw in claims:
        claim = {**{bound: None for bound in BOUNDS}, **raw}
        symbol, metric = claim["symbol"], claim["metric"]
        key = (symbol, metric, *(claim[bound] for bound in BOUNDS))
        if key in seen:
            continue
        seen.add(key)
        if metric not in pools:
            pools[metric] = {s: row[metric] for s, row in rows.items() if s in universe and day
                             and isinstance(row, Mapping) and row.get("as_of_close") == day
                             and _number(row.get(metric))}
        pool = pools[metric]
        result = {**claim, "as_of_close": day, "peers": len(pool)}
        if all(claim[bound] is None for bound in BOUNDS):
            results.append({**result, "status": "no_bound"})
            continue
        ranked = claim["minimum_percentile"] is not None or claim["maximum_percentile"] is not None
        if symbol not in pool or (ranked and (snapshot.get("deferred") or len(pool) < MIN_PEERS)):
            results.append({**result, "status": "unavailable"})
            continue
        value = pool[symbol]
        checks = []
        if ranked:
            # Average ranks for ties, with 100 denoting the highest value.
            twice_rank = (2 * sum(other < value for other in pool.values())
                          + sum(other == value for other in pool.values()) + 1)
            percentile = 50 * twice_rank / len(pool)
            result["actual_percentile"] = round(percentile, 2)
            if claim["minimum_percentile"] is not None:
                checks.append(percentile >= claim["minimum_percentile"])
            if claim["maximum_percentile"] is not None:
                checks.append(percentile <= claim["maximum_percentile"])
        if claim["minimum"] is not None or claim["maximum"] is not None:
            result["actual_value"] = value
            if claim["minimum"] is not None:
                checks.append(value >= claim["minimum"])
            if claim["maximum"] is not None:
                checks.append(value <= claim["maximum"])
        results.append({**result, "status": "correct" if all(checks) else "incorrect"})
    return results


def score_claims(decisions):
    """Absent claims and unavailable evidence are reported separately from false claims."""
    checked = [c for _, row in decisions for c in row.get("claim_checks", [])]
    correct = sum(c["status"] == "correct" for c in checked)
    incorrect = sum(c["status"] == "incorrect" for c in checked)
    scored = correct + incorrect
    by_metric = {}
    for c in checked:
        if c["status"] in {"correct", "incorrect"}:
            group = by_metric.setdefault(c.get("metric"), {"correct": 0, "incorrect": 0})
            group[c["status"]] += 1
    return {"decisions": len(decisions), "decisions_with_claims": sum(bool(row.get("claim_checks")) for _, row in decisions),
            "correct": correct, "incorrect": incorrect, "unavailable": len(checked) - scored,
            "incorrect_rate": incorrect / scored if scored else None, "by_metric": dict(sorted(by_metric.items())),
            "recent": checked[-8:]}
