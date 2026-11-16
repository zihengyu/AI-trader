"""Cutoff-safe execution diagnostics from recorded orders, quotes and fills only."""
import json

from .risk import RiskError, instant, number


def analyze(db, until):
    cutoff = instant(until)
    orders, callbacks, cumulative, states, modes = {}, {}, {}, {}, []
    incomplete = set()
    conflicting = set()
    for seq, at, kind, raw in db.execute("SELECT sequence,at,kind,body FROM events WHERE kind IN "
            "('startup','order_submitted','order_observed','fill','fill_reconciled','fill_details_unavailable') "
            "AND julianday(at)<=julianday(?) ORDER BY sequence", (cutoff.isoformat(),)):
        at = instant(at)
        if at > cutoff:
            continue
        body = json.loads(raw)
        if kind == "startup":
            modes.append((at, seq, body.get("mode", "unknown")))
            continue
        key = str(body.get("broker_id") or "")
        if not key:
            continue
        if kind == "order_submitted":
            orders[key] = (at, seq, body)
        elif kind == "order_observed":
            states[key] = body
        elif kind == "fill_details_unavailable":
            incomplete.add(key)
        elif kind == "fill_reconciled":
            cumulative[key] = (at, body)
        elif kind == "fill":
            identity = body.get("execution_id")
            # An explicit execution id proves duplicate delivery; indistinguishable
            # legacy fills without one are retained, never guessed to be duplicates.
            execution = str(identity) if identity else f"event:{seq}"
            saved = callbacks.setdefault(key, {}).get(execution)
            if saved is not None:
                def equivalent(field):
                    a, b = saved[1].get(field), body.get(field)
                    if field in {"symbol", "side"} or a is None or b is None:
                        return a == b
                    try:
                        return number(a) == number(b)
                    except (RiskError, TypeError, ValueError):
                        return False
                if not all(equivalent(f) for f in ("symbol", "side", "quantity", "price", "trade_cost")):
                    conflicting.add(key)
                at = min(at, saved[0])
            callbacks[key][execution] = (at, body)
    quotes, submissions = {}, {}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "fund_orders" in tables:
        for key, at in db.execute("SELECT intent_id,submitted_at FROM fund_orders"):
            try:
                submissions[key] = instant(at)
            except (RiskError, TypeError, ValueError):
                continue
    if {"fund_orders", "fund_order_quotes"} <= tables:
        for key, submitted, bid, ask, stamp, observed in db.execute(
                "SELECT o.intent_id,o.submitted_at,q.bid,q.ask,q.as_of,q.observed_at "
                "FROM fund_orders o JOIN fund_order_quotes q ON q.intent_id=o.intent_id"):
            try:
                bid, ask = number(bid), number(ask)
                if 0 < bid <= ask and instant(stamp) <= instant(observed) <= instant(submitted) <= cutoff:
                    quotes[key] = (bid, ask, instant(stamp), instant(submitted))
            except (RiskError, TypeError, ValueError):
                continue
    rows, groups = [], {}
    for key, (submitted, sequence, body) in orders.items():
        # Older order events can carry iteration-start time. The immutable send
        # preparation receipt is the more precise clock, including at cutoffs.
        prepared = submissions.get(body.get("intent_id"))
        if prepared is not None:
            if prepared > cutoff:
                continue
            submitted = prepared
        available_modes = [m for at, seq, m in modes if at <= submitted and seq <= sequence]
        mode = available_modes[-1] if available_modes else "unknown"
        group = groups.setdefault(mode, {"orders": 0, "with_fills": 0, "unfilled_observations": 0,
            "terminal_unfilled": 0, "open_or_unknown": 0, "priced_notional": 0., "shortfall_usd": 0.,
            "beyond_quote_usd": 0., "known_fees_usd": 0., "fees_unknown_orders": 0,
            "arrival_unknown_orders": 0, "invalid_fill_orders": 0, "first_fill_observation_seconds": []})
        group.setdefault("incomplete_fill_orders", 0)
        group["orders"] += 1
        fills = [cumulative[key]] if key in cumulative else list(callbacks.get(key, {}).values())
        state = states.get(key, {})
        terminal = state.get("status") in {"canceled", "cancelled", "expired", "rejected", "fill", "filled"}
        try:
            observed_quantity = number(state.get("filled_quantity") or 0)
            callback_quantity = sum((number(b["quantity"]) for _, b in fills), number(0))
        except (RiskError, TypeError, ValueError):
            observed_quantity, callback_quantity = None, number(0)
        except KeyError:
            observed_quantity, callback_quantity = None, number(0)
        if key not in cumulative and (key in incomplete or observed_quantity is None or
                observed_quantity > callback_quantity):
            group["incomplete_fill_orders"] += 1
            group["fees_unknown_orders"] += 1
            continue  # Known/possible partial fill is neither a proven zero-fill nor complete cost evidence.
        if not fills:
            group["unfilled_observations"] += 1
            group["terminal_unfilled" if terminal else "open_or_unknown"] += 1
            continue
        group["with_fills"] += 1
        try:
            if body["side"] not in {"buy", "sell"} or (key in conflicting and key not in cumulative):
                raise RiskError("Invalid or conflicting execution evidence")
            if any(at < submitted for at, _ in fills):
                raise RiskError("Fill observed before submission")
            if any(b.get("symbol") != body["symbol"] or b.get("side") != body["side"] for _, b in fills):
                raise RiskError("Fill identity mismatch")
            quantities = [number(b["quantity"]) for _, b in fills]
            prices = [number(b["price"]) for _, b in fills]
            total = sum(quantities)
            if total <= 0 or any(q <= 0 for q in quantities) or any(p <= 0 for p in prices) or total > number(body["quantity"]):
                raise RiskError("Invalid observed fill quantity/price")
            fees = [number(b["trade_cost"]) if b.get("trade_cost") is not None else None for _, b in fills]
            if any(f is not None and f < 0 for f in fees):
                raise RiskError("Negative observed fee")
        except (RiskError, KeyError, TypeError, ValueError):
            group["invalid_fill_orders"] += 1
            continue
        observed = [at for at, _ in fills]
        # A later cumulative reconciliation supersedes financial totals, not the
        # first observation of a valid partial fill.
        for at, item in callbacks.get(key, {}).values():
            try:
                if (key not in conflicting and at >= submitted and item.get("symbol") == body["symbol"]
                        and item.get("side") == body["side"] and 0 < number(item["quantity"]) <= number(body["quantity"])
                        and number(item["price"]) > 0):
                    observed.append(at)
            except (RiskError, KeyError, TypeError, ValueError):
                continue
        latency = (min(observed) - submitted).total_seconds()
        if latency >= 0:
            group["first_fill_observation_seconds"].append(latency)
        fee = sum(fees) if all(f is not None for f in fees) else None
        group["known_fees_usd"] += float(sum(f for f in fees if f is not None))
        group["fees_unknown_orders"] += int(fee is None)
        quote = quotes.get(body.get("intent_id"))
        if quote is not None and quote[3] > submitted:
            quote = None
        row = {"broker_id": key, "mode": mode, "quantity": str(total), "fee_usd": float(fee) if fee is not None else None,
               "submission_clock": "send_preparation_receipt" if prepared is not None else "legacy_order_event",
               "filled_fraction": float(total / number(body["quantity"])),
               "first_fill_observation_seconds": latency if latency >= 0 else None}
        if quote is None:
            group["arrival_unknown_orders"] += 1
            row["arrival"] = "unknown"
        else:
            bid, ask, source_time, _ = quote
            mid, sign = (bid + ask) / 2, 1 if body["side"] == "buy" else -1
            executable = ask if sign == 1 else bid
            shortfall = sum(q * sign * (p - mid) for q, p in zip(quantities, prices))
            beyond = sum(q * sign * (p - executable) for q, p in zip(quantities, prices))
            notional = total * mid
            group["priced_notional"] += float(notional)
            group["shortfall_usd"] += float(shortfall)
            group["beyond_quote_usd"] += float(beyond)
            row.update(arrival="recorded_bid_ask", quote_age_seconds=(submitted - source_time).total_seconds(),
                       implementation_shortfall_bps=float(shortfall / notional * 10000),
                       beyond_executable_quote_bps=float(beyond / notional * 10000))
        rows.append(row)
    for group in groups.values():
        notional = group["priced_notional"]
        group["implementation_shortfall_bps"] = group["shortfall_usd"] / notional * 10000 if notional else None
        group["beyond_executable_quote_bps"] = group["beyond_quote_usd"] / notional * 10000 if notional else None
        times = sorted(group.pop("first_fill_observation_seconds"))
        group["first_fill_observation_p95_seconds"] = times[max(0, (95 * len(times) + 99) // 100 - 1)] if times else None
        group["latency_samples"] = len(times)
    return {"groups": groups, "recent": rows[-10:],
            "meaning": "Notional-weighted implementation shortfall from recorded bid/ask, never order limits. "
                       "Cumulative fills replace callbacks; explicit execution IDs deduplicate callbacks. "
                       "Latency starts at send preparation where recorded, otherwise the legacy order-event clock; "
                       "it is time to observation, not exchange fill latency. Open/unfilled orders are censored. "
                       "Local and paper observations are separated; neither proves live impact/queue calibration."}


def lines(report):
    pct = lambda x: "未知" if x is None else f"{x:.2f}bp"
    out = ["成交校准：按资金量加权、区分账户模式；限价不当作到达价，未成交订单另列。"]
    for mode, row in report["groups"].items():
        wait = "未知" if row["first_fill_observation_p95_seconds"] is None else f"{row['first_fill_observation_p95_seconds']:.2f} 秒"
        out.append(f"{mode}：提交 {row['orders']}，观察到成交 {row['with_fills']}，未观察成交 {row['unfilled_observations']}；"
                   f"相对报价中间价差额 {pct(row['implementation_shortfall_bps'])}，"
                   f"超出可执行买卖价 {pct(row['beyond_executable_quote_bps'])}；"
                   f"已知手续费 ${row['known_fees_usd']:.2f}，费用未知 {row['fees_unknown_orders']}，到达价未知 {row['arrival_unknown_orders']}。")
        out.append(f"  未成交中已终结 {row['terminal_unfilled']}、仍挂单或状态未知 {row['open_or_unknown']}；"
                   f"成交明细不完整 {row.get('incomplete_fill_orders', 0)}，成交证据无效 {row['invalid_fill_orders']}，等待时间样本 {row['latency_samples']}，"
                   f"P95 {wait}。")
    if not report["groups"]:
        out.append("尚无可核对的已提交订单。")
    out.append("本地与券商模拟成交均非实盘校准；等待时间包含轮询/网络观察延迟，不能视为交易所撮合延迟。")
    return out
