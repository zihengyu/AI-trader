"""Degradation monitoring and rollback of what is in effect (V2 spec 4.2).

Thresholds are fixed in advance; short-term profit or loss alone never triggers anything.

- Comparisons (learning memo vs baseline, construction AI weights vs risk budgets):
  the full-record switch rule is unchanged. In addition, when the last WINDOW
  paired sessions turned against the arm that trades, with |t| at least the
  same switch threshold, the other arm (the previous version) trades again.
- Validated factors: the factor lab already retires a factor whose live IC
  turned against its direction; the experiment registry records it.
- Turnover: when the mean daily turnover of the last TURNOVER_RECENT sessions
  is above the 95th percentile of at least TURNOVER_HISTORY earlier sessions,
  the manager's minimum review interval doubles, so it trades less often;
  stops, take-profits and other protective exits are unaffected. It returns to
  normal when that mean falls back to the earlier median.

Exposure limits are enforced by the risk engine on every allocation, so drift
between allocations is corrected at the next one; there is no separate rule.
"""
from zoneinfo import ZoneInfo

from .risk import instant

WINDOW = 20
TURNOVER_RECENT = 5
TURNOVER_HISTORY = 40
ZONE = ZoneInfo("America/New_York")


def rolling_rollback(differences, champion, positive, negative, threshold, window=WINDOW):
    """(arm to roll back to or None, stats of the last `window` differences).

    differences are `positive` minus `negative`, oldest first. Only a full window counts.
    """
    from .validation import paired_test
    recent = paired_test(list(differences)[-window:], window)
    if not recent["sufficient"] or recent["t_stat"] is None:
        return None, recent
    if champion == positive and recent["t_stat"] <= -threshold:
        return negative, recent
    if champion == negative and recent["t_stat"] >= threshold:
        return positive, recent
    return None, recent


def daily_turnover(fills, equity_by_day):
    """[(New York date, traded notional / that day's equity)] for every day with an equity mark.

    fills: (at, kind, body) rows of fill and fill_reconciled. A broker's cumulative
    observation replaces that order's fill callbacks, as in the account's trade list.
    """
    callbacks, reconciled = [], {}
    for at, kind, body in fills:
        if kind == "fill_reconciled":
            reconciled[str(body.get("broker_id"))] = (at, body)
        elif kind == "fill":
            callbacks.append((at, body))
    chosen = [(at, b) for at, b in callbacks if str(b.get("broker_id")) not in reconciled] + list(reconciled.values())
    traded = {}
    for at, body in chosen:
        try:
            notional = abs(float(body["quantity"]) * float(body["price"]))
        except (KeyError, TypeError, ValueError):
            continue
        day = instant(at).astimezone(ZONE).date().isoformat()
        traded[day] = traded.get(day, 0.0) + notional
    return [(day, traded.get(day, 0.0) / equity) for day, equity in sorted(equity_by_day.items()) if equity > 0]


def _quantile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def turnover_check(series, active):
    """{"status", "active", ...}: whether the turnover guard should be on after these sessions."""
    values = [value for _, value in series]
    recent, earlier = values[-TURNOVER_RECENT:], values[:-TURNOVER_RECENT]
    if len(recent) < TURNOVER_RECENT or len(earlier) < TURNOVER_HISTORY:
        return {"status": "insufficient_history", "active": active, "sessions": len(values),
                "required": TURNOVER_RECENT + TURNOVER_HISTORY}
    mean = sum(recent) / len(recent)
    p95, median = _quantile(earlier, 0.95), _quantile(earlier, 0.5)
    result = {"recent_mean": round(mean, 6), "p95": round(p95, 6), "median": round(median, 6),
              "recent_sessions": TURNOVER_RECENT, "earlier_sessions": len(earlier), "last_session": series[-1][0]}
    if not active and mean > p95:
        return {**result, "status": "elevated", "active": True}
    if active and mean <= median:
        return {**result, "status": "recovered", "active": False}
    return {**result, "status": "elevated" if active else "normal", "active": active}
