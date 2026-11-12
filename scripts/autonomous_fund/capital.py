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
- paper (Alpaca paper): a subscription moves reserved cash (account cash the
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
from .risk import RiskError, fresh_account_snapshot, instant, number, trading_day

KINDS = ("subscription", "redemption")
REQUESTS = "capital_requests"
UNITS = "fund_units"
FLOW = "capital_flow"
CASH_FLOW = "local_cash_flow"
CANCELLED = "capital_request_cancelled"
REJECTED = "capital_request_rejected"
TARGET_REACHED = "capital_target_reached"
MAX_AMOUNT = Decimal("10000000")
MAX_PENDING = 10
MAX_REDEMPTION_SHARE = Decimal("0.95")
EXPOSURE_DRIFT = Decimal("0.05")
EXPOSURE_TOLERANCE = Decimal("0.01")
UNIT_PLACES = Decimal("0.00000001")
NAV_PLACES = Decimal("0.000001")
CENT = Decimal("0.01")
RISK_KEYS = ("peak", "session_start_equity", "session_last_equity", "session_last_cash")


class ValuationChanged(RiskError):
    """Retry the request after a fresh account observation."""


class OrderAuthorityChanged(ValuationChanged):
    """Capital-request changes revoked an order's authority before execution."""

    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


class QueueFull(RiskError):
    """The existing requests must make progress before adding another."""


class InsufficientReserve(RiskError):
    """The paper account's reserved cash cannot cover this subscription."""


class CapitalLedgerError(RiskError):
    """Saved capital does not match confirmed flow history."""


def require_fresh_valuation(as_of, now, max_age_seconds):
    if trading_day(as_of) != trading_day(now) or not fresh_account_snapshot(as_of, now, max_age_seconds):
        raise ValuationChanged("The account valuation is stale or from another trading day")


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


def _operation(db, operation_id, terms):
    """Look up an optional retry key inside the transaction that applies the command."""
    if operation_id is None:
        return None, None
    if not (1 <= len(operation_id) <= 64 and operation_id.isascii()
            and all(char.isalnum() or char in "-_." for char in operation_id)):
        raise RiskError("Operation ID must contain 1 to 64 ASCII letters, digits, dots, hyphens or underscores")
    key = f"capital_operation:{operation_id}"
    previous = _load(db, key, None)
    if previous is not None and previous["terms"] != terms:
        raise RiskError("Operation ID is already recorded with different terms")
    return key, previous["result"] if previous is not None else None


def _remember_operation(db, key, terms, result):
    if key is not None:
        _store(db, key, {"terms": terms, "result": result})


def enqueue(db, now, kind, amount, *, source="operator", target=None):
    """Queue one request inside the caller's write transaction; return it."""
    if kind not in KINDS:
        raise RiskError("A capital request is a subscription or a redemption")
    # A configuration request is an absolute target. Its projected difference
    # may be zero or change before it reaches the head of the queue.
    amount = parse_amount(amount) if target is None else number(amount)
    _, basis = fund_units(db)
    if basis is None:
        raise RiskError("No capital mandate is recorded yet: start the fund once with FUND_CAPITAL_USD set "
                        "to an amount (not 'account')")
    requests = _requests(db)
    if len(requests) >= MAX_PENDING:
        raise QueueFull(f"{MAX_PENDING} requests are already waiting; cancel one or wait until they are applied")
    request = {"id": uuid4().hex[:12], "kind": kind, "amount": str(amount), "requested_at": str(now),
               "source": source, "state": "pending"}
    if target is not None:
        request["target_capital"] = str(target)
    _store(db, REQUESTS, requests + [request])
    _event(db, now, "capital_request", request)
    return request


def signed_amount(row):
    return number(row["amount"]) * (-1 if row["kind"] == "redemption" else 1)


def _target_delta(target, basis):
    return (number(target) - basis).quantize(CENT)


def _requests(db):
    """Project each configuration request against the capital ahead of it."""
    requests = _load(db, REQUESTS, [])
    if not any(row["source"] == "configuration" for row in requests):
        return requests
    [value] = db.execute("SELECT value FROM meta WHERE key='mandate_capital_basis'").fetchone()
    basis = number(value)
    for request in requests:
        if request["source"] == "configuration":
            if "target_capital" not in request:
                # Older queued configuration requests stored the target in the
                # change event, with only the estimated amount in the queue.
                saved = db.execute(
                    "SELECT json_extract(body,'$.to') FROM events WHERE kind='mandate_capital_change_requested' "
                    "AND json_extract(body,'$.request_id')=? ORDER BY sequence DESC LIMIT 1", (request["id"],)
                ).fetchone()
                if saved and saved[0] is not None:
                    request["target_capital"] = saved[0]
            if "target_capital" in request:
                delta = _target_delta(request["target_capital"], basis)
                request.update(kind="redemption" if delta < 0 else "subscription", amount=str(abs(delta)))
        basis += signed_amount(request)
    return requests


def queue_target(db, now, basis, target):
    """Replace earlier configuration targets, preserving explicit operator requests."""
    requests = _requests(db)
    remaining = [row for row in requests if row["source"] != "configuration"]
    if len(remaining) >= MAX_PENDING:
        raise QueueFull("The capital queue is full; the configuration target will be queued when a slot opens")
    if len(remaining) != len(requests):
        for row in requests:
            if row["source"] == "configuration":
                _event(db, now, CANCELLED, {**row, "reason": "configuration_changed"})
        _store(db, REQUESTS, remaining)
    delta = _target_delta(target, number(basis) + sum(signed_amount(row) for row in remaining))
    if not delta and not remaining:
        return None
    return enqueue(db, now, "redemption" if delta < 0 else "subscription", abs(delta),
                   source="configuration", target=target)


def latest_valuation(db):
    """Return equity and its timestamp from the latest committed valuation."""
    row = db.execute("SELECT at,kind,body FROM events WHERE kind IN ('account',?) "
                     "ORDER BY sequence DESC LIMIT 1", (FLOW,)).fetchone()
    if row is None:
        return None, None
    at, kind, body = row
    return number(json.loads(body)["equity_after" if kind == FLOW else "equity"]), at


def request(journal, now, kind, amount, *, source="operator", operation_id=None):
    """Queue an operator request; a redemption the fund could not pay is refused at once, not at the next session.

    The fund still checks it again against its live valuation when it applies it.
    """
    amount = parse_amount(amount)
    terms = {"kind": kind, "amount": str(amount), "source": source}
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        key, previous = _operation(db, operation_id, terms)
        if previous is not None:
            return previous
        if kind == "redemption":
            equity, _ = latest_valuation(db)
            waiting = sum((signed_amount(r) for r in _requests(db)), Decimal(0))
            if equity is not None and amount - waiting > equity * MAX_REDEMPTION_SHARE:
                raise RiskError(f"A redemption may take at most {MAX_REDEMPTION_SHARE:.0%} of the fund (last valued at "
                                f"{equity.quantize(CENT)} USD, waiting requests included); to close the fund, "
                                "STOP it and liquidate")
        queued = enqueue(db, now, kind, amount, source=source)
        _remember_operation(db, key, terms, queued)
        return queued


def _broker_reserve(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_account'").fetchone():
        raise RiskError("A local virtual book has no broker transfers: use subscribe / redeem")
    row = db.execute("SELECT value FROM meta WHERE key='mandate_reserve_cash'").fetchone()
    if row is None:
        raise RiskError("No mandate reserve is recorded for this account yet")
    return number(row[0])


def deposit(journal, now, amount, *, subscribe=True, operation_id=None):
    """Broker account (fund stopped): record money transferred in as reserved cash and, by default, subscribe it.

    One transaction: the reserve, its `operator_reserve_adjusted` event and the
    subscription request (applied by the fund at its next valuation once restarted).
    """
    amount = parse_amount(amount)
    terms = {"kind": "deposit", "amount": str(amount), "subscribe": subscribe}
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        key, previous = _operation(db, operation_id, terms)
        if previous is not None:
            return number(previous["reserve"]), previous["request"]
        reserve = _broker_reserve(db) + amount
        db.execute("INSERT OR REPLACE INTO meta VALUES ('mandate_reserve_cash',?)", (str(reserve),))
        _event(db, now, "operator_reserve_adjusted", {"amount": str(amount), "reserved_cash": str(reserve),
                                                      "external_rebaselined": False, "transfer": "deposit"})
        queued = enqueue(db, now, "subscription", amount, source="deposit") if subscribe else None
        _remember_operation(db, key, terms, {"reserve": str(reserve), "request": queued})
    return reserve, queued


def withdraw(journal, now, amount, *, operation_id=None):
    """Broker account (fund stopped): record money transferred out of reserved cash (a redemption puts it there)."""
    amount = parse_amount(amount)
    terms = {"kind": "withdrawal", "amount": str(amount)}
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        key, previous = _operation(db, operation_id, terms)
        if previous is not None:
            return number(previous)
        reserve = _broker_reserve(db)
        if amount > reserve:
            raise RiskError(f"Reserved cash is only {reserve.quantize(CENT)} USD: redeem first (fund-capital.sh redeem), "
                            "wait for the confirmation, then transfer and record the withdrawal")
        reserve -= amount
        db.execute("INSERT OR REPLACE INTO meta VALUES ('mandate_reserve_cash',?)", (str(reserve),))
        _event(db, now, "operator_reserve_adjusted", {"amount": str(-amount), "reserved_cash": str(reserve),
                                                      "external_rebaselined": False, "transfer": "withdrawal"})
        _remember_operation(db, key, terms, str(reserve))
    return reserve


def pending(journal):
    """Waiting requests, oldest first (applied in that order)."""
    with journal.connection() as db:
        db.execute("BEGIN")
        return _requests(db)


def redemption_pending(journal):
    return any(row["kind"] == "redemption" for row in pending(journal))


def guard_redemption_sale(db, request_id, *, rest=None, sale=None, day=None):
    """Check authority and checkpoint the slice when the sell intent is reserved."""
    requests = _requests(db)
    if not requests or requests[0]["id"] != request_id or requests[0]["kind"] != "redemption":
        raise ValuationChanged("Redemption no longer authorizes a sale")
    fund_units(db)
    if rest is not None:
        requests[0].update(plan=rest, slice_sale=sale, plan_day=day)
        _store(db, REQUESTS, requests)


def guard_purchase(db):
    if any(row["kind"] == "redemption" for row in _requests(db)):
        raise OrderAuthorityChanged("redemption_pending", "Purchases wait for the pending redemption")


def redemption_order(db, intent_id):
    """The request attached to this persisted submission, or None for other orders."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_orders'").fetchone():
        return None  # Standalone local books can have ordinary orders without fund metadata.
    row = db.execute("SELECT decision_id FROM fund_orders WHERE intent_id=?", (intent_id,)).fetchone()
    if row and str(row[0]).startswith("redemption:"):
        return str(row[0]).split(":", 1)[1]
    return None


def require_order_authority(db, intent_id, side):
    """Recheck capital authority in the transaction that accepts or fills an order."""
    if side == "buy":
        guard_purchase(db)
        return
    request_id = redemption_order(db, intent_id)
    if request_id is not None:
        try:
            guard_redemption_sale(db, request_id)
        except ValuationChanged as exc:
            raise OrderAuthorityChanged("redemption_request_closed", str(exc)) from None


def update(journal, request_id, **fields):
    """Record liquidation progress on a waiting request; None when it is no longer waiting."""
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        requests = _requests(db)
        for row in requests:
            if row["id"] == request_id:
                row.update(fields)
                _store(db, REQUESTS, requests)
                return dict(row)
    return None


def _remove(journal, now, request_id, kind, reason):
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        requests = _requests(db)
        found = next((row for row in requests if row["id"] == request_id), None)
        if found is None:
            raise RiskError(f"No waiting capital request {request_id}")
        _store(db, REQUESTS, [row for row in requests if row["id"] != request_id])
        _event(db, now, kind, {**found, "reason": reason})
        if kind == REJECTED:
            _event(db, now, "notification", {"kind": REJECTED, "details": {
                "request_id": request_id, "kind": found["kind"], "amount": found["amount"], "reason": reason}})
    return found


def cancel(journal, now, request_id):
    """Operator: withdraw a waiting request. Sales a redemption already made stay in the fund as cash."""
    return _remove(journal, now, request_id, CANCELLED, "operator")


def reject(journal, now, request_id, reason):
    """The fund refuses a request it cannot apply (the reason says why); nothing else changes."""
    return _remove(journal, now, request_id, REJECTED, reason)


def fund_units(db):
    """Return (units outstanding, capital basis) checked against the latest confirmed flow."""
    meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (?, 'mandate_capital_basis')", (UNITS,)))
    row = db.execute("SELECT body FROM events WHERE kind=? ORDER BY sequence DESC LIMIT 1", (FLOW,)).fetchone()
    try:
        basis = number(meta["mandate_capital_basis"]) if "mandate_capital_basis" in meta else None
        if row is None and UNITS not in meta:
            return basis, basis
        if row is not None and UNITS in meta:
            units = number(json.loads(meta[UNITS]))
            flow = json.loads(row[0])
            if units == number(flow["units_after"]) and basis == number(flow["capital_basis_after"]):
                return units, basis
    except (ValueError, KeyError, TypeError):
        pass
    raise CapitalLedgerError("Saved units or net capital disagree with confirmed flows. Review the account journal")


def apply(journal, now, request_id, *, equity, cash, mode, observed_at=None, max_age_seconds=float("inf"),
          check_authority=None):
    """Apply one request and return its flow, or None when its target is already met.

    equity / cash: the sleeve's equity and real cash (without the cash-equivalent
    ETF) from this iteration's snapshot. A redemption needs that cash to cover it.
    The cash move, the capital basis, the units, the scaled risk state and the
    `capital_flow` event commit together or not at all.
    check_authority runs after acquiring the write lock, returns the current
    instant or raises, and must not perform provider I/O while holding the lock.
    """
    equity, cash = number(equity), number(cash)
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")

        def current_valuation_time():
            checked_at = check_authority() if check_authority is not None else now
            if observed_at is not None:
                require_fresh_valuation(observed_at, checked_at, max_age_seconds)
                _, last_at = latest_valuation(db)
                if last_at is not None and instant(last_at) > instant(observed_at):
                    raise ValuationChanged("A newer account valuation is available")
            return checked_at

        now = current_valuation_time()  # Locks can cross STOP, close or snapshot expiry.
        requests = _requests(db)
        request = next((row for row in requests if row["id"] == request_id), None)
        if request is None:
            raise RiskError("The capital request is no longer waiting")
        if requests[0]["id"] != request_id or db.execute(
                "SELECT 1 FROM intents WHERE status != 'terminal' LIMIT 1").fetchone():
            raise ValuationChanged("Another request or order is ahead of this capital flow")
        units_before, basis = fund_units(db)
        if basis is None:
            raise RiskError("No capital mandate is recorded for this account")
        amount = number(request["amount"])
        if not amount and "target_capital" in request:
            now = current_valuation_time()
            _store(db, REQUESTS, requests[1:])
            _event(db, now, TARGET_REACHED, request)
            return None
        amount = parse_amount(amount)
        redeem = request["kind"] == "redemption"
        signed = -amount if redeem else amount
        if equity <= 0:
            raise RiskError("The fund has no positive value to price units at")
        if redeem and amount > equity * MAX_REDEMPTION_SHARE:
            raise RiskError(f"A redemption may take at most {MAX_REDEMPTION_SHARE:.0%} of the fund "
                            f"(now {equity.quantize(CENT)} USD); to close the fund, STOP it and liquidate")
        if redeem and amount > cash:
            raise RiskError("The sleeve's cash does not cover the redemption yet")
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (%s)" % ",".join("?" * (len(RISK_KEYS) + 1)),
                               (*RISK_KEYS, "mandate_reserve_cash")))
        reserve = number(meta.get("mandate_reserve_cash", 0))
        if units_before <= 0:
            raise RiskError("The fund has no units outstanding")
        nav = equity / units_before
        units_after = (units_before + signed / nav).quantize(UNIT_PLACES)
        if units_after <= 0:
            raise RiskError("The request would round the remaining fund units to zero")
        if units_after == units_before or abs((units_after - units_before) * nav - signed) >= CENT / 2:
            # No cash may move for zero units, or for ownership whose rounded
            # value materially differs from the operator's cent-denominated amount.
            raise RiskError("The amount cannot be represented at the fund's unit precision at this NAV; "
                            "use a representable amount")
        after = equity + signed
        updates = {"mandate_capital_basis": str(basis + signed)}
        # Reads and unit calculations also take time. Recheck at the mutation
        # boundary, so stale input is never used to change cash or ownership.
        now = current_valuation_time()
        # A pre-flow journal only knew the original configured amount as its basis.
        db.execute("INSERT OR IGNORE INTO meta VALUES ('mandate_configured_capital',?)", (str(basis),))
        if mode == "local":
            row = db.execute("SELECT cash FROM local_account WHERE id=1").fetchone()
            if row is None:
                raise RiskError("The local virtual account is missing")
            book_cash = number(row[0])
            # The broker's balance API supplies float(cash). Compare at that
            # precision, then use the exact ledger cash for the mutation.
            if observed_at is not None and number(float(book_cash)) - reserve != cash:
                raise ValuationChanged("The local book's cash changed after valuation")
            book_cash += signed
            if book_cash < reserve:
                raise RiskError("The virtual account's cash does not cover the redemption")
            db.execute("UPDATE local_account SET cash=? WHERE id=1", (str(book_cash),))
            _event(db, now, CASH_FLOW, {"request_id": request_id, "amount": str(signed), "cash_after": str(book_cash)})
        else:
            reserve -= signed
            if reserve < 0:
                raise InsufficientReserve(
                    f"Reserved cash ({(reserve + signed).quantize(CENT)} USD) is below the subscription. "
                    "If the paper account balance was adjusted separately, record the deposit with --reserve-only")
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
                  "nav_per_unit": str(nav.quantize(NAV_PLACES)),
                  "capital_basis_after": str(basis + signed), "reserved_cash_after": str(reserve)}
        _event(db, now, FLOW, record)
        _event(db, now, "notification", {"kind": FLOW, "details": record})
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
    """Size the sellable whole-share slice for the target, rounding up where needed."""
    target = number(target)
    available = {symbol: int(number(quantity)) for symbol, quantity in holdings.items()
                 if number(quantity) >= 1 and number(prices.get(symbol) or 0) > 0}
    values = {symbol: quantity * number(prices[symbol]) for symbol, quantity in available.items()}
    total = sum(values.values(), Decimal(0))
    if target <= 0 or total <= 0:
        return {}
    fraction = min(Decimal(1), target / total)
    plan = {symbol: int((quantity * fraction).to_integral_value(rounding=ROUND_FLOOR))
            for symbol, quantity in available.items()}
    raised = sum((plan[s] * number(prices[s]) for s in values), Decimal(0))
    for symbol in sorted(available, key=lambda s: (fraction * values[s] - plan[s] * number(prices[s]), s),
                         reverse=True):
        if raised >= target:
            break
        if plan[symbol] < available[symbol]:
            plan[symbol] += 1
            raised += number(prices[symbol])
    return {s: q for s, q in sorted(plan.items()) if q > 0}


class Units:
    """Units outstanding over time, from `capital_flow` events.

    scale(key) turns an equity observed at `key` (an event sequence or its
    index in a sequence-ordered projection) into flow-adjusted equity.
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
    def from_db(cls, db, *, until=None):
        rows = []
        for sequence, at, body in db.execute(
                "SELECT sequence,at,body FROM events WHERE kind=? ORDER BY sequence", (FLOW,)):
            moment = instant(at)
            if until is not None and moment > instant(until):
                continue
            body = json.loads(body)
            rows.append((sequence, body["units_before"], body["units_after"]))
        return cls(rows)

    @classmethod
    def from_events(cls, events):
        """From journal-ordered (at, kind, body) rows; keys are list indexes."""
        return cls([(index, body["units_before"], body["units_after"])
                    for index, (_, kind, body) in enumerate(events) if kind == FLOW])


def status(journal):
    """What the operator sees: units, NAV per unit, capital basis, waiting requests and recent flows."""
    with journal.connection() as db:
        db.execute("BEGIN")
        units, basis = fund_units(db)
        reserve = db.execute("SELECT value FROM meta WHERE key='mandate_reserve_cash'").fetchone()
        equity, valued_at = latest_valuation(db)
        flows = [json.loads(body) | {"at": at} for at, body in db.execute(
            "SELECT at,body FROM events WHERE kind=? ORDER BY sequence DESC LIMIT 5", (FLOW,))]
        outcomes = [json.loads(body) | {"at": at, "outcome": kind} for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events WHERE kind IN (?,?,?) ORDER BY sequence DESC LIMIT 5",
            (CANCELLED, REJECTED, TARGET_REACHED))]
        requests = _requests(db)
    return {"capital_basis": str(basis) if basis is not None else None,
            "reserved_cash": reserve[0] if reserve else None,
            "units": str(units) if units is not None else None,
            "equity": str(equity) if equity is not None else None, "equity_at": valued_at,
            "nav_per_unit": str((equity / units).quantize(Decimal("0.0001"))) if equity is not None and units else None,
            "pending": requests, "recent_flows": list(reversed(flows)), "recent_outcomes": list(reversed(outcomes))}


def context(journal):
    """The manager's view: operator flows are not performance; a waiting redemption pauses purchases."""
    state = status(journal)
    return {"capital_basis_usd": state["capital_basis"], "units": state["units"],
            "nav_per_unit": state["nav_per_unit"],
            "pending_requests": [{k: row[k] for k in ("kind", "amount", "requested_at", "state", "target_capital")
                                  if k in row}
                                 for row in state["pending"]],
            "recent_flows": [{k: row.get(k) for k in ("at", "kind", "amount", "nav_per_unit", "equity_after")}
                             for row in state["recent_flows"][-3:]],
            "meaning": "Operator subscriptions add cash and redemptions take it out at the NAV per unit; they "
                       "are not gains or losses, and every return, drawdown and scoreboard figure excludes them. "
                       "Weights are fractions of the current equity, so targets stay valid after a flow. While a "
                       "redemption waits, the system raises its cash (keeping the stock weight within 5 percentage "
                       "points, else by a pro-rata slice of every holding, then the cash-equivalent ETF) and "
                       "purchases and cash parking wait; sales and protective exits continue."}
