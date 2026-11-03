"""Human-readable messages built only from the selected account's journal."""
import json
from collections import Counter
from zoneinfo import ZoneInfo

from .execution import operator_connection
from .journal import encode
from .risk import RiskError, instant


TRIGGERS = {
    "single_order_at_least_8_percent": "单笔交易达到净值的 8%（旧版逐笔规则）",
    "single_order_threshold": "单笔交易达到人工审批阈值（旧版逐笔规则）",
    "daily_turnover_threshold": "当日累计买卖额达到阈值（旧版逐笔规则）",
    "liquidate_entire_portfolio": "拟清空现有全部证券持仓（旧版规则）",
    "exposure_ramp": "当日总敞口增幅将超过自动额度",
    "buy_turnover": "当日买入现金流将超过自动额度",
    "market_stress": "基准市场处于压力状态（大跌或高波动），任何新增买入都需确认",
    "automatic_envelope": "超出自动风险增量额度",
}


def _pct(value):
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def approval_message(details):
    reasons = "；".join(TRIGGERS.get(r, r) for r in details["reasons"])
    if "buys" in details:
        code = str(details["id"])[:6].upper()
        lines = ["量化风控：调仓计划超出自动额度，待一次性审批（只覆盖新增买入；卖出/减仓已自动执行）",
                 f"审批代码 {code}：直接回复本消息「同意」或「拒绝」，或发送「同意 {code}」／「拒绝 {code}」。",
                 f"审批触发：{reasons}",
                 f"参考净值：${details.get('observed_equity')}；开盘敞口 {_pct(details.get('start_exposure'))}，"
                 f"当前 {_pct(details.get('current_exposure'))}，批准后最高 {_pct(details.get('max_exposure_after'))}",
                 f"当日买入现金流上限：${details.get('max_buy_turnover_usd')}",
                 f"自动额度：{encode(details.get('auto_envelope'))}"]
        risk = details.get("risk") or {}
        if risk:
            lines.append(f"构建后组合：年化波动 {_pct(risk.get('annual_vol'))}，1日97.5% ES {_pct(risk.get('es_975_1d'))}")
        stress = details.get("stress") or {}
        if stress.get("stressed"):
            lines.append(f"市场压力：{encode(stress.get('reasons'))}")
        lines.append("拟买入（权重上限 / 限价上界）：")
        for symbol, scope in sorted(details["buys"].items()):
            lines.append(f"- {symbol}: {_pct(scope['max_weight'])} / ${scope['max_limit']}")
        lines += [f"有效期至（UTC）：{details['expires_at']}", f"请求 ID {details['id']}，摘要 {details['digest']}",
                  "批准不放宽任何硬性风控；每笔仍实时复核波动率、ES、行业、回撤与报价。"
                  "超时只停止超额部分，已在自动额度内的买入不受影响。", "",
                  "AI 理由（可能包含不确定判断）：", str(details.get("rationale") or "")]
        return "\n".join(lines)
    side = {"buy": "买入", "sell": "卖出"}.get(details["side"], details["side"])
    return (f"重大模拟交易待审批：{side} {details['symbol']}\n"
            f"当前持股：{details.get('current_quantity', '见审批原文')}；目标权重：{details.get('target_weight', '见原文')}\n"
            f"最多 {details['max_quantity']} 股；委托限价上界 ${details.get('max_limit', '见原文')}；"
            f"卖出限价下界 ${details.get('min_limit', '旧请求须重新审阅')}\n"
            f"预留名义金额上界 ${details['max_notional']}（卖出实际收入仍按成交价）\n"
            f"参考账户净值：${details.get('observed_equity', '见原文')}\n"
            "审批触发：" + reasons + "\n"
            f"有效期至（UTC）：{details['expires_at']}\n"
            f"请求 ID：{details['id']}\n摘要：{details['digest']}\n"
            "请在服务器 approvals 查看完整组合与理由，再用 approve/reject + ID + 摘要处理。"
            "Telegram 回复不会批准；超时不下单，批准后仍须通过实时风控。\n\n"
            "AI 买卖理由／证据／主要风险（可能包含不确定判断）：\n" +
            str(details.get("rationale") or "旧请求没有通知理由，请查看 approvals 中的完整原文。"))


def _cost_text(costs):
    from .status import cost_line
    return cost_line(costs) or "模型费用：没有可估算的调用（设置 FUND_MODEL_PRICES_JSON 后显示）"


FACTOR_STATUS = {"validated": "通过", "rejected": "未通过", "insufficient_data": "数据不足", "invalid": "表达式无效"}
HEALTH_KINDS = {"blocked", "bookkeeping_error", "watchdog_stop", "watchdog_recovered", "auto_resumed",
                "auto_resume_blocked", "auto_resume_exhausted", "bot_crash", "server_supervision_stop",
                "postmarket_review_error", "premarket_research_error", "model_budget_exhausted",
                "model_cost_budget_exhausted", "decision_drought"}


def _percent(value):
    return "暂无" if value is None else f"{value * 100:.2f}%"


def daily_document(root, sequence):
    # The AI supplies neither a file path nor an upload URL. Render one exact
    # journal row in memory, so a prompt cannot turn reports into file exfiltration.
    with operator_connection(root) as db:
        row = db.execute("SELECT body FROM events WHERE sequence=? AND kind='postmarket_review'", (sequence,)).fetchone()
        if not row:
            raise RiskError("Daily report has no matching completed review")
        packet = json.loads(row[0])
        cutoff, day = instant(packet["completed_at"]), packet["session_date"]
        events = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events ORDER BY sequence") if instant(at) <= cutoff]
        # The scorecard and factor tests are written just after the review itself
        # (a live clock has moved on), so select them by session, not by time.
        related = [(kind, json.loads(body)) for kind, body in db.execute(
            "SELECT kind,body FROM events WHERE kind IN ('scorecard','factor_tests') AND "
            "json_extract(body,'$.session_date')=? ORDER BY sequence", (day,))]
    from .report import report
    snapshot = report(root, write_files=False, as_of=cutoff)
    review, session = packet["review"], packet["session"]
    zone = ZoneInfo("America/New_York")
    day_events = [{"at": at, "kind": kind, "body": body} for at, kind, body in events
                  if instant(at).astimezone(zone).date().isoformat() == day and
                  kind in {"order_submitted", "fill", "fill_reconciled", "fill_details_unavailable", "operator_approval"}]
    watch_events = [{"at": at, "kind": kind, "body": body} for at, kind, body in events
                    if instant(at).astimezone(zone).date().isoformat() == day and
                    kind in {"protective_trigger", "price_alert", "review_triggered", "entry_waiting", "buy_chase_limit"}]
    lines = [f"# {day} 模拟账户日报及盘后复盘", "",
             f"报告截止：{packet['completed_at']}；模式：{snapshot['mode']}；Mock：{packet['mock']}",
             f"模型：{encode(packet['models'])}；策略备忘版本：{packet['version']}", "",
             "## 当日表现与账户", "",
             f"观测盈亏 USD：{session['observed_pnl_usd']}；观测收益率：{session['observed_return']}",
             f"对齐观测窗口的 SPY／QQQ 价格收益：{encode(session['benchmark_observed_price_returns'])}",
             f"窗口起点：{encode(session['baseline_account'])}",
             f"窗口终点（现金、应收与持仓）：{encode(session['last_observed_account'])}", "",
             f"运行至报告截止的最大观测回撤：{snapshot['max_observed_drawdown']}",
             f"运行至报告截止的已确认交易费用 USD：{snapshot['observed_fees']}（null 表示不完整）",
             f"运行至报告截止的模型 usage：{encode(snapshot['model_usage_by_model'])}",
             _cost_text(snapshot.get("model_costs")),
             f"没有确认结束的模型调用（用量未知）：{encode(snapshot['unfinished_model_calls'])}",
             "模型美元费用须按配置费率查看 report；本报告不把缺少费率或未知 usage 当作零费用。", "",
             "## 买卖、成交及人工审批记录", "",
             "```json", json.dumps(day_events, ensure_ascii=False, indent=2), "```", "",
             "fill_reconciled 是券商累计成交观测，不能与成交回调重复相加；未成交订单不算实际交易。", "",
             "## 授权资金与约束", "",
             f"AI 授权资金（初始/当前基数）USD：{snapshot['mandate']['capital_usd']}；账户中保留不动用的现金 USD："
             f"{snapshot['mandate']['reserved_cash']}（为空表示整个专用账户都由 AI 管理）",
             "约束遵守情况（AI 自评，需人工抽查）：" + (review.get("mandate_compliance") or "本次未填写"), "",
             "## 盯盘触发（止损／止盈／价格提醒／提前复核）", "",
             "```json", json.dumps(watch_events, ensure_ascii=False, indent=2), "```", "",
             "## AI 记分卡（只用已完成收盘价客观打分）", ""]
    # Only this session's card: an earlier one under today's heading would be stale.
    card = next((body for kind, body in reversed(related) if kind == "scorecard"), None)
    if card:
        predictions, decisions = card["predictions"], card["decisions"]["by_horizon"]
        lines += [f"预测：共 {predictions['total']} 条，已打分 {predictions['scored']}，命中 {predictions['correct']}，"
                  f"命中率 {predictions['hit_rate']}，待验证 {predictions['pending']}",
                  "决策超额收益（1/5/20 个交易日；已扣除组合 beta 对应的市场涨跌，net 再扣交易成本）："
                  + json.dumps(decisions, ensure_ascii=False)]
        timing = card.get("timing")
        if timing:
            stop, profit = timing["protective_exits"]["stop_loss"], timing["protective_exits"]["take_profit"]
            waits = timing["entry_waits"]
            lines += [f"择时：止损 {stop['count']} 次，5 日后平均贡献 {_percent(stop['5']['mean_value_added'])}"
                      f"（正＝避免了继续下跌）；止盈 {profit['count']} 次，5 日后平均贡献 "
                      f"{_percent(profit['5']['mean_value_added'])}（负＝卖早了）；等待入场 {waits['count']} 次，"
                      f"买入 {waits['entered']} 次，平均价格改善 {_percent(waits['mean_entry_improvement'])}，"
                      f"未买入的 5 日后平均贡献 {_percent(waits['not_entered']['5']['mean_value_added'])}（负＝错过了上涨）"]
        lines += ["样本小时结论不稳定；记分差的假设应由 AI 在复盘中撤回。", ""]
    else:
        lines += ["本交易日没有记分卡（尚无可打分的样本，或当日数据不可用）。", ""]
    lines += ["## 因子挖掘（AI 提出，代码客观检验）", ""]
    results = [result for kind, body in related if kind == "factor_tests" for result in body.get("results", [])]
    for result in results:
        sample, holdout = result.get("in_sample") or {}, result.get("holdout") or {}
        lines.append(f"- {result.get('name')}：`{result.get('expression')}`（{result.get('horizon_sessions', '?')} 日）→ "
                     f"{FACTOR_STATUS.get(result.get('status'), result.get('status'))}；样本内 t={sample.get('t')}，"
                     f"样本外 t={holdout.get('t')}；{result.get('reason', '')}")
    lines += ([] if results else ["本次没有提出新因子。"]) + [""]
    health = Counter(body.get("kind") for at, kind, body in events if kind == "notification" and
                     body.get("kind") in HEALTH_KINDS and instant(at).astimezone(zone).date().isoformat() == day)
    lines += ["## 当日运行状况", "",
              ("；".join(f"{kind} × {count}" for kind, count in sorted(health.items())) if health else
               "当日没有阻断、故障、停止或自动恢复记录。"), ""]
    lines += ["## 下一交易日预测（将被自动打分）", "",
              "```json", json.dumps(review.get("predictions", []), ensure_ascii=False, indent=2), "```", "",
              "## 复盘结论", "", review["summary"], ""]
    for title, key in (("选股调整", "selection_adjustments"), ("择时调整", "timing_adjustments")):
        lines.extend([f"## {title}", ""])
        for change in review[key]:
            lines.extend([f"- 假设：{change['hypothesis']}", f"  依据：{change['rationale']}",
                          f"  证据：{', '.join(change['evidence_refs'])}", f"  验证条件：{change['validation']}",
                          f"  失效条件：{change['invalidated_when']}", ""])
        if not review[key]:
            lines.extend(["没有新增调整。", ""])
    for title, values in (("保留教训", review["retained_lessons"]), ("撤回假设", review["retired_hypotheses"]),
                          ("数据缺项", session["account_observation_gaps"] + review["data_gaps"])):
        lines.extend([f"## {title}", "", *[f"- {item}" for item in values], ""])
    lines.extend(["## 下一交易日策略备忘", "", review["strategy_memo"], "",
                  f"置信度：{review['confidence']}。调整仍是待验证假设；未实现盈亏不等于已实现利润。",
                  session["limitations"], "", "## 原始研究", "", packet["research"]["summary"], "",
                  encode(packet["research"]["evidence"])])
    return f"daily-{day}.md", "\n".join(lines)


# ----- Readable operator notices ---------------------------------------------------------------

LONG_MESSAGE = 3800  # Telegram allows 4096 characters; longer texts go as a document.


def _when(value):
    """New York time and the server's local time; never guesses an unknown time."""
    try:
        moment = instant(value)
    except (TypeError, ValueError):
        return "未知"
    return (f"美东 {moment.astimezone(ZoneInfo('America/New_York')):%m-%d %H:%M}"
            f" · 本地 {moment.astimezone():%m-%d %H:%M}")


def _stop_text(stop):
    from . import stops
    from .status import CLASSES, REASONS
    label = f"{CLASSES.get(stop.get('class'), stop.get('class'))}：{REASONS.get(stop.get('reason'), stop.get('reason'))}"
    if stop.get("class") == "operator":
        hint = "运行 bash scripts/fund-start.sh 确认恢复"
    elif stops.resumable({"class": stop.get("class"), "reason": stop.get("reason")}):
        hint = "满足条件后会自动恢复"
    else:
        hint = "需要先处理原因再恢复"
    return label, hint


def readable(kind, details, sequence=None):
    """Chinese text for the notices an operator reads most; None keeps the generic format."""
    from .settings import MODES
    from .status import REASONS
    details = details if details is not None else {}
    text = None
    if kind == "premarket_research":
        phase = {"before_market_opens": "开盘前", "closed_startup": "休市期间"}.get(details.get("phase"), "盘前")
        lines = [f"📊 盘前研究 · {phase}（开盘 {_when(details.get('session_open'))}）", "", details.get("summary") or ""]
        evidence = details.get("evidence") or []
        if evidence:
            lines += ["", f"证据 {details.get('evidence_count', len(evidence))} 条："]
            lines += [f"• {e.get('source')}（{_when(e.get('available_at') or e.get('observed_at'))}）" for e in evidence]
        lines += ["", "这只是研究：开盘后由 AI 决策，并经过风控后才会下单。"]
        text = "\n".join(lines)
    elif kind == "session_open":
        lines = [f"🔔 美股开盘 · {details.get('session_date')}"]
        stop = details.get("stop")
        if stop:
            label, hint = _stop_text(stop)
            lines.append(f"交易：暂停（{label}）——{hint}")
        else:
            lines.append("交易：运行中")
        mode = details.get("mode")
        lines.append(f"模式：{mode}（{MODES.get(mode, mode)}）")
        account = details.get("account")
        if account:
            held = "、".join(f"{s}×{q}" for s, q in (account.get("positions") or {}).items()) or "无"
            lines.append(f"账户：净值 {account.get('equity')}，现金 {account.get('cash')}，持仓 {held}")
        if details.get("research_summary"):
            lines.append(f"今日盘前研究要点：{details['research_summary']}")
        if details.get("close"):
            lines.append(f"收盘：{_when(details['close'])}")
        if not stop:
            lines.append("AI 会在开盘后复核并决定今天的组合；买入在自动额度内直接执行，超出额度会发审批请求。")
        text = "\n".join(lines)
    elif kind == "decision_changed" and isinstance(details, dict):
        def percent(value):
            return f"{float(value) * 100:g}%"

        def levels(row):
            parts = [f"{label} {row[key]}" for key, label in (("entry_limit", "入场 ≤"), ("stop_loss", "止损"),
                                                                ("take_profit", "止盈")) if row.get(key)]
            return f"（{'，'.join(parts)}）" if parts else ""
        changes = details.get("changes") or []
        header = ("🧭 今日首次复核：维持目标组合" if details.get("first_today") else "🧭 AI 调整目标组合")
        lines = [f"{header}（{_when(details.get('at'))}）"]
        for label, rows in (("买入／加仓", [r for r in changes if float(r["to"]) > float(r["from"])]),
                            ("卖出／减仓", [r for r in changes if float(r["to"]) < float(r["from"])]),
                            ("不变", [r for r in changes if float(r["to"]) == float(r["from"])])):
            for row in rows:
                if label == "不变":
                    lines.append(f"不变：{row['symbol']} {percent(row['to'])}{levels(row)}")
                else:
                    lines.append(f"{label}：{row['symbol']} {percent(row['from'])}→{percent(row['to'])}{levels(row)}")
        lines.append(f"现金约 {percent(details.get('cash_weight') or 0)}")
        if details.get("rationale"):
            lines += ["", f"理由：{details['rationale']}"]
        if details.get("next_review_at"):
            lines.append(f"下次复核：{_when(details['next_review_at'])}")
        lines.append("权重是上限：风控可能按波动、尾部风险等再缩小；买入在自动额度内直接执行，超出额度会发审批。")
        text = "\n".join(lines)
    elif kind == "stop_active" and isinstance(details, dict):
        label, hint = _stop_text(details)
        text = f"⏸ 交易已停止（{label}）——{hint}；期间不会下新单。"
    elif kind == "auto_resumed" and isinstance(details, dict):
        text = f"▶️ 已自动恢复交易（之前因{REASONS.get(details.get('stop_reason'), details.get('stop_reason'))}停止）。"
    elif kind == "auto_resume_blocked":
        text = "⚠️ 未能自动恢复：有状态不明的订单，请到券商核对后运行 bash scripts/fund-start.sh。"
    elif kind == "auto_resume_exhausted" and isinstance(details, dict):
        text = (f"⚠️ 未能自动恢复：24 小时内已自动恢复 {details.get('resumes_last_24h')} 次，达到上限。"
                "请运行 bash scripts/fund-logs.sh errors 查看原因，再运行 bash scripts/fund-start.sh。")
    elif kind == "watchdog_stop" and isinstance(details, dict):
        reason = details.get("reason")
        _, hint = _stop_text({"class": "watchdog", "reason": reason})
        text = f"🚨 监控停止交易：{REASONS.get(reason, reason)}（{reason}）——{hint}。"
    elif kind == "watchdog_recovered":
        automatic = "resumes by itself" in str(details)
        text = "✅ 健康已恢复：" + ("满足条件后会自动恢复交易。" if automatic else "交易仍需确认后恢复（bash scripts/fund-start.sh）。")
    elif kind == "server_supervision_stop":
        text = f"🚨 服务监督停止交易：{REASONS.get(str(details), details)}（{details}）。"
    elif kind == "bot_crash":
        text = f"🚨 交易进程崩溃（{details}）：服务会自动重启，监控确认安全后才恢复交易。"
    elif kind == "blocked":
        text = f"⚠️ 本轮未执行：{details}（下一轮会再检查）"
    elif kind == "bookkeeping_error":
        text = f"⚠️ 记账步骤出错，下一轮自动重试：{details}"
    elif kind == "model_budget_exhausted" and isinstance(details, dict):
        text = (f"ℹ️ 今日模型调用预算已用完（{details.get('runs')}/{details.get('limit')}）："
                "已有目标继续执行，止损照常，明天恢复研究。")
    elif kind == "data_desk_stale" and isinstance(details, dict):
        text = (f"ℹ️ 数据员已 {details.get('heartbeat_minutes_ago')} 分钟没有更新：交易进程改为自己拉取行情、资讯和宏观数据，"
                "交易不受影响。查看：bash scripts/fund-logs.sh errors；重启：bash scripts/fund-restart.sh -y")
    elif kind == "decision_drought" and isinstance(details, dict):
        last = details.get("last_decision_at")
        text = (f"⚠️ 交易时段已经 {details.get('minutes_without_decision')} 分钟没有有效的投资决策"
                f"（今天最近一次：{_when(last) if last else '还没有'}；今天决策失败重试 "
                f"{details.get('decision_failures_today', 0)} 次）。进程是健康的，但没有做出决策，常见原因是模型持续失败或"
                "订单一直未完成。查看：bash scripts/fund-logs.sh errors")
    elif kind == "model_cost_budget_exhausted" and isinstance(details, dict):
        text = (f"ℹ️ 今日模型费用已到上限（约 ${details.get('cost_usd')} / ${details.get('limit_usd')}）："
                "当天不再发起新决策、反方审查和盘前研究；已有目标和止损照常执行，明天恢复。")
    elif kind == "drawdown_pause":
        text = "🚨 回撤触及上限，暂停增加仓位；卖出和止损照常。确认后由人工复位（reset-drawdown）。"
    elif kind == "construction_switched" and isinstance(details, dict):
        names = {"ai": "AI 原始权重", "risk_budget": "风险预算（按 AI 权重分配风险贡献）"}
        test = details.get("test") or {}
        mean = test.get("mean")
        text = (f"🔁 组合构建方式切换：{names.get(details.get('from'), details.get('from'))} → "
                f"{names.get(details.get('to'), details.get('to'))}。依据 {test.get('pairs')} 个交易日的配对比较，"
                f"日均超额差 {mean * 100:.3f}%，t = {test.get('t_stat'):.2f}。风控上限不变。" if mean is not None and
                test.get("t_stat") is not None else "🔁 组合构建方式切换：" + str(details))
    if text is None:
        return None
    return text + (f"\n（LumiBot 模拟账户 · 事件 #{sequence} · {kind}）" if sequence is not None else "")
