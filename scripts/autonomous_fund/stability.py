"""Decision stability: the same input to the manager twice; how different are its answers?

A language model's answer varies between calls on identical input. If two answers
to one context differ about as much as consecutive real decisions do, much of the
portfolio's turnover is sampling noise rather than new information. Once a
session (validation.stability_checks_per_session) an accepted decision's exact
context is sent to the manager again; the second answer is compared, recorded and
scored, and never traded.
"""
from __future__ import annotations


def _weights(body):
    weights = {}
    for target in (body or {}).get("targets", []):
        try:
            weight = float(target["weight"])
        except (KeyError, TypeError, ValueError):
            continue
        if weight > 0:
            weights[target["symbol"]] = weight
    return weights


def compare(first, second):
    """How far apart two target portfolios are: symbol overlap, weight distance, same by the 1-point rule."""
    a, b = _weights(first), _weights(second)
    union = set(a) | set(b)
    overlap = len(set(a) & set(b)) / len(union) if union else 1.0
    distance = sum(abs(a.get(s, 0.0) - b.get(s, 0.0)) for s in union) / 2
    same = set(a) == set(b) and all(abs(a[s] - b[s]) < 0.01 for s in a)
    return {"symbol_overlap": round(overlap, 4), "weight_distance": round(distance, 4), "same_targets": same,
            "exposure_difference": round(sum(b.values()) - sum(a.values()), 4),
            "first_symbols": sorted(a), "second_symbols": sorted(b)}


def score(checks, decisions):
    """Summary of the stability checks, against how much consecutive real decisions changed.

    checks: decision_stability bodies; decisions: validated decision bodies in time order.
    noise_ratio = mean repeat distance / mean distance between consecutive decisions: near 1
    means real changes are about as large as asking again.
    """
    valid = [c for c in checks if c.get("valid")]
    moves = [compare(a, b)["weight_distance"] for a, b in zip(decisions, decisions[1:])]
    mean = (lambda values: sum(values) / len(values) if values else None)  # noqa: E731
    repeat, actual = mean([c["weight_distance"] for c in valid]), mean(moves)
    return {"checks": len(valid), "failed": len(checks) - len(valid),
            "mean_weight_distance": repeat, "mean_symbol_overlap": mean([c["symbol_overlap"] for c in valid]),
            "same_rate": mean([1.0 if c["same_targets"] else 0.0 for c in valid]),
            "mean_consecutive_distance": actual,
            "noise_ratio": repeat / actual if repeat is not None and actual else None,
            "recent": [{k: c.get(k) for k in ("decision_id", "weight_distance", "symbol_overlap", "same_targets")}
                       for c in valid[-5:]],
            "meaning": "weight_distance = share of the portfolio that differs when the same input is asked again "
                       "(half the summed weight differences); noise_ratio compares it with the change between "
                       "consecutive real decisions"}
