"""Read-only local-book reconciliation; absent historical evidence is never invented."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from .risk import RiskError, instant, number


def _rows(db, sql):
    cursor = db.execute(sql)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


_ZERO, _CENT_MILLIONTH = Decimal(0), Decimal("0.00000001")


def account_observation(sequence, at, body):
    """Validate one derived NAV snapshot without scanning unrelated history.

    Return no usable body when the observation is malformed or inconsistent.
    Both full-history audits and bounded notification reads use this contract.
    """
    warnings = []
    def warn(code):
        warnings.append({"code": code, "sequence": sequence})
    try:
        body = json.loads(body) if isinstance(body, (str, bytes)) else body
        # Each value is converted once: full audits run this for every NAV mark.
        cash, equity = number(body["cash"]), number(body["equity"])
        instant(body.get("as_of") or at)
        quantities = {symbol: number(quantity) for symbol, quantity in body["positions"].items()}
        if body.get("valuation") is not None:
            marks = body["valuation"].get("quotes") or {}
            if not all(symbol in marks for symbol in quantities):
                warn("account_mark_missing")
            else:
                expected = cash + number(body.get("receivables", 0)) + sum(
                    (quantity * number(marks[symbol]["price"]) for symbol, quantity in quantities.items()), _ZERO)
                if abs(equity - expected) > _CENT_MILLIONTH:
                    warn("account_nav_arithmetic_mismatch")
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        warn("invalid_account_observation")
    return (None if warnings else body), warnings


def observation_findings(db, after=0):
    """Warnings for the NAV marks after sequence `after`: (warnings, invalid sequences, last sequence seen).

    Marks are checked one at a time and never kept: there is one about every
    30 seconds of market time, and holding them all took memory in proportion
    to the fund's whole history on every audit.
    """
    warnings, invalid, last = [], set(), after
    for sequence, at, body in db.execute(
            "SELECT sequence,at,body FROM events WHERE kind='account' AND sequence>? ORDER BY sequence", (after,)):
        notices = account_observation(sequence, at, body)[1]
        warnings.extend(notices)
        if notices:
            invalid.add(sequence)
        last = sequence
    return warnings, invalid, last


def with_observations(ledger, warnings, invalid):
    """A ledger-scope result extended with NAV-mark findings, shaped as `audit_connection(db)` returns it."""
    result = {**ledger, "scope": "ledger_and_observations"}
    if "warnings" not in ledger:  # The ledger itself could not be read; nothing to add.
        return result
    result["warnings"] = list(warnings) + ledger["warnings"]
    result["invalid_account_sequences"] = sorted(set(ledger["invalid_account_sequences"]) | set(invalid))
    result["status"] = ("failed" if result["errors"] else
                        "partial" if result["missing_evidence"] or result["warnings"] else "verified")
    return result


def _audit_connection(db, *, include_trades=False, include_observations=True):
    """Caller owns a consistent read transaction. Do not change tables or rows."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "local_account" not in tables:
        return {"status": "not_applicable", "reason": "No local simulator book"}
    errors, warnings, invalid_accounts, missing = [], [], set(), set()
    def check(ok, code, **details):
        if not ok:
            errors.append({"code": code, **details})
    def observation(ok, code, **details):
        if not ok:
            warnings.append({"code": code, **details})
            if "sequence" in details:
                invalid_accounts.add(details["sequence"])
    def same(left, right):
        return abs(number(left) - number(right)) <= Decimal("0.00000001")
    [balance] = _rows(db, "SELECT cash,receivables FROM local_account WHERE id=1")
    check(number(balance["cash"]) >= 0 and number(balance["receivables"]) >= 0, "invalid_balance")
    for position in _rows(db, "SELECT * FROM local_positions"):
        check(number(position["quantity"]) >= 0 and number(position["quantity"]) == int(number(position["quantity"]))
              and number(position["average"]) > 0, "invalid_position", symbol=position["symbol"])
    orders = {row["id"]: row for row in _rows(db, "SELECT * FROM local_orders")}
    # Each order body is parsed once. These checks run before every submission
    # and fill, so repeated parsing per fill and per dividend grew quadratically.
    bodies = {identifier: json.loads(order["body"]) for identifier, order in orders.items()}
    intents = {row["id"]: row for row in _rows(
        db, "SELECT id,body,broker_id FROM intents WHERE id IN (SELECT intent_id FROM local_orders)")}
    fills = _rows(db, "SELECT * FROM local_fills ORDER BY observed_at,order_id,bar_start")
    # Money checks run before every submission and fill. Historical NAV rows
    # are derived projections, checked by the explicit audit/report path, not
    # dependencies of a new cash/share transaction.
    events = _rows(db, "SELECT sequence,at,kind,body FROM events WHERE kind IN "
                       "('fill','local_corporate_action','local_ledger_origin','startup') ORDER BY sequence")
    for event in events:
        event["body"] = json.loads(event["body"])
    if include_observations:
        notices, invalid, _ = observation_findings(db)
        warnings.extend(notices)
        invalid_accounts |= invalid
    evidence = {}
    for event in events:
        if event["kind"] == "fill" and event["body"].get("bar_start") is not None:
            body = event["body"]
            key = (str(body["broker_id"]), str(instant(body["bar_start"])))
            check(key not in evidence, "duplicate_fill_event", order_id=key[0])
            evidence[key] = event
    totals = {key: {"quantity": 0, "notional": number(0), "fee": number(0)} for key in orders}
    from .execution import observed_arrivals
    arrivals = observed_arrivals(db)
    if "fund_order_quotes" in tables:
        orders_by_intent = {}
        for order in orders.values():
            orders_by_intent.setdefault(order["intent_id"], []).append(order)
        for quote in _rows(db, "SELECT * FROM fund_order_quotes"):
            for order in orders_by_intent.get(quote["intent_id"], []):
                try:
                    valid = (instant(quote["as_of"]) <= instant(quote["observed_at"]) <= instant(order["created_at"])
                             and 0 < number(quote["bid"]) <= number(quote["ask"]))
                except (ValueError, TypeError):
                    valid = False
                observation(valid, "invalid_arrival_observation", order_id=order["id"])
    fill_steps, execution_verified, arrival_verified, quality = [], 0, 0, []
    for fill in fills:
        identifier = fill["order_id"]
        order = orders.get(identifier)
        if order is None:
            check(False, "fill_without_order", order_id=identifier)
            continue
        body = bodies[identifier]
        qty, price, fee = number(fill["quantity"]), number(fill["price"]), number(fill["fee"])
        check(qty > 0 and qty == int(qty) and price > 0 and fee >= 0, "invalid_fill", order_id=identifier)
        check(body["side"] in {"buy", "sell"}, "invalid_order_side", order_id=identifier)
        buy = body["side"] == "buy"
        check(price <= number(body["limit"]) if buy else price >= number(body["limit"]),
              "fill_outside_limit", order_id=identifier)
        start, observed = instant(fill["bar_start"]), instant(fill["observed_at"])
        check(start >= instant(order["created_at"]) and start.timestamp() + 60 <= observed.timestamp()
              and observed < instant(order["expires_at"]), "invalid_fill_time", order_id=identifier)
        total = totals[identifier]
        total["quantity"] += qty
        total["notional"] += qty * price
        total["fee"] += fee
        event = evidence.pop((identifier, str(start)), None)
        if event is None:
            check(False, "fill_event_missing", order_id=identifier)
            continue
        recorded = event["body"]
        check(recorded.get("symbol") == body["symbol"] and recorded.get("side") == body["side"]
              and same(recorded["quantity"], qty) and same(recorded["price"], price)
              and recorded.get("trade_cost") is not None and same(recorded["trade_cost"], fee),
              "fill_event_mismatch", order_id=identifier)
        assumptions, bar = recorded.get("execution_assumptions"), recorded.get("bar")
        if assumptions is None or bar is None:
            missing.add("historical_execution_inputs")
        else:
            friction = number(assumptions["slippage_bps"]) / 10000 + number(assumptions["spread_bps"]) / 20000
            raw = number(bar["high"]) * (1 + friction) if buy else number(bar["low"]) * (1 - friction)
            expected = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING if buy else ROUND_FLOOR)
            check(same(price, expected), "execution_price_mismatch", order_id=identifier)
            check(same(fee, price * qty * number(assumptions["fee_rate"])), "fee_mismatch", order_id=identifier)
            check(qty <= int(number(bar["volume"]) * number(assumptions["volume_fraction"])),
                  "volume_participation_mismatch", order_id=identifier)
            execution_verified += 1
        arrival = arrivals.get(order["intent_id"])
        if arrival is None:
            missing.add("historical_arrival_quotes")
        else:
            arrival_verified += 1
        if include_trades:
            quality.append({"order_id": identifier, "symbol": body["symbol"], "side": body["side"],
                "quantity": str(qty), "price": str(price), "fee": str(fee), "bar_start": fill["bar_start"],
                "arrival_price": arrival, "adverse_slippage_bps":
                    float((price / number(arrival) - 1) * 10000 * (1 if buy else -1)) if arrival is not None else None,
                "execution_assumptions": assumptions, "bar": bar})
        fill_steps.append((event["sequence"], {**body, "quantity": qty, "price": price, "fee": fee}))
    check(not evidence, "orphan_fill_events")
    for identifier, order in orders.items():
        total, body = totals[identifier], bodies[identifier]
        check(same(order["filled"], total["quantity"]) and same(order["notional"], total["notional"])
              and same(order["fee"], total["fee"]), "order_fill_totals_mismatch", order_id=identifier)
        check(total["quantity"] <= number(body["quantity"]), "order_overfilled", order_id=identifier)
        check((order["status"] != "fill" or total["quantity"] == number(body["quantity"]))
              and (order["status"] != "new" or total["quantity"] == 0), "order_status_mismatch", order_id=identifier)
        intent = intents.get(order["intent_id"])
        check(intent is not None and json.loads(intent["body"]) == body and str(intent["broker_id"]) == identifier,
              "order_intent_mismatch", order_id=identifier)
    origin = next((event["body"] for event in events if event["kind"] == "local_ledger_origin"), None)
    origin_source = "recorded_opening_book" if origin else None
    if origin is None:
        startup = next((event["body"] for event in events if event["kind"] == "startup"), {})
        cash = (startup.get("mandate") or {}).get("local_account_usd")
        if cash is not None:
            origin = {"cash": cash, "receivables": 0, "positions": {}}
            origin_source = "startup_configuration"
            missing.add("opening_balance_receipt")
        else:
            missing.add("opening_balance")
    replayed = None
    if origin is not None:
        cash, receivables = number(origin["cash"]), number(origin.get("receivables", 0))
        positions = {s: {"quantity": number(p["quantity"]), "average": number(p["average"])}
                     for s, p in origin.get("positions", {}).items()}
        actions = {row["id"]: number(row["amount"]) for row in _rows(db, "SELECT * FROM local_actions")}
        share_changes = {}  # symbol -> [(observed_at, "fill", signed quantity)], built once for every dividend.
        for fill in fills:
            body = bodies.get(fill["order_id"])
            if body is not None:
                share_changes.setdefault(body["symbol"], []).append((
                    instant(fill["observed_at"]), "fill", number(fill["quantity"]) * (1 if body["side"] == "buy" else -1)))
        def entitlement(symbol, date):
            cutoff = datetime.combine(datetime.fromisoformat(date).date(), time(), tzinfo=ZoneInfo("America/New_York"))
            changes = [change for change in share_changes.get(symbol, ()) if change[0] < cutoff]
            for key, amount in actions.items():
                name, day, kind = key.rsplit(":", 2)
                if name == symbol and kind == "split" and day <= date:
                    changes.append((datetime.combine(datetime.fromisoformat(day).date(), time(), tzinfo=ZoneInfo("America/New_York")), "split", amount))
            quantity = number((origin.get("positions", {}).get(symbol) or {}).get("quantity", 0))
            for _, kind, amount in sorted(changes):
                quantity = quantity * amount if kind == "split" else quantity + amount
            return quantity
        steps = [(sequence, "fill", fill) for sequence, fill in fill_steps]
        steps += [(event["sequence"], "action", event["body"]) for event in events if event["kind"] == "local_corporate_action"]
        seen = set()
        for _, kind, body in sorted(steps):
            symbol = body["symbol"]
            position = positions.setdefault(symbol, {"quantity": number(0), "average": number(0)})
            if kind == "fill":
                qty, notional = body["quantity"], body["quantity"] * body["price"]
                if body["side"] == "buy":
                    position["average"] = (position["quantity"] * position["average"] + notional) / (position["quantity"] + qty)
                    position["quantity"] += qty
                    cash -= notional + body["fee"]
                else:
                    check(qty <= position["quantity"], "sale_exceeds_replayed_position", symbol=symbol)
                    position["quantity"] -= qty
                    cash += notional - body["fee"]
            else:
                action_kind = body["kind"]
                key = f"{symbol}:{body['date']}:{action_kind}"
                check(key in actions and key not in seen, "corporate_action_mismatch", symbol=symbol)
                seen.add(key)
                amount = actions.get(key, number(0))
                check(action_kind in body and same(body[action_kind], amount), "corporate_action_amount_mismatch", symbol=symbol)
                if action_kind == "split":
                    position["quantity"] *= amount
                    if amount > 0:
                        position["average"] /= amount
                elif action_kind == "dividend":
                    entitled = entitlement(symbol, body["date"])
                    if body.get("entitled_quantity") is not None:
                        check(same(entitled, body["entitled_quantity"]), "dividend_entitlement_mismatch", symbol=symbol)
                    receivables += entitled * amount
        check(seen == set(actions), "corporate_action_event_missing")
        [actual] = _rows(db, "SELECT cash,receivables FROM local_account WHERE id=1")
        check(same(cash, actual["cash"]), "cash_mismatch", expected=str(cash), recorded=actual["cash"])
        check(same(receivables, actual["receivables"]), "receivables_mismatch")
        observed_positions = {row["symbol"]: row for row in _rows(db, "SELECT * FROM local_positions WHERE quantity>0")}
        expected_positions = {s: p for s, p in positions.items() if p["quantity"] != 0}
        check(set(observed_positions) == set(expected_positions), "position_symbols_mismatch")
        for symbol in set(observed_positions) & set(expected_positions):
            check(same(observed_positions[symbol]["quantity"], expected_positions[symbol]["quantity"])
                  and same(observed_positions[symbol]["average"], expected_positions[symbol]["average"]),
                  "position_quantity_or_average_mismatch", symbol=symbol)
        replayed = {"cash": str(cash), "receivables": str(receivables), "positions": {
            s: {k: str(v) for k, v in p.items()} for s, p in expected_positions.items()}}
    return {"status": "failed" if errors else "partial" if missing or warnings else "verified", "errors": errors,
            "warnings": warnings, "invalid_account_sequences": sorted(invalid_accounts),
            "missing_evidence": sorted(missing), "origin": origin_source, "replayed": replayed,
            "orders_checked": len(orders), "fills_checked": len(fills), "execution_inputs_verified": execution_verified,
            "arrival_quotes_verified": arrival_verified, **({"execution_quality": quality} if include_trades else {})}


def audit_connection(db, *, include_trades=False, include_observations=True):
    try:
        result = _audit_connection(db, include_trades=include_trades, include_observations=include_observations)
    except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error, ArithmeticError) as exc:
        result = {"status": "failed", "errors": [{"code": "invalid_ledger_record", "error": type(exc).__name__}]}
    return {**result, "scope": "ledger_and_observations" if include_observations else "ledger"}


def audit(root):
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        return {"status": "unavailable", "reason": "Account book does not exist"}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        try:
            result = audit_connection(db, include_trades=True)
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            result = {"status": "failed", "errors": [{"code": "invalid_ledger_record", "error": type(exc).__name__}]}
    return {"checked_at": datetime.now(timezone.utc).isoformat(), **result}


def stamp(result):
    """An audit's verdict to store beside a NAV mark: status, scope, counts, missing evidence and codes.

    The full result repeats the replayed book (cash and every position) and each
    warning; a mark is written about every 30 seconds, and `fund-logs.sh audit`
    reruns the full replay from the ledger at any time.
    """
    kept = ("status", "scope", "origin", "missing_evidence", "orders_checked", "fills_checked",
            "execution_inputs_verified", "arrival_quotes_verified")
    return {**{key: result[key] for key in kept if key in result},
            "error_codes": sorted({row["code"] for row in result.get("errors", [])}),
            "warning_codes": sorted({row["code"] for row in result.get("warnings", [])}),
            "warning_count": len(result.get("warnings", []))}


# What _audit_connection reads for the ledger scope: every committed change to it,
# from any connection or tool, bumps meta 'ledger_version' through these triggers.
LEDGER_TABLES = ("local_account", "local_positions", "local_orders", "local_fills", "local_actions", "intents",
                 "fund_orders", "fund_order_quotes")
LEDGER_EVENT_KINDS = ("fill", "local_corporate_action", "local_ledger_origin", "startup")
_BUMP = "UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='ledger_version';"


def _trigger_names():
    return ({f"ledger_version_{table}_{op}" for table in LEDGER_TABLES for op in ("insert", "update", "delete")} |
            {f"ledger_version_events_{op}" for op in ("insert", "update", "delete")})


def install_version_triggers(db):
    """Create the ledger_version triggers for the tables that exist (idempotent; caller commits)."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" not in tables or "events" not in tables:
        return
    db.execute("INSERT OR IGNORE INTO meta VALUES('ledger_version','0')")
    for table in LEDGER_TABLES:
        if table in tables:
            for op in ("insert", "update", "delete"):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS ledger_version_{table}_{op} AFTER {op.upper()} ON {table} "
                           f"BEGIN {_BUMP} END")
    kinds = ",".join(f"'{kind}'" for kind in LEDGER_EVENT_KINDS)
    for op, condition in (("insert", f"NEW.kind IN ({kinds})"), ("delete", f"OLD.kind IN ({kinds})"),
                          ("update", f"OLD.kind IN ({kinds}) OR NEW.kind IN ({kinds})")):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS ledger_version_events_{op} AFTER {op.upper()} ON events "
                   f"WHEN {condition} BEGIN {_BUMP} END")


def ledger_version(db):
    """The ledger's change counter, or None when any trigger is missing (then nothing may be reused)."""
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND "
                                            "name LIKE 'ledger_version_%'")}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {name for name in _trigger_names()
                if name.startswith("ledger_version_events_") or name.rsplit("_", 1)[0][len("ledger_version_"):] in tables}
    if not expected <= present:
        return None
    row = db.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()
    return None if row is None else str(row[0])


def ledger_result(db, cache):
    """audit_connection(db, include_observations=False), reused while ledger_version is unchanged.

    Only for a read transaction or one with no writes yet: uncommitted writes bump
    the counter too, and a rolled-back value could later recur for other content.
    `cache` is a dict the caller keeps; failures are never reused.
    """
    import copy
    version = ledger_version(db)
    if version is not None and cache.get("version") == version:
        return copy.deepcopy(cache["result"])
    result = audit_connection(db, include_observations=False)
    if version is not None and result["status"] != "failed":
        cache.update(version=version, result=copy.deepcopy(result))
    else:
        cache.clear()
    return result


def failed_message(result):
    return "Local ledger reconciliation failed: " + ", ".join(sorted({r["code"] for r in result["errors"]}))


def require_consistent(db):
    result = audit_connection(db, include_observations=False)
    if result["status"] == "failed":
        raise RiskError(failed_message(result))
    return result
