"""Deterministic, look-ahead-safe scorecards for AI predictions and allocations.

The AI writes falsifiable predictions in each post-market review and makes
allocation decisions. This module scores both against COMPLETED daily closes
only, so a later review sees an objective track record rather than its own
narrative. Nothing here calls a model, a broker or the network, and nothing is
estimated when a close is missing: such items stay `pending` or `unavailable`.
"""
import math
from bisect import bisect_left
from datetime import date, timedelta
from functools import lru_cache
from statistics import mean, variance
from zoneinfo import ZoneInfo

from .risk import instant

ZONE = ZoneInfo("America/New_York")
HORIZONS = (1, 5, 20)


@lru_cache(maxsize=32)
def _exchange_sessions(first, cutoff):
    import pandas_market_calendars as mcal

    end = date.fromisoformat(str(cutoff)) - timedelta(days=1)
    if date.fromisoformat(first) > end:
        return ()
    schedule = mcal.get_calendar("NYSE").schedule(start_date=first, end_date=end)
    return tuple(day.date().isoformat() for day in schedule.index)


def completed_sessions(closes, cutoff):
    """Real US equity sessions within the price history and before the exclusive cutoff."""
    first = min((str(day) for rows in closes.values() for day, _ in rows or ()), default=None)
    return _exchange_sessions(first, str(cutoff)) if first else ()


def _series(rows):
    ordered = sorted((str(day), float(close)) for day, close in rows or [])
    return [day for day, _ in ordered], [close for _, close in ordered]


def _forward(rows, start_day, horizon, *, sessions=None):
    """Return endpoint closes on a shared session calendar when supplied.

    A missing endpoint is unavailable, never replaced by the next observed bar.
    Intermediate bars are not needed to measure an endpoint-to-endpoint return.
    """
    days, closes = _series(rows)
    if start_day not in days:
        return None
    calendar = days if sessions is None else sessions
    if start_day not in calendar:
        return None
    index = calendar.index(start_day)
    if index + horizon >= len(calendar):
        return "pending"
    values = dict(zip(days, closes))
    end_day = calendar[index + horizon]
    if end_day not in values:
        return None
    return values[start_day], values[end_day]


def score_predictions(reviews, closes, *, sessions=None):
    """Score every review prediction; `closes` maps symbol -> completed daily closes."""
    items = []
    for packet in reviews:
        for prediction in (packet.get("review") or {}).get("predictions", []):
            symbol, benchmark = prediction["symbol"], prediction.get("benchmark") or "SPY"
            horizon = int(prediction["horizon_sessions"])
            item = {"memo_version": packet.get("version"), "session_date": packet["session_date"],
                    "symbol": symbol, "benchmark": benchmark, "direction": prediction["direction"],
                    "horizon_sessions": horizon, "hypothesis_ref": prediction.get("hypothesis_ref", "")}
            calendar = _series(closes.get(benchmark))[0] if sessions is None else sessions
            own = _forward(closes.get(symbol), packet["session_date"], horizon, sessions=calendar)
            base = _forward(closes.get(benchmark), packet["session_date"], horizon, sessions=calendar)
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


@lru_cache(maxsize=512)
def _idea_session_close(day):
    """A bounded calendar cache for historical formation dates across scorecard refreshes."""
    from .market_data import _session_close
    return _session_close(day)


def _start_index(days, formed_at, *, require_prior=True):
    """Index of the first actual exchange close strictly after `formed_at`.

    "pending" when no later close is available. None when the history does not
    reach back before formation (an older idea cannot be placed honestly).
    """
    formed = instant(formed_at).astimezone(ZONE)
    day = formed.date().isoformat()
    if not require_prior and days and day < days[0]:
        return None
    # Locate the formation day before consulting its calendar. Historical bars
    # before formation need no calendar builds or assumed 16:00 closing times.
    index = bisect_left(days, day)
    if index < len(days) and days[index] == day:
        closed = _idea_session_close(day)
        if closed is None or closed <= formed:
            index += 1
    if index >= len(days):
        return "pending"
    return index if index > 0 or not require_prior else None


def decision_start(at, day, sessions):
    """Return the decision's start session and whether that close is still pending."""
    if sessions is None:
        return day, False
    start = _start_index(sessions, at, require_prior=False) if sessions else None
    return sessions[start] if isinstance(start, int) else None, start == "pending"


def binomial_tail(successes, trials):
    """P(X >= successes) for X ~ Binomial(trials, 1/2), exact (integers, no underflow)."""
    if trials <= 0:
        return None
    return sum(math.comb(trials, k) for k in range(successes, trials + 1)) / 2 ** trials


def _idea_outcome(idea, closes, sessions=None):
    symbol, benchmark = idea["symbol"], idea.get("benchmark") or "SPY"
    horizon = int(idea["horizon_sessions"])
    item = {key: idea.get(key) for key in ("idea_id", "symbol", "direction", "horizon_sessions", "confidence",
                                           "formed_at")}
    item["benchmark"] = benchmark
    if not closes.get(symbol) or not closes.get(benchmark):
        return {**item, "status": "unavailable", "reason": "no completed closes"}
    days, values = _series(closes[symbol])
    base_days, base_values = _series(closes[benchmark])
    calendar = base_days if sessions is None else sessions
    base_start = _start_index(calendar, idea["formed_at"], require_prior=sessions is None)
    if base_start is None or (sessions is None and _start_index(days, idea["formed_at"]) is None):
        return {**item, "status": "unavailable", "reason": "the close history does not cover the formation time"}
    if base_start == "pending":
        return {**item, "status": "pending"}
    start_day = calendar[base_start]
    own, base = dict(zip(days, values)), dict(zip(base_days, base_values))
    if start_day not in own or start_day not in base:
        return {**item, "status": "unavailable", "reason": "missing close at the scheduled start"}
    if base_start + horizon >= len(calendar):
        return {**item, "status": "pending"}
    end_day = calendar[base_start + horizon]
    if end_day not in own or end_day not in base:
        return {**item, "status": "unavailable", "reason": "missing close at the scheduled end"}
    mine = own[end_day] / own[start_day] - 1
    market = base[end_day] / base[start_day] - 1
    rising, relative = RISING[idea["direction"]], idea["direction"] in {"outperform", "underperform"}
    move = mine - market if relative else mine
    exposure = beta(closes[symbol], closes[benchmark], start_day, sessions=sessions)
    residual = mine - exposure * market if exposure is not None else None
    return {**item, "status": "correct" if (move > 0) == rising and move != 0 else "incorrect",
            "start_day": start_day, "end_day": end_day, "symbol_return": mine,
            "benchmark_return": market, "beta": exposure, "residual_return": residual,
            # Beyond the beta-matched market move: was the stock chosen, not just the market's direction?
            "selection_correct": None if residual is None or residual == 0 else (residual > 0) == rising,
            # The naive "always up" caller: up for absolute ideas, outperform for relative ones.
            "always_up_correct": move > 0}


def score_ideas(ideas, closes, regimes=None, *, sessions=None):
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
    items = [_idea_outcome(idea, closes, sessions=sessions) for idea in ideas]
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
        # How far a scored idea's symbol moved over its horizon (relative ideas: beyond the benchmark).
        "mean_abs_move": (sum(abs(i["symbol_return"] - i["benchmark_return"]
                                  if i["direction"] in {"outperform", "underperform"} else i["symbol_return"])
                              for i in scored) / count) if count else None,
        "by_confidence": buckets,
        "confidence_bounds": {f"{low:.1f}-{min(high, 0.95):.2f}": [low, high] for low, high in CONFIDENCE_BUCKETS},
        "baselines": {
            "coin": {"hit_rate": 0.5, "brier": 0.25, "p_value": versus_coin},
            "always_up": {"hit_rate": sum(up) / count if count else None, "ideas_where_different": wins + losses,
                          "better": wins, "worse": losses, "p_value": versus_up}},
        "selection": {"scored": len(picks), "hit_rate": sum(picks) / len(picks) if picks else None,
                      "p_value": binomial_tail(sum(picks), len(picks))},
        "verdict": verdict, "minimum_sample": MIN_INFORMATIVE_IDEAS,
        # Background only: the hit rate in each market regime, and whether it is fading.
        "by_regime": _idea_regimes(scored, hits, regimes),
        "trend": recent_versus_earlier(hits),
        "recent": items[-12:],
        "interpretation": "Ideas are scored on completed closes from the first close after they were formed. "
                          "informative needs at least minimum_sample scored ideas, a hit rate above a coin and "
                          "above always-up (p < 0.05 each) and a Brier score under 0.25. selection uses the return "
                          "beyond the beta-matched benchmark. Overlapping horizons make p-values optimistic.",
    }


TREND_WINDOW = 200     # Sessions in the benchmark's trend mean.
VOL_WINDOW = 20        # Sessions in the benchmark's realized volatility.
VOL_HISTORY = 252      # Earlier sessions whose median volatility separates calm from volatile.
MIN_VOL_HISTORY = 60
REGIMES = ("up_calm", "up_volatile", "down_calm", "down_volatile")
MIN_TREND_SAMPLE = 20  # Scored items before recent and earlier results are compared.
TREND_T = 1.65
MIN_REGIME_WINDOWS = 10
REGIME_T95 = 2.263  # Conservative 95% Student t critical value at the minimum of nine degrees of freedom.


def market_regimes(benchmark_rows, *, sessions=None):
    """{session: regime} of the benchmark, each from closes through that session only.

    Trend: the close against its TREND_WINDOW-session mean (up when at or above it).
    Volatility: the VOL_WINDOW-session realized volatility against the median of up
    to VOL_HISTORY earlier sessions' (volatile when above it). Sessions without
    enough history have no label. Background for grouping results, never a signal.
    """
    days, closes = _series(benchmark_rows)
    if sessions is not None:
        observed = dict(zip(days, closes))
        days, closes = sessions, [observed.get(day) for day in sessions]
    labels, vols = {}, []
    for i in range(len(days)):
        window_closes = closes[max(0, i - VOL_WINDOW):i + 1]
        if i < VOL_WINDOW or any(close is None or close <= 0 for close in window_closes):
            vols.append(None)
            continue
        window = [b / a - 1 for a, b in zip(window_closes, window_closes[1:])]
        mean = sum(window) / len(window)
        vols.append(math.sqrt(sum((r - mean) ** 2 for r in window) / max(1, len(window) - 1)))
        earlier = sorted(v for v in vols[max(0, i - VOL_HISTORY):i] if v is not None)
        if i + 1 < TREND_WINDOW or len(earlier) < MIN_VOL_HISTORY:
            continue
        trend_closes = closes[i - TREND_WINDOW + 1:i + 1]
        if any(close is None or close <= 0 for close in trend_closes):
            continue
        trend = "up" if closes[i] >= sum(trend_closes) / TREND_WINDOW else "down"
        middle = len(earlier) // 2
        median = earlier[middle] if len(earlier) % 2 else (earlier[middle - 1] + earlier[middle]) / 2
        labels[days[i]] = f"{trend}_{'volatile' if vols[i] > median else 'calm'}"
    return labels


def recent_versus_earlier(values):
    """Is a record getting worse? The most recent third of chronological values against the rest.

    Returns the two means, their difference and a Welch t; `change` is weaker_recently or
    stronger_recently only when |t| >= TREND_T. Overlapping horizons are correlated, so
    the t is optimistic: evidence of decay (crowding, a changed market), not proof.
    """
    values = [float(v) for v in values if v is not None and math.isfinite(v)]
    if len(values) < MIN_TREND_SAMPLE:
        return {"status": "insufficient_sample", "scored": len(values), "minimum_sample": MIN_TREND_SAMPLE}
    cut = len(values) - len(values) // 3
    earlier, recent = values[:cut], values[cut:]

    m1, m2 = mean(earlier), mean(recent)
    v1, v2 = variance(earlier), variance(recent)
    scale = math.sqrt(v1 / len(earlier) + v2 / len(recent))
    t = (m2 - m1) / scale if scale > 0 else None
    change = ("weaker_recently" if t is not None and t <= -TREND_T else
              "stronger_recently" if t is not None and t >= TREND_T else "no_clear_change")
    return {"status": "observed", "earlier": {"n": len(earlier), "mean": m1}, "recent": {"n": len(recent), "mean": m2},
            "difference": m2 - m1, "t_stat": t, "change": change}


def _idea_regimes(scored, hits, regimes):
    if not regimes:
        return {}
    groups = {}
    for item, hit in zip(scored, hits):
        groups.setdefault(regimes.get(item["start_day"], "unlabeled"), []).append((item, hit))
    result = {}
    for label, rows in sorted(groups.items()):
        by_day = {}
        for item, hit in rows:
            by_day.setdefault(item["start_day"], []).append((item["end_day"], hit))
        # One equally weighted observation per start date, reserving the longest horizon that day.
        windows = [(day, max(end for end, _ in group), mean(hit for _, hit in group))
                   for day, group in by_day.items()]
        result[label] = {"scored": len(rows), "hit_rate": mean(hit for _, hit in rows),
                         "uncertainty": _regime_interval(windows, label, bounded=True)}
    return result


def _regime_interval(windows, label, *, bounded=False):
    """Approximate 95% interval on a chronological non-overlapping sample."""
    sample, previous_end = [], ""
    for start, end, value in sorted(windows, key=lambda window: window[0]):
        if start >= previous_end:
            sample.append(value)
            previous_end = end
    n = len(sample)
    center = mean(sample) if sample else None
    result = {"non_overlapping": n, "minimum_sample": MIN_REGIME_WINDOWS, "sample_mean": center,
              "status": "unreliable", "interval_95": None}
    if label == "unlabeled" or n < MIN_REGIME_WINDOWS:
        return result
    if bounded:
        # Day-level hit rates are bounded by 0 and 1. This conservative interval
        # also has width when all sampled days are successes or failures.
        radius = math.sqrt(math.log(40) / (2 * n))
        interval = [max(0.0, center - radius), min(1.0, center + radius)]
    else:
        spread = variance(sample)
        if spread <= 0:
            return result
        radius = REGIME_T95 * math.sqrt(spread / n)
        interval = [center - radius, center + radius]
    return {**result, "status": "estimated", "interval_95": interval}


BETA_LOOKBACK = 126
MIN_BETA_OBSERVATIONS = 20
DEFAULT_COST_BPS = 10


def beta(rows, benchmark_rows, through_day, lookback=BETA_LOOKBACK, *, sessions=None):
    """OLS beta of daily close-to-close returns to the benchmark, using closes through `through_day` only.

    None when fewer than MIN_BETA_OBSERVATIONS common returns exist or the benchmark did not move.
    """
    own, base = dict(_series_pairs(rows)), dict(_series_pairs(benchmark_rows))
    common = set(own) & set(base)
    calendar = sorted(common) if sessions is None else sessions
    days = [day for day in calendar if day <= through_day][-(lookback + 1):]
    pairs = [(a, b) for a, b in zip(days, days[1:]) if a in common and b in common and base[a] > 0 and own[a] > 0]
    x = [base[b] / base[a] - 1 for a, b in pairs]
    y = [own[b] / own[a] - 1 for a, b in pairs]
    if len(x) < MIN_BETA_OBSERVATIONS:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    variance = sum((v - mx) ** 2 for v in x)
    if variance <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / variance


def _series_pairs(rows):
    return [(str(day), float(close)) for day, close in rows or []]


def score_decisions(decisions, closes, benchmark="SPY", cost_bps=DEFAULT_COST_BPS, sectors=None, regimes=None, *,
                    sessions=None):
    """Forward return of each AI target portfolio vs a BETA-MATCHED benchmark, gross and net of costs.

    decisions: [(accepted_at_iso, decision_body)] in time order. The start is the
    first scheduled CLOSE after acceptance when an exchange calendar is supplied.
    Each name's beta comes from closes through
    that session only. A portfolio of high-beta names in a rising market is not
    skill: excess_return = portfolio - portfolio_beta x benchmark. A name whose
    beta cannot be estimated is assumed to have beta 1 and is listed in
    beta_assumed. Costs: |change in target weights| versus the previous decision
    (the first one starts from cash) times cost_bps per side. Cash earns zero and
    the risk overlay is excluded: this isolates the AI's selection skill.

    With `sectors` ({symbol: sector}) the excess also splits, exactly, into
    within_sector_selection (each holding against the equal-weighted return of its
    sector's names with closes), sector_allocation (that sector return against the
    holding's beta x benchmark: a sector bet) and unassigned_excess (holdings with no
    sector, such as broad or bond ETFs). A portfolio that only overweights a rising
    sector shows its excess as sector_allocation, not stock selection.
    """
    rows, previous = [], {}
    members = {}
    for symbol, sector in (sectors or {}).items():
        members.setdefault(sector, []).append(symbol)
    sector_cache = {}

    def sector_return(sector, day, horizon):
        key = (sector, day, horizon)
        if key not in sector_cache:
            paths = [_forward(closes.get(s), day, horizon, sessions=calendar) for s in members.get(sector, ())]
            returns = [p[1] / p[0] - 1 for p in paths if isinstance(p, tuple)]
            sector_cache[key] = sum(returns) / len(returns) if returns else None
        return sector_cache[key]
    calendar = _series(closes.get(benchmark))[0] if sessions is None else sessions
    positions = {day: index for index, day in enumerate(calendar)}
    for at, body in decisions:
        day = instant(at).astimezone(ZONE).date().isoformat()
        start_day, start_pending = decision_start(at, day, sessions)
        weights = {t["symbol"]: float(t["weight"]) for t in body.get("targets", []) if float(t["weight"]) > 0}
        exposure = sum(weights.values())
        betas, assumed = {}, []
        for symbol in weights:
            estimate = 1.0 if symbol == benchmark else beta(closes.get(symbol), closes.get(benchmark),
                                                           start_day or day, sessions=sessions)
            if estimate is None:
                estimate = 1.0
                assumed.append(symbol)
            betas[symbol] = estimate
        portfolio_beta = sum(w * betas[s] for s, w in weights.items())
        turnover = sum(abs(weights.get(s, 0.0) - previous.get(s, 0.0)) for s in set(weights) | set(previous))
        cost = turnover * cost_bps / 10_000
        previous = weights
        row = {"decided_at": at, "session_date": day, "start_day": start_day,
               "gross_exposure": exposure, "beta": portfolio_beta,
               **({"regime": regimes.get(start_day, "unlabeled")} if regimes else {}),
               "beta_assumed": sorted(assumed), "turnover": turnover,
               "prompt_version": body.get("prompt_version") or "unversioned", "horizons": {}}
        for horizon in HORIZONS:
            if start_day is None:
                row["horizons"][str(horizon)] = {"status": "pending" if start_pending else "unavailable"}
                continue
            market = _forward(closes.get(benchmark), start_day, horizon, sessions=calendar)
            paths = {s: _forward(closes.get(s), start_day, horizon, sessions=calendar) for s in weights}
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
                    "end_day": calendar[positions[start_day] + horizon],
                    "beta_matched_benchmark_return": portfolio_beta * benchmark_return,
                    "excess_return": excess, "cost": cost, "net_excess_return": excess - cost,
                    "exposure_matched_benchmark_return": exposure * benchmark_return,
                    "exposure_excess_return": portfolio - exposure * benchmark_return}
                if sectors:
                    selection = allocation = other = assigned = 0.0
                    for symbol, weight in weights.items():
                        own = paths[symbol][1] / paths[symbol][0] - 1
                        peer = sector_return(sectors[symbol], start_day, horizon) if symbol in sectors else None
                        if peer is None:
                            other += weight * (own - betas[symbol] * benchmark_return)
                            continue
                        assigned += weight
                        selection += weight * (own - peer)
                        allocation += weight * (peer - betas[symbol] * benchmark_return)
                    row["horizons"][str(horizon)].update(
                        within_sector_selection=selection, sector_allocation=allocation, unassigned_excess=other,
                        sector_assigned_weight=assigned)
        rows.append(row)

    def summary(selected, regime=None):
        result = {}
        for horizon in HORIZONS:
            selected_observed = [(r["start_day"], r["horizons"][str(horizon)]) for r in selected
                                 if r["horizons"][str(horizon)]["status"] == "observed"]
            observed = [o for _, o in selected_observed]
            gross = [o["excess_return"] for o in observed]
            net = [o["net_excess_return"] for o in observed]
            # Cost sensitivity: the same record if trading cost twice or three times the estimate.
            stressed = {k: [o["excess_return"] - k * o["cost"] for o in observed] for k in (2, 3)}
            result[str(horizon)] = {
                "observed": len(observed),
                "mean_excess_return": sum(gross) / len(gross) if gross else None,
                "mean_net_excess_return": sum(net) / len(net) if net else None,
                "mean_net_excess_return_2x_costs": sum(stressed[2]) / len(observed) if observed else None,
                "mean_net_excess_return_3x_costs": sum(stressed[3]) / len(observed) if observed else None,
                "positive_excess_rate": sum(e > 0 for e in gross) / len(gross) if gross else None,
                "mean_exposure_excess_return": (sum(o["exposure_excess_return"] for o in observed) / len(observed)
                                                if observed else None)}
            if regime is not None:
                result[str(horizon)]["uncertainty"] = _regime_interval(
                    [(day, o["end_day"], o["net_excess_return"]) for day, o in selected_observed], regime)
            split = [o for o in observed if "within_sector_selection" in o]
            if split:
                result[str(horizon)].update(
                    mean_within_sector_selection=sum(o["within_sector_selection"] for o in split) / len(split),
                    mean_sector_allocation=sum(o["sector_allocation"] for o in split) / len(split),
                    positive_within_sector_rate=sum(o["within_sector_selection"] > 0 for o in split) / len(split))
        return result

    versions = {}
    for row in rows:
        versions.setdefault(row["prompt_version"], []).append(row)
    regimes_seen = {}
    for row in rows:
        if "regime" in row:
            regimes_seen.setdefault(row["regime"], []).append(row)
    five = [r["horizons"]["5"]["net_excess_return"] for r in rows if r["horizons"]["5"]["status"] == "observed"]
    return {"decisions": len(rows), "by_horizon": summary(rows),
            "by_prompt_version": {version: summary(selected) for version, selected in versions.items()},
            # Background only: results by market regime, and whether the 5-session net excess is fading.
            "by_regime": {label: summary(selected, label) for label, selected in sorted(regimes_seen.items())},
            "trend_5d_net_excess": recent_versus_earlier(five),
            "cost_bps_per_side": cost_bps, "recent": rows[-10:],
            "interpretation": "excess_return is over a beta-matched benchmark (portfolio beta from closes through "
                              "the decision day), so market exposure is not counted as skill; net_excess_return "
                              "also charges turnover costs. Overlapping horizons are correlated; this is evidence, "
                              "not proof."}


def score_timing(events, closes, *, sessions=None):
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
                path = _forward(closes.get(body["symbol"]), day_of(at), horizon, sessions=sessions)
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
                    path = _forward(closes.get(symbol), day_of(at), horizon, sessions=sessions)
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


REVERSAL_SESSIONS = 3  # A move against the previous move within this many sessions is a reversal.
MOVE_STEP = 0.01  # Target changes smaller than one weight point are not moves.


def score_consistency(decisions, triggers, cost_bps=DEFAULT_COST_BPS, window=REVERSAL_SESSIONS):
    """How often the manager reverses itself: a target move against the same symbol's previous move.

    decisions: (at, body) validated decisions in order; triggers: (at, kind, body) protective
    triggers. A move is a target weight change of at least MOVE_STEP (a symbol added,
    raised, lowered or removed). A reversal is a move in the opposite direction of that
    symbol's previous move within `window` NYSE sessions, with no protective exit (stop or
    take-profit) of the symbol in between: the manager changed its mind, the market did
    not force it. Each reversal pays about the round trip of the reversed amount, so its
    estimated cost is 2 x |change| x cost_bps of equity.
    """
    import pandas_market_calendars as mcal
    if not decisions:
        return {"decisions": 0, "moves": 0, "reversals": 0, "reversal_rate": None, "estimated_cost": 0.0,
                "window_sessions": window, "recent": []}
    days = [instant(at).astimezone(ZONE).date() for at, _ in decisions]
    calendar = [index.date() for index in mcal.get_calendar("NYSE").schedule(
        start_date=min(days), end_date=max(days)).index]
    position = {day: i for i, day in enumerate(calendar)}
    exits = sorted((instant(at), body.get("symbol")) for at, _, body in triggers if body.get("symbol"))
    weights, moved, moves, reversals, cost, recent = {}, {}, 0, 0, 0.0, []
    for (at, body), day in zip(decisions, days):
        moment = instant(at)
        now = {t["symbol"]: float(t["weight"]) for t in body.get("targets", []) if float(t["weight"]) > 0}
        for symbol in sorted(set(now) | set(weights)):
            change = now.get(symbol, 0.0) - weights.get(symbol, 0.0)
            if abs(change) < MOVE_STEP:
                continue
            moves += 1
            sign, previous = (1 if change > 0 else -1), moved.get(symbol)
            if previous and previous[0] != sign:
                apart = position.get(day, len(calendar)) - position.get(previous[1], -len(calendar))
                forced = any(previous[2] < when <= moment and name == symbol for when, name in exits)
                if 0 <= apart <= window and not forced:
                    reversals += 1
                    cost += 2 * abs(change) * cost_bps / 10_000
                    recent.append({"symbol": symbol, "first_move_at": previous[2].isoformat(), "reversed_at": moment.isoformat(),
                                   "sessions_apart": apart, "change": round(change, 4)})
            moved[symbol] = (sign, day, moment)
        weights = now
    return {"decisions": len(decisions), "moves": moves, "reversals": reversals,
            "reversal_rate": reversals / moves if moves else None, "estimated_cost": round(cost, 6),
            "window_sessions": window, "cost_bps_per_side": cost_bps, "recent": recent[-8:],
            "interpretation": "A reversal is a target move against the same symbol's previous move within "
                              "window_sessions sessions with no stop or take-profit in between. estimated_cost is a "
                              "fraction of equity: about the round trip of each reversed amount. A high reversal rate "
                              "means the manager changes its mind faster than its own horizon."}
