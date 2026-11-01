"""Human-readable messages built only from the selected account's journal."""
import json
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
    card = next((body for at, kind, body in reversed(events) if kind == "scorecard"), None)
    if card:
        predictions, decisions = card["predictions"], card["decisions"]["by_horizon"]
        lines += [f"预测：共 {predictions['total']} 条，已打分 {predictions['scored']}，命中 {predictions['correct']}，"
                  f"命中率 {predictions['hit_rate']}，待验证 {predictions['pending']}",
                  "决策相对同敞口基准的超额收益（1/5/20 个交易日）：" + json.dumps(decisions, ensure_ascii=False),
                  "样本小时结论不稳定；记分差的假设应由 AI 在复盘中撤回。", ""]
    else:
        lines += ["尚无可打分的样本。", ""]
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
