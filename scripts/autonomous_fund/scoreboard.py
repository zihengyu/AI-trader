"""The fund's own scoreboard: since inception, net of every cost, against its benchmarks.

A professional manager is judged on the return left after every cost, against a
benchmark, and the fund pays its model calls and data whatever the manager decides.
Cash is therefore not neutral: it earns only the bill rate, which those costs can
exceed; cash loses money net of costs only when its income is insufficient.
Benchmark-relative results must be measured, not assumed. From completed sessions only:

- account_return: the account's own return (after trading fees and slippage,
  including the cash-equivalent's interest);
- model_costs_usd / data_costs_usd and net_return: the return after them too;
- benchmarks: each predefined benchmark's total return over the same sessions, and
  the fund's net return minus it;
- cost_pace: the recent spend (last PACE_SESSIONS sessions) as a share of equity per
  year, which is the excess return a year needed just to pay for the system;
- cash: the cash-equivalent's annualized yield over the window and what an all-cash
  portfolio earns net of that cost pace.

A model call without a price is never counted as free: net figures then include
only the priced calls, say how many were unpriced, and are an upper bound.
Evidence for the manager and the reports; never a trading rule.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from zoneinfo import ZoneInfo

from .performance import benchmark_series

TRADING_DAYS = 252
PACE_SESSIONS = 5
MIN_CASH_SESSIONS = 5


def _chained(definition, window, marks, actions):
    growth = 1.0
    for value in benchmark_series(definition, window, marks, actions):
        if value is None:
            return None
        growth *= 1 + value
    return growth - 1


def compute(pairs, calls, data_cost_per_session, definitions, marks, actions, *, sweep=None, equity=None,
            data_charges=None):
    """pairs: validation.session_pairs output; calls: [(instant, cost_usd or None)] of paid model calls.

    data_cost_per_session: USD per session, or None when the operator's data costs are unknown.
    """
    if not pairs:
        return {"status": "insufficient_sample", "sessions": 0,
                "meaning": "no two completed sessions with account marks yet"}
    account, normalized_costs = 1.0, 0.0
    model_total, data_total, unpriced, session_costs = 0.0, 0.0, 0, []
    unknown_data = False
    for pair in pairs:
        inside = [cost for moment, cost in calls if pair["start"] < moment <= pair["end"]]
        known = sum(cost for cost in inside if cost is not None)
        unpriced += sum(cost is None for cost in inside)
        charges = ([cost for moment, cost in data_charges if pair["start"] < moment <= pair["end"]]
                   if data_charges is not None else [data_cost_per_session])
        data = sum(cost for cost in charges if cost is not None)
        unknown_data |= any(cost is None for cost in charges)
        # These expenses accrue outside the broker: money already spent still
        # earns the account's observed return. Do not compound a fictitious debit.
        # Rebase each matched interval to preserve flow-adjusted comparisons.
        normalized_costs += account * (known + data) / pair["equity_start"]
        account *= 1 + pair["return"]
        model_total += known
        data_total += data
        session_costs.append(known + data)
    net = account - normalized_costs
    window = [(p["start_day"], p["end_day"]) for p in pairs]
    capital = pairs[0]["equity_start"]
    current = float(equity) if equity else pairs[-1]["equity_start"] * (1 + pairs[-1]["return"])
    recent = session_costs[-PACE_SESSIONS:]
    pace = sum(recent) / len(recent) * TRADING_DAYS / current if current > 0 else None
    benchmarks = {}
    for definition in definitions:
        value = _chained(definition, window, marks, actions)
        benchmarks[definition["name"]] = {
            "weights": definition["weights"], "return": value,
            "net_excess": (net - 1) - value if value is not None else None}
    cash = None
    if sweep:
        value = _chained({"weights": {sweep: "1"}}, window, marks, actions)
        if value is not None and len(pairs) >= MIN_CASH_SESSIONS:
            annual = (1 + value) ** (TRADING_DAYS / len(pairs)) - 1
            cash = {"symbol": sweep, "return": value, "annual_yield": annual,
                    "annual_net_of_cost_pace": annual - pace if pace is not None else None}
    return {"status": "computed", "since": pairs[0]["start_day"], "through": pairs[-1]["end_day"],
            "sessions": len(pairs), "starting_equity": capital,
            "account_return": account - 1, "model_costs_usd": round(model_total, 4),
            "data_costs_usd": round(data_total, 4) if not unknown_data else None,
            "known_data_costs_usd": round(data_total, 4),
            "costs_share_of_starting_equity": (model_total + data_total) / capital if capital else None,
            "net_return": net - 1, "unpriced_calls": unpriced,
            "net_is_upper_bound": bool(unpriced) or unknown_data,
            "benchmarks": benchmarks,
            "cost_pace": {"sessions": len(recent), "usd_per_session": sum(recent) / len(recent),
                          "share_of_equity_per_year": pace},
            "cash": cash,
            "meaning": "account_return is after trading fees and slippage and includes cash interest; net_return "
                       "also subtracts model and data costs, which the fund pays whatever you decide; each benchmark "
                       "is its total return over the same completed sessions and net_excess is net_return minus it; "
                       "cost_pace.share_of_equity_per_year is the recent spend a year as a share of equity, the "
                       "excess return a year needed just to cover it; cash.annual_net_of_cost_pace is what an "
                       "all-cash portfolio earns a year net of that pace. Evidence, not a trading rule, and never "
                       "a reason to research or think less: spending limits are the operator's"}


def cost_ledger(db, until):
    """Paid calls and one data accrual per New York session, strictly visible at `until`.

    A session locks its rate at its first recorded benchmark. Later startups do
    not reprice earlier sessions, including sessions whose rate was unknown.
    Read lightweight fields; do not load model transcripts or benchmark bodies.
    """
    from .costs import scripted
    from .performance import PerformanceSettings
    from .risk import instant
    if not db.in_transaction:
        db.execute("BEGIN")  # All projections share one SQLite read snapshot.
    cutoff, calls = instant(until), []
    for at, cost, mock, name, cache_hit in db.execute(
            "SELECT at,json_extract(body,'$.cost_usd'),json_extract(body,'$.mock'),json_extract(body,'$.model'),"
            "json_extract(body,'$.cache_hit') FROM events WHERE kind='agent' AND json_valid(body) "
            "AND julianday(at)<=julianday(?)", (until,)):
        moment = instant(at)
        if moment > cutoff or cache_hit or scripted({"mock": mock, "model": name}):
            continue
        calls.append((moment, _cost(cost)))
    default = PerformanceSettings().data_costs_usd_per_month
    rates = []
    for sequence, at, body in db.execute(
            "SELECT sequence,at,body FROM events WHERE kind='startup' AND json_valid(body) "
            "AND julianday(at)<=julianday(?)", (until,)):
        moment = instant(at)
        if moment <= cutoff:
            monthly = (json.loads(body).get("performance") or {}).get("data_costs_usd_per_month", default)
            rates.append((moment, sequence, _cost(monthly)))
    observations = (moment for at, in db.execute(
        "SELECT at FROM events WHERE kind='benchmark' AND julianday(at)<=julianday(?)", (until,))
        for moment in (instant(at),) if moment <= cutoff)
    return calls, session_data_charges(observations, rates)


def session_data_charges(observations, rates):
    """Shared journal/report accrual rule; rates are (instant, sequence, monthly USD)."""
    from .performance import SESSIONS_PER_MONTH, PerformanceSettings
    rates = sorted(rates, key=lambda row: (row[0], row[1]))
    times = [row[0] for row in rates]
    zone, first = ZoneInfo("America/New_York"), {}
    for moment in observations:
        day = moment.astimezone(zone).date()
        first[day] = min(first.get(day, moment), moment)
    charges = []
    for moment in sorted(first.values()):
        index = bisect_right(times, moment) - 1
        rate = _cost(rates[index][2]) if index >= 0 else _cost(PerformanceSettings().data_costs_usd_per_month)
        charges.append((moment, rate / SESSIONS_PER_MONTH if rate is not None else None))
    return charges


def _cost(value):
    """Invalid historical prices remain unknown instead of corrupting a net value."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def known_cost_total(costs):
    """Known accrued USD, including priced data sessions beside unknown ones."""
    return costs["model_usd"] + costs.get("known_data_usd", costs.get("data_usd") or 0)


def accrued_costs(db, until):
    """Estimated accrued costs; unknown prices remain explicit, never a free call/session."""
    calls, charges = cost_ledger(db, until)
    model = sum(cost for _, cost in calls if cost is not None)
    data = sum(cost for _, cost in charges if cost is not None)
    unknown_data = sum(cost is None for _, cost in charges)
    return {"model_usd": round(model, 6), "unpriced_calls": sum(cost is None for _, cost in calls),
            "data_usd": round(data, 6) if not unknown_data else None, "known_data_usd": round(data, 6),
            "unpriced_data_sessions": unknown_data, "as_of": until}
