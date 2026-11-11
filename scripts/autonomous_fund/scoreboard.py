"""The fund's own scoreboard: since inception, net of every cost, against its benchmarks.

A professional manager is judged on the return left after every cost, against a
benchmark, and the fund pays its model calls and data whatever the manager decides.
Cash is therefore not neutral: it earns only the bill rate, which those costs can
exceed, so an all-cash portfolio loses money net of costs and, in expectation, falls
behind benchmarks that hold equities. From completed sessions only:

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


def compute(pairs, calls, data_cost_per_session, definitions, marks, actions, *, sweep=None, equity=None):
    """pairs: validation.session_pairs output; calls: [(instant, cost_usd or None)] of paid model calls.

    data_cost_per_session: USD per session, or None when the operator's data costs are unknown.
    """
    if not pairs:
        return {"status": "insufficient_sample", "sessions": 0,
                "meaning": "no two completed sessions with account marks yet"}
    account = net = 1.0
    model_total, unpriced, session_costs = 0.0, 0, []
    data = data_cost_per_session or 0.0
    for pair in pairs:
        inside = [cost for moment, cost in calls if pair["start"] < moment <= pair["end"]]
        known = sum(cost for cost in inside if cost is not None)
        unpriced += sum(cost is None for cost in inside)
        account *= 1 + pair["return"]
        net *= 1 + pair["return"] - (known + data) / pair["equity_start"]
        model_total += known
        session_costs.append(known + data)
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
            "data_costs_usd": round(data * len(pairs), 4) if data_cost_per_session is not None else None,
            "costs_share_of_starting_equity": (model_total + data * len(pairs)) / capital if capital else None,
            "net_return": net - 1, "unpriced_calls": unpriced,
            "net_is_upper_bound": bool(unpriced) or data_cost_per_session is None,
            "benchmarks": benchmarks,
            "cost_pace": {"sessions": len(recent), "usd_per_session": sum(recent) / len(recent),
                          "share_of_equity_per_year": pace},
            "cash": cash,
            "meaning": "account_return is after trading fees and slippage and includes cash interest; net_return "
                       "also subtracts model and data costs, which the fund pays whatever you decide; each benchmark "
                       "is its total return over the same completed sessions and net_excess is net_return minus it; "
                       "cost_pace.share_of_equity_per_year is the recent spend a year as a share of equity, the "
                       "excess return a year needed just to cover it; cash.annual_net_of_cost_pace is what an "
                       "all-cash portfolio earns a year net of that pace. Evidence, not a trading rule"}


def accrued_costs(db, until):
    """Model and data costs the fund has accrued through `until` (an ISO instant), read from the journal.

    Paid model calls only (scripted test answers and LumiBot cache hits cost nothing); a call
    without a price is counted, never taken as free. Data costs are the latest startup's monthly
    subscription per recorded session (sessions with a benchmark mark), None when unknown.
    """
    from .costs import scripted
    from .performance import SESSIONS_PER_MONTH, PerformanceSettings
    model, unpriced = 0.0, 0
    for cost, mock, name, cache_hit in db.execute(
            "SELECT json_extract(body,'$.cost_usd'),json_extract(body,'$.mock'),json_extract(body,'$.model'),"
            "json_extract(body,'$.cache_hit') FROM events WHERE kind='agent' AND json_valid(body) "
            "AND julianday(at)<=julianday(?)", (until,)):
        if cache_hit or scripted({"mock": mock, "model": name}):
            continue
        if cost is None:
            unpriced += 1
        else:
            model += float(cost)
    row = db.execute("SELECT body FROM events WHERE kind='startup' AND json_valid(body) "
                     "AND julianday(at)<=julianday(?) ORDER BY sequence DESC LIMIT 1", (until,)).fetchone()
    settings = (json.loads(row[0]).get("performance") or {}) if row else {}
    monthly = settings.get("data_costs_usd_per_month", PerformanceSettings().data_costs_usd_per_month)
    sessions = db.execute("SELECT count(DISTINCT substr(at,1,10)) FROM events WHERE kind='benchmark' "
                          "AND julianday(at)<=julianday(?)", (until,)).fetchone()[0]
    data = float(monthly) / SESSIONS_PER_MONTH * sessions if monthly is not None else None
    return {"model_usd": round(model, 4), "unpriced_calls": unpriced,
            "data_usd": round(data, 4) if data is not None else None, "as_of": until}
