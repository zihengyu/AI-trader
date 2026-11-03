"""A one-screen, read-only operator summary for the maintenance script.

It reads only what the services already write (STOP, monitor.json, the runner
heartbeat and the journal) and never changes trading state.
"""
import json
import sqlite3
import sys
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


def resumes_exhausted(root, record):
    """The monitor gave up resuming this STOP: an auto_resume_exhausted notice at or after it was written."""
    book = Path(root) / "state.sqlite3"
    if not record or not book.is_file():
        return False
    since = instant(record["at"]) if record.get("at") else None
    with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT at, body FROM events WHERE kind='notification' ORDER BY sequence DESC LIMIT 200")
        for at, body in rows:
            if json.loads(body).get("kind") == "auto_resume_exhausted" and (since is None or instant(at) >= since):
                return True
    return False


def needs_operator(root):
    """Only a person can lift this STOP: operator stops, faults needing a person, or exhausted automatic resumes."""
    record = stops.read(root) if Path(root).is_dir() else None
    if record is None:
        return False
    return record["class"] == "operator" or not stops.resumable(record) or resumes_exhausted(root, record)


def monitor_ready(root, now=None, after=None):
    """The independent monitor wrote a fresh, healthy (or deliberately stopped) state.

    after: a Unix time the state must be newer than (a restart must not be
    confirmed by the previous monitor's last write).
    """
    now = now or datetime.now(timezone.utc)
    state = _read(Path(root) / "monitor.json") or {}
    age = _age(state.get("at"), now)
    if after is not None:
        try:
            if instant(state.get("at")).timestamp() <= after:
                return False
        except (TypeError, ValueError):
            return False
    return age is not None and 0 <= age <= MONITOR_FRESH_SECONDS and state.get("health") in {"healthy", "stopped"}


DESK_LABELS = {"news": "资讯", "intraday": "盘中行情", "premarket": "盘前行情", "global": "全球市场", "macro": "宏观",
               "calendar": "经济日历", "earnings": "财报日历", "bars": "日线", "snapshot": "行情快照", "sec": "SEC 财报"}


NOTE_LABELS = (("needs FRED_API_KEY", "需要在 .env 填 FRED_API_KEY，免费申请"),
               ("Yahoo is failing for the trading process", "交易进程的 Yahoo 请求出错，暂时让路"),
               ("no pre-market trades yet", "暂无盘前成交"), ("no account journal yet", "账本尚未创建"),
               ("waiting for the day's daily bars", "等美东 06:00 后的当天日线；期间交易进程自己计算"))


def _note(text):
    return next((label for prefix, label in NOTE_LABELS if str(text).startswith(prefix)), text)


def desk_line(root, now):
    """One line on the data desk (数据员): running or not, and how fresh each dataset is."""
    from .datadesk import DeskStatus
    view = DeskStatus(root).summary(now)
    if view.get("disabled"):
        return "数据员：已关闭（FUND_DATA_DESK=false，交易进程自己拉取数据）"
    if "datasets" not in view:
        return "数据员：还没有运行过（交易进程自己拉取数据）"
    parts = []
    for name, label in DESK_LABELS.items():
        item = view["datasets"].get(name)
        if not item:
            continue
        age = item.get("updated_minutes_ago")
        text = f"{label} {age:g} 分钟前" if age is not None else f"{label} 尚无成功"
        note = f"（出错：{item['error']}）" if item.get("error") else (f"（{_note(item['note'])}）" if item.get("note") else "")
        parts.append(text + note)
    state = "运行中" if view["running"] else "已停止（交易进程自己拉取数据，交易不受影响）"
    from .datastore import bars_settled
    if "bars" not in view["datasets"] and (DeskStatus(root).read() or {}).get("mode") == "local" \
            and not bars_settled(now):
        parts.insert(0, "日线 美东 06:00 后拉取")
    busy = view.get("busy_with") or {}
    doing = f"，正在处理 {DESK_LABELS.get(busy.get('job'), busy.get('job'))}" if busy.get("job") else ""
    return f"数据员：{state}{doing}；" + ("、".join(parts) or "还没有完成的数据")


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
        elif stops.resumable(record) and resumes_exhausted(root, record):
            hint = ("24 小时内自动恢复次数已用完，不会再自动恢复；查看 bash scripts/fund-logs.sh errors 确认原因后，"
                    "运行 bash scripts/fund-start.sh -y 恢复")
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
    lines.append(desk_line(root, now))
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
        swept = account.get("cash_equivalents") or {}
        parked = (f"（其中 {account.get('cash_equivalent_value')} 停在短债 ETF：" +
                  "、".join(f"{s}×{q}" for s, q in swept.items()) + "）") if swept else ""
        lines.append(f"账户：净值 {account.get('equity')}，现金 {account.get('cash')}{parked}，持仓 {held}"
                     f"（{account.get('as_of') or account.get('at') or ''}）")
    if data.get("return") is not None:
        lines.append(f"累计收益：{data['return'] * 100:.2f}%；最大观测回撤：{(data.get('max_observed_drawdown') or 0) * 100:.2f}%")
    lines.append(f"待审批：{data.get('pending_approval_count', 0)} 个；已复盘：{data.get('postmarket_review_runs', 0)} 次")
    with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        failure = db.execute("SELECT at,body FROM events WHERE kind='operation_error' ORDER BY sequence DESC LIMIT 1").fetchone()
        age = _age(failure[0], now) if failure else None
        if age is not None and age > 86400:
            failure = None  # Only the last 24 hours: an old, fixed error is not a current problem.
        retries = {key: json.loads(value) for key, value in db.execute(
            "SELECT key,value FROM meta WHERE key IN ('decision_failures','decision_retry_at','decision_failure_role')")}
        rows = db.execute("SELECT at,body FROM events WHERE kind='notification' ORDER BY sequence DESC LIMIT 6").fetchall()
    if retries.get("decision_failures") and retries.get("decision_retry_at"):
        label = "基金经理失败退避" if retries.get("decision_failure_role") == "manager" else "AI 失败退避"
        lines.append(f"{label}：连续失败 {retries['decision_failures']} 次；"
                     f"记录的重试时间 {retries['decision_retry_at']}")
    if failure:
        detail = json.loads(failure[1])
        # mkdir(parents=True) handles missing parents before its final EROFS.
        # Those ENOENT contexts must not replace the errno that escaped.
        causes = detail.get("causes") or []
        origin = detail if "errno" in detail else next(
            (cause for cause in causes if "errno" in cause), detail)
        frames = origin.get("frames") or detail.get("frames") or []
        location = next((frame for frame in reversed(frames)
                         if frame.get("module", "").split(".")[0] not in sys.stdlib_module_names),
                        frames[-1] if frames else {})
        code = f" errno={origin['errno']} ({origin.get('errno_name', '')})" if "errno" in origin else ""
        ago = f"{int(age // 3600)} 小时前" if age >= 3600 else f"{int(age // 60)} 分钟前"
        lines.append(f"最近业务异常（{ago}）：{failure[0]} {detail.get('phase')} {detail.get('error')}{code}；"
                     f"位置 {location.get('module', '?')}.{location.get('function', '?')}:{location.get('line', '?')}")
    if rows:
        lines.append("最近通知：")
        for at, body in rows:
            note = json.loads(body)
            details = note.get("details")
            text = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
            lines.append(f"  {at}  {note.get('kind')}  {text[:120]}")
    return "\n".join(lines)
