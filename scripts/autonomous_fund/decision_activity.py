"""Read-only explanation of portfolio assessments, execution and notification delivery."""
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .holdings import recent_trades
from .risk import RiskError, decision_levels, instant, number

ZONE = ZoneInfo("America/New_York")
ORDER_UPDATES = {
    "local_order_canceled": "模拟券商确认撤单",
    "local_risk_canceled": "模拟券商按风控撤单",
    "order_reprice_cancel": "申请撤单重报",
    "order_authority_canceled": "因授权变化申请撤单",
    "protective_priority_cancel": "为保护性退出申请撤单",
    "buy_canceled_for_redemption": "为赎回申请撤单",
    "redemption_request_closed": "赎回请求已结束，申请撤单",
    "cash_parking_superseded": "现金管理计划被取代，申请撤单",
    "buy_canceled_stop_breached": "触及止损，申请撤买单",
}
KINDS = ("decision_validated", "decision_accepted", "decision_backoff", "review_skipped", "notification",
         "order_submitted", "fill", "fill_reconciled", "allocation_complete", "entry_waiting",
         "stop_breached_no_buy", "buy_chase_limit", "risk_overlay", "review_triggered", "event_triage",
         "triage_scored", "decision_basis", *ORDER_UPDATES)
# Why the manager was woken before a decision, in the operator's words.
WAKES = {"news": "资讯", "price_move": "价格异动", "price_alert": "价格提醒", "market_stress": "市场压力",
         "stop_loss": "止损", "take_profit": "止盈", "capital_flow": "申购/赎回", "entry": "入场价"}
TRIAGE = {"dismissed": "快速模型判断不需要（未叫基金经理）", "audit": "快速模型判断不需要，抽查仍叫了基金经理",
          "escalated": "快速模型判断需要", "shadow": "快速模型影子判断", "deadline": "已到必复核时间",
          "scheduled": "例行复核需要基金经理", "superseded": "筛选期间事件有变化，交基金经理复核"}
LABELS = {"initial": "首次记录", "portfolio": "调整目标组合", "levels": "调整交易价位",
          "minor": "小幅调整权重（不足 1 个百分点）", "maintain": "维持目标组合"}
DELIVERY = {"sent": "已送达", "pending": "待发送", "retry": "等待重试", "sending": "发送中",
            "untracked": "尚无投递记录"}


def _change(previous, current):
    def weights(body):
        return {row["symbol"]: number(row["weight"]) for row in body.get("targets", []) if number(row["weight"]) > 0}
    if previous is None:
        return "initial"
    old, new = weights(previous), weights(current)
    if set(old) != set(new) or any(abs(new[s] - old[s]) >= number(".01") for s in new):
        return "portfolio"
    before, after = decision_levels(previous), decision_levels(current)
    for symbol in set(old) & set(new):
        if ({k: number(v) for k, v in before.get(symbol, {}).items()} !=
                {k: number(v) for k, v in after.get(symbol, {}).items()}):
            return "levels"
    return "minor" if old != new else "maintain"


def activity(root, session_date=None, now=None):
    """One selected account and New York date. No inference, orders, exports or database writes."""
    now = instant(now or datetime.now(timezone.utc))
    try:
        day = date.fromisoformat(session_date) if session_date else now.astimezone(ZONE).date()
    except ValueError as exc:
        raise RiskError("Use a New York date in YYYY-MM-DD format") from exc
    start = datetime.combine(day, time(), ZONE)
    end = datetime.combine(day + timedelta(days=1), time(), ZONE)
    cutoff = min(end, now)
    book = Path(root) / "state.sqlite3"
    if not book.is_file():
        raise RiskError("No existing account journal at the selected run directory")
    with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")  # Counts, events and delivery state describe one consistent read snapshot.
        rows = [(seq, at, kind, json.loads(body)) for seq, at, kind, body in db.execute(
            "SELECT sequence,at,kind,body FROM events WHERE kind IN (%s) "
            "AND julianday(at)>=julianday(?) AND julianday(at)<=julianday(?) ORDER BY sequence"
            % ",".join("?" * len(KINDS)), (*KINDS, start.isoformat(), cutoff.isoformat()))
                if start <= instant(at) < end and instant(at) <= now]
        previous = db.execute("SELECT body FROM events WHERE kind='decision_validated' "
                              "AND julianday(at)<julianday(?) ORDER BY julianday(at) DESC,sequence DESC LIMIT 1",
                              (start.isoformat(),)).fetchone()
        previous = json.loads(previous[0]) if previous else None
        total = db.execute("SELECT count(*) FROM events WHERE kind='decision_validated' "
                           "AND julianday(at)<=julianday(?)", (now.isoformat(),)).fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        delivery = dict(db.execute("SELECT sequence,state FROM fund_deliveries")) if "fund_deliveries" in tables else {}
        approvals = []
        if "fund_approvals" in tables:
            for row in db.execute("SELECT id,decision_id,state,body FROM fund_approvals"):
                body = json.loads(row[3])
                if instant(body["created_at"]) <= cutoff:
                    approvals.append({"id": row[0], "decision_id": row[1], "state": row[2], "body": body})

    accepted, decisions, by_id, orders, notices = {}, [], {}, defaultdict(list), []
    complete, pending, overlays = set(), defaultdict(list), {}
    wakes, screens, scored, bases = [], [], {}, {}
    updates = defaultdict(list)
    for seq, at, kind, body in rows:
        if kind == "decision_basis":
            bases[body.get("decision_id")] = body
            continue
        if kind == "review_triggered":
            wakes.append(body)
            continue
        if kind == "event_triage":
            screens.append(body)
            continue
        if kind == "triage_scored":
            scored[body.get("id")] = body
            continue
        if kind == "decision_accepted":
            accepted[body["id"]] = {**body, "at": at}
        elif kind == "decision_validated":
            key = body.get("decision_id")
            if key is None:
                # Older journals predate explicit IDs. Match the original complete
                # decision exactly; never borrow the nearest other plan's orders.
                raw = {k: v for k, v in body.items() if k not in {"prompt_version", "decision_id"}}
                key = next((key for key, item in reversed(list(accepted.items())) if item["decision"] == raw), None)
            item = {"sequence": seq, "at": at, "id": key, "change": _change(previous, body),
                    "decision": body, "notifications": [], "orders": [], "fills": [],
                    "wakes": wakes, "screens": screens}
            wakes, screens = [], []
            decisions.append(item)
            if key is not None:
                by_id[key] = item
            previous = body
        elif kind == "notification" and body.get("kind") == "decision_changed":
            details = body.get("details") or {}
            notice = {"sequence": seq, "state": delivery.get(seq, "untracked")}
            notices.append(notice)
            item = by_id.get(details.get("decision_id"))
            if item is None and not details.get("decision_id") and decisions:
                # Legacy announcements followed validation in the same runner iteration.
                candidate = decisions[-1]
                if instant(details.get("at") or at) == instant(candidate["at"]):
                    item = candidate
            if item is not None:
                item["notifications"].append(notice)
        elif kind == "order_submitted":
            orders[body.get("decision_id")].append({**body, "at": at})
        elif kind in ORDER_UPDATES and body.get("broker_id") is not None:
            updates[str(body["broker_id"])].append({"at": at, "kind": kind, **body})
        elif kind == "allocation_complete":
            complete.add(body["decision_id"])
        elif kind == "notification" and body.get("kind") == "allocation_session_pending":
            details = body.get("details") or {}
            if details.get("decision_id"):
                pending[details["decision_id"]].append({"kind": "allocation_session_pending", **details})
        elif kind in {"entry_waiting", "stop_breached_no_buy", "buy_chase_limit"}:
            pending[body["decision_id"]].append({"kind": kind, **body})
        elif kind == "risk_overlay":
            overlays[body["decision_id"]] = body
    fills = recent_trades([(at, kind, body) for _, at, kind, body in rows], cutoff, limit=max(1, len(rows)))
    fills_by_order = defaultdict(list)
    for fill in fills:
        fills_by_order[str(fill["broker_id"])].append(fill)
    for item in decisions:
        key = item["id"]
        item["orders"] = orders.get(key, []) if key is not None else []
        item["accepted"] = accepted.get(key)
        for order in item["orders"]:
            broker_id = str(order["broker_id"])
            order["updates"] = updates[broker_id]
            order["fills"] = fills_by_order[broker_id]
        ids = dict.fromkeys(str(order["broker_id"]) for order in item["orders"])
        item["fills"] = sorted((fill for broker_id in ids for fill in fills_by_order[broker_id]),
                               key=lambda fill: instant(fill["at"]))
        item["completed"] = key in complete
        item["waits"] = pending.get(key, [])
        item["risk_binding"] = (overlays.get(key) or {}).get("binding") or []
        item["basis"] = bases.get(key) if key is not None else None
        item["approvals"] = [row for row in approvals if row["decision_id"] == key] if key is not None else []
        for screen in item["screens"]:
            score = scored.get(screen.get("id")) or {}
            screen["miss"] = score.get("miss")
            screen["manager_changed"] = score.get("manager_changed")
            # An escalated screen cannot miss, whether or not the manager
            # changes targets. Only a dismissal's score allows this legacy inference.
            if screen["manager_changed"] is None:
                if screen["miss"] is True:
                    screen["manager_changed"] = True
                elif screen["miss"] is False and (screen.get("verdict") or {}).get("escalate") is False:
                    screen["manager_changed"] = False
    counts = Counter(item["change"] for item in decisions)
    return {"session_date": day.isoformat(), "cumulative_validated": total, "decisions": decisions,
            "changes": dict(counts), "validated": len(decisions),
            "failures": sum(kind == "decision_backoff" for _, _, kind, _ in rows),
            "quiet_skips": sum(kind == "review_skipped" for _, _, kind, _ in rows),
            "orders": sum(len(group) for group in orders.values()), "fills": len(fills),
            "notifications": len(notices), "deliveries": dict(Counter(item["state"] for item in notices))}


def _clock(value):
    return f"{instant(value).astimezone(ZONE):%m-%d %H:%M:%S}"


def plan_execution(db, decision_id, now):
    """Exact plan receipts visible to the sender at the time of delivery."""
    now = instant(now)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_decisions'").fetchone():
        return None
    plan = db.execute("SELECT accepted_at,until_at FROM fund_decisions WHERE id=?", (decision_id,)).fetchone()
    if plan is None or instant(plan[0]) > now:
        return None
    since, until = plan
    def events(kinds, field, key):
        return [(at, kind, json.loads(body)) for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events WHERE kind IN (%s) AND julianday(at)>=julianday(?) "
            "AND julianday(at)<=julianday(?) AND json_valid(body) AND json_extract(body,?)=? ORDER BY sequence"
            % ",".join("?" * len(kinds)), (*kinds, since, now.isoformat(), "$." + field, key))
                if instant(since) <= instant(at) <= now]

    rows = events(("order_submitted", "allocation_complete"), "decision_id", decision_id)
    orders = {body["broker_id"]: body for _, kind, body in rows
              if kind == "order_submitted" and body.get("kind") == "allocation" and body.get("broker_id")}
    complete = any(kind == "allocation_complete" for _, kind, _ in rows)
    filled = terminal = observed_fill = 0
    for key, order in orders.items():
        observations = events(("fill", "fill_reconciled", "order_observed", "local_order_canceled"), "broker_id", key)
        reported = {}
        for _, kind, item in observations:
            if kind in {"order_observed", "local_order_canceled"}:
                reported[kind] = item
        status = reported.get("order_observed", {}).get("status")
        terminal += int("local_order_canceled" in reported or status in {"canceled", "cancelled", "expired", "rejected"})
        receipts = recent_trades(observations, now, limit=len(observations))
        callbacks = {row.get("execution_id") or index: row for index, row in enumerate(receipts)}
        quantity = max(sum((number(row["quantity"]) for row in callbacks.values()), number(0)),
                       number(reported.get("order_observed", {}).get("filled_quantity") or 0))
        observed_fill += int(quantity > 0)
        filled += int(quantity >= number(order["quantity"]))
    expired = now >= instant(until)
    from .market_data import SessionClock
    session = SessionClock(now=lambda: now).status()
    return {"at": now.isoformat(), "orders": len(orders), "filled": filled,
            "orders_with_fill": observed_fill, "terminal": terminal, "complete": complete, "expired": expired,
            "session_open": session["open"], "next_open": session.get("next_open"), "valid_until": until,
            "valid_at_next_open": bool(session.get("next_open") and instant(session["next_open"]) < instant(until))}


def lines(data, timeline=False, *, event_sequence=None):
    decisions = data["decisions"]
    if event_sequence is not None:
        decisions = [item for item in decisions if item["sequence"] == event_sequence or
                     any(notice["sequence"] == event_sequence for notice in item["notifications"])]
        if not decisions:
            raise RiskError("No validated decision or linked target notification with that event sequence in the selected account/date")
    counts = data["changes"]
    result = [f"AI 复核（纽约日期 {data['session_date']}）：通过校验 {data['validated']} 次，"
              f"失败尝试 {data['failures']} 次，静默跳过 {data['quiet_skips']} 次",
              "复核内容：" + "；".join(f"{label} {counts.get(kind, 0)} 次" for kind, label in LABELS.items()),
              f"执行记录：提交订单 {data['orders']} 笔；确认成交 {data['fills']} 笔（分笔与累计观测去重）",
              f"调仓通知：入队 {data['notifications']} 条；" + (
                  "；".join(f"{DELIVERY.get(state, state)} {count} 条" for state, count in data["deliveries"].items())
                  or "没有调仓通知")]
    if timeline:
        if event_sequence is not None:
            result.append(f"以下仅展开事件 #{event_sequence}，上方统计仍为当日合计。")
        for item in decisions:
            targets = "、".join(f"{row['symbol']} {number(row['weight']) * 100:g}%"
                               for row in item["decision"].get("targets", []) if number(row["weight"]) > 0) or "现金"
            delivered = "、".join(DELIVERY.get(n["state"], n["state"]) for n in item["notifications"]) or "未发调仓通知"
            execution = (f"下单 {len(item['orders'])}／成交 {len(item['fills'])}" if item["id"] is not None else
                         "旧日志缺少可关联计划 ID")
            result.append(f"{instant(item['at']).astimezone(ZONE):%H:%M:%S} 事件 #{item['sequence']} "
                          f"{LABELS[item['change']]}：{targets} ｜ {execution} ｜ {delivered}")
            accepted = item.get("accepted")
            if accepted and accepted.get("until_at"):
                result.append(f"  计划接纳 {_clock(accepted['at'])} ｜ 有效至 {_clock(accepted['until_at'])}")
            for order in item["orders"]:
                side = {"buy": "买", "sell": "卖"}.get(order.get("side"), "方向未知")
                execution_time = (f"送单 {_clock(order['send_at'])}，回执 {_clock(order['at'])}"
                                  if order.get("send_at") else f"下单日志 {_clock(order['at'])}（旧记录）")
                limit = f"${order['limit']}" if order.get("limit") is not None else "未记录"
                result.append(f"  {order.get('symbol')} {side} {order.get('quantity', '?')} 股"
                              f" ｜ 限价 {limit} ｜ {execution_time}")
                lifecycle = []
                for fill in order["fills"]:
                    action = ("累计成交观测" if fill["evidence_kind"] == "broker_cumulative_observation"
                              else "成交观测")
                    lifecycle.append((fill["at"], f"{action} {fill['quantity']} 股 @ ${fill['price']}"))
                for update in order["updates"]:
                    reason = f"（{update['reason']}）" if update.get("reason") else ""
                    lifecycle.append((update["at"], ORDER_UPDATES[update["kind"]] + reason))
                for at, label in sorted(lifecycle, key=lambda row: instant(row[0])):
                    result.append(f"    {_clock(at)} {label}")
            wakes = Counter(WAKES.get(w.get("reason"), w.get("reason")) for w in item["wakes"])
            if wakes:
                examples = [w.get("title") or (f"{w.get('symbol')} {w.get('reason')}" if w.get("symbol") else None)
                            for w in item["wakes"]][-2:]
                result.append("  触发：" + "、".join(f"{name} {count} 次" for name, count in wakes.items())
                              + "".join(f"｜{text[:60]}" for text in examples if text))
            for screen in item["screens"][-2:]:
                verdict = screen.get("verdict") or {}
                changed = screen.get("manager_changed")
                note = ""
                if changed is not None:
                    label = {"audit": "抽查结果", "shadow": "对照结果"}.get(screen.get("action"), "复核结果")
                    note = f"（{label}：基金经理{'改了' if changed else '没改'}组合"
                    note += "，算漏判）" if screen.get("miss") else "）"
                action = screen.get("action")
                description = TRIAGE.get(action, action)
                if action == "dismissed" and screen.get("manager_review_skipped") is not True:
                    description = "快速模型判断不需要（是否跳过复核缺少记录）"
                result.append(f"  事件判断：{description}{note}"
                              + (f"｜{str(verdict.get('reason'))[:80]}" if verdict.get("reason") else ""))
            original = str(item["decision"].get("rationale") or "")
            rationale = " ".join(original.split())
            if rationale:
                if event_sequence is not None:
                    result.append("  完整理由（模型原文）：")
                    result.extend("  " + line for line in original.splitlines())
                else:
                    result.append(f"  理由：{rationale[:160]}{'…' if len(rationale) > 160 else ''}")
            if event_sequence is not None:
                for order in item["orders"]:
                    proof = order.get("authorization") or {}
                    label = {"automatic": "自动额度内", "ai": "独立 AI 审批", "operator": "人工审批",
                             "approved": "已批准（旧来源未记录）", "risk_reducing": "卖出／减仓，无需审批",
                             "system_cash_management": "系统现金管理", "unattributed": "未记录独立审批依据"}
                    result.append(f"  订单授权：{order.get('symbol')} {order.get('side')} {order.get('quantity')} 股，"
                                  + label.get(proof.get("source"), "旧记录缺少独立审批依据"))
                    if proof.get("approval_id"):
                        result.append("  使用审批：" + proof["approval_id"])
                    if proof.get("audit"):
                        audit = proof["audit"]
                        result.append(f"  自动额度依据：开盘敞口 {audit.get('start_exposure')}，"
                                      f"下单前敞口 {audit.get('exposure_before')}，"
                                      f"剩余自动买入金额 ${audit.get('automatic_buy_budget_usd')}")
                for approval in item.get("approvals", []):
                    body = approval["body"]
                    result.append(f"  关联审批请求 {approval['id']}：{'独立 AI' if body.get('approver') == 'ai' else '人工'}，"
                                  f"查询时状态 {approval['state']}；自动额度 {json.dumps(body.get('auto_envelope'), ensure_ascii=False)}")
                basis = item.get("basis")
                if basis is None:
                    result.append("  此记录未保存关联反方原文与上轮输入，不能按相邻调用补配。")
                else:
                    previous = basis.get("previous_decision")
                    result.append("  上轮计划（本次实际输入）：" + json.dumps(previous, ensure_ascii=False))
                    result.append("  近期成交（本次实际输入）：" + json.dumps(basis.get("recent_trades", []), ensure_ascii=False))
                    critique, source = basis.get("critique"), basis.get("critique_source") or {}
                    if critique is None:
                        result.append("  本轮无可用反方审查。")
                    else:
                        result.append(f"  关联反方原文（模型判断，时间 {source.get('at')}，"
                                      f"源调用 {source.get('call_id') or '未记录'}，复用 {bool(source.get('reused'))}）：")
                        result.append("  " + str(critique.get("summary") or ""))
                        for concern in critique.get("concerns", []):
                            result.append(f"  {concern.get('symbol') or '组合'}：{concern.get('concern')}"
                                          f"｜证据：{concern.get('evidence')}")
                        if critique.get("missing_evidence"):
                            result.append("  反方指出的缺项：" + "；".join(critique["missing_evidence"]))
            for wait in item["waits"]:
                if wait["kind"] == "allocation_session_pending":
                    message = "  等待常规交易时段"
                    if wait.get("next_open"):
                        message += f" ｜ 下次开盘 {_clock(wait['next_open'])}"
                    if wait.get("valid_at_next_open") is False:
                        message += " ｜ 计划届时已过期"
                    elif wait.get("valid_at_next_open") is True:
                        message += " ｜ 届时仍须复核计划和风控"
                    result.append(message)
                    continue
                label = {"entry_waiting": "等待入场价", "stop_breached_no_buy": "已触及止损，停止买入",
                         "buy_chase_limit": "达到追价上限"}[wait["kind"]]
                result.append(f"  {wait['symbol']}：{label}")
            if item["risk_binding"]:
                result.append("  风控约束：" + "、".join(item["risk_binding"]))
            if item["completed"]:
                result.append("  目标执行已完成（可能无需新增交易）")
        result += ["维持组合也是一次复核；通过校验不等于下单、成交或已证明投资有效。",
                   "投递状态为查询时的当前状态；未记录执行原因时不能仅凭无成交认定模型失效。"]
    return result
