"""Offline, account-scoped quote evidence; never fetches or certifies a current price."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .risk import RiskError, instant
from .status import RUNNER_FRESH_SECONDS, _age, _read


def symbol_name(value):
    value = value.strip().upper()
    if not value or len(value) > 64 or not value.isprintable():
        raise RiskError("SYMBOL must be a nonempty printable symbol of at most 64 characters")
    return value


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _pending(root):
    path = root / "state.sqlite3"
    if not path.is_file():
        return {}, "尚无账户账本，不能查询提醒故障记录。"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT value FROM meta WHERE key='price_alert_health'").fetchone()
        if not row:
            return {}, None
        saved = _mapping(json.loads(row[0]))
        # Removed alerts are not active faults, even before the next watch prunes metadata.
        active = {row[0] for row in db.execute(
            "SELECT DISTINCT json_extract(body,'$.symbol') FROM fund_alerts WHERE state='active'")}
        result = {}
        for symbol, state in saved.items():
            if symbol in active and isinstance(state, dict):
                detail = _mapping(state.get("diagnostic"))
                result[symbol] = {key: state[key] for key in (
                    "first_failed_at", "last_failed_at", "failed_checks") if key in state}
                result[symbol]["diagnostic"] = {key: detail[key] for key in (
                    "reason", "source_as_of", "checked_at", "stream_enabled", "stream_as_of", "chart_as_of") if key in detail}
        return result, None


def inspect(root, *, mode="local", symbol=None, now=None):
    """Read only the selected account's last snapshot and pending alert evidence."""
    if mode not in {"local", "paper"}:
        raise RiskError("Quote diagnostics require local or paper account mode")
    root, now = Path(root), instant(now or datetime.now(timezone.utc))
    symbol = symbol_name(symbol) if symbol is not None else None
    beat = _read(root / ("status.json" if mode == "local" else "runner_health.json"))
    issues = []
    if not beat:
        issues.append("交易进程快照缺失、损坏或不可读；不能据此判断当前行情是否正常。")
    elif beat.get("mode", mode) != mode:
        issues.append("快照模式与所选账户模式不一致，未采用该快照。")
        beat = None
    beat = beat or {}
    try:
        pending, issue = _pending(root)
        if issue:
            issues.append(issue)
    except (OSError, ValueError, sqlite3.Error):
        pending = {}
        issues.append("提醒故障记录不可读；未将它当作已恢复。")
    market = _mapping(beat.get("market_data"))
    quotes = {key: {field: row[field] for field in (
        "price_kind", "checked_at", "source_as_of", "stream_as_of", "chart_as_of") if field in row}
        for key, row in _mapping(market.get("quotes_by_symbol")).items() if isinstance(row, dict)}
    failures = market.get("source_failures") or []
    failures = [row for row in failures if isinstance(row, dict) and isinstance(row.get("symbol"), str)] if isinstance(failures, list) else []
    names = {symbol} if symbol else set(quotes) | set(pending) | {row["symbol"] for row in failures}
    return {"mode": mode, "queried_at": now.isoformat(), "snapshot_at": beat.get("heartbeat_at"),
            "snapshot_age_seconds": _age(beat.get("heartbeat_at"), now),
            "runner_state": beat.get("runner_state"), "issues": issues,
            "symbols": [{"symbol": name, "observation": quotes.get(name), "pending_alert": pending.get(name),
                         # Do not expose raw provider errors, account fields or unrequested symbols.
                         "request_failures": [{key: row[key] for key in (
                             "interval", "failed_at", "consecutive_failures", "retry_in_seconds") if key in row}
                             for row in failures if row["symbol"] == name]} for name in sorted(names)]}


def _clock(value):
    try:
        return instant(value).isoformat()
    except (TypeError, ValueError):
        return "未知或时间无效"


def _source_clock(value, checked_at):
    try:
        age = _age(value, instant(checked_at))
    except (TypeError, ValueError):
        age = None
    suffix = "；相对检查时间的年龄未知" if age is None else (
        f"；检查时源时间超前 {-age:.1f} 秒" if age < 0 else f"；检查时已过去 {age:.1f} 秒")
    return _clock(value) + suffix


def lines(report):
    output = [f"只读报价诊断（{report['mode']}）：不请求新行情，不触发复核或买卖。",
              f"查询时间：{report['queried_at']}"]
    age = report["snapshot_age_seconds"]
    state = ("时间未知" if age is None else "时间超前" if age < 0 else
             "已过期" if age > RUNNER_FRESH_SECONDS else f"距查询 {age:.1f} 秒")
    runner = {"running": "运行中", "exited": "已退出"}.get(report["runner_state"], "未知")
    output += [f"交易进程快照：{_clock(report['snapshot_at'])}（{state}；进程{runner}）",
               "以下是已保存的离散观测；新鲜心跳不代表报价有效，无记录不代表正常，不能推断连续故障。"]
    output += report["issues"]
    kinds = {"live_trade_with_streamed_bid_ask": "推送成交及买卖价",
             "live_trade_with_spread_proxy": "推送成交及点差代理",
             "chart_last_trade_with_spread_proxy": "图表最新成交及点差代理",
             "completed_minute_close_with_spread_proxy": "已完成分钟线及点差代理"}
    reasons = {"stale": "报价过期", "future": "源时间超前", "nonpositive": "价格非正数",
               "invalid_price": "价格无效", "source_backoff": "请求冷却", "source_unavailable": "数据源不可用"}
    for row in report["symbols"]:
        output.append(f"• {json.dumps(row['symbol'], ensure_ascii=False)}")
        observation = row["observation"]
        if observation:
            checked = observation.get("checked_at")
            output += [f"  选用：{kinds.get(observation.get('price_kind'), '未知类型')}；检查时间：{_clock(checked)}",
                       f"  报价源：{_source_clock(observation.get('source_as_of'), checked)}",
                       f"  推送源：{_source_clock(observation.get('stream_as_of'), checked)}",
                       f"  图表成交源：{_source_clock(observation.get('chart_as_of'), checked)}"]
        else:
            output.append("  所选账户尚无该标的报价诊断快照，或该条记录不可用。")
        pending = row["pending_alert"]
        if pending:
            detail = _mapping(pending.get("diagnostic"))
            stream = "已启用" if detail.get("stream_enabled") is True else (
                "未启用" if detail.get("stream_enabled") is False else "启用状态未知")
            output += [f"  活动提醒：尚未记录恢复；{reasons.get(detail.get('reason'), '报价不可用')}；"
                       f"已记录 {pending.get('failed_checks', '?')} 次失败检查。",
                       f"  首次／最近失败：{_clock(pending.get('first_failed_at'))} ／ {_clock(pending.get('last_failed_at'))}",
                       f"  最近拒绝的源时间：{_source_clock(detail.get('source_as_of'), detail.get('checked_at'))}",
                       f"  最近拒绝时推送{stream}：{_source_clock(detail.get('stream_as_of'), detail.get('checked_at'))}",
                       f"  最近拒绝时图表成交源：{_source_clock(detail.get('chart_as_of'), detail.get('checked_at'))}"]
        for failure in row["request_failures"]:
            output.append(f"  快照中的请求故障：最近失败 {_clock(failure.get('failed_at'))}；"
                          f"连续请求失败次数 {failure.get('consecutive_failures', '?')}。")
    if not report["symbols"]:
        output.append("所选账户尚无可展示的逐标的记录；不会从其他账户或数据源补取。")
    return output
