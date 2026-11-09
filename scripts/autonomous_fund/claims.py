"""Verify quantitative decision claims against the snapshot sent to the model."""
from collections.abc import Mapping
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MomentumClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(min_length=1, max_length=12)
    metric: Literal["ret_20d", "ret_60d"]
    minimum_percentile: int = Field(ge=1, le=100)


MIN_PEERS = 5


def check_claims(claims, snapshot, universe):
    """Persist verifiable results as seen at decision time, without a second market read."""
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    day = snapshot.get("as_of_close")
    rows = snapshot.get("symbols")
    rows = rows if isinstance(rows, Mapping) else {}
    pools = {}
    results, seen = [], set()
    for claim in claims:
        symbol, metric = claim["symbol"], claim["metric"]
        key = (symbol, metric, claim["minimum_percentile"])
        if key in seen:
            continue
        seen.add(key)
        if metric not in pools:
            pools[metric] = {s: row[metric] for s, row in rows.items() if s in universe and day
                             and isinstance(row, Mapping) and row.get("as_of_close") == day
                             and type(row.get(metric)) in (int, float)
                             and isfinite(row[metric])}
        pool = pools[metric]
        result = {**claim, "as_of_close": day, "peers": len(pool)}
        if snapshot.get("deferred") or symbol not in pool or len(pool) < MIN_PEERS:
            results.append({**result, "status": "unavailable"})
            continue
        value = pool[symbol]
        # Average ranks for ties, with 100 denoting the highest unique return.
        twice_rank = 2 * sum(other < value for other in pool.values()) + sum(other == value for other in pool.values()) + 1
        correct = 50 * twice_rank >= claim["minimum_percentile"] * len(pool)
        results.append({**result, "status": "correct" if correct else "incorrect",
                        "actual_percentile": round(50 * twice_rank / len(pool), 2)})
    return results


def score_claims(decisions):
    """Absent claims and unavailable evidence are reported separately from false claims."""
    checked = [c for _, row in decisions for c in row.get("claim_checks", [])]
    correct = sum(c["status"] == "correct" for c in checked)
    incorrect = sum(c["status"] == "incorrect" for c in checked)
    scored = correct + incorrect
    return {"decisions": len(decisions), "decisions_with_claims": sum(bool(row.get("claim_checks")) for _, row in decisions),
            "correct": correct, "incorrect": incorrect, "unavailable": len(checked) - scored,
            "incorrect_rate": incorrect / scored if scored else None, "recent": checked[-8:]}
