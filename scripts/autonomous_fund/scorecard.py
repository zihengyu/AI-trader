"""Deterministic, look-ahead-safe scorecards for AI predictions and allocations.

The AI writes falsifiable predictions in each post-market review and makes
allocation decisions. This module scores both against COMPLETED daily closes
only, so a later review sees an objective track record rather than its own
narrative. Nothing here calls a model, a broker or the network, and nothing is
estimated when a close is missing: such items stay `pending` or `unavailable`.
"""
from zoneinfo import ZoneInfo

from .risk import instant

ZONE = ZoneInfo("America/New_York")
HORIZONS = (1, 5, 20)


def _series(rows):
    ordered = sorted((str(day), float(close)) for day, close in rows or [])
    return [day for day, _ in ordered], [close for _, close in ordered]


def _forward(rows, start_day, horizon):
    """Return (start_close, end_close) `horizon` completed sessions after start_day."""
    days, closes = _series(rows)
    if start_day not in days:
        return None
    index = days.index(start_day)
    if index + horizon >= len(days):
        return "pending"
    return closes[index], closes[index + horizon]


def score_predictions(reviews, closes):
    """Score every review prediction; `closes` maps symbol -> completed daily closes."""
    items = []
    for packet in reviews:
        for prediction in (packet.get("review") or {}).get("predictions", []):
            symbol, benchmark = prediction["symbol"], prediction.get("benchmark") or "SPY"
            horizon = int(prediction["horizon_sessions"])
            item = {"memo_version": packet.get("version"), "session_date": packet["session_date"],
                    "symbol": symbol, "benchmark": benchmark, "direction": prediction["direction"],
                    "horizon_sessions": horizon, "hypothesis_ref": prediction.get("hypothesis_ref", "")}
            own = _forward(closes.get(symbol), packet["session_date"], horizon)
            base = _forward(closes.get(benchmark), packet["session_date"], horizon)
            if own is None or base is None:
                item["status"] = "unavailable"
            elif own == "pending" or base == "pending":
                item["status"] = "pending"
            else:
                mine, market = own[1] / own[0] - 1, base[1] / base[0] - 1
                correct = {"up": mine > 0, "down": mine < 0, "outperform": mine > market,
                           "underperform": mine < market}[prediction["direction"]]
                item.update(status="correct" if correct else "incorrect", symbol_return=mine, benchmark_return=market)
            items.append(item)
    scored = [i for i in items if i["status"] in {"correct", "incorrect"}]
    def rate(rows):
        done = [r for r in rows if r["status"] in {"correct", "incorrect"}]
        return {"scored": len(done), "correct": sum(r["status"] == "correct" for r in done),
                "hit_rate": sum(r["status"] == "correct" for r in done) / len(done) if done else None}
    by_hypothesis = {}
    for item in items:
        by_hypothesis.setdefault(item["hypothesis_ref"] or "unlabeled", []).append(item)
    return {"total": len(items), "pending": sum(i["status"] == "pending" for i in items),
            "unavailable": sum(i["status"] == "unavailable" for i in items), **rate(scored),
            "by_hypothesis": {key: rate(rows) for key, rows in sorted(by_hypothesis.items())},
            "recent": items[-12:],
            "interpretation": "Scored on completed closes only. Small samples are noise; a hypothesis "
                              "should be retired when its scored record is persistently weak."}


def score_decisions(decisions, closes, benchmark="SPY"):
    """Forward return of each AI target portfolio vs the benchmark at equal gross exposure.

    decisions: [(accepted_at_iso, decision_body)]. The start is the CLOSE of the
    decision's own session, so the measurement never uses prices from before the
    decision was made. Cash earns zero; costs and the risk overlay are excluded:
    this isolates the AI's selection skill from execution.
    """
    rows = []
    for at, body in decisions:
        day = instant(at).astimezone(ZONE).date().isoformat()
        weights = {t["symbol"]: float(t["weight"]) for t in body.get("targets", []) if float(t["weight"]) > 0}
        exposure = sum(weights.values())
        row = {"decided_at": at, "session_date": day, "gross_exposure": exposure, "horizons": {}}
        for horizon in HORIZONS:
            market = _forward(closes.get(benchmark), day, horizon)
            paths = {s: _forward(closes.get(s), day, horizon) for s in weights}
            if market is None or any(p is None for p in paths.values()):
                row["horizons"][str(horizon)] = {"status": "unavailable"}
            elif market == "pending" or any(p == "pending" for p in paths.values()):
                row["horizons"][str(horizon)] = {"status": "pending"}
            else:
                portfolio = sum(w * (paths[s][1] / paths[s][0] - 1) for s, w in weights.items())
                benchmark_return = market[1] / market[0] - 1
                row["horizons"][str(horizon)] = {
                    "status": "observed", "portfolio_return": portfolio,
                    "exposure_matched_benchmark_return": exposure * benchmark_return,
                    "excess_return": portfolio - exposure * benchmark_return}
        rows.append(row)
    summary = {}
    for horizon in HORIZONS:
        observed = [r["horizons"][str(horizon)] for r in rows if r["horizons"][str(horizon)]["status"] == "observed"]
        excess = [o["excess_return"] for o in observed]
        summary[str(horizon)] = {"observed": len(observed),
                                 "mean_excess_return": sum(excess) / len(excess) if excess else None,
                                 "positive_excess_rate": sum(e > 0 for e in excess) / len(excess) if excess else None}
    return {"decisions": len(rows), "by_horizon": summary, "recent": rows[-10:],
            "interpretation": "Excess over the benchmark at the same gross exposure, before costs and risk "
                              "scaling. Overlapping horizons are correlated; this is evidence, not proof."}
