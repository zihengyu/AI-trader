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
KINDS = ("decision_validated", "decision_accepted", "decision_backoff", "review_skipped", "notification",
         "order_submitted", "fill", "fill_reconciled", "allocation_complete", "entry_waiting",
         "stop_breached_no_buy", "buy_chase_limit", "risk_overlay")
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

    accepted, decisions, by_id, orders, notices = {}, [], {}, defaultdict(list), []
    complete, pending, overlays = set(), defaultdict(list), {}
    for seq, at, kind, body in rows:
        if kind == "decision_accepted":
            accepted[body["id"]] = body
        elif kind == "decision_validated":
            key = body.get("decision_id")
            if key is None:
                # Older journals predate explicit IDs. Match the original complete
                # decision exactly; never borrow the nearest other plan's orders.
                raw = {k: v for k, v in body.items() if k not in {"prompt_version", "decision_id"}}
                key = next((key for key, item in reversed(list(accepted.items())) if item["decision"] == raw), None)
            item = {"sequence": seq, "at": at, "id": key, "change": _change(previous, body),
                    "decision": body, "notifications": [], "orders": [], "fills": []}
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
            orders[body.get("decision_id")].append(body)
        elif kind == "allocation_complete":
            complete.add(body["decision_id"])
        elif kind in {"entry_waiting", "stop_breached_no_buy", "buy_chase_limit"}:
            pending[body["decision_id"]].append({"kind": kind, **body})
        elif kind == "risk_overlay":
            overlays[body["decision_id"]] = body
    fills = recent_trades([(at, kind, body) for _, at, kind, body in rows], cutoff, limit=max(1, len(rows)))
    for item in decisions:
        key = item["id"]
        item["orders"] = orders.get(key, []) if key is not None else []
        ids = {str(order["broker_id"]) for order in item["orders"]}
        item["fills"] = [fill for fill in fills if str(fill["broker_id"]) in ids]
        item["completed"] = key in complete
        item["waits"] = pending.get(key, [])
        item["risk_binding"] = (overlays.get(key) or {}).get("binding") or []
    counts = Counter(item["change"] for item in decisions)
    return {"session_date": day.isoformat(), "cumulative_validated": total, "decisions": decisions,
            "changes": dict(counts), "validated": len(decisions),
            "failures": sum(kind == "decision_backoff" for _, _, kind, _ in rows),
            "quiet_skips": sum(kind == "review_skipped" for _, _, kind, _ in rows),
            "orders": sum(len(group) for group in orders.values()), "fills": len(fills),
            "notifications": len(notices), "deliveries": dict(Counter(item["state"] for item in notices))}


def lines(data, timeline=False):
    counts = data["changes"]
    result = [f"AI 复核（纽约日期 {data['session_date']}）：通过校验 {data['validated']} 次，"
              f"失败尝试 {data['failures']} 次，静默跳过 {data['quiet_skips']} 次",
              "复核内容：" + "；".join(f"{label} {counts.get(kind, 0)} 次" for kind, label in LABELS.items()),
              f"执行记录：提交订单 {data['orders']} 笔；确认成交 {data['fills']} 笔（分笔与累计观测去重）",
              f"调仓通知：入队 {data['notifications']} 条；" + (
                  "；".join(f"{DELIVERY.get(state, state)} {count} 条" for state, count in data["deliveries"].items())
                  or "没有调仓通知")]
    if timeline:
        for item in data["decisions"]:
            targets = "、".join(f"{row['symbol']} {number(row['weight']) * 100:g}%"
                               for row in item["decision"].get("targets", []) if number(row["weight"]) > 0) or "现金"
            delivered = "、".join(DELIVERY.get(n["state"], n["state"]) for n in item["notifications"]) or "未发调仓通知"
            execution = (f"下单 {len(item['orders'])}／成交 {len(item['fills'])}" if item["id"] is not None else
                         "旧日志缺少可关联计划 ID")
            result.append(f"{instant(item['at']).astimezone(ZONE):%H:%M:%S} 事件 #{item['sequence']} "
                          f"{LABELS[item['change']]}：{targets} ｜ {execution} ｜ {delivered}")
            for wait in item["waits"]:
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
