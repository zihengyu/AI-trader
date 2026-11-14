"""Account-local, sampled watch checks; no market requests, trading gates or inference."""
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .risk import MarketDataUnavailable, RiskError, instant, trading_day

PURPOSES = {"portfolio_watch": "持仓／目标盯盘", "price_alert": "价位提醒"}
REASONS = {"stale": "报价过期", "future": "源时间超前", "nonpositive": "价格非正数",
           "invalid_price": "价格无效", "source_backoff": "请求冷却", "source_unavailable": "行情不可用",
           "risk_rejected": "交易报价或流动性校验未通过", "check_error": "检查异常"}


def sample(symbol, purpose, checked_at, *, observation=None, error=None):
    """Retain only clocks and the actual check outcome, never exception/provider text."""
    when = instant(checked_at).astimezone(timezone.utc)
    detail = getattr(error, "diagnostic", None)
    detail = detail if isinstance(detail, dict) else {}
    reason = None
    if error is not None:
        fallback = ("source_unavailable" if isinstance(error, MarketDataUnavailable) else
                    "risk_rejected" if isinstance(error, RiskError) else "check_error")
        cause = detail.get("reason")
        reason = cause if isinstance(cause, str) and cause in REASONS else fallback
    elif observation is None:
        reason = "check_error"
    stamp = getattr(observation, "as_of", None) if error is None else detail.get("source_as_of")
    try:
        age = (when - instant(stamp)).total_seconds()
        age = age if math.isfinite(age) else None
    except (TypeError, ValueError, OverflowError):
        age = None
    return {"symbol": symbol, "purpose": purpose, "checked_at": when.isoformat(),
            "accepted": error is None and observation is not None, "reason": reason, "age_seconds": age}


def record(journal, checks):
    """One transaction per watch iteration, including iterations ending in a protective exit.

    Rows are deliberately immutable: rebuilding an old report must not borrow
    later checks from a mutable daily counter. Existing SQLite backups retain them.
    """
    if not checks:
        return
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS fund_quote_checks ("
                   "sequence INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, checked_epoch REAL NOT NULL, "
                   "day TEXT NOT NULL, symbol TEXT NOT NULL, purpose TEXT NOT NULL, accepted INTEGER NOT NULL, "
                   "reason TEXT, age_seconds REAL, recovered INTEGER NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS fund_quote_checks_day ON fund_quote_checks(day,checked_epoch)")
        db.execute("CREATE INDEX IF NOT EXISTS fund_quote_checks_previous ON "
                   "fund_quote_checks(symbol,purpose,checked_epoch DESC,sequence DESC)")
        for check in checks:
            when = instant(check["checked_at"]).astimezone(timezone.utc)
            purpose, accepted = check["purpose"], bool(check["accepted"])
            if purpose not in PURPOSES:
                raise RiskError("Unknown watch-check purpose")
            previous = db.execute("SELECT checked_epoch,accepted FROM fund_quote_checks WHERE symbol=? AND purpose=? "
                                  "ORDER BY checked_epoch DESC,sequence DESC LIMIT 1",
                                  (check["symbol"], purpose)).fetchone()
            # A regressed clock cannot establish recovery from a later observation.
            recovered = bool(accepted and previous and not previous[1] and when.timestamp() >= previous[0])
            db.execute("INSERT INTO fund_quote_checks(checked_at,checked_epoch,day,symbol,purpose,accepted,reason,"
                       "age_seconds,recovered) VALUES(?,?,?,?,?,?,?,?,?)",
                       (when.isoformat(), when.timestamp(), trading_day(when), check["symbol"], purpose, int(accepted),
                        check["reason"], check["age_seconds"], int(recovered)))


def summarize(db, *, start_day, end_day, until, symbol=None):
    """Indexed aggregation of this account's recorded checks through an exact cutoff."""
    cutoff = instant(until).timestamp()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_quote_checks'").fetchone():
        return {"status": "untracked", "start_day": start_day, "end_day": end_day, "groups": []}
    where, args = "day>=? AND day<=? AND checked_epoch<=?", [start_day, end_day, cutoff]
    if symbol is not None:
        where += " AND symbol=?"
        args.append(symbol)
    rows = db.execute("SELECT symbol,purpose,count(*),sum(accepted),sum(recovered),min(checked_at),max(checked_at),"
        "count(CASE WHEN age_seconds>=0 THEN 1 END),avg(CASE WHEN age_seconds>=0 THEN age_seconds END),"
        "max(CASE WHEN age_seconds>=0 THEN age_seconds END),sum(age_seconds<0),"
        "sum(age_seconds IS NULL),"
        "sum(CASE WHEN age_seconds>=0 AND age_seconds<=1 THEN 1 ELSE 0 END),"
        "sum(CASE WHEN age_seconds>1 AND age_seconds<=10 THEN 1 ELSE 0 END),"
        "sum(CASE WHEN age_seconds>10 AND age_seconds<=30 THEN 1 ELSE 0 END),"
        "sum(CASE WHEN age_seconds>30 AND age_seconds<=60 THEN 1 ELSE 0 END),"
        "sum(CASE WHEN age_seconds>60 THEN 1 ELSE 0 END) "
        f"FROM fund_quote_checks WHERE {where} GROUP BY symbol,purpose ORDER BY symbol,purpose", args).fetchall()
    reasons = {}
    for name, purpose, reason, count in db.execute(
            f"SELECT symbol,purpose,reason,count(*) FROM fund_quote_checks WHERE {where} AND accepted=0 "
            "GROUP BY symbol,purpose,reason", args):
        group = reasons.setdefault((name, purpose), {})
        key = reason if reason in REASONS else "check_error"
        group[key] = group.get(key, 0) + count
    groups = []
    for name, purpose, total, passed, recovered, first, last, aged, mean, maximum, future, unknown, *buckets in rows:
        groups.append({"symbol": name, "purpose": purpose, "checks": total, "accepted": passed,
            "rejected": total - passed, "accepted_share": passed / total, "recoveries": recovered,
            "first_checked_at": first, "last_checked_at": last, "reasons": reasons.get((name, purpose), {}),
            "age": {"samples": aged, "mean_seconds": mean, "max_seconds": maximum,
                    "future": future or 0, "unknown": unknown,
                    "buckets": dict(zip(("0_1s", "1_10s", "10_30s", "30_60s", "over_60s"), buckets))}})
    # Table creation by a later runner cannot change a report from before tracking began.
    started = db.execute("SELECT checked_at FROM fund_quote_checks WHERE day<=? AND checked_epoch<=? "
                         "ORDER BY day,checked_epoch LIMIT 1", (trading_day(instant(until)), cutoff)).fetchone()
    return {"status": "recorded" if groups else "empty" if started else "untracked",
            "start_day": start_day, "end_day": end_day, "groups": groups}


def inspect(root, *, start_day=None, end_day=None, until=None, symbol=None):
    """Read only; absent legacy evidence never creates a database or means zero failures."""
    until = instant(until or datetime.now(timezone.utc))
    start_day = start_day or trading_day(until)
    end_day = end_day or start_day
    empty = {"status": "untracked", "start_day": start_day, "end_day": end_day, "groups": []}
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            return summarize(db, start_day=start_day, end_day=end_day, until=until, symbol=symbol)
    except (OSError, ValueError, sqlite3.Error):
        return {**empty, "status": "unavailable"}


def lines(report):
    period = report["start_day"] if report["start_day"] == report["end_day"] else f"{report['start_day']} 至 {report['end_day']}"
    output = [f"盯盘报价检查统计（美东日期 {period}）："]
    if report["status"] != "recorded":
        detail = {"untracked": "尚未记录这项统计，旧检查不追溯补算", "empty": "所选窗口没有检查样本",
                  "unavailable": "统计记录不可读"}.get(report["status"], "统计状态未知")
        return output + [f"{detail}；不能据此判断行情正常。"]
    output.append("仅统计已保存的实际盯盘检查（包括复用报价），不是独立行情请求数或全天可用率；"
                  "拒绝也可能来自交易报价／流动性限制，失败后首次通过不代表期间持续故障。")
    output.append("失败后首次通过按通过日计数，前次拒绝可能发生在此前日期。")
    for group in report["groups"]:
        output.append(f"• {group['symbol']}｜{PURPOSES.get(group['purpose'], '未知检查')}："
                      f"检查 {group['checks']}，通过 {group['accepted']}，拒绝 {group['rejected']}，"
                      f"通过率 {group['accepted_share']:.1%}，失败后首次通过 {group['recoveries']} 次。")
        output.append(f"  样本窗口：{group['first_checked_at']} 至 {group['last_checked_at']}")
        if group["reasons"]:
            output.append("  拒绝原因：" + "；".join(f"{REASONS[key]} {count} 次" for key, count in sorted(group["reasons"].items())))
        age = group["age"]
        if age["samples"]:
            output.append(f"  非负源报价年龄：{age['samples']} 条，平均 {age['mean_seconds']:.1f} 秒，"
                          f"最大 {age['max_seconds']:.1f} 秒；≤1／1–10／10–30／30–60／>60 秒："
                          + "／".join(str(count) for count in age["buckets"].values()) + "。")
        output.append(f"  源时间超前 {age['future']} 条，年龄未知 {age['unknown']} 条（均不计入年龄均值）。")
    return output
