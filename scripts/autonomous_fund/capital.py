"""Fund subscriptions and redemptions: operator capital flows priced at the fund's NAV per unit.

The operator adds money to the fund (a subscription) or takes money out (a
redemption) with a script (`bash scripts/fund-capital.sh`). The script only
queues a request. The running fund applies it itself, at its next live
valuation in a regular session, with no order of its own open, the way a fund
prices orders at its next NAV. A request therefore never races an order, a fill
or a risk check, and investing continues as before.

Units. The fund's capital is divided into units. Before the first flow there is
one unit per dollar of the mandate's capital basis, so the NAV per unit is the
fund's growth since inception. A subscription of X at NAV n issues X/n units;
a redemption of X cancels X/n units. A flow leaves the NAV per unit unchanged,
so returns computed on it are time-weighted and a flow is never a gain or a loss.
`Units` turns any recorded equity into flow-adjusted equity (what the original
capital alone would be worth) for every return, drawdown and Sharpe figure.

Where the money comes from and goes:
- local (virtual book): a subscription deposits virtual cash and a redemption
  withdraws it; each is a `local_cash_flow` event the ledger audit replays.
- paper/live (real broker): a subscription moves reserved cash (account cash the
  AI may not use) into the sleeve; a redemption moves sleeve cash back to the
  reserve. Money is moved into or out of the broker account by the operator,
  with the fund stopped, and recorded with `deposit` / `withdraw`.

Redemption liquidity when the sleeve's cash is short, as funds do it:
1. cash pays as long as the stock weight stays within EXPOSURE_DRIFT of where it
   was when the redemption started (and within the hard exposure limit);
2. beyond that, a pro-rata "vertical slice" of every AI holding is sold, so the
   remaining portfolio keeps its weights;
3. the cash-equivalent ETF is sold for the rest of the cash part;
4. the redemption is paid once real cash covers it.
While a redemption is pending, AI purchases and cash parking wait. Sales,
protective exits, research and decisions continue. A request above
MAX_REDEMPTION_SHARE of the fund is refused: closing the fund is a STOP and an
operator liquidation, not a redemption.

Risk state follows the fund, not the flow: the drawdown peak and the day's
starting equity are scaled by the flow, so a redemption never latches the
drawdown lock or the daily loss pause, and a subscription never hides a loss.
"""
from __future__ import annotations

import json
from bisect import bisect_left
from decimal import ROUND_FLOOR, Decimal
from uuid import uuid4

from .journal import encode
from .risk import RiskError, instant, number

KINDS = ("subscription", "redemption")
REQUESTS = "capital_requests"
UNITS = "fund_units"
FLOW = "capital_flow"
CASH_FLOW = "local_cash_flow"
MAX_AMOUNT = Decimal("10000000")
MAX_PENDING = 10
MAX_REDEMPTION_SHARE = Decimal("0.95")
EXPOSURE_DRIFT = Decimal("0.05")
EXPOSURE_TOLERANCE = Decimal("0.01")
UNIT_PLACES = Decimal("0.00000001")
CENT = Decimal("0.01")
RISK_KEYS = ("peak", "session_start_equity", "session_last_equity", "session_last_cash")


def parse_amount(value):
    """A positive amount of US dollars with at most two decimals."""
    try:
        amount = number(value)
    except RiskError:
        raise RiskError("The amount must be a number of US dollars, e.g. 5000 or 2500.50") from None
    if amount <= 0 or amount > MAX_AMOUNT:
        raise RiskError("The amount must be above 0 and at most 10,000,000 US dollars")
    if amount != amount.quantize(CENT):
        raise RiskError("The amount may have at most two decimals (cents)")
    return amount.quantize(CENT)


def _load(db, key, default):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _store(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))


def _event(db, at, kind, body):
    db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(at), kind, encode(body)))


def enqueue(db, now, kind, amount, *, source="operator"):
    """Queue one request inside the caller's write transaction; return it."""
    if kind not in KINDS:
        raise RiskError("A capital request is a subscription or a redemption")
    amount = parse_amount(amount)
    if not db.execute("SELECT 1 FROM meta WHERE key='mandate_capital_basis'").fetchone():
        raise RiskError("No capital mandate is recorded yet: start the fund once with FUND_CAPITAL_USD set "
                        "to an amount (not 'account')")
    requests = _load(db, REQUESTS, [])
    if len(requests) >= MAX_PENDING:
        raise RiskError(f"{MAX_PENDING} requests are already waiting; cancel one or wait until they are applied")
    request = {"id": uuid4().hex[:12], "kind": kind, "amount": str(amount), "requested_at": str(now),
               "source": source, "state": "pending"}
    _store(db, REQUESTS, requests + [request])
    _event(db, now, "capital_request", request)
    return request


def request(journal, now, kind, amount, *, source="operator"):
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return enqueue(db, now, kind, amount, source=source)


def pending(journal):
    """Waiting requests, oldest first (applied in that order)."""
    with journal.connection() as db:
        return _load(db, REQUESTS, [])


def redemption_pending(journal):
    return any(row["kind"] == "redemption" for row in pending(journal))


def update(journal, request_id, **fields):
    """Record liquidation progress on a waiting request; None when it is no longer waiting."""
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        requests = _load(db, REQUESTS, [])
        for row in requests:
            if row["id"] == request_id:
                row.update(fields)
                _store(db, REQUESTS, requests)
                return dict(row)
    return None


def _remove(journal, now, request_id, kind, reason):
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        requests = _load(db, REQUESTS, [])
        found = next((row for row in requests if row["id"] == request_id), None)
        if found is None:
            raise RiskError(f"No waiting capital request {request_id}")
        _store(db, REQUESTS, [row for row in requests if row["id"] != request_id])
        _event(db, now, kind, {**found, "reason": reason})
    return found


def cancel(journal, now, request_id):
    """Operator: withdraw a waiting request. Sales a redemption already made stay in the fund as cash."""
    return _remove(journal, now, request_id, "capital_request_cancelled", "operator")


def reject(journal, now, request_id, reason):
    """The fund refuses a request it cannot apply (the reason says why); nothing else changes."""
    return _remove(journal, now, request_id, "capital_request_rejected", reason)


def fund_units(db):
    """(units outstanding, capital basis); before the first flow the units equal the basis."""
    meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (?, 'mandate_capital_basis')", (UNITS,)))
    basis = number(meta["mandate_capital_basis"]) if "mandate_capital_basis" in meta else None
    units = number(json.loads(meta[UNITS])) if UNITS in meta else basis
    return units, basis


def apply(journal, now, request_id, *, equity, cash, mode):
    """Price one waiting request at this valuation and apply it in one transaction; return the flow record.

    equity / cash: the sleeve's equity and real cash (without the cash-equivalent
    ETF) from this iteration's snapshot. A redemption needs that cash to cover it.
    The cash move, the capital basis, the units, the scaled risk state and the
    `capital_flow` event commit together or not at all.
    """
    equity, cash = number(equity), number(cash)
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        requests = _load(db, REQUESTS, [])
        request = next((row for row in requests if row["id"] == request_id), None)
        if request is None:
            raise RiskError("The capital request is no longer waiting")
        amount = number(request["amount"])
        redeem = request["kind"] == "redemption"
        signed = -amount if redeem else amount
        if equity <= 0:
            raise RiskError("The fund has no positive value to price units at")
        if redeem and amount > equity * MAX_REDEMPTION_SHARE:
            raise RiskError(f"A redemption may take at most {MAX_REDEMPTION_SHARE:.0%} of the fund "
                            f"(now {equity.quantize(CENT)} USD); to close the fund, STOP it and liquidate")
        if redeem and amount > cash:
            raise RiskError("The sleeve's cash does not cover the redemption yet")
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (%s)" % ",".join("?" * (len(RISK_KEYS) + 3)),
                               (*RISK_KEYS, "mandate_capital_basis", "mandate_reserve_cash", UNITS)))
        if "mandate_capital_basis" not in meta:
            raise RiskError("No capital mandate is recorded for this account")
        basis, reserve = number(meta["mandate_capital_basis"]), number(meta.get("mandate_reserve_cash", 0))
        units_before = number(json.loads(meta[UNITS])) if UNITS in meta else basis
        if units_before <= 0:
            raise RiskError("The fund has no units outstanding")
        nav = equity / units_before
        units_after = (units_before + signed / nav).quantize(UNIT_PLACES)
        after = equity + signed
        updates = {"mandate_capital_basis": str(basis + signed)}
        if mode == "local":
            row = db.execute("SELECT cash FROM local_account WHERE id=1").fetchone()
            if row is None:
                raise RiskError("The local virtual account is missing")
            book_cash = number(row[0]) + signed
            if book_cash < reserve:
                raise RiskError("The virtual account's cash does not cover the redemption")
            db.execute("UPDATE local_account SET cash=? WHERE id=1", (str(book_cash),))
            _event(db, now, CASH_FLOW, {"request_id": request_id, "amount": str(signed), "cash_after": str(book_cash)})
        else:
            reserve -= signed
            if reserve < 0:
                raise RiskError(f"Reserved cash ({(reserve + signed).quantize(CENT)} USD) is below the subscription: "
                                "transfer the money into the broker account and record it with deposit first")
            updates["mandate_reserve_cash"] = str(reserve)
        # Ratios stay what they were: the drawdown from the peak and the day's loss are unchanged by a flow.
        factor = after / equity
        for key in ("peak", "session_start_equity"):
            if key in meta:
                updates[key] = str(number(meta[key]) * factor)
        for key in ("session_last_equity", "session_last_cash"):
            if key in meta:  # The next observation must not count the flow as spending or as income.
                updates[key] = str(number(meta[key]) + signed)
        for key, value in updates.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        _store(db, UNITS, str(units_after))
        _store(db, REQUESTS, [row for row in requests if row["id"] != request_id])
        record = {"request_id": request_id, "kind": request["kind"], "amount": str(amount),
                  "source": request.get("source", "operator"), "requested_at": request.get("requested_at"),
                  "mode": mode, "equity_before": str(equity), "equity_after": str(after),
                  "units_before": str(units_before), "units_after": str(units_after),
                  "nav_per_unit": str(nav.quantize(Decimal("0.000001"))),
                  "capital_basis_after": str(basis + signed), "reserved_cash_after": str(reserve)}
        _event(db, now, FLOW, record)
    return record


def stock_sale_needed(equity, stock_value, amount, exposure_start, max_exposure):
    """Stock value to sell so that, after paying `amount`, the stock weight stays within the drift.

    Paying from cash raises the weight of what remains invested. Up to
    EXPOSURE_DRIFT above the starting weight (never above the hard limit) no
    stock is sold; beyond it, just enough is sold to stay there.
    """
    remaining = number(equity) - number(amount)
    allowed = min(number(exposure_start) + EXPOSURE_DRIFT, number(max_exposure))
    return max(Decimal(0), number(stock_value) - allowed * max(remaining, Decimal(0)))


def slice_plan(holdings, prices, target):
    """Whole shares of every holding, in proportion to its value, raising at least `target` at `prices`.

    Each holding first gives the whole shares of its pro-rata part; the rest is
    covered one share at a time by the holding furthest below its part, so no
    holding is sold out of proportion by more than one share.
    """
    target = number(target)
    values = {s: number(q) * number(prices[s]) for s, q in holdings.items()
              if number(q) > 0 and number(prices.get(s) or 0) > 0}
    total = sum(values.values(), Decimal(0))
    if target <= 0 or total <= 0:
        return {}
    fraction = min(Decimal(1), target / total)
    plan = {s: int((number(holdings[s]) * fraction).to_integral_value(rounding=ROUND_FLOOR)) for s in values}
    raised = sum((plan[s] * number(prices[s]) for s in values), Decimal(0))
    while raised < target:
        open_ = [s for s in values if plan[s] < int(number(holdings[s]))]
        if not open_:
            break
        symbol = max(open_, key=lambda s: (fraction * values[s] - plan[s] * number(prices[s]), s))
        plan[symbol] += 1
        raised += number(prices[symbol])
    return {s: q for s, q in sorted(plan.items()) if q > 0}


class Units:
    """Units outstanding over time, from `capital_flow` events.

    scale(key) turns an equity observed at `key` (an event sequence, or an
    instant) into flow-adjusted equity: E x units_before_first_flow / units(key).
    A flow applies to observations strictly after it. With no flow the scale is 1,
    so every existing figure is unchanged.
    """

    def __init__(self, flows):
        rows = sorted(flows, key=lambda row: row[0])
        self._keys = [row[0] for row in rows]
        self._after = [float(row[2]) for row in rows]
        self._reference = float(rows[0][1]) if rows else None
        self.flows = len(rows)

    def __bool__(self):
        return bool(self._keys)

    def scale(self, key):
        index = bisect_left(self._keys, key) - 1
        return 1.0 if index < 0 else self._reference / self._after[index]

    @classmethod
    def from_db(cls, db, *, by="sequence", until=None):
        rows = []
        for sequence, at, body in db.execute(
                "SELECT sequence,at,body FROM events WHERE kind=? ORDER BY sequence", (FLOW,)):
            moment = instant(at)
            if until is not None and moment > instant(until):
                continue
            body = json.loads(body)
            rows.append((sequence if by == "sequence" else moment, body["units_before"], body["units_after"]))
        return cls(rows)

    @classmethod
    def from_events(cls, events):
        """From (at, kind, body) rows; keys are instants."""
        return cls([(instant(at), body["units_before"], body["units_after"])
                    for at, kind, body in events if kind == FLOW])


def status(journal):
    """What the operator sees: units, NAV per unit, capital basis, waiting requests and recent flows."""
    with journal.connection() as db:
        db.execute("BEGIN")
        units, basis = fund_units(db)
        reserve = db.execute("SELECT value FROM meta WHERE key='mandate_reserve_cash'").fetchone()
        mark = db.execute("SELECT at,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        flows = [json.loads(body) | {"at": at} for at, body in db.execute(
            "SELECT at,body FROM events WHERE kind=? ORDER BY sequence DESC LIMIT 5", (FLOW,))]
        requests = _load(db, REQUESTS, [])
    equity = number(json.loads(mark[1])["equity"]) if mark else None
    return {"capital_basis": str(basis) if basis is not None else None,
            "reserved_cash": reserve[0] if reserve else None,
            "units": str(units) if units is not None else None,
            "equity": str(equity) if equity is not None else None, "equity_at": mark[0] if mark else None,
            "nav_per_unit": str((equity / units).quantize(Decimal("0.0001"))) if equity is not None and units else None,
            "pending": requests, "recent_flows": list(reversed(flows))}


def context(journal):
    """The manager's view: operator flows are not performance; a waiting redemption pauses purchases."""
    state = status(journal)
    return {"capital_basis_usd": state["capital_basis"], "units": state["units"],
            "nav_per_unit": state["nav_per_unit"],
            "pending_requests": [{k: row.get(k) for k in ("kind", "amount", "requested_at", "state")}
                                 for row in state["pending"]],
            "recent_flows": [{k: row.get(k) for k in ("at", "kind", "amount", "nav_per_unit", "equity_after")}
                             for row in state["recent_flows"][-3:]],
            "meaning": "Operator subscriptions add cash and redemptions take it out at the NAV per unit; they "
                       "are not gains or losses, and every return, drawdown and scoreboard figure excludes them. "
                       "Weights are fractions of the current equity, so targets stay valid after a flow. While a "
                       "redemption waits, the system raises its cash (keeping the stock weight within 5 percentage "
                       "points, else by a pro-rata slice of every holding, then the cash-equivalent ETF) and "
                       "purchases and cash parking wait; sales and protective exits continue."}
