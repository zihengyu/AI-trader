"""How the fund did on the market's large down days (and, for contrast, its large up days).

Averages hide the days that matter most: a fund can look fine on average and still
lose as much as the market when it falls hard. From each reviewed session's
deterministic attribution (fund return, benchmark return, the market part =
beta x benchmark, selection and timing) this separates days when the benchmark
fell by at least DOWN_DAY (and rose by at least UP_DAY) and reports:

- capture: the fund's summed return over the benchmark's on those days (0.6 = lost
  60% of what the market lost; negative = gained while the market fell);
- the effective beta at the start of those days against the other days (a lower
  beta before falls means risk was already reduced; with few days it may be luck);
- selection and timing contributions on those days;
- protective exits (stops and take-profits) on those days and their next-session value.

Evidence for the review, never a trading rule.
"""
from __future__ import annotations

from math import isfinite

from .risk import instant
from .scorecard import ZONE, _forward

DOWN_DAY = -0.02
UP_DAY = 0.02
MIN_DAYS = 3


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _group(rows):
    if not rows:
        return {"days": 0}
    fund, market = sum(r["fund"] for r in rows), sum(r["benchmark"] for r in rows)
    betas = [r["beta"] for r in rows if r["beta"] is not None]
    result = {"days": len(rows), "mean_fund_return": fund / len(rows), "mean_benchmark_return": market / len(rows),
              "capture": fund / market if market else None,
              "mean_beta": sum(betas) / len(betas) if betas else None}
    for key in ("selection", "timing"):
        known = [r[key] for r in rows if r[key] is not None]
        # A day whose part is unknown is counted, never summed as zero.
        result[key] = sum(known) if known else None
        result[f"{key}_unknown_days"] = len(rows) - len(known)
    return result


def score(reviews, triggers=(), closes=None, *, sessions=None):
    """reviews: postmarket_review bodies (with `attribution`); triggers: [(at, body)] protective_trigger events."""
    days = {}
    for body in reviews:
        item = body.get("attribution") or {}
        if item.get("status") not in {"complete", "partial"}:
            continue
        benchmark, fund = item.get("benchmark_return"), item.get("observed_return")
        if not _number(benchmark) or not _number(fund) or not item.get("session_date"):
            continue
        parts = item.get("components") or {}
        market = parts.get("market")
        days[item["session_date"]] = {
            "day": item["session_date"], "benchmark": benchmark, "fund": fund,
            "beta": market / benchmark if _number(market) and benchmark else None,
            "selection": parts.get("selection") if _number(parts.get("selection")) else None,
            "timing": parts.get("timing") if _number(parts.get("timing")) else None}
    rows = [days[day] for day in sorted(days)]
    down = [r for r in rows if r["benchmark"] <= DOWN_DAY]
    up = [r for r in rows if r["benchmark"] >= UP_DAY]
    other = [r for r in rows if DOWN_DAY < r["benchmark"] < UP_DAY]
    down_days = {r["day"] for r in down}
    exits = []
    for at, body in triggers:
        day = instant(at).astimezone(ZONE).date().isoformat()
        if day not in down_days or body.get("bid") is None:
            continue
        path = _forward((closes or {}).get(body.get("symbol")), day, 1, sessions=sessions)
        exits.append(-(path[1] / float(body["bid"]) - 1) if isinstance(path, tuple) else None)
    valued = [v for v in exits if v is not None]
    down_summary, other_summary = _group(down), _group(other)
    beta_cut = (other_summary["mean_beta"] - down_summary["mean_beta"]
                if down_summary.get("mean_beta") is not None and other_summary.get("mean_beta") is not None else None)
    return {"status": "observed" if len(down) >= MIN_DAYS else "insufficient_sample", "minimum_days": MIN_DAYS,
            "down_threshold": DOWN_DAY, "up_threshold": UP_DAY, "sessions": len(rows),
            "down_days": {**down_summary, "dates": [r["day"] for r in down][-10:]},
            "up_days": _group(up), "other_days": other_summary,
            "beta_lower_on_down_days_by": beta_cut,
            "protective_exits_on_down_days": {"count": len(exits), "valued": len(valued),
                                              "mean_value_added_1d": sum(valued) / len(valued) if valued else None},
            "meaning": "capture = the fund's summed return / the benchmark's on those days (0.6 = lost 60% of the "
                       "market's fall); beta is the effective beta at the start of the day from the attribution; "
                       "evidence for the review, not a trading rule"}
