"""Human-readable messages built only from the selected account's journal."""
import json
from zoneinfo import ZoneInfo

from .execution import operator_connection
from .journal import encode
from .risk import RiskError, instant


TRIGGERS = {
    "single_order_at_least_8_percent": "单笔交易达到净值的 8%",
    "single_order_threshold": "单笔交易达到人工审批阈值",
    "daily_turnover_threshold": "当日累计预留／提交买卖额达到净值的 20%（含撤单）",
    "liquidate_entire_portfolio": "拟清空现有全部证券持仓",
}


def approval_message(details):
    side = {"buy": "买入", "sell": "卖出"}.get(details["side"], details["side"])
    return (f"重大模拟交易待审批：{side} {details['symbol']}\n"
            f"当前持股：{details.get('current_quantity', '见审批原文')}；目标权重：{details.get('target_weight', '见原文')}\n"
            f"最多 {details['max_quantity']} 股；委托限价上界 ${details.get('max_limit', '见原文')}；"
            f"卖出限价下界 ${details.get('min_limit', '旧请求须重新审阅')}\n"
            f"预留名义金额上界 ${details['max_notional']}（卖出实际收入仍按成交价）\n"
            f"参考账户净值：${details.get('observed_equity', '见原文')}\n"
            "审批触发：" + "；".join(TRIGGERS.get(r, r) for r in details["reasons"]) + "\n"
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
