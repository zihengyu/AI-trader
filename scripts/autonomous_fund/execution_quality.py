"""Cutoff-safe execution diagnostics from recorded orders, quotes and fills only."""
import json
import math
import sqlite3
from datetime import timedelta

from .risk import RiskError, instant, number


REFERENCE_LOOKBACK_DAYS = 90


def analyze(db, until, *, since=None):
    cutoff = instant(until)
    start = instant(since) if since is not None else None
    orders, callbacks, cumulative, states, modes, accepted = {}, {}, {}, {}, [], {}
    incomplete = set()
    conflicting = set()
    for seq, at, kind, raw in db.execute("SELECT sequence,at,kind,body FROM events WHERE kind IN "
            "('startup','decision_accepted','order_submitted','order_observed','fill','fill_reconciled','fill_details_unavailable') "
            "AND julianday(at)<=julianday(?) ORDER BY sequence", (cutoff.isoformat(),)):
        at = instant(at)
        if at > cutoff:
            continue
        body = json.loads(raw)
        if kind == "startup":
            modes.append((at, seq, body.get("mode", "unknown")))
            continue
        if kind == "decision_accepted":
            accepted[body.get("id")] = at
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
    rows, groups, costs = [], {}, {}
    for key, (acknowledged, sequence, body) in orders.items():
        submitted = acknowledged
        # Older order events can carry iteration-start time. The immutable send
        # preparation receipt is the more precise clock, including at cutoffs.
        prepared = submissions.get(body.get("intent_id"))
        if prepared is not None:
            if prepared > cutoff:
                continue
            submitted = prepared
        try:
            sent = instant(body["send_at"])
            if not (prepared or sent) <= sent <= acknowledged:
                sent = None
        except (KeyError, TypeError, ValueError):
            sent = None
        submitted = sent or submitted
        if start is not None and submitted < start:
            continue
        available_modes = [m for at, seq, m in modes if at <= submitted and seq <= sequence]
        mode = available_modes[-1] if available_modes else "unknown"
        group = groups.setdefault(mode, {"orders": 0, "with_fills": 0, "unfilled_observations": 0,
            "terminal_unfilled": 0, "open_or_unknown": 0, "priced_notional": 0., "shortfall_usd": 0.,
            "beyond_quote_usd": 0., "known_fees_usd": 0., "fees_unknown_orders": 0,
            "arrival_unknown_orders": 0, "invalid_fill_orders": 0, "first_fill_observation_seconds": []})
        group.setdefault("incomplete_fill_orders", 0)
        stages = group.setdefault("stage_samples", {"decision_to_send": [], "send_to_ack": [],
                                                    "send_to_first_fill": []})
        if sent is not None:
            stages["send_to_ack"].append((acknowledged - sent).total_seconds())
            plan_at = accepted.get(body.get("decision_id"))
            if plan_at is not None and plan_at <= sent and body.get("kind") == "allocation":
                stages["decision_to_send"].append((sent - plan_at).total_seconds())
        evidence = costs.setdefault(mode, {"matched_orders": 0, "unclosed_quantity_orders": 0,
            "verified_executed_notional": number(0), "matched_executed_notional": number(0),
            "matched_arrival_notional": number(0), "fees": number(0), "shortfall": number(0), "max_quote_age_seconds": 0.})
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
        if (observed_quantity is None or observed_quantity > callback_quantity
                or key in incomplete and key not in cumulative):
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
            if sent is not None:
                stages["send_to_first_fill"].append(latency)
        fee = sum(fees) if all(f is not None for f in fees) else None
        executed_notional = sum(q * price for q, price in zip(quantities, prices))
        evidence["verified_executed_notional"] += executed_notional
        # Complete execution costs require an entire order quantity or a
        # terminal receipt that accounts for every filled share.
        closed_quantity = total == number(body["quantity"]) or (
            terminal and state.get("filled_quantity") is not None and total == observed_quantity)
        evidence["unclosed_quantity_orders"] += int(not closed_quantity)
        group["known_fees_usd"] += float(sum(f for f in fees if f is not None))
        group["fees_unknown_orders"] += int(fee is None)
        quote = quotes.get(body.get("intent_id"))
        if quote is not None and quote[3] > submitted:
            quote = None
        row = {"broker_id": key, "mode": mode, "quantity": str(total), "fee_usd": float(fee) if fee is not None else None,
               "submission_clock": ("native_send_start" if sent is not None else
                                    "send_preparation_receipt" if prepared is not None else "legacy_order_event"),
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
            quote_age = (submitted - source_time).total_seconds()
            group["priced_notional"] += float(notional)
            group["shortfall_usd"] += float(shortfall)
            group["beyond_quote_usd"] += float(beyond)
            if fee is not None and closed_quantity:
                evidence["matched_orders"] += 1
                evidence["matched_executed_notional"] += executed_notional
                evidence["matched_arrival_notional"] += notional
                evidence["fees"] += fee
                evidence["shortfall"] += shortfall
                evidence["max_quote_age_seconds"] = max(evidence["max_quote_age_seconds"], quote_age)
            row.update(arrival="recorded_bid_ask", quote_age_seconds=quote_age,
                       implementation_shortfall_bps=float(shortfall / notional * 10000),
                       beyond_executable_quote_bps=float(beyond / notional * 10000))
        rows.append(row)
    for mode, group in groups.items():
        notional = group["priced_notional"]
        group["implementation_shortfall_bps"] = group["shortfall_usd"] / notional * 10000 if notional else None
        group["beyond_executable_quote_bps"] = group["beyond_quote_usd"] / notional * 10000 if notional else None
        times = sorted(group.pop("first_fill_observation_seconds"))
        group["first_fill_observation_p95_seconds"] = times[max(0, (95 * len(times) + 99) // 100 - 1)] if times else None
        group["latency_samples"] = len(times)
        group["stage_latency"] = {}
        for stage, values in group.pop("stage_samples").items():
            ordered = sorted(values)
            group["stage_latency"][stage] = {"samples": len(ordered),
                "p95_seconds": ordered[(95 * len(ordered) + 99) // 100 - 1] if ordered else None}
        sample = costs[mode]
        arrival = sample["matched_arrival_notional"]
        executed = sample["verified_executed_notional"]
        group["matched_costs"] = {
            "matched_orders": sample["matched_orders"], "unclosed_quantity_orders": sample["unclosed_quantity_orders"],
            "matched_arrival_notional_usd": float(arrival),
            "matched_execution_notional_usd": float(sample["matched_executed_notional"]),
            "verified_execution_notional_usd": float(executed),
            "verified_notional_coverage": float(sample["matched_executed_notional"] / executed) if executed else None,
            "max_quote_age_seconds": sample["max_quote_age_seconds"] if arrival else None,
            "fee_bps": float(sample["fees"] / arrival * 10000) if arrival else None,
            "midpoint_shortfall_bps": float(sample["shortfall"] / arrival * 10000) if arrival else None,
            "all_in_bps": float((sample["fees"] + sample["shortfall"]) / arrival * 10000) if arrival else None,
        }
    return {"groups": groups, "recent": rows[-10:],
            "meaning": "Notional-weighted implementation shortfall from recorded bid/ask, never order limits. "
                       "Cumulative fills replace callbacks; explicit execution IDs deduplicate callbacks. "
                       "Matched costs use only orders with both complete fees and reconciled filled quantity. "
                       "Latency starts at native send where recorded, then send preparation, then the legacy order-event clock; "
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
        stages = row.get("stage_latency", {})
        samples = [f"{label} {stages[key]['p95_seconds']:.2f} 秒（{stages[key]['samples']} 笔）"
                   for key, label in (("decision_to_send", "决策到送单"), ("send_to_ack", "送单到回执"),
                                      ("send_to_first_fill", "送单到首次成交观测"))
                   if stages.get(key, {}).get("samples")]
        if samples:
            out.append("  分段耗时 P95 " + "，".join(samples) + "。")
        paired = row.get("matched_costs")
        if paired:
            coverage = paired["verified_notional_coverage"]
            share = "未知" if coverage is None else f"{coverage:.1%}"
            out.append(f"  报价、成交量和手续费齐备 {paired['matched_orders']} 笔，覆盖已核实成交金额 {share}，"
                       f"中间价差额加手续费 {pct(paired['all_in_bps'])}。")
    if not report["groups"]:
        out.append("尚无可核对的已提交订单。")
    out.append("本地与券商模拟成交均非实盘校准；等待时间包含轮询/网络观察延迟，不能视为交易所撮合延迟。")
    return out


def prior_cost_reference(db, until):
    """Freeze aggregate account-local evidence available before a study declaration."""
    cutoff = instant(until)
    start = cutoff - timedelta(days=REFERENCE_LOOKBACK_DAYS)
    window = {"from": start.isoformat(), "through": cutoff.isoformat(), "lookback_days": REFERENCE_LOOKBACK_DAYS}
    try:
        groups = analyze(db, cutoff, since=start)["groups"]
    except (sqlite3.DatabaseError, ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
        return {**window, "status": "execution_ledger_unavailable", "groups": {}}
    matched = {mode: {**row["matched_costs"], "orders_submitted": row["orders"],
                      "orders_without_observed_fills": row["unfilled_observations"],
                      "orders_with_incomplete_fills": row["incomplete_fill_orders"],
                      "orders_with_invalid_fills": row["invalid_fill_orders"],
                      "arrival_unknown_orders": row["arrival_unknown_orders"],
                      "fees_unknown_orders": row["fees_unknown_orders"]} for mode, row in groups.items()}
    if any(not math.isfinite(value) for row in matched.values() for value in row.values() if isinstance(value, float)):
        return {**window, "status": "execution_ledger_unavailable", "groups": {}}
    return {**window, "status": "matched_sample" if any(row["matched_orders"] for row in matched.values())
            else "no_matched_sample", "groups": matched,
            "basis": "account-local security executions versus recorded arrival midpoint plus complete fees",
            "use": "Context for cost assumptions. Security execution is distinct from modeled sleeve transfers."}


def reference_lines(reference):
    if reference["status"] == "execution_ledger_unavailable":
        return ["  声明前的成交成本账本暂不可用。"]
    if not reference["groups"]:
        return [f"  声明前 {reference['lookback_days']} 天暂无可核对的订单。"]
    lines = [f"  声明前 {reference['lookback_days']} 天的证券成交参考，按账户模式分列。"]
    for mode, row in reference["groups"].items():
        coverage = row["verified_notional_coverage"]
        share = "未知" if coverage is None else f"{coverage:.1%}"
        rate = "未知" if row["all_in_bps"] is None else f"{row['all_in_bps']:+.2f} 基点"
        lines.append(f"    {mode} 有效配对 {row['matched_orders']} 笔，覆盖已核实成交金额 {share}，"
                     f"中间价差额加手续费 {rate}。")
        age = row.get("max_quote_age_seconds")
        if age is not None:
            lines.append(f"      配对样本的到达报价最长已过 {age:.1f} 秒。")
        lines.append(f"      缺到达报价 {row['arrival_unknown_orders']} 笔，费用未知 {row['fees_unknown_orders']} 笔，"
                     f"成交量待核 {row['orders_with_incomplete_fills'] + row['unclosed_quantity_orders']} 笔。")
    return lines
