"""Deterministic, look-ahead-safe scorecards for AI predictions and allocations.

The AI writes falsifiable predictions in each post-market review and makes
allocation decisions. This module scores both against COMPLETED daily closes
only, so a later review sees an objective track record rather than its own
narrative. Nothing here calls a model, a broker or the network, and nothing is
estimated when a close is missing: such items stay `pending` or `unavailable`.
"""
import math
from datetime import date, datetime, time
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


CONFIDENCE_BUCKETS = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.96))
MIN_INFORMATIVE_IDEAS = 30  # Below this a hit rate is noise, whatever the p-value says.
IDEA_SIGNIFICANCE = 0.05
RISING = {"up": True, "outperform": True, "down": False, "underperform": False}


def _start_index(days, formed_at):
    """Index of the first completed close after `formed_at` (16:00 New York on its session day).

    None when the series has no such close yet, or does not reach back before the
    formation (an idea older than the window cannot be placed honestly).
    """
    formed = instant(formed_at).astimezone(ZONE)
    for index, day in enumerate(days):
        if datetime.combine(date.fromisoformat(day), time(16), ZONE) > formed:
            return index if index > 0 else None
    return "pending"


def binomial_tail(successes, trials):
    """P(X >= successes) for X ~ Binomial(trials, 1/2), exact (integers, no underflow)."""
    if trials <= 0:
        return None
    return sum(math.comb(trials, k) for k in range(successes, trials + 1)) / 2 ** trials


def _idea_outcome(idea, closes):
    symbol, benchmark = idea["symbol"], idea.get("benchmark") or "SPY"
    horizon = int(idea["horizon_sessions"])
    item = {key: idea.get(key) for key in ("idea_id", "symbol", "direction", "horizon_sessions", "confidence",
                                           "formed_at")}
    item["benchmark"] = benchmark
    if not closes.get(symbol) or not closes.get(benchmark):
        return {**item, "status": "unavailable", "reason": "no completed closes"}
    days, values = _series(closes[symbol])
    base_days, base_values = _series(closes[benchmark])
    start, base_start = _start_index(days, idea["formed_at"]), _start_index(base_days, idea["formed_at"])
    if start is None or base_start is None:
        return {**item, "status": "unavailable", "reason": "the close history does not cover the formation time"}
    if start == "pending" or base_start == "pending" or start + horizon >= len(days) \
            or base_start + horizon >= len(base_days):
        return {**item, "status": "pending"}
    if days[start] != base_days[base_start] or days[start + horizon] != base_days[base_start + horizon]:
        return {**item, "status": "unavailable", "reason": "symbol and benchmark sessions differ"}
    mine = values[start + horizon] / values[start] - 1
    market = base_values[base_start + horizon] / base_values[base_start] - 1
    rising, relative = RISING[idea["direction"]], idea["direction"] in {"outperform", "underperform"}
    move = mine - market if relative else mine
    exposure = beta(closes[symbol], closes[benchmark], days[start])  # Closes through the start only.
    residual = mine - exposure * market if exposure is not None else None
    return {**item, "status": "correct" if (move > 0) == rising and move != 0 else "incorrect",
            "start_day": days[start], "end_day": days[start + horizon], "symbol_return": mine,
            "benchmark_return": market, "beta": exposure, "residual_return": residual,
            # Beyond the beta-matched market move: was the stock chosen, not just the market's direction?
            "selection_correct": None if residual is None or residual == 0 else (residual > 0) == rising,
            # The naive "always up" caller: up for absolute ideas, outperform for relative ones.
            "always_up_correct": move > 0}


def score_ideas(ideas, closes):
    """Score research ideas registered before the open: calibration, Brier and naive baselines.

    Each idea is measured from the first completed close after it was formed (so a
    pre-market move the researcher had already seen is never credited) to the close
    `horizon_sessions` later. An idea counts as informative only when the sample is
    large enough and it beats both naive baselines: a coin (50% hit rate, Brier 0.25)
    and "always up" (a sign test on the ideas where the two disagree, i.e. the
    down/underperform calls). Selection is judged separately on the return beyond the
    beta-matched benchmark move. Overlapping horizons are correlated, so the p-values
    are optimistic; they gate nothing and are evidence for the AI and the operator.
    """
    items = [_idea_outcome(idea, closes) for idea in ideas]
    scored = [i for i in items if i["status"] in {"correct", "incorrect"}]
    hits = [i["status"] == "correct" for i in scored]
    count, correct = len(scored), sum(hits)
    brier = sum((float(i["confidence"]) - h) ** 2 for i, h in zip(scored, hits)) / count if count else None
    buckets = {}
    for low, high in CONFIDENCE_BUCKETS:
        rows = [(i, h) for i, h in zip(scored, hits) if low <= float(i["confidence"]) < high]
        stated = sum(float(i["confidence"]) for i, _ in rows) / len(rows) if rows else None
        rate = sum(h for _, h in rows) / len(rows) if rows else None
        buckets[f"{low:.1f}-{min(high, 0.95):.2f}"] = {
            "scored": len(rows), "hit_rate": rate, "mean_confidence": stated,
            "calibration_gap": rate - stated if rows else None}
    up = [i["always_up_correct"] for i in scored]
    wins = sum(h and not u for h, u in zip(hits, up))
    losses = sum(u and not h for h, u in zip(hits, up))
    picks = [i["selection_correct"] for i in scored if i["selection_correct"] is not None]
    versus_coin = binomial_tail(correct, count)
    versus_up = binomial_tail(wins, wins + losses)
    if count < MIN_INFORMATIVE_IDEAS:
        verdict = "insufficient_sample"
    elif (versus_coin is not None and versus_coin < IDEA_SIGNIFICANCE and versus_up is not None
          and versus_up < IDEA_SIGNIFICANCE and brier < 0.25):
        verdict = "informative"
    else:
        verdict = "not_informative"
    return {
        "total": len(items), "scored": count, "correct": correct, "hit_rate": correct / count if count else None,
        "pending": sum(i["status"] == "pending" for i in items),
        "unavailable": sum(i["status"] == "unavailable" for i in items),
        "brier": brier, "brier_skill_vs_coin": 1 - brier / 0.25 if brier is not None else None,
        "by_confidence": buckets,
        "baselines": {
            "coin": {"hit_rate": 0.5, "brier": 0.25, "p_value": versus_coin},
            "always_up": {"hit_rate": sum(up) / count if count else None, "ideas_where_different": wins + losses,
                          "better": wins, "worse": losses, "p_value": versus_up}},
        "selection": {"scored": len(picks), "hit_rate": sum(picks) / len(picks) if picks else None,
                      "p_value": binomial_tail(sum(picks), len(picks))},
        "verdict": verdict, "minimum_sample": MIN_INFORMATIVE_IDEAS,
        "recent": items[-12:],
        "interpretation": "Ideas are scored on completed closes from the first close after they were formed. "
                          "informative needs at least minimum_sample scored ideas, a hit rate above a coin and "
                          "above always-up (p < 0.05 each) and a Brier score under 0.25. selection uses the return "
                          "beyond the beta-matched benchmark. Overlapping horizons make p-values optimistic.",
    }


BETA_LOOKBACK = 126
MIN_BETA_OBSERVATIONS = 20
DEFAULT_COST_BPS = 10


def beta(rows, benchmark_rows, through_day, lookback=BETA_LOOKBACK):
    """OLS beta of daily close-to-close returns to the benchmark, using closes through `through_day` only.

    None when fewer than MIN_BETA_OBSERVATIONS common returns exist or the benchmark did not move.
    """
    own, base = dict(_series_pairs(rows)), dict(_series_pairs(benchmark_rows))
    days = sorted(day for day in set(own) & set(base) if day <= through_day)[-(lookback + 1):]
    x = [base[b] / base[a] - 1 for a, b in zip(days, days[1:]) if base[a] > 0 and own[a] > 0]
    y = [own[b] / own[a] - 1 for a, b in zip(days, days[1:]) if base[a] > 0 and own[a] > 0]
    if len(x) < MIN_BETA_OBSERVATIONS:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    variance = sum((v - mx) ** 2 for v in x)
    if variance <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / variance


def _series_pairs(rows):
    return [(str(day), float(close)) for day, close in rows or []]


def score_decisions(decisions, closes, benchmark="SPY", cost_bps=DEFAULT_COST_BPS):
    """Forward return of each AI target portfolio vs a BETA-MATCHED benchmark, gross and net of costs.

    decisions: [(accepted_at_iso, decision_body)] in time order. The start is the
    CLOSE of the decision's own session, so the measurement never uses prices
    from before the decision was made. Each name's beta comes from closes through
    that session only. A portfolio of high-beta names in a rising market is not
    skill: excess_return = portfolio - portfolio_beta x benchmark. A name whose
    beta cannot be estimated is assumed to have beta 1 and is listed in
    beta_assumed. Costs: |change in target weights| versus the previous decision
    (the first one starts from cash) times cost_bps per side. Cash earns zero and
    the risk overlay is excluded: this isolates the AI's selection skill.
    """
    rows, previous = [], {}
    for at, body in decisions:
        day = instant(at).astimezone(ZONE).date().isoformat()
        weights = {t["symbol"]: float(t["weight"]) for t in body.get("targets", []) if float(t["weight"]) > 0}
        exposure = sum(weights.values())
        betas, assumed = {}, []
        for symbol in weights:
            estimate = 1.0 if symbol == benchmark else beta(closes.get(symbol), closes.get(benchmark), day)
            if estimate is None:
                estimate = 1.0
                assumed.append(symbol)
            betas[symbol] = estimate
        portfolio_beta = sum(w * betas[s] for s, w in weights.items())
        turnover = sum(abs(weights.get(s, 0.0) - previous.get(s, 0.0)) for s in set(weights) | set(previous))
        cost = turnover * cost_bps / 10_000
        previous = weights
        row = {"decided_at": at, "session_date": day, "gross_exposure": exposure, "beta": portfolio_beta,
               "beta_assumed": sorted(assumed), "turnover": turnover,
               "prompt_version": body.get("prompt_version") or "unversioned", "horizons": {}}
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
                excess = portfolio - portfolio_beta * benchmark_return
                row["horizons"][str(horizon)] = {
                    "status": "observed", "portfolio_return": portfolio,
                    "beta_matched_benchmark_return": portfolio_beta * benchmark_return,
                    "excess_return": excess, "cost": cost, "net_excess_return": excess - cost,
                    "exposure_matched_benchmark_return": exposure * benchmark_return,
                    "exposure_excess_return": portfolio - exposure * benchmark_return}
        rows.append(row)

    def summary(selected):
        result = {}
        for horizon in HORIZONS:
            observed = [r["horizons"][str(horizon)] for r in selected
                        if r["horizons"][str(horizon)]["status"] == "observed"]
            gross = [o["excess_return"] for o in observed]
            net = [o["net_excess_return"] for o in observed]
            result[str(horizon)] = {
                "observed": len(observed),
                "mean_excess_return": sum(gross) / len(gross) if gross else None,
                "mean_net_excess_return": sum(net) / len(net) if net else None,
                "positive_excess_rate": sum(e > 0 for e in gross) / len(gross) if gross else None,
                "mean_exposure_excess_return": (sum(o["exposure_excess_return"] for o in observed) / len(observed)
                                                if observed else None)}
        return result

    versions = {}
    for row in rows:
        versions.setdefault(row["prompt_version"], []).append(row)
    return {"decisions": len(rows), "by_horizon": summary(rows),
            "by_prompt_version": {version: summary(selected) for version, selected in versions.items()},
            "cost_bps_per_side": cost_bps, "recent": rows[-10:],
            "interpretation": "excess_return is over a beta-matched benchmark (portfolio beta from closes through "
                              "the decision day), so market exposure is not counted as skill; net_excess_return "
                              "also charges turnover costs. Overlapping horizons are correlated; this is evidence, "
                              "not proof."}


def score_timing(events, closes):
    """Did the AI's own entry and exit levels add value? Completed closes only.

    events: [(at_iso, kind, body)] with kinds protective_trigger, entry_waiting and
    order_submitted. A protective exit sold near `bid` on its session; its value
    added after h sessions is -(close[h] / bid - 1): positive means the exit avoided
    a further fall, negative that it gave up a rebound. An entry wait compares the
    ask when waiting began with the buy limit eventually submitted for the same
    decision (an upper bound on the fill price), or, when no buy followed, with the
    later close: a rise is the gain the wait missed, a fall is the loss it avoided.
    """
    def day_of(at):
        return instant(at).astimezone(ZONE).date().isoformat()

    exits, waits = [], []
    buys = [(instant(at), body) for at, kind, body in events
            if kind == "order_submitted" and body.get("side") == "buy" and body.get("kind", "allocation") == "allocation"]
    for at, kind, body in events:
        if kind == "protective_trigger" and body.get("bid") is not None:
            price, row = float(body["bid"]), {"at": at, "symbol": body["symbol"], "trigger": body["trigger"],
                                              "bid": float(body["bid"]), "horizons": {}}
            for horizon in HORIZONS:
                path = _forward(closes.get(body["symbol"]), day_of(at), horizon)
                if path is None or path == "pending":
                    row["horizons"][str(horizon)] = {"status": "unavailable" if path is None else "pending"}
                else:
                    row["horizons"][str(horizon)] = {"status": "observed", "value_added": -(path[1] / price - 1)}
            exits.append(row)
        elif kind == "entry_waiting":
            ask, symbol = float(body["ask"]), body["symbol"]
            entered = next((b for t, b in buys if t >= instant(at) and b.get("symbol") == symbol
                            and b.get("decision_id") == body.get("decision_id")), None)
            row = {"at": at, "symbol": symbol, "entry_limit": body.get("entry_limit"), "ask": ask,
                   "entered": entered is not None, "horizons": {}}
            if entered is not None:
                row["entry_improvement"] = (ask - float(entered["limit"])) / ask
            else:
                for horizon in HORIZONS:
                    path = _forward(closes.get(symbol), day_of(at), horizon)
                    if path is None or path == "pending":
                        row["horizons"][str(horizon)] = {"status": "unavailable" if path is None else "pending"}
                    else:
                        row["horizons"][str(horizon)] = {"status": "observed", "value_added": -(path[1] / ask - 1)}
            waits.append(row)

    def summary(rows):
        result = {"count": len(rows)}
        for horizon in HORIZONS:
            values = [r["horizons"][str(horizon)]["value_added"] for r in rows
                      if r["horizons"].get(str(horizon), {}).get("status") == "observed"]
            result[str(horizon)] = {"observed": len(values),
                                    "mean_value_added": sum(values) / len(values) if values else None,
                                    "helpful_rate": sum(v > 0 for v in values) / len(values) if values else None}
        return result

    entered = [r for r in waits if r["entered"]]
    improvements = [r["entry_improvement"] for r in entered]
    return {
        "protective_exits": {trigger: summary([r for r in exits if r["trigger"] == trigger])
                             for trigger in ("stop_loss", "take_profit")},
        "entry_waits": {"count": len(waits), "entered": len(entered),
                        "mean_entry_improvement": sum(improvements) / len(improvements) if improvements else None,
                        "not_entered": summary([r for r in waits if not r["entered"]])},
        "recent": (exits + waits)[-12:],
        "interpretation": "value_added > 0: the exit avoided a further fall, or skipping the entry avoided a loss; "
                          "< 0: the exit gave up a rebound, or the wait missed a gain. entry_improvement compares the "
                          "submitted buy limit with the ask when waiting began. Small samples are noise; use this to "
                          "calibrate how tight stops and entry limits should be, not to stop using protection.",
    }
