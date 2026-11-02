"""A one-screen, read-only operator summary for the maintenance script.

It reads only what the services already write (STOP, monitor.json, the runner
heartbeat and the journal) and never changes trading state.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import stops
from .risk import instant

CLASSES = {"operator": "人工停止", "watchdog": "监控停止", "service": "服务重启／异常退出"}
REASONS = {
    "operator_stop": "stop 命令", "stopped_by_operator": "手动关闭服务", "first_start": "新账户首次启动",
    "first_install": "首次安装", "restored_backup": "从备份恢复", "unrecorded": "旧版本留下的停止",
    "service_start": "服务启动", "service_exit": "服务退出", "system_shutdown": "系统关机／重启",
    "heartbeat_stale": "心跳超时", "heartbeat_unavailable": "读不到心跳", "runner_exited": "交易进程退出",
    "iteration_stalled": "迭代卡住", "agent_call_stalled": "模型调用卡住", "low_disk_space": "磁盘空间不足",
    "runner_upgrade_required": "交易进程版本与监控不一致", "matching_error": "本地撮合出错",
    "independent_monitor_stale": "独立监控心跳超时", "independent_monitor_unavailable": "独立监控不可用",
    "independent_notifications_unconfigured": "未配置 Telegram", "monitor_child_stalled": "监控子进程卡住",
}
MONITOR_FRESH_SECONDS = 90


def _age(at, now):
    try:
        return (now - instant(at)).total_seconds()
    except (TypeError, ValueError):
        return None


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stop_class(root):
    record = stops.read(root) if Path(root).is_dir() else None
    return record["class"] if record else "none"


def monitor_ready(root, now=None):
    """The independent monitor wrote a fresh, healthy (or deliberately stopped) state."""
    now = now or datetime.now(timezone.utc)
    state = _read(Path(root) / "monitor.json") or {}
    age = _age(state.get("at"), now)
    return age is not None and 0 <= age <= MONITOR_FRESH_SECONDS and state.get("health") in {"healthy", "stopped"}


def summary(root, mode, now=None):
    from .settings import MODES
    root, now = Path(root), now or datetime.now(timezone.utc)
    lines = [f"模式：{mode}（{MODES.get(mode, mode)}）", f"账户目录：{root}"]
    record = stops.read(root) if root.is_dir() else None
    if record is None:
        lines.append("交易：运行中（没有 STOP）")
    else:
        why = REASONS.get(record["reason"], record["reason"])
        since = f"，自 {record['at']}" if record.get("at") else ""
        if record["class"] == "operator":
            hint = "需要运行 bash scripts/fund-start.sh 确认恢复"
        elif stops.resumable(record):
            hint = "交易进程健康 3 分钟、没有不明订单后会自动恢复"
        else:
            hint = "需要先处理原因，再运行 bash scripts/fund-start.sh 恢复"
        lines.append(f"交易：已停止（{CLASSES.get(record['class'], record['class'])}：{why}{since}）——{hint}")
    monitor = _read(root / "monitor.json")
    if monitor:
        age = _age(monitor.get("at"), now)
        fresh = "新鲜" if age is not None and 0 <= age <= MONITOR_FRESH_SECONDS else "过期"
        lines.append(f"独立监控：{monitor.get('health')}（{int(age) if age is not None else '?'} 秒前，{fresh}）；"
                     f"Telegram：{'已配置' if monitor.get('telegram_configured') else '未配置'}")
    else:
        lines.append("独立监控：还没有状态文件（监控尚未运行）")
    beat = _read(root / "status.json") or _read(root / "runner_health.json")
    if beat:
        age = _age(beat.get("heartbeat_at"), now)
        market = beat.get("market") or {}
        open_text = ("开盘中" if market.get("open") else f"休市，下次开盘 {market.get('next_open')}") if market else ""
        lines.append(f"交易进程：{beat.get('runner_state')}（心跳 {int(age) if age is not None else '?'} 秒前）"
                     + (f"；市场：{open_text}" if open_text else ""))
    book = root / "state.sqlite3"
    if not book.is_file():
        lines.append("账本：尚未创建（交易进程第一次启动中）")
        return "\n".join(lines)
    from .report import report
    try:
        data = report(root, write_files=False)
    except Exception as exc:
        lines.append(f"账本：读取失败（{type(exc).__name__}）")
        return "\n".join(lines)
    account = data.get("latest_account") or {}
    if account:
        positions = account.get("positions") or {}
        held = "、".join(f"{s}×{q}" for s, q in positions.items()) or "无"
        lines.append(f"账户：净值 {account.get('equity')}，现金 {account.get('cash')}，持仓 {held}"
                     f"（{account.get('as_of') or account.get('at') or ''}）")
    if data.get("return") is not None:
        lines.append(f"累计收益：{data['return'] * 100:.2f}%；最大观测回撤：{(data.get('max_observed_drawdown') or 0) * 100:.2f}%")
    lines.append(f"待审批：{data.get('pending_approval_count', 0)} 个；已复盘：{data.get('postmarket_review_runs', 0)} 次")
    with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        failure = db.execute("SELECT at,body FROM events WHERE kind='operation_error' ORDER BY sequence DESC LIMIT 1").fetchone()
        retries = {key: json.loads(value) for key, value in db.execute(
            "SELECT key,value FROM meta WHERE key IN ('decision_failures','decision_retry_at','decision_failure_role')")}
        rows = db.execute("SELECT at,body FROM events WHERE kind='notification' ORDER BY sequence DESC LIMIT 6").fetchall()
    if retries.get("decision_failures") and retries.get("decision_retry_at"):
        label = "基金经理失败退避" if retries.get("decision_failure_role") == "manager" else "AI 失败退避"
        lines.append(f"{label}：连续失败 {retries['decision_failures']} 次；"
                     f"记录的重试时间 {retries['decision_retry_at']}")
    if failure:
        detail = json.loads(failure[1])
        origin = (detail.get("causes") or [detail])[-1]
        frames = origin.get("frames") or detail.get("frames") or []
        location = frames[-1] if frames else {}
        code = f" errno={origin['errno']} ({origin.get('errno_name', '')})" if "errno" in origin else ""
        lines.append(f"最近业务异常：{failure[0]} {detail.get('phase')} {detail.get('error')}{code}；"
                     f"位置 {location.get('module', '?')}.{location.get('function', '?')}:{location.get('line', '?')}")
    if rows:
        lines.append("最近通知：")
        for at, body in rows:
            note = json.loads(body)
            details = note.get("details")
            text = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
            lines.append(f"  {at}  {note.get('kind')}  {text[:120]}")
    return "\n".join(lines)
