"""Closing security weights reconstructed from prospective research receipts."""
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal

from .risk import instant, number
from .strategy_validation import digest


RECONCILIATION_TOLERANCE = Decimal("1e-10")


def _policy_exposures(db, identity):
    row = db.execute("SELECT policy,policy_hash FROM fund_strategy_versions WHERE id=? AND version=?",
                     identity).fetchone()
    policy = json.loads(row[0]) if row else None
    if not isinstance(policy, dict) or digest(policy) != row[1] or not isinstance(policy.get("universe"), dict):
        raise ValueError("Missing or inconsistent frozen policy")
    symbols = {symbol for symbol, kind in policy["universe"].items() if kind == "cash_equivalent"}
    if policy.get("cash_symbol"):
        symbols.add(policy["cash_symbol"])
    groups = {name: frozenset(members) for name, _, members in policy.get("groups", ())}
    return symbols, groups


def _closing_book(raw, day, cash_symbols):
    archived = json.loads(raw)
    receipt = archived["receipt"]
    if archived["evidence"] != "prospective" or receipt["day"] != day:
        raise ValueError("Missing matched prospective closing receipt")
    equity, cash = number(receipt["equity"]), number(receipt["cash"])
    quantities, prices, receivables = receipt["positions"], receipt["position_prices"], receipt["receivables"]
    if equity <= 0 or cash < 0 or not isinstance(quantities, dict) or not isinstance(prices, dict):
        raise ValueError("Invalid long-only closing book")
    if not isinstance(receivables, list):
        raise ValueError("Missing closing receivables")
    amounts = [number(row["amount"]) for row in receivables]
    if any(value < 0 for value in amounts):
        raise ValueError("Invalid receivable")
    unpaid = sum(amounts)
    positions, total = {}, cash + unpaid
    for symbol, raw_quantity in quantities.items():
        quantity = number(raw_quantity)
        if quantity == 0:
            continue
        price = number(prices[symbol])
        if not symbol or quantity < 0 or price <= 0:
            raise ValueError("Invalid closing position")
        value = quantity * price
        total += value
        positions[symbol] = (price, symbol in cash_symbols, float(value / equity))
    if abs(total - equity) > equity * RECONCILIATION_TOLERANCE:
        raise ValueError("Closing positions, cash and receivables do not reconcile")
    return positions, float(cash / equity), float(unpaid / equity)


def _closing_exposure(day, paths, weights, books):
    # Transfer costs scale all post-transfer capital by the same factor. The
    # closing capital shares depend only on start weights and net sleeve returns.
    outside = max(0.0, 1 - math.fsum(weights.values()))
    balances = {key: weight * (1 + paths[key][day]) for key, weight in weights.items()}
    total = outside + math.fsum(balances.values())
    securities, holders, by_sleeve = defaultdict(float), Counter(), {}
    sleeve_cash = cash_equivalents = receivables = 0.0
    for key, balance in balances.items():
        positions, cash, unpaid = books[key]
        share = balance / total
        sleeve_cash += share * cash
        receivables += share * unpaid
        risky = by_sleeve[key] = {}
        for symbol, (_, is_cash, within_sleeve) in positions.items():
            weight = share * within_sleeve
            if is_cash:
                cash_equivalents += weight
            else:
                risky[symbol] = weight
                securities[symbol] += weight
                holders[symbol] += 1
    summary = {"risk_security_weight": math.fsum(securities.values()),
               "max_single_security_weight": max(securities.values(), default=0.0),
               "shared_security_weight": math.fsum(weight for symbol, weight in securities.items() if holders[symbol] > 1),
               "outside_usd_cash_weight": outside / total, "sleeve_usd_cash_weight": sleeve_cash,
               "cash_equivalent_weight": cash_equivalents, "receivables_weight": receivables}
    return summary, securities, by_sleeve


def position_exposure(db, days, paths, scaled, candidate, now):
    """Compare only closing dates with a complete book for every declared sleeve."""
    unavailable = {"status": "unavailable", "evaluation_sessions": len(days), "covered_sessions": 0}
    identities = scaled["combined"]
    try:
        policies = {key: _policy_exposures(db, key) for key in identities}
    except (TypeError, ValueError):
        return {**unavailable, "missing_by_reason": {"missing_or_inconsistent_policy": len(days)}}
    receipts = {key: {day: (at, raw) for day, at, raw in db.execute(
        "SELECT day,at,body FROM fund_strategy_forward WHERE id=? AND version=? AND day BETWEEN ? AND ?",
        (*key, days[0], days[-1]))} for key in identities}
    first_groups = next(iter(policies.values()))[1]
    shared_groups = {group: members for group, members in first_groups.items()
                     if all(other.get(group) == members for _, other in policies.values())}

    missing, peak, latest = Counter(), {arm: 0.0 for arm in scaled}, None
    for day in days:
        books = {}
        try:
            for key in identities:
                at, raw = receipts[key][day]
                if instant(at) > now:
                    raise ValueError("Closing holdings published after cutoff")
                books[key] = _closing_book(raw, day, policies[key][0])
        except (ArithmeticError, KeyError, TypeError, ValueError):
            missing["missing_or_invalid_closing_book"] += 1
            continue
        marks = {}
        if any(marks.setdefault(symbol, (price, is_cash)) != (price, is_cash)
               for positions, _, _ in books.values() for symbol, (price, is_cash, _) in positions.items()):
            missing["inconsistent_shared_marks_or_classification"] += 1
            continue

        base, base_symbols, _ = _closing_exposure(day, paths, scaled["base"], books)
        combined, combined_symbols, sleeve_symbols = _closing_exposure(day, paths, scaled["combined"], books)
        combined["candidate_overlap_weight"] = math.fsum(
            weight for symbol, weight in sleeve_symbols[candidate].items() if symbol in base_symbols)
        sizes = {symbol: max(base_symbols.get(symbol, 0.0), combined_symbols.get(symbol, 0.0))
                 for symbol in base_symbols.keys() | combined_symbols.keys()}
        largest = [{"symbol": symbol, "base_weight": base_symbols.get(symbol, 0.0),
                    "combined_weight": combined_symbols.get(symbol, 0.0)}
                   for symbol in sorted(sizes, key=lambda symbol: (-sizes[symbol], symbol))[:5]]
        risk_groups = {
            name: {"base_weight": math.fsum(base_symbols.get(symbol, 0.0) for symbol in members),
                   "combined_weight": math.fsum(combined_symbols.get(symbol, 0.0) for symbol in members),
                   "candidate_weight": math.fsum(sleeve_symbols[candidate].get(symbol, 0.0) for symbol in members)}
            for name, members in shared_groups.items()
            if members & (base_symbols.keys() | combined_symbols.keys())}
        latest = {"day": day, "base": base, "combined": combined, "largest_risk_securities": largest,
                  "risk_groups": risk_groups}
        for arm in scaled:
            peak[arm] = max(peak[arm], latest[arm]["max_single_security_weight"])

    result = {**unavailable, "missing_by_reason": dict(missing)}
    if latest is not None:
        result.update(status="partial" if missing else "complete", covered_sessions=len(days) - sum(missing.values()),
                      latest=latest, peak_single_security_weight=peak)
    return result
