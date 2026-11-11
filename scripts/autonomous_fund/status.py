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
from .journal import notification_body
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
RUNNER_FRESH_SECONDS = 180


DELIVERY_STATES = {"sent": "已发送", "pending": "等待发送", "sending": "正在发送", None: "已生成，等待独立监控发送"}


def weekly_lines(db, now, stopped=False):
    """Where the weekly report stands: the latest one, why the last week has none yet, and the next one."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from .journal import NOTIFICATION_KIND
    from .notifications import week_of, week_sessions
    today = instant(now).astimezone(ZoneInfo("America/New_York")).date()
    weeks = sorted(week_sessions(today - timedelta(days=10), today + timedelta(days=17)).items())
    done = [(week, day) for week, (day, close) in weeks if close <= now]
    upcoming = [(week, day) for week, (day, close) in weeks if close > now]
    tracked = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_deliveries'").fetchone()
    latest = db.execute(
        "SELECT json_extract(e.body,'$.details.week'),json_extract(e.body,'$.details.session_date')," +
        ("d.state,d.last_error " if tracked else "NULL,NULL ") + "FROM events e " +
        ("LEFT JOIN fund_deliveries d ON d.sequence=e.sequence " if tracked else "") +
        f"WHERE e.kind='notification' AND {NOTIFICATION_KIND.replace('body', 'e.body')}='weekly_report' "
        "ORDER BY e.sequence DESC LIMIT 1").fetchone()
    lines = []
    if latest:
        week, day, state, error = latest
        if state == "sent" and error:
            text = "报告生成失败，已发提醒；原因见 bash scripts/fund-logs.sh errors"
        elif state == "retry":
            text = f"发送失败，正在重试（{error}）"
        elif state == "suppressed":
            text = f"未发送（{error or '重复或过期'}）"
        else:
            text = DELIVERY_STATES.get(state, state)
        lines.append(f"周报：最近一份 {week}（{day} 收盘后，{text}）")
    else:
        lines.append("周报：还没有生成过")
    first = db.execute("SELECT at FROM events ORDER BY sequence LIMIT 1").fetchone()
    if first:  # A week that ended before the account existed has nothing to report.
        done = [(week, day) for week, day in done if dict(weeks)[week][1] > instant(first[0])]
    if done and (not latest or latest[0] != done[-1][0]):
        week, day = done[-1]
        monday, sunday, _ = week_of(day)
        reviews = dict(db.execute(
            "SELECT json_extract(body,'$.session_date'),1 FROM events WHERE kind='postmarket_review' AND json_valid(body) "
            "AND json_extract(body,'$.session_date') BETWEEN ? AND ?", (monday, sunday)).fetchall())
        if day in reviews:
            lines.append(f"  {week}：{day} 已复盘，等待独立监控生成周报（监控每 10 秒检查一次）")
        else:
            why = "STOP 期间不做复盘。" if stopped else ""
            later = (f"本周有 {len(reviews)} 次复盘，下周第一个交易日开盘后用其中最近一次发周报。" if reviews else
                     "本周没有任何复盘，这一周不会有周报。")
            lines.append(f"  {week}：本周最后一个交易日 {day} 还没有盘后复盘。{why}如果那天有交易记录，"
                         f"交易进程会在下次启动或开盘前一小时补做复盘；否则{later}")
    if upcoming:
        week, day = upcoming[0]
        lines.append(f"  下一份 {week}：{day}（纽约）收盘复盘完成后发送")
    return lines


def day_trade_line(db, now):
    """Estimated same-day round trips in the last five sessions."""
    from .holdings import day_trades
    fills = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
        "SELECT at,kind,body FROM events WHERE kind IN ('fill','fill_reconciled') ORDER BY sequence")]
    count = day_trades(fills, now)
    return f"日内交易（同一天买入又卖出，估算）：最近 {count['window']} 个交易日 {count['last_sessions']} 次"


def triage_line(db, now):
    """Intraday event triage: shadow or enforcing, the scored misses, and today's screening calls."""
    from zoneinfo import ZoneInfo
    from .triage import upper_bound
    meta = {key: json.loads(value) for key, value in db.execute(
        "SELECT key,value FROM meta WHERE key IN ('triage_enforcing','triage_pairs')")}
    scored = db.execute("SELECT body FROM events WHERE kind='triage_scored' AND json_valid(body) "
                        "AND json_type(body,'$.stats')='object' ORDER BY sequence DESC LIMIT 1").fetchone()
    window = (json.loads(scored[0]).get("stats") or {}).get("window", 40) if scored else 40
    if type(window) is not int or not 10 <= window <= 500:
        window = 40
    pairs = (meta.get("triage_pairs") or [])[-window:]
    zone = ZoneInfo("America/New_York")
    today = instant(now).astimezone(zone).date().isoformat()
    actions = {}
    for at, body in db.execute("SELECT at,body FROM events WHERE kind='event_triage' ORDER BY sequence DESC LIMIT 400"):
        if instant(at).astimezone(zone).date().isoformat() == today:
            action = json.loads(body).get("action")
            actions[action] = actions.get(action, 0) + 1
    if not pairs and not actions:
        return None
    misses = sum(bool(p) for p in pairs)
    state = "生效中" if meta.get("triage_enforcing") else "影子观察（基金经理照常被叫，只打分）"
    return (f"盘中事件判断：{state}；最近 {len(pairs)} 次“不需要”里漏判 {misses} 次"
            f"（95% 上限 {upper_bound(misses, len(pairs)) * 100:.1f}%）；今天判断 {sum(actions.values())} 次"
            + (f"，其中未叫基金经理 {actions.get('dismissed', 0)} 次" if meta.get("triage_enforcing") else ""))


def _age(at, now):
    try:
        return (now - instant(at)).total_seconds()
    except (TypeError, ValueError):
        return None


def _read(path):
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else None
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
            if (notification_body(body) or {}).get("kind") == "auto_resume_exhausted" and (
                    since is None or instant(at) >= since):
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
               ("waiting for the day's daily bars", "等美东 06:00 后的当天日线；期间交易进程自己计算"),
               ("no daily bars for", "等美东 06:00 后的当天日线；期间交易进程自己计算"))


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


def cost_line(costs):
    """模型费用一行：今天、近 30 天、年化节奏占净值的比例（AI 每年至少要跑赢基准这么多才能打平费用）。"""
    from .costs import fraction_text
    if not costs or not costs.get("calls"):
        return None
    if not costs.get("days_with_calls"):
        return (f"模型费用：近 {costs['window_days']} 天 {costs['calls']} 次调用都没有单价，无法估算"
                "（在 .env 设置 FUND_MODEL_PRICES_JSON）")
    text = (f"模型费用（估算）：今天 ${costs['today_usd']:.2f}；近 {costs['window_days']} 天 ${costs['window_usd']:.2f}"
            f"（{costs['days_with_calls']} 个交易日，日均 ${costs['average_per_day_usd']:.2f}）；按此速度一年约 "
            f"${costs['annual_pace_usd']:.0f}，占净值 {fraction_text(costs['break_even_excess_return'])}"
            "——AI 每年至少要跑赢基准这么多才能打平模型费用")
    if costs.get("unpriced_calls"):
        text += f"（另有 {costs['unpriced_calls']} 次调用没有单价，未计入）"
    return text


def fund_value_line(account, costs):
    """The account equity less the fund's accrued model and data costs (scoreboard.accrued_costs); None without both."""
    from .risk import number
    from .scoreboard import known_cost_total
    if not costs or not account or account.get("unavailable") or account.get("equity") is None:
        return None
    try:
        equity = float(number(account["equity"]))
    except (TypeError, ValueError):
        return None
    spent = known_cost_total(costs)
    data = costs.get("known_data_usd", costs.get("data_usd"))
    text = (f"扣除 AI 费用后的基金净值 ${equity - spent:,.2f}（成立以来模型费用 ${costs['model_usd']:,.2f}"
            + (f"、数据费用 ${data:,.2f}" if data else "") + "）")
    if costs.get("unpriced_calls"):
        text += f"，另有 {costs['unpriced_calls']} 次调用未定价、未计入，实际更低"
    if costs.get("data_usd") is None:
        text += "，数据费用未知、未计入"
    return text


def account_lines(account, recent_trades=None, *, now=None, costs=None):
    """Separate observed holdings from AI targets, retaining the snapshot's own time.

    costs: scoreboard.accrued_costs, for the fund's value after them (the account itself never pays them)."""
    from zoneinfo import ZoneInfo
    from .risk import number
    now = now or datetime.now(timezone.utc)

    def money(value):
        try:
            return f"${number(value):,.2f}"
        except (TypeError, ValueError):
            return "暂无"

    def shares(value):
        text = format(number(value), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text

    def when(value):
        try:
            moment = instant(value)
            return (f"美东 {moment.astimezone(ZoneInfo('America/New_York')):%m-%d %H:%M}"
                    f" · 本地 {moment.astimezone():%m-%d %H:%M}")
        except (TypeError, ValueError):
            return "时间未知"

    available = bool(account and not account.get("unavailable"))
    account = account if available else {}
    at = account.get("as_of") or account.get("at")
    age = _age(at, now)
    updated = "时间未知" if age is None else "时间异常"
    if age is not None and age >= 0:
        unit, label = next((unit, label) for unit, label in (
            (86400, "天"), (3600, "小时"), (60, "分钟"), (1, "秒")) if age >= unit or unit == 1)
        updated = f"{int(age // unit)}{label}前"
    from .market_data import regular_session_open
    if not regular_session_open(now):
        updated = "休市"
    lines = [f"📌 当前实际持仓（快照：{when(at)}）",
             f"账户：净值 {money(account.get('equity'))}（{updated}）"
             f" ｜ 现金 {money(account.get('cash'))}"]
    value = fund_value_line(account, costs) if available else None
    if value:
        lines.append(value)
    if not available:
        lines = ["📌 当前实际持仓：暂无可用账户快照"]
    quote_times = [mark.get("as_of") for mark in (account.get("valuation", {}).get("quotes") or {}).values()]
    try:
        if quote_times:
            lines.append(f"估值行情最早时间：{when(min(quote_times, key=instant))}")
    except (TypeError, ValueError):
        pass  # Legacy records still disclose the account snapshot time above.
    prices = account.get("position_average_prices") or {}
    positions = account.get("positions") or {}
    for symbol, quantity in sorted(positions.items()):
        if number(quantity) == 0:
            continue
        lines.append(f"• {symbol}：{shares(quantity)} 股 ｜ 买入均价 {money(prices.get(symbol))}")
    if available and not any(number(quantity) != 0 for quantity in positions.values()):
        lines.append("• 暂无证券持仓")
    swept = account.get("cash_equivalents") or {}
    if swept:
        lines.append(f"现金中含短债 ETF {money(account.get('cash_equivalent_value'))}：")
        lines += [f"• {symbol}：{shares(quantity)} 股 ｜ 买入均价 {money(prices.get(symbol))}"
                  for symbol, quantity in sorted(swept.items())]
    if recent_trades:
        lines.append("🧾 最近成交（最近 6 笔，时间为成交观测时间）：")
        for trade in recent_trades:
            side = {"buy": "买入", "sell": "卖出"}.get(trade.get("side"), trade.get("side"))
            label = "累计成交均价" if trade.get("evidence_kind") == "broker_cumulative_observation" else "成交价"
            lines.append(f"• {side} {trade['symbol']}：{shares(trade['quantity'])} 股 ｜ {label} {money(trade.get('price'))}"
                         f"（{when(trade.get('at'))}）")
    elif recent_trades is not None:
        lines.append("最近成交：暂无已确认的买卖成交")
    return lines


def summary(root, mode, now=None):
    from .settings import MODES
    root, now = Path(root), now or datetime.now(timezone.utc)
    lines = [f"模式：{mode}（{MODES.get(mode, mode)}）", f"账户目录：{root}"]
    beat = _read(root / ("status.json" if mode == "local" else "runner_health.json"))
    record = stops.read(root) if root.is_dir() else None
    if record is None:
        age = _age(beat.get("heartbeat_at"), now) if beat else None
        if not beat:
            lines.append("交易：尚未确认运行（没有 STOP；尚无交易进程心跳）")
        elif beat.get("runner_state") == "exited":
            lines.append("交易：未运行（没有 STOP；交易进程已退出）")
        elif age is None or not 0 <= age <= RUNNER_FRESH_SECONDS:
            lines.append("交易：状态未知（没有 STOP；交易进程心跳过期或时间异常）")
        elif beat.get("runner_state") != "running":
            lines.append("交易：尚未确认运行（没有 STOP；交易进程尚未就绪）")
        else:
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
    if beat:
        age = _age(beat.get("heartbeat_at"), now)
        market = beat.get("market") or {}
        open_text = ("开盘中" if market.get("open") else f"休市，下次开盘 {market.get('next_open')}") if market else ""
        lines.append(f"交易进程：{beat.get('runner_state')}（心跳 {int(age) if age is not None else '?'} 秒前）"
                     + (f"；市场：{open_text}" if open_text else ""))
        if beat.get("valuation_error"):
            lines.append(f"账户估值暂未更新：{beat['valuation_error']}；账户区块保留上次成功快照及其时间。")
        if beat.get("market_data_error"):
            lines.append(f"成交等待新行情：{beat['market_data_error']}；自动重试，原订单仍遵守限价和有效期。")
    lines.append(desk_line(root, now))
    book = root / "state.sqlite3"
    if not book.is_file():
        lines.append("账本：尚未创建")
        return "\n".join(lines)
    from .report import report
    try:
        data = report(root, write_files=False)
    except Exception as exc:
        lines.append(f"账本：读取失败（{type(exc).__name__}）")
        return "\n".join(lines)
    account = data.get("latest_account") or {}
    integrity = data.get("ledger_audit") or {}
    if integrity.get("status") == "failed":
        lines.append("账务校验失败：" + ", ".join(sorted({r["code"] for r in integrity.get("errors", [])}))
                     + "；净值与成交明细暂不可用，需先核对账本。")
    elif integrity.get("status") == "partial":
        details = []
        if integrity.get("missing_evidence"):
            details.append("缺少历史证据：" + ", ".join(integrity["missing_evidence"]))
        if integrity.get("warnings"):
            details.append("估值／统计证据存在异常，已停用对应数据；现金持仓成交校验不受其影响")
        lines.append("账务校验：部分核实（" + "；".join(details) + "）")
    elif integrity.get("status") == "verified":
        lines.append("账务校验：通过（现金、持仓、成交、费用及模拟执行依据一致）")
    if account:
        lines += ["", *account_lines(account, data.get("recent_trades"), now=now, costs=data.get("fund_costs")), ""]
    if data.get("return") is not None:
        lines.append(f"累计收益：{data['return'] * 100:.2f}%；最大观测回撤：{(data.get('max_observed_drawdown') or 0) * 100:.2f}%")
    lines.append(f"待审批：{data.get('pending_approval_count', 0)} 个；已复盘：{data.get('postmarket_review_runs', 0)} 次")
    line = cost_line(data.get("model_costs"))
    if line:
        lines.append(line)
    with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        failure = db.execute("SELECT at,body FROM events WHERE kind='operation_error' ORDER BY sequence DESC LIMIT 1").fetchone()
        age = _age(failure[0], now) if failure else None
        if age is not None and age > 86400:
            failure = None  # Only the last 24 hours: an old, fixed error is not a current problem.
        retries = {key: json.loads(value) for key, value in db.execute(
            "SELECT key,value FROM meta WHERE key IN ('decision_failures','decision_retry_at','decision_failure_role')")}
        rows = db.execute("SELECT at,body FROM events WHERE kind='notification' ORDER BY sequence DESC LIMIT 6").fetchall()
        try:
            lines += weekly_lines(db, now, stopped=record is not None)
        except Exception as exc:  # A status line, never a reason to hide the rest of the summary.
            lines.append(f"周报：状态暂不可用（{type(exc).__name__}）")
        try:
            lines.append(day_trade_line(db, now))
        except Exception as exc:
            lines.append(f"日内交易计数：暂不可用（{type(exc).__name__}）")
        try:
            line = triage_line(db, now)
            if line:
                lines.append(line)
        except Exception as exc:
            lines.append(f"盘中事件判断：状态暂不可用（{type(exc).__name__}）")
    from zoneinfo import ZoneInfo
    from .decision_activity import activity, lines as activity_lines
    audit = activity(root, now=now)
    zone = ZoneInfo("America/New_York")
    todays = [instant(item["at"]) for item in audit["decisions"]]
    lines.append(f"决策：今天 {audit['validated']} 次有效决策（通过校验的复核，不代表下单或成交）" + (
        f"，最近一次 {max(todays).astimezone(zone):%H:%M}（纽约）" if todays else "") + (
        f"；累计 {audit['cumulative_validated']} 次" if audit["cumulative_validated"] else "；还没有过有效决策"))
    lines += activity_lines(audit)[1:]
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
