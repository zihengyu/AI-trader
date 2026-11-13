"""Human-readable messages built only from the selected account's journal."""
import json
from collections import Counter
from zoneinfo import ZoneInfo

from .execution import operator_connection
from .journal import encode, event_body
from .risk import RiskError, instant


class FundOutboxProvider:
    """Native notification provider backed by this account's durable outbox."""

    provider = "fund_outbox"

    def __init__(self, strategy):
        self.strategy = strategy

    def notify(self, *, title, message, severity="info", **kwargs):
        from lumibot.components.notifications.base import NotificationResult

        # AI text must never masquerade as an approval request or health event.
        # The monitor owns transport and its already bound recipient; queueing
        # acknowledges durable acceptance, not Telegram delivery.
        queued = self.strategy._notify_fund("agent_notice", {
            "title": title, "message": message, "severity": severity})
        return NotificationResult(ok=True, provider=self.provider, title=title, message=message,
                                  severity=severity, skipped=not queued,
                                  reason=None if queued else "duplicate notice",
                                  payload={"status": "queued" if queued else "deduplicated"})


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


FACTOR_STATUS = {"validated": "历史检验通过（待样本外确认）", "rejected": "未通过", "insufficient_data": "数据不足", "invalid": "表达式无效"}
HEALTH_KINDS = {"blocked", "bookkeeping_error", "watchdog_stop", "watchdog_recovered", "auto_resumed",
                "auto_resume_blocked", "auto_resume_exhausted", "bot_crash", "server_supervision_stop",
                "postmarket_review_error", "premarket_research_error", "model_budget_exhausted",
                "model_cost_budget_exhausted", "decision_drought", "malformed_notification_body",
                "holding_without_market_data"}


REGIME_TEXT = {"up_calm": "上涨·平稳", "up_volatile": "上涨·波动大", "down_calm": "下跌·平稳",
               "down_volatile": "下跌·波动大", "unlabeled": "历史不足"}
CHANGE_TEXT = {"weaker_recently": "最近明显变弱（可能是失效、拥挤或市场变了）", "stronger_recently": "最近明显变强",
               "no_clear_change": "没有明显变化"}


def _regime_note(group):
    uncertainty = group.get("uncertainty")
    if not uncertainty:  # Older scorecards had no intervals.
        return ""
    count = uncertainty["non_overlapping"]
    interval = uncertainty["interval_95"]
    if interval is None:
        return f"（不可靠，非重叠样本 {count}）"
    return (f"（非重叠样本 {count}，均值 {_percent(uncertainty['sample_mean'])}，"
            f"95% 区间 {_percent(interval[0])} 至 {_percent(interval[1])}）")


def fund_scoreboard_line(fund):
    """One line: since inception, net of every cost, against each benchmark (scoreboard.py); None when not computed."""
    if not fund or fund.get("status") != "computed":
        return None

    def label(name, weights):
        if name == "spy":
            return "SPY"
        parts = " + ".join(f"{float(w) * 100:.0f}% {s}" for s, w in weights.items())
        return f"{'风险匹配基准' if name == 'risk_matched' else name}（{parts}）"
    costs = fund["model_costs_usd"] + fund.get("known_data_costs_usd", fund.get("data_costs_usd") or 0)
    text = (f"扣除全部成本后的基金收益（{fund['since']} 至 {fund['through']}，{fund['sessions']} 个交易日）："
            f"账户收益 {_percent(fund['account_return'])}（已扣交易费用和滑点，含现金利息），"
            f"模型和数据费用 ${costs:.2f}（占起始资金 {_percent(fund.get('costs_share_of_starting_equity'))}），"
            f"净收益 {_percent(fund['net_return'])}")
    if fund.get("unpriced_calls"):
        text += f"（另有 {fund['unpriced_calls']} 次调用未定价、未计入，实际更低）"
    if fund.get("data_costs_usd") is None:
        text += "（数据费用未知、未计入）"
    marks = [f"{label(name, row['weights'])} {_percent(row['return'])}（净超额 {_percent(row['net_excess'])}）"
             for name, row in (fund.get("benchmarks") or {}).items()]
    if marks:
        text += "；同期 " + "、".join(marks)
    pace = (fund.get("cost_pace") or {}).get("share_of_equity_per_year")
    if pace is not None:
        text += (f"。按最近 {fund['cost_pace']['sessions']} 个交易日的花费，系统一年的费用约占资金 {_percent(pace)}，"
                 "要跑赢基准至少得多赚这么多")
    cash = fund.get("cash") or {}
    if cash.get("annual_net_of_cost_pace") is not None:
        text += (f"；全拿现金（{cash['symbol']} 年化约 {_percent(cash['annual_yield'])}）扣费后年化约 "
                 f"{_percent(cash['annual_net_of_cost_pace'])}")
    return text + "。"


def evidence_lines(card):
    from .stability import INPUT_SCOPE

    lines = []
    fund = fund_scoreboard_line((card or {}).get("fund"))
    if fund:
        lines.append(fund)
    stability = (card or {}).get("stability")
    if stability and stability.get("input_scope") == INPUT_SCOPE and stability.get("checks"):
        lines.append(f"决策稳定性（复用任务和策略上下文，运行状态实时更新）：{stability['checks']} 次，两次目标组合平均有 "
                     f"{_percent(stability['mean_weight_distance'])} 的仓位不同，完全一致 {_percent(stability['same_rate'])}；"
                     f"近期相邻两次真实决策平均调整 {_percent(stability['mean_consecutive_distance'])}"
                     + (f"，复问差异相当于真实调整的 {_percent(stability['noise_ratio'])}"
                        if stability.get("noise_ratio") is not None else "") + "。")
    down = (card or {}).get("down_days") or {}
    if (down.get("down_days") or {}).get("days"):
        days, up, exits = down["down_days"], down.get("up_days") or {}, down.get("protective_exits_on_down_days") or {}
        threshold = _percent(abs(down["down_threshold"]))
        lines.append(
            f"大跌日表现（基准单日跌 {threshold} 以上）：{days['days']} 天"
            + ("（不足 3 天，仅供参考）" if down.get("status") != "observed" else "")
            + f"，基准平均 {_percent(days['mean_benchmark_return'])}、基金平均 {_percent(days['mean_fund_return'])}，"
            f"下跌捕获 {_percent(days['capture'])}（100% ＝ 跟市场跌得一样多）"
            + (f"；大涨日 {up['days']} 天上涨捕获 {_percent(up['capture'])}" if up.get("days") else "")
            + (f"；大跌日开盘时有效贝塔比平常低 {down['beta_lower_on_down_days_by']:.2f}"
               if down.get("beta_lower_on_down_days_by") is not None else "")
            + f"；当天选股贡献 {_percent(days['selection'])}、择时 {_percent(days['timing'])}"
            + (f"（{days['timing_unknown_days']} 天择时未知，未计入）" if days.get("timing_unknown_days") else "")
            + (f"；止损/止盈触发 {exits['count']} 次，{exits.get('valued', 0)} 次可估值"
               + (f"，按触发时买一价估算的次日平均价值 {_percent(exits['mean_value_added_1d'])}"
                  if exits.get("mean_value_added_1d") is not None else "")
               if exits.get("count") else "") + "。")
    claims = (card or {}).get("claims")
    if claims and claims.get("decisions"):
        wrong = [f"{metric} {group['incorrect']}/{group['correct'] + group['incorrect']}"
                 for metric, group in (claims.get("by_metric") or {}).items() if group["incorrect"]]
        lines.append(f"AI 事实核验（对照它看到的行情快照）：{claims['decisions_with_claims']}/{claims['decisions']} 次决策提交断言，"
                     f"正确 {claims['correct']}，错误 {claims['incorrect']}，"
                     f"无法核验 {claims['unavailable']}，已核验断言错误率 {_percent(claims['incorrect_rate'])}。"
                     + (f"错得多的指标（错/核验）：{'、'.join(wrong[:5])}。" if wrong else ""))
    for factor in (card or {}).get("factor_crowding", [])[:3]:
        if factor.get("status") != "observed":
            lines.append(f"因子 {factor['name']} 拥挤度：分组样本不足。")
            continue
        favored, disfavored = factor["favored"], factor["disfavored"]
        lines.append(f"因子 {factor['name']} 拥挤度（{factor['as_of_close']}）："
                     f"偏好减回避组的账面市值比差 {_percent(factor['book_to_price_spread'])}，"
                     f"估值覆盖 {favored['valuation_names']}／{disfavored['valuation_names']}，"
                     f"组内平均相关性 {_percent(favored['mean_pairwise_return_correlation'])}／"
                     f"{_percent(disfavored['mean_pairwise_return_correlation'])}"
                     f"（{favored['correlation_pairs']}／{disfavored['correlation_pairs']} 对），"
                     f"FINRA 场外卖空成交占比（{factor['finra_session'] or '无日期'}）"
                     f"{_percent(favored['mean_finra_short_volume_ratio'])}／"
                     f"{_percent(disfavored['mean_finra_short_volume_ratio'])}"
                     f"（覆盖 {favored['finra_names']}／{disfavored['finra_names']}）。")
    return lines


def regime_trend_lines(card):
    """Scorecard results by market regime and recent-vs-earlier change: background, never a criterion."""
    lines = []
    decisions, ideas = (card or {}).get("decisions") or {}, (card or {}).get("ideas") or {}
    groups = [f"{REGIME_TEXT.get(label, label)} {value['5']['observed']} 次、平均 {_percent(value['5']['mean_net_excess_return'])}"
              + _regime_note(value["5"])
              for label, value in (decisions.get("by_regime") or {}).items()
              if value["5"]["observed"] or value["5"].get("uncertainty")]
    hits = [f"{REGIME_TEXT.get(label, label)} {value['scored']} 条、命中率 {_percent(value['hit_rate'])}"
            + _regime_note(value)
            for label, value in (ideas.get("by_regime") or {}).items() if value["scored"]]
    if groups or hits:
        lines.append("按市场状态分组（SPY 相对 200 日均线、20 日波动相对过去一年中位数；只作背景，不作判断标准）："
                     + ("决策 5 日净超额：" + "；".join(groups) if groups else "")
                     + ("。" if groups and hits else "") + ("研究想法：" + "；".join(hits) if hits else ""))
    for name, trend in (("决策 5 日净超额", decisions.get("trend_5d_net_excess")),
                        ("研究想法命中率", ideas.get("trend"))):
        if (trend or {}).get("status") == "observed":
            detail = (f"t={trend['t_stat']:.2f}，{CHANGE_TEXT.get(trend['change'], trend['change'])}"
                      if trend.get("t_stat") is not None else "组内无波动，无法计算 t")
            lines.append(f"{name}：最近 {trend['recent']['n']} 条平均 {_percent(trend['recent']['mean'])}，"
                         f"之前 {trend['earlier']['n']} 条 {_percent(trend['earlier']['mean'])}（{detail}）")
    return lines


ATTRIBUTION_LABELS = {"market": "市场（beta×基准）", "selection": "选股", "cash_equivalent": "现金等价物",
                      "timing": "当日交易择时（相对收盘）", "slippage": "执行滑点（相对到达价）", "fees": "费用"}


def attribution_lines(result):
    """The daily attribution as readable lines; missing parts are named, never filled in."""
    if not result:
        return ["无数据（这次复盘没有归因）。"]
    if result.get("status") == "unavailable":
        return [f"不可用：{result.get('reason')}"]
    lines = [f"- 观测收益 {_percent(result['observed_return'])}（{result['previous_session']} → {result['session_date']}，"
             f"基准 {result['benchmark']} {_percent(result.get('benchmark_return'))}）"]
    lines += [f"- {ATTRIBUTION_LABELS[key]}：{_percent(value)}" for key, value in result["components"].items()]
    lines.append(f"- 未解释（收盘前估值、分红、公司行为等，不强行配平）：{_percent(result['unexplained'])}")
    costs = result.get("fund_costs")
    if costs and costs.get("share") is not None:
        from .scoreboard import known_cost_total
        text = (f"- AI 和数据费用（账户本身不付，基金承担）：{_percent(costs['share'])}"
                f"（${known_cost_total(costs):.2f}）；扣费后当日收益 {_percent(result.get('net_return'))}")
        if costs.get("unpriced_calls"):
            text += f"（另有 {costs['unpriced_calls']} 次调用未定价、未计入，实际更低）"
        if costs.get("data_usd") is None:
            text += "（另有未知数据费用未计入，净收益仅为上限）"
        lines.append(text)
    groups = result.get("selection_by_group") or {}
    if groups:
        lines.append("- 选股按组：" + "；".join(f"{name} {_percent(value)}" for name, value in groups.items()))
    for key, label in (("unavailable", "缺数据"), ("fees_unknown", "费用未知"), ("arrival_unknown", "缺到达报价（择时／滑点未知）"),
                       ("beta_assumed", "beta 按 1 计")):
        if result.get(key):
            lines.append(f"- {label}：" + "；".join(result[key]))
    return lines


DIRECTIONS = {"up": "看涨", "down": "看跌", "outperform": "跑赢基准", "underperform": "跑输基准"}
VERDICTS = {"informative": "有预测信息", "not_informative": "未优于朴素基准", "insufficient_sample": "样本不足"}


def idea_text(ideas):
    """One line on the research ideas' scored record, or None without any."""
    if not ideas or not ideas.get("total"):
        return None
    coin, up = ideas["baselines"]["coin"], ideas["baselines"]["always_up"]
    brier = "暂无" if ideas.get("brier") is None else f"{ideas['brier']:.3f}"
    return (f"研究想法：共 {ideas['total']} 条，已打分 {ideas['scored']}，命中率 {_percent(ideas.get('hit_rate'))}"
            f"（总是看涨 {_percent(up.get('hit_rate'))}，抛硬币 50%），Brier {brier}（抛硬币 0.25），"
            f"剔除大盘后的选股命中率 {_percent(ideas['selection'].get('hit_rate'))}；"
            f"结论：{VERDICTS.get(ideas.get('verdict'), ideas.get('verdict'))}（至少 {ideas.get('minimum_sample')} 条才下结论）；"
            f"待验证 {ideas['pending']}")


def _decimal(value, places):
    """A stored decimal string for reading (e.g. 10003.123456789 -> 10,003.12); unreadable values unchanged."""
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return value


def _usd(value):
    return _decimal(value, 2)


def _percent(value):
    return "暂无" if value is None else f"{value * 100:.2f}%"


TRADE_KINDS = ("order_submitted", "fill", "fill_reconciled", "fill_details_unavailable", "operator_approval")
WATCH_KINDS = ("protective_trigger", "price_alert", "review_triggered", "entry_waiting", "buy_chase_limit")
DAILY_KINDS = TRADE_KINDS + WATCH_KINDS + ("notification",)


def daily_document(root, sequence):
    # The AI supplies neither a file path nor an upload URL. Render one exact
    # journal row in memory, so a prompt cannot turn reports into file exfiltration.
    with operator_connection(root) as db:
        row = db.execute("SELECT body FROM events WHERE sequence=? AND kind='postmarket_review'", (sequence,)).fetchone()
        if not row:
            raise RiskError("Daily report has no matching completed review")
        packet = json.loads(row[0])
        cutoff, day = instant(packet["completed_at"]), packet["session_date"]
        # Only the kinds this document lists; the journal also holds a NAV mark
        # and a benchmark about every 30 seconds of market time.
        events = [(at, kind, event_body(kind, body)) for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(DAILY_KINDS)),
            DAILY_KINDS) if instant(at) <= cutoff]
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
                  if instant(at).astimezone(zone).date().isoformat() == day and kind in TRADE_KINDS]
    watch_events = [{"at": at, "kind": kind, "body": body} for at, kind, body in events
                    if instant(at).astimezone(zone).date().isoformat() == day and kind in WATCH_KINDS]
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
             "## 当日归因（代码按收盘价计算）", ""] + attribution_lines(packet.get("attribution")) + [
             "", "## AI 记分卡（只用已完成收盘价客观打分）", ""]
    # Only this session's card: an earlier one under today's heading would be stale.
    card = next((body for kind, body in reversed(related) if kind == "scorecard"), None)
    if card:
        predictions, decisions = card["predictions"], card["decisions"]["by_horizon"]
        lines += [f"预测：共 {predictions['total']} 条，已打分 {predictions['scored']}，命中 {predictions['correct']}，"
                  f"命中率 {predictions['hit_rate']}，待验证 {predictions['pending']}",
                  "决策超额收益（1/5/20 个交易日；已扣除组合 beta 对应的市场涨跌，net 再扣交易成本；"
                  "within_sector_selection＝行业内选股，sector_allocation＝押行业带来的部分）："
                  + json.dumps(decisions, ensure_ascii=False)]
        line = idea_text(card.get("ideas"))
        if line:
            lines.append(line)
        timing = card.get("timing")
        if timing:
            stop, profit = timing["protective_exits"]["stop_loss"], timing["protective_exits"]["take_profit"]
            waits = timing["entry_waits"]
            lines += [f"择时：止损 {stop['count']} 次，5 日后平均贡献 {_percent(stop['5']['mean_value_added'])}"
                      f"（正＝避免了继续下跌）；止盈 {profit['count']} 次，5 日后平均贡献 "
                      f"{_percent(profit['5']['mean_value_added'])}（负＝卖早了）；等待入场 {waits['count']} 次，"
                      f"买入 {waits['entered']} 次，平均价格改善 {_percent(waits['mean_entry_improvement'])}，"
                      f"未买入的 5 日后平均贡献 {_percent(waits['not_entered']['5']['mean_value_added'])}（负＝错过了上涨）"]
        consistency = card.get("consistency")
        if consistency and consistency.get("moves"):
            lines.append(f"决策反复：最近 {consistency['decisions']} 次决策调整目标 {consistency['moves']} 次，"
                         f"{consistency['reversals']} 次在 {consistency['window_sessions']} 个交易日内反向调整"
                         f"（{_percent(consistency['reversal_rate'])}，不含止损止盈），"
                         f"估算多花成本约占净值 {_percent(consistency['estimated_cost'])}")
        lines += regime_trend_lines(card) + evidence_lines(card)
        lines += ["样本小时结论不稳定；记分差的假设应由 AI 在复盘中撤回。", ""]
    else:
        lines += ["本交易日没有记分卡（尚无可打分的样本，或当日数据不可用）。", ""]
    lines += ["## 因子挖掘（AI 提出，代码客观检验）", ""]
    results = [result for kind, body in related if kind == "factor_tests" for result in body.get("results", [])]
    for result in results:
        sample, holdout = result.get("in_sample") or {}, result.get("holdout") or {}
        within = ((result.get("sector_neutral") or {}).get("in_sample") or {}).get("t")
        lines.append(f"- {result.get('name')}：`{result.get('expression')}`（{result.get('horizon_sessions', '?')} 日）→ "
                     f"{FACTOR_STATUS.get(result.get('status'), result.get('status'))}；样本内 t={sample.get('t')}，"
                     f"样本外 t={holdout.get('t')}"
                     + (f"，行业内 t={within}（接近 0 说明主要是押行业，不是行业内选股）" if result.get("sector_neutral") else "")
                     + f"；{result.get('reason', '')}")
    if results:
        from .universe import survivorship_text
        lines.append(f"- 注意：{survivorship_text(root)}")
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


def readable(kind, details, sequence=None, *, now=None):
    """Chinese text for the notices an operator reads most; None keeps the generic format."""
    from .settings import MODES
    from .status import REASONS
    details = details if details is not None else {}
    text = None
    if kind == "backup_skipped":
        text = ("⚠️ 今天的账户备份已跳过：磁盘空间不足（为避免磁盘写满触发交易停止）。\n"
                f"{details.get('reason', '')}\n请清理磁盘或把旧备份移到别处；交易未受影响。")
    elif kind == "agent_notice":
        text = (f"AI 通知（供参考） · {details.get('severity', 'info')}\n"
                f"{details.get('title', '')}\n{details.get('message', '')}")
    elif kind == "premarket_research":
        phase = {"before_market_opens": "开盘前", "closed_startup": "休市期间"}.get(details.get("phase"), "盘前")
        lines = [f"📊 盘前研究 · {phase}（开盘 {_when(details.get('session_open'))}）", "", details.get("summary") or ""]
        evidence = details.get("evidence") or []
        if evidence:
            lines += ["", f"证据 {details.get('evidence_count', len(evidence))} 条："]
            lines += [f"• {e.get('source')}（{_when(e.get('available_at') or e.get('observed_at'))}）" for e in evidence]
        ideas = details.get("ideas")
        if ideas:
            lines += ["", f"投资想法 {len(ideas)} 条（已登记为预测，之后按收盘价打分）："]
            lines += [f"• {i.get('symbol')} {DIRECTIONS.get(i.get('direction'), i.get('direction'))}，"
                      f"{i.get('horizon_sessions')} 个交易日，把握 {_percent(i.get('confidence'))}：{(i.get('thesis') or '')[:120]}"
                      for i in ideas]
        elif "ideas" in details:
            lines += ["", "投资想法：今天没有足够把握的机会（允许为空）。"]
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
            from .status import account_lines
            lines += ["", *account_lines(account, details.get("recent_trades"), now=now,
                                         costs=details.get("fund_costs")), ""]
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
        if details.get("level_changes") and details.get("weight_changed") is False:
            header = "🧭 AI 更新交易价位"
        lines = [f"{header}（{_when(details.get('at'))}）", "这是目标计划，实际买卖以成交更新为准。"]
        for label, rows in (("买入／加仓", [r for r in changes if float(r["to"]) > float(r["from"])]),
                            ("卖出／减仓", [r for r in changes if float(r["to"]) < float(r["from"])]),
                            ("不变", [r for r in changes if float(r["to"]) == float(r["from"])])):
            for row in rows:
                if label == "不变":
                    lines.append(f"不变：{row['symbol']} {percent(row['to'])}{levels(row)}")
                else:
                    lines.append(f"计划{label}：{row['symbol']} {percent(row['from'])}→{percent(row['to'])}{levels(row)}")
        for change in details.get("level_changes") or []:
            before, after = change["before"], change["after"]
            parts = []
            for key, label in (("entry_limit", "入场上限"), ("stop_loss", "止损"),
                               ("take_profit", "止盈"), ("take_profit_weight", "止盈后保留权重")):
                if before.get(key) == after.get(key):
                    continue
                show = percent if key == "take_profit_weight" else str
                left = show(before[key]) if key in before else "未设置"
                right = show(after[key]) if key in after else "已撤销"
                parts.append(f"{label} {left}→{right}")
            lines.append(f"价位调整：{change['symbol']}（{'，'.join(parts)}）")
        lines.append(f"目标现金约 {percent(details.get('cash_weight') or 0)}")
        from .status import account_lines
        lines += ["", *account_lines(details.get("account"), details.get("recent_trades"), now=now,
                                     costs=details.get("fund_costs"))]
        if details.get("rationale"):
            lines += ["", f"理由：{details['rationale']}"]
        if details.get("next_review_at"):
            lines.append(f"下次复核：{_when(details['next_review_at'])}")
        lines.append("权重是上限：风控可能按波动、尾部风险等再缩小；买入在自动额度内直接执行，超出额度会发审批。")
        text = "\n".join(lines)
    elif kind == "execution_update" and isinstance(details, dict):
        from .status import account_lines
        title = "✅ 成交已确认，账户快照待更新" if details.get("account_pending") else "✅ 成交后账户更新"
        text = "\n".join([f"{title}（{_when(details.get('at'))}）", "",
                           *account_lines(details.get("account"), details.get("recent_trades"), now=now,
                                          costs=details.get("fund_costs"))])
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
    elif kind == "local_market_data_pending":
        text = f"⏳ 行情暂不可用，成交等待新行情，系统会自动重试：{details}"
    elif kind == "opening_valuation_pending" and isinstance(details, dict):
        text = (f"⏳ 开盘估值等待 {details['symbol']} 的本交易日有效报价。"
                f"首根一分钟线最早于 {_when(details['first_bar_at'])} 收完。"
                "新鲜成交价或已完成分钟线到达后，系统会继续检查。")
    elif kind == "price_alert_unavailable" and isinstance(details, dict):
        reason = {"Stale, future or nonpositive observation": "报价过期、时间异常或价格无效",
                  "Missing source quote timestamp": "报价缺少源时间戳"}.get(details.get("error"), details.get("error"))
        text = (f"⏳ {details.get('symbol', '')} 的价位提醒等待有效报价：{reason}。"
                "本次跳过该标的提醒检查，后续自动重试。")
    elif kind == "closing_valuation_pending":
        text = (f"⏳ 收盘估值等待有效行情：{details}\n后台会继续重试；盘后复盘可使用已有观测，暂不认定收盘净值。"
                "\n诊断：bash scripts/fund-logs.sh errors")
    elif kind == "report_error":
        text = f"⚠️ 收盘账户核对或估值失败：{details}\n具体位置：bash scripts/fund-logs.sh errors"
    elif kind == "bookkeeping_error":
        text = f"⚠️ 记账步骤出错，下一轮自动重试：{details}"
    elif kind == "postmarket_review_error":
        text = (f"⚠️ 盘后复盘失败：{details}\n这次的盘后日报暂不生成；原有策略备忘继续有效，交易不受影响。"
                "下一个开盘前约 1 小时自动重试复盘。本周最后一个交易日仍没有复盘时，周报在下周第一个交易日开盘后"
                "用本周最近一次复盘发出。\n原因：bash scripts/fund-logs.sh errors")
    elif kind == "premarket_research_error":
        text = (f"⚠️ 盘前研究失败：{details}\n开盘后基金经理照常复核和决策，只是缺少这次的盘前研究摘要。\n"
                "原因：bash scripts/fund-logs.sh errors")
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
    elif kind == "fund_cost_alert" and isinstance(details, dict):
        spent = (details.get("model_usd") or 0) + (details.get("data_usd") or 0)
        text = (f"💸 AI 和数据费用累计已占起始资金 {_percent(details.get('share'))}（${spent:.2f}，"
                f"{details.get('since')} 以来）：账户收益 {_percent(details.get('account_return'))}，"
                f"扣除这些费用后的净收益 {_percent(details.get('net_return'))}")
        if details.get("unpriced_calls"):
            text += f"（另有 {details['unpriced_calls']} 次调用未定价、未计入，实际更低）"
        if details.get("pace") is not None:
            text += f"；按最近的花费节奏，一年约占资金 {_percent(details['pace'])}"
        text += (f"。\n这只是提醒，交易照常。要限制花费，可在 .env 设置 FUND_MAX_DAILY_COST_USD（每个交易日上限）；"
                 f"费用每再增加起始资金的 {_percent(details.get('threshold'))} 会再提醒一次。")
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
        if str(details.get("reason") or "").startswith("degradation"):
            text += "\n这是退化回滚：最近 20 个交易日的配对比较转为不利于原方式，换回另一种方式。"
    elif kind == "learning_champion_switched" and isinstance(details, dict):
        names = {"memo": "看复盘备忘的基金经理", "baseline": "不看备忘的对照组"}
        rollback = str(details.get("reason") or "").startswith("degradation")
        text = (f"🔁 学习对照切换：{names.get(details.get('from'), details.get('from'))} → "
                f"{names.get(details.get('to'), details.get('to'))} 开始交易。"
                + ("这是退化回滚：最近 20 个交易日的配对比较转为不利于原来交易的一组。" if rollback else
                   f"依据全部 {details.get('pairs')} 个交易日的配对比较，t = {details.get('t_stat')}。")
                + "风控上限不变；另一组继续在影子里打分，证据反转时会再切回。")
    elif kind == "degradation_detected" and isinstance(details, dict):
        text = (f"⚠️ 换手偏高：最近 {details.get('recent_sessions')} 个交易日的日均换手 {_percent(details.get('recent_mean'))}，"
                f"高于之前 {details.get('earlier_sessions')} 个交易日的 95% 分位 {_percent(details.get('p95'))}。"
                f"基金经理的最短复核间隔加倍为 {details.get('min_review_minutes')} 分钟，少做调整；止损、止盈照常。"
                "换手回到之前的中位数后自动恢复。")
    elif kind == "unaffordable_symbols" and isinstance(details, dict):
        rows = details.get("unaffordable") or []
        if rows:
            text = (f"ℹ️ 按当前账户净值 {details.get('equity'):,.2f} 美元，"
                    "下面这些证券一股的买入限价（含价格缓冲及美分取整）就超过了它最多能占的仓位。"
                    "系统只买整股，所以买不进：\n"
                    + "\n".join(f"- {r['symbol']}：一股约 {r['share_price']:,.2f} 美元"
                                 + ("（实时报价）" if r.get("basis") == "live_quote" else "")
                                 + f"，占净值 {r['one_share_weight'] * 100:.1f}%，上限 {r['max_weight'] * 100:.1f}%"
                                 + ("（已持有，可以继续持有或卖出）" if r.get("held") else "")
                                 for r in rows[:20])
                    + "\nAI 已被告知不要计划买入它们，研究照常。要交易它们，需要提高授权资金或单只／分组上限。")
        else:
            text = "ℹ️ 投资范围里的证券现在都至少买得起一股了。"
    elif kind == "holding_without_market_data" and isinstance(details, dict):
        symbol, last = details.get("symbol"), details.get("last_bar")
        gap = (f"已连续 {details.get('sessions_without_bar')} 个交易日没有新的日线（最后一根是 {last}）" if last else
               "数据源已经不再提供它的日线")
        text = (f"⚠️ 持仓 {symbol} {gap}，可能已被收购、退市或长期停牌。没有价格就无法给账户估值，"
                "所以账户估值和新的交易会暂停，直到它被处理。")
        if details.get("mode") == "local":
            text += ("\n请先查明实际结算情况（例如现金收购价；破产退市一般为 0），然后停止基金，运行：\n"
                     f"bash scripts/run_autonomous_fund.sh settle-position --run-dir <账户目录> --symbol {symbol} "
                     "--cash-per-share <每股实收现金> --reason \"<发生了什么>\"\n"
                     "账本会把这笔持仓按该金额转为现金，并用严格对账复核；之后重新启动即可。")
        else:
            text += "\n模拟券商会自行处理收购或退市的结算；请在券商账户里确认，之后账户读数会自动恢复。"
    elif kind == "bar_quality_issues" and isinstance(details, dict):
        parts = []
        if details.get("withheld"):
            parts.append("两个数据源的日线或公司行动明显不一致，今天不使用这些证券的历史数据（不新增买入、不进入因子检验）："
                         + "、".join(details["withheld"]))
        if details.get("disputed"):
            parts.append("个别交易日两边收盘价相差超过 2%，这些日子的数据已剔除（不替换、不插值）："
                         + "、".join(f"{s} {n} 天" for s, n in details["disputed"].items()))
        if details.get("missing_latest"):
            parts.append("Yahoo 缺少 Alpaca 已提供的最新交易日日线，未补写行情："
                         + "、".join(f"{s} {day}" for s, day in details["missing_latest"].items()))
        for item in details.get("examples") or []:
            parts.append(f"例：{item['symbol']} {item['date']} Yahoo {item['primary']:.2f}，"
                         f"Alpaca {item['reference']:.2f}（差 {item['difference'] * 100:+.1f}%）")
        if details.get("unavailable"):
            parts.append(f"第二数据源（Alpaca）暂时连不上（{details['unavailable']}），部分日线未经核对就使用了。")
        actions = details.get("corporate_actions") or {}
        for item in actions.get("examples") or []:
            kind = {"split": "拆股比例", "dividend": "每股现金分红（除权日股数口径）"}[item["kind"]]
            parts.append(f"{item['symbol']} {item['date']} {kind}："
                         f"Yahoo {item.get('yahoo_ex_date_basis', item['yahoo'])}，Alpaca {item['alpaca']}。")
        unconfirmed = actions.get("unconfirmed") or {}
        unconfirmed_examples = actions.get("unconfirmed_examples") or []
        if unconfirmed_examples:
            parts.append(f"{sum(unconfirmed.values())} 条公司行动待核对。")
            for item in unconfirmed_examples:
                kind = {"split": "拆股", "dividend": "现金分红"}.get(item["kind"], item["kind"])
                if item.get("reason") == "one_source_only":
                    missing = "Yahoo" if item.get("yahoo") is None else "Alpaca"
                    reason = f"{missing} 缺少同日记录"
                elif item.get("reason") == "currency_not_comparable":
                    reason = "Alpaca 有同日记录，币种口径待核对"
                else:
                    reason = "每股计价口径待确认"
                parts.append(f"{item['symbol']} {item['date']} {kind} · {reason}")
            remaining = sum(unconfirmed.values()) - len(unconfirmed_examples)
            if remaining > 0:
                parts.append(f"另有 {remaining} 条待核对。")
        elif unconfirmed:
            parts.append("公司行动只有单一来源或计价口径未确认："
                         + "、".join(f"{s} {n} 项" for s, n in sorted(unconfirmed.items())))
        if actions.get("unavailable"):
            parts.append(f"{actions['unavailable']} 个证券的公司行动核对暂不可用。")
        text = "ℹ️ 日线数据交叉核对（Yahoo 对照 Alpaca）：\n" + "\n".join(f"- {p}" for p in parts)
        text += "\n数据台会重新核对。持续出现时，按标的和日期查发行方公告或数据源记录。"
    elif kind == "market_out_of_history" and isinstance(details, dict):
        labels = {"benchmark_vol_20d": "基准近 20 日波动率（年化）", "benchmark_return_1d": "基准单日涨跌",
                  "benchmark_drawdown": "基准从高点回撤", "stock_mean_correlation_20d": "股票之间近 20 日平均相关性"}
        beyond_history = set(details.get("beyond_history") or [])
        parts = [f"{labels.get(name, name)} "
                 + (f"{value:.2f}" if name == "stock_mean_correlation_20d" else _percent(value))
                 + ("（超出全部历史）" if name in beyond_history else "（超出风险模型窗口）")
                 for name, value in (details.get("values") or {}).items()]
        text = (f"⚠️ 市场超出历史范围（{details.get('as_of_close')} 收盘）：" + "；".join(parts) + "。\n"
                f"风险模型的预期损失和相关性是用最近 {details.get('risk_window_sessions')} 个交易日估出来的，"
                f"基金手上的历史共 {details.get('history_sessions')} 个交易日。"
                "遇到历史少见的行情时，风险估计可能失准（历史压力情景仍照常计入）。\n"
                "这只是提醒：风险上限和订单都不会因此自动改变，基金经理在决策时也能看到这条信息。"
                "需要更保守时，可以手动收紧风险设置或暂停（STOP）。")
    elif kind == "model_identity_changed" and isinstance(details, dict):
        roles = {"researcher": "研究员", "manager": "基金经理", "critic": "反方评审", "triage": "事件判断"}
        used = "、".join(roles.get(r, r) for r in details.get("roles") or []) or "未在用的角色"
        what = {"served_version": f"提供方返回的模型版本从 {', '.join(details.get('before') or []) or '无'} "
                                  f"变成了 {details.get('after')}",
                "knowledge_later": f"每日固定问题显示它的知识截止从 {details.get('before')} 推后到 {details.get('after')}"
                                   "（连续 3 天一致）",
                "self_reported_name": f"每日固定问题里它自称的型号从 {details.get('before')} 变成了 {details.get('after')}"
                                      "（连续 3 天一致，自报不一定准）"}.get(details.get("kind"), details.get("kind"))
        text = (f"⚠️ 模型可能换了：{details.get('model')}（用于{used}）{what}。\n"
                f"从现在起记分卡、实验登记和事件判断的校准按新版本（第 {details.get('epoch')} 版）单独统计，"
                "不再和之前的记录混在一起。交易照常进行，不需要操作；"
                "如果在因子实验室配置了该模型的知识截止日期，请核对是否需要更新。")
    elif kind == "model_cutoff_tightened" and isinstance(details, dict):
        text = (f"ℹ️ {details.get('model')} 在每日固定问题里显示的知识截止（{details.get('observed')}）晚于配置的 "
                f"{details.get('configured')}。因子实验室已自动改用较晚的日期，只把这之后的日子算作干净的样本外记录"
                "（只会更保守，不会放宽）。核实后可以更新 factor_lab.model_knowledge_cutoff。")
    elif kind == "market_back_within_history" and isinstance(details, dict):
        text = (f"ℹ️ 市场回到历史范围内（{details.get('as_of_close')} 收盘）：波动、单日涨跌、回撤和相关性"
                "都不再超出风险模型窗口。不需要操作。")
    elif kind == "triage_enforced" and isinstance(details, dict):
        text = (f"✅ 盘中事件判断开始生效：最近 {details.get('dismissals_scored')} 次快速模型判断“不需要叫基金经理”时，"
                f"基金经理实际改了组合的只有 {details.get('misses')} 次（漏判率 95% 上限 "
                f"{_percent(details.get('miss_rate_upper_95'))}，要求不超过 {_percent(details.get('max_miss_rate'))}）。\n"
                "从现在起，普通新闻和价格异动先由快速模型判断，确实重要才叫基金经理；市场压力、价格提醒、止损止盈后仍直接叫，"
                f"基金经理仍至少每 2 小时复核一次。每 {details.get('audit_every')} 次“不需要”仍抽查一次，漏判变多会自动退回。")
    elif kind == "triage_rolled_back" and isinstance(details, dict):
        text = (f"⚠️ 盘中事件判断退回影子观察：最近 {details.get('dismissals_scored')} 次检查里，快速模型说“不需要”但基金经理"
                f"实际改了组合 {details.get('misses')} 次（{_percent(details.get('miss_rate'))}，超过 "
                f"{_percent(details.get('max_miss_rate'))}）。\n现在恢复为所有触发都叫基金经理，快速模型只在旁边打分；"
                "漏判率重新达标后会再自动生效。")
    elif kind == "degradation_cleared" and isinstance(details, dict):
        text = (f"✅ 换手已回到正常：最近日均 {_percent(details.get('recent_mean'))}，不高于之前的中位数 "
                f"{_percent(details.get('median'))}。最短复核间隔恢复为 {details.get('min_review_minutes')} 分钟。")
    elif kind == "capital_flow" and isinstance(details, dict):
        subscription = details.get("kind") == "subscription"
        text = (f"{'💰 申购' if subscription else '🏦 赎回'}已确认：{_usd(details.get('amount'))} 美元，单位净值 "
                f"{_decimal(details.get('nav_per_unit'), 4)}；基金 {_usd(details.get('equity_before'))} → "
                f"{_usd(details.get('equity_after'))} 美元，份额 {_decimal(details.get('units_before'), 2)} → "
                f"{_decimal(details.get('units_after'), 2)}。"
                + ("（来自 .env 里 FUND_CAPITAL_USD 的变化）" if details.get("source") == "configuration" else "")
                + "\n这笔钱不算收益也不算亏损：收益率、回撤和记分卡都按单位净值计算，回撤锁和当日亏损暂停不受影响。")
        if subscription:
            text += "基金经理将按新的规模复核。"
        elif details.get("mode") == "local":
            text += "本地虚拟账户已扣除这笔现金。"
        elif details.get("mode") == "paper":
            text += (f"模拟盘的钱不会离开券商账户：已转入保留现金（现在 {_usd(details.get('reserved_cash_after'))} 美元），"
                     "AI 不会再动用。")
        else:
            text += (f"这笔钱已转入保留现金（现在 {_usd(details.get('reserved_cash_after'))} 美元），AI 不会再动用。"
                     "要从券商转出：先停止基金（bash scripts/fund-stop.sh），转账，再登记 "
                     f"bash scripts/fund-capital.sh withdraw {details.get('amount')}，然后重新启动。"
                     "卖出所得可能要等结算后才能转出。")
    elif kind == "redemption_liquidating" and isinstance(details, dict):
        text = (f"🏦 赎回 {_usd(details.get('amount'))} 美元正在筹资。"
                f"可用现金 {_usd(details.get('cash'))} 美元，现金替代 ETF {_usd(details.get('cash_equivalent'))} 美元。"
                "先按单只证券和总敞口上限调整持仓，再按需卖出现金替代 ETF。\n"
                "处理期间暂停买入和现金停泊；卖出、止损和基金经理的决策照常。")
    elif kind == "redemption_waiting" and isinstance(details, dict):
        why = {"redemption_sale_budget": "今天的卖出次数已经用完，下一个交易日继续卖出",
               "redemption_quote_unavailable": f"暂时拿不到可用的报价（{details.get('error')}），会自动重试",
               "redemption_waits_for_cash": "可用现金和符合条件的整股不足，应收款未计入可用现金"
               }.get(details.get("reason"), details.get("reason"))
        text = (f"⏳ 赎回还在进行：{why}。期间暂停买入，卖出和止损照常；不需要操作，"
                "要放弃这笔赎回可以 bash scripts/fund-capital.sh cancel <编号>（查看编号：bash scripts/fund-capital.sh）。")
    elif kind == "subscription_waiting" and isinstance(details, dict):
        text = (f"⏳ 申购 {_usd(details.get('amount'))} 美元正在等待保留现金（编号 {details.get('request_id')}）。"
                "如果已单独调整模拟盘余额，停止基金后用 deposit --reserve-only 登记，再启动即可继续处理。")
    elif kind == "capital_request_rejected" and isinstance(details, dict):
        name = "申购" if details.get("kind") == "subscription" else "赎回"
        text = (f"⚠️ {name} {_usd(details.get('amount'))} 美元没有执行：{details.get('reason')}。\n"
                "该请求未划转资金。已经成交的筹资卖单仍留在账本中。查看：bash scripts/fund-capital.sh")
    if text is None:
        return None
    return text + (f"\n（LumiBot 模拟账户 · 事件 #{sequence} · {kind}）" if sequence is not None else "")


# ----- Weekly report (V2 3.6) ---------------------------------------------------------------------

WEEKLY_KINDS = ("account", "benchmark", "benchmark_action", "fill", "fill_reconciled", "agent", "notification",
                "agent_outcome", "review_skipped", "critic_reused",
                "postmarket_review", "learning_evaluation", "startup", "factor_negative_control", "capital_flow")
NO_DATA = "无数据"


def week_of(day):
    """(Monday, Sunday, "YYYY-Www") of the ISO week holding the New York session date `day`."""
    from datetime import date, timedelta
    session = date.fromisoformat(day)
    monday = session - timedelta(days=session.weekday())
    year, week, _ = session.isocalendar()
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat(), f"{year}-W{week:02d}"


def week_sessions(start, end):
    """{"YYYY-Www": (last NYSE session date, its UTC close)} for ISO weeks with a session in [start, end]."""
    import pandas_market_calendars as mcal
    weeks = {}
    for index, market in mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end).iterrows():
        day = index.date().isoformat()
        weeks[week_of(day)[2]] = (day, market.market_close.to_pydatetime())  # Later sessions replace earlier ones.
    return weeks


def week_calendar(day):
    """For New York session date `day`: (is it the last NYSE session of its ISO week,
    UTC open of the first session of a later ISO week or None)."""
    from datetime import date, timedelta
    import pandas_market_calendars as mcal
    session = date.fromisoformat(day)
    schedule = mcal.get_calendar("NYSE").schedule(start_date=session + timedelta(days=1),
                                                  end_date=session + timedelta(days=17))
    week = session.isocalendar()[:2]
    later = [market.market_open.to_pydatetime() for index, market in schedule.iterrows()
             if index.date().isocalendar()[:2] != week]
    ends = not len(schedule) or schedule.index[0].date().isocalendar()[:2] != week
    return ends, (later[0] if later else None)


def _weekly_facts(events, monday, sunday, zone):
    """Account, benchmark, trading and model-cost facts of one week, from the journal only."""
    from .costs import scripted, unpriced_reason
    from .performance import PerformanceSettings, benchmark_series
    def local(at):
        return instant(at).astimezone(zone).date().isoformat()
    from .capital import FLOW, Units, signed_amount
    units = Units.from_events(events)  # Operator subscriptions and redemptions are not returns.
    marks, dollars, mark_indexes = {}, {}, {}
    for index, (at, kind, body) in enumerate(events):
        if kind == "account":
            day = local(at)
            dollars[day] = float(body["equity"])
            marks[day] = dollars[day] * units.scale(index)
            mark_indexes[day] = index
    before = [d for d in sorted(marks) if d < monday]
    inside = [d for d in sorted(marks) if monday <= d <= sunday]
    facts = {"start_day": before[-1] if before else (inside[0] if inside else None),
             "end_day": inside[-1] if inside else None, "start_from_previous_week": bool(before)}
    days = ([facts["start_day"]] if before else []) + inside
    facts["daily"] = [(b, marks[b] / marks[a] - 1) for a, b in zip(days, days[1:]) if marks[a] > 0]
    if facts["start_day"] and facts["end_day"] and marks[facts["start_day"]] > 0:
        facts["week_return"] = marks[facts["end_day"]] / marks[facts["start_day"]] - 1
        facts["start_equity"], facts["end_equity"] = dollars[facts["start_day"]], dollars[facts["end_day"]]
        start, end = mark_indexes[facts["start_day"]], mark_indexes[facts["end_day"]]
        flows = [signed_amount(body) for _, kind, body in events[start + 1:end] if kind == FLOW]
        if flows:
            facts["capital_flows_usd"] = float(sum(flows))
    level, peak, worst = 1.0, 1.0, 0.0
    for _, r in facts["daily"]:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    facts["max_drawdown"] = worst if facts["daily"] else None
    prices, actions = {}, {}
    for at, kind, body in events:
        if kind == "benchmark":
            for symbol, value in body.items():
                try:
                    if float(value) > 0:
                        prices.setdefault(symbol, {})[local(at)] = float(value)
                except (TypeError, ValueError):
                    pass
        elif kind == "benchmark_action":
            actions.setdefault(body["symbol"], []).append(body)
    configured = [b.get("performance") for _, k, b in events if k == "startup" and b.get("performance")]
    definitions = configured[-1]["benchmarks"] if configured else PerformanceSettings().definitions()
    facts["benchmarks"] = {}
    if facts.get("week_return") is not None:
        pairs = list(zip(days, days[1:]))
        for definition in definitions:
            series = benchmark_series(definition, pairs, prices, actions)
            growth = 1.0
            for value in series:
                growth = growth * (1 + value) if value is not None and growth is not None else None
            facts["benchmarks"][definition["id"]] = growth - 1 if growth is not None else None
    orders = {}
    for at, kind, body in events:
        if kind in {"fill", "fill_reconciled"} and monday <= local(at) <= sunday:
            orders.setdefault(str(body.get("broker_id")), {}).setdefault(kind, []).append(body)
    fills = [b for kinds in orders.values() for b in (kinds["fill_reconciled"][-1:] if "fill_reconciled" in kinds
                                                      else kinds["fill"])]
    fees = [b.get("trade_cost") for b in fills]
    facts["trades"] = {"fills": len(fills),
                       "notional": sum(float(b["quantity"]) * float(b["price"]) for b in fills),
                       "fees": sum(float(f) for f in fees if f is not None),
                       "fees_unknown": sum(f is None for f in fees)}
    calls = [b for at, kind, b in events if kind == "agent" and monday <= local(at) <= sunday
             and not scripted(b) and not b.get("cache_hit")]
    by_role = {}
    for body in calls:
        if body.get("cost_usd") is not None:
            by_role[body.get("name") or "unknown"] = by_role.get(body.get("name") or "unknown", 0.0) + float(body["cost_usd"])
    reasons = Counter(unpriced_reason(b) for b in calls if b.get("cost_usd") is None)
    facts["model"] = {"calls": len(calls), "usd": sum(by_role.values()), "by_role": by_role,
                      "unpriced": sum(reasons.values()), "unpriced_by_reason": dict(reasons)}
    facts["health"] = Counter(b.get("kind") for at, kind, b in events if kind == "notification"
                              and b.get("kind") in HEALTH_KINDS and monday <= local(at) <= sunday)
    return facts


def _weekly_events(db, cutoff, zone):
    """WEEKLY_KINDS events up to `cutoff`, as _weekly_facts reads them, without every NAV mark and benchmark.

    _weekly_facts keeps the last equity of each New York date and, per symbol,
    the last positive benchmark price of each date. Those are the only account
    and benchmark facts kept here (one event each per date); a NAV mark and a
    benchmark are written about every 30 seconds of market time.
    """
    events, marks, prices = [], {}, {}
    for sequence, at, kind, body, equity in db.execute(
            "SELECT sequence,at,kind,CASE WHEN kind='account' THEN NULL ELSE body END,"
            "CASE WHEN kind='account' AND json_valid(body) THEN json_extract(body,'$.equity') END "
            "FROM events WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(WEEKLY_KINDS)), WEEKLY_KINDS):
        moment = instant(at)
        if moment > cutoff:
            continue
        day = moment.astimezone(zone).date()
        if kind == "account":
            marks[day] = (sequence, at, equity)
        elif kind == "benchmark":
            latest = prices.setdefault(day, [sequence, at, {}])
            latest[:2] = sequence, at
            for symbol, value in json.loads(body).items():
                try:
                    if float(value) > 0:
                        latest[2][symbol] = float(value)
                except (TypeError, ValueError):
                    pass
        else:
            events.append((sequence, at, kind, event_body(kind, body)))
    events += [(sequence, at, "account", {"equity": equity}) for sequence, at, equity in marks.values()]
    events += [(sequence, at, "benchmark", values) for sequence, at, values in prices.values()]
    return [(at, kind, body) for _, at, kind, body in sorted(events, key=lambda row: row[0])]


def latest_weekly(root):
    """The weekly report so far, from the latest completed post-market review: (filename, markdown).

    For the operator on demand (`fund-logs.sh weekly`); it is printed, never sent.
    """
    with operator_connection(root) as db:
        row = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review' ORDER BY sequence DESC LIMIT 1").fetchone()
    if not row:
        raise RiskError("还没有盘后复盘，周报没有内容：第一个交易日收盘复盘完成后再试")
    return weekly_document(root, row[0])


def weekly_document(root, sequence):
    """The week's report ending with one completed post-market review: (filename, markdown).

    Facts come from the journal and code; statistical inferences say their sample;
    model judgments are the AI's own words; hypotheses are untested. A section
    without data says so instead of being left out or filled in.
    """
    zone = ZoneInfo("America/New_York")
    with operator_connection(root) as db:
        row = db.execute("SELECT body FROM events WHERE sequence=? AND kind='postmarket_review'", (sequence,)).fetchone()
        if not row:
            raise RiskError("Weekly report has no matching completed review")
        packet = json.loads(row[0])
        cutoff, day = instant(packet["completed_at"]), packet["session_date"]
        monday, sunday, label = week_of(day)
        events = _weekly_events(db, cutoff, zone)
        # Written just after each review, so selected by session date like the daily report.
        related = [(kind, json.loads(body)) for kind, body in db.execute(
            "SELECT kind,body FROM events WHERE kind IN ('scorecard','factor_tests') AND "
            "json_extract(body,'$.session_date') BETWEEN ? AND ? ORDER BY sequence", (monday, day))]
        # Experiment registry (V2 4.1): this week's state changes. The registry syncs just after
        # each review is saved, so like the scorecard they are selected by date, not by the cutoff.
        changes = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_experiment_history'").fetchone():
            for experiment, at, before, after, reason in db.execute(
                    "SELECT id,at,from_state,to_state,reason FROM fund_experiment_history ORDER BY rowid"):
                if monday <= instant(at).astimezone(zone).date().isoformat() <= sunday:
                    changes.append((experiment, before, after, reason))
    facts = _weekly_facts(events, monday, sunday, zone)
    reviews = [b for _, k, b in events if k == "postmarket_review" and monday <= b["session_date"] <= sunday]
    lines = [f"# {label} 模拟账户周报（{monday} 至 {sunday}）", "",
             f"报告截止：{cutoff.isoformat()}（最后一次复盘：{day}）。", "",
             "本报告区分四类内容：【事实】账本记录和代码计算；【统计推断】带样本量的检验，样本少时不稳定；"
             "【模型判断】AI 自己的复盘文字；【待验证假设】尚未被数据证实的想法。", "",
             "## 一、本周收益与风险【事实】", ""]
    if facts.get("week_return") is None:
        lines.append(NO_DATA)
    else:
        origin = "上周最后一次估值" if facts["start_from_previous_week"] else "本周第一次估值（没有上周估值）"
        lines.append(f"- 净值 {facts['start_equity']:.2f} → {facts['end_equity']:.2f}，本周收益 "
                     f"{_percent(facts['week_return'])}（起点：{origin}，{facts['start_day']}）")
        if "capital_flows_usd" in facts:
            lines.append(f"- 本周申购/赎回净额 {facts['capital_flows_usd']:+.2f} 美元：已从收益里剔除（按单位净值计算）")
        model = facts.get("model") or {}
        if model.get("calls") and facts.get("start_equity"):
            # The account never pays its models: the fund's own return is what is left after them.
            spent = model.get("usd") or 0.0
            text = (f"- 本周 AI 费用 ${spent:.2f}（{model['calls']} 次调用），扣除后本周收益 "
                    f"{_percent(facts['week_return'] - spent / facts['start_equity'])}")
            if model.get("unpriced"):
                text += f"（另有 {model['unpriced']} 次调用未定价、未计入，实际更低）"
            lines.append(text)
        lines.append("- 每日：" + ("；".join(f"{d} {_percent(r)}" for d, r in facts["daily"]) or NO_DATA))
        lines.append(f"- 本周最大回撤（按每日估值）：{_percent(facts['max_drawdown'])}")
        for benchmark, value in facts["benchmarks"].items():
            excess = facts["week_return"] - value if value is not None else None
            lines.append(f"- 基准 {benchmark}：{_percent(value)}，超额 {_percent(excess)}" if value is not None else
                         f"- 基准 {benchmark}：{NO_DATA}（缺价格记录）")
    trades, model = facts["trades"], facts["model"]
    lines += ["", "## 二、交易成本和模型费用【事实】", "",
              f"- 成交 {trades['fills']} 笔，金额 {trades['notional']:.2f} 美元，已记录费用 {trades['fees']:.2f} 美元"
              + (f"，{trades['fees_unknown']} 笔费用未知（不按 0 计）" if trades["fees_unknown"] else ""),
              (f"- 模型调用 {model['calls']} 次，估算费用 {model['usd']:.4f} 美元"
               + (f"（另有 {model['unpriced']} 次未定价，不按 0 计）" if model["unpriced"] else "")
               + ("；按角色：" + "、".join(f"{r} {v:.4f}" for r, v in sorted(model["by_role"].items()))
                  if model["by_role"] else "")) if model["calls"] else f"- 模型调用：{NO_DATA}"]
    from .holdings import day_trades
    from .costs import unpriced_text
    if model.get("unpriced_by_reason"):
        lines.append("- 未定价原因：" + unpriced_text(model["unpriced_by_reason"]) + "；未知费用未计入，不按零计算。")
    week_trades = day_trades([(at, kind, body) for at, kind, body in events if kind in {"fill", "fill_reconciled"}],
                             cutoff)["by_day"]
    lines.append(f"- 日内交易（同一天买入又卖出，估算）：本周 "
                 f"{sum(n for d, n in week_trades.items() if monday <= d <= sunday)} 次")
    from .call_value import from_events, lines as value_lines
    week = [(at, kind, body) for at, kind, body in events if kind in {"agent", "agent_outcome", "review_skipped",
            "critic_reused"} and monday <= instant(at).astimezone(zone).date().isoformat() <= sunday]
    value = value_lines(from_events(week))
    if value:
        lines += ["", "AI 调用价值（被采用＝已记录结果通过校验并被使用；未记录采用结果不能证明未使用；"
                  "改变目标组合＝增删证券或权重变动至少 1 个百分点）："] + value
    lines += ["", "## 三、本周归因合计【事实，代码按收盘价计算】", ""]
    usable = [r.get("attribution") for r in reviews if (r.get("attribution") or {}).get("status") in {"complete", "partial"}]
    if usable:
        totals, unknown = {}, {}
        for item in usable:
            for key, value in item["components"].items():
                totals.setdefault(key, 0.0)
                if value is None:
                    # A day whose part is unknown (a fill without an arrival quote or a fee): never summed as zero.
                    unknown[key] = unknown.get(key, 0) + 1
                else:
                    totals[key] += value
        lines += [f"- {ATTRIBUTION_LABELS[key]}："
                  + ("未知（每天都缺数据）" if unknown.get(key) == len(usable) else _percent(value)
                     + (f"（{unknown[key]} 天未知，未计入）" if key in unknown else ""))
                  for key, value in totals.items()]
        lines.append(f"- 未解释：{_percent(sum(i['unexplained'] for i in usable))}（{len(usable)} 个交易日相加，"
                     f"其中不完整 {sum(i['status'] == 'partial' for i in usable)} 天）")
    else:
        lines.append(NO_DATA)
    card = next((body for kind, body in reversed(related) if kind == "scorecard"), None)
    lines += ["", "## 四、预测与研究想法打分【统计推断】", ""]
    if card:
        predictions = card.get("predictions") or {}
        lines.append(f"- 复盘预测：共 {predictions.get('total', 0)} 条，已打分 {predictions.get('scored', 0)}，"
                     f"命中率 {_percent(predictions.get('hit_rate'))}")
        ideas = card.get("ideas")
        text = idea_text(ideas)
        lines.append(f"- {text}" if text else f"- 研究想法：{NO_DATA}")
        for bucket, value in ((ideas or {}).get("by_confidence") or {}).items():
            if value["scored"]:
                lines.append(f"  - 把握 {bucket}：{value['scored']} 条，命中率 {_percent(value['hit_rate'])}，"
                             f"平均把握 {_percent(value['mean_confidence'])}")
    else:
        lines.append(NO_DATA)
    consistency = (card or {}).get("consistency")
    if consistency and consistency.get("moves"):
        lines.append(f"- 决策反复：最近 {consistency['decisions']} 次有效决策中，目标调整 {consistency['moves']} 次，"
                     f"其中 {consistency['reversals']} 次在 {consistency['window_sessions']} 个交易日内反向调整"
                     f"（{_percent(consistency['reversal_rate'])}，不含止损止盈触发的），"
                     f"估算多花成本约占净值 {_percent(consistency['estimated_cost'])}")
    lines += [f"- {line}" for line in regime_trend_lines(card) + evidence_lines(card)]
    tests = [r for kind, body in related if kind == "factor_tests" for r in body.get("results", [])]
    control = next((b for _, k, b in reversed(events) if k == "factor_negative_control"), None)
    lines += ["", "## 五、因子实验进展【统计推断】", ""]
    if tests:
        counts = Counter(FACTOR_STATUS.get(r.get("status"), r.get("status")) for r in tests)
        lines.append("- 本周检验：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
        lines += [f"  - 历史检验通过（待样本外确认）：{r.get('name')} `{r.get('expression')}`"
                  for r in tests if r.get("status") == "validated"]
    else:
        lines.append(f"- 本周检验：{NO_DATA}")
    lines.append(f"- 随机因子负对照（{control['as_of']}）：样本内通过率 {_percent(control.get('in_sample_pass_rate'))}，"
                 f"最终通过率 {_percent(control.get('validated_rate'))}，显著性 {_percent(control.get('significance'))}"
                 if control else f"- 随机因子负对照：{NO_DATA}")
    from .universe import survivorship_text
    lines.append(f"- 注意：{survivorship_text(root)}")
    learning = next((b for _, k, b in reversed(events) if k == "learning_evaluation"), None)
    construction = (card or {}).get("construction")
    lines += ["", "## 六、对照实验状态【统计推断】", ""]
    if construction:
        test = construction.get("test") or {}
        lines.append(f"- 组合构建对照：当前 {construction.get('champion')}，配对 {test.get('pairs')} 个交易日，"
                     f"t = {test.get('t_stat')}")
    else:
        lines.append(f"- 组合构建对照：{NO_DATA}")
    if learning:
        stats = learning.get("memo_minus_baseline_1d_excess") or {}
        lines.append(f"- 学习对照（影子 A/B）：当前 {learning.get('champion')}，配对 {stats.get('pairs')} 个交易日，"
                     f"t = {stats.get('t_stat')}")
    else:
        lines.append(f"- 学习对照（影子 A/B）：{NO_DATA}")
    from .experiments import STATE_LABELS
    if changes:
        lines.append(f"- 实验登记本周的状态变化（{len(changes)} 条，完整列表：bash scripts/fund-logs.sh experiments）：")
        lines += [f"  - {experiment}：{STATE_LABELS.get(before, '新登记') if before else '新登记'} → "
                  f"{STATE_LABELS.get(after, after)}（{reason}）" for experiment, before, after, reason in changes[:12]]
    else:
        lines.append("- 实验登记本周没有状态变化")
    lines += ["", "## 七、异常和缺失数据【事实】", ""]
    health = facts["health"]
    lines.append("- 运行异常：" + ("；".join(f"{k} × {v}" for k, v in sorted(health.items())) if health else "本周没有记录"))
    gaps = []
    for r in reviews:
        item = r.get("attribution") or {}
        if item.get("status") in {"unavailable", "partial", None}:
            detail = item.get("reason") or "、".join(
                list(item.get("unavailable") or []) + [f"{s} 费用未知" for s in item.get("fees_unknown") or []]
                + [f"{s} 缺到达报价（择时／滑点未知）" for s in item.get("arrival_unknown") or []])
            gaps.append(f"{r['session_date']}：{detail or '这次复盘没有归因'}")
    lines.append("- 归因缺数据：" + ("；".join(gaps) if gaps else "无"))
    data_gaps = [g for r in reviews for g in (r.get("review") or {}).get("data_gaps", [])]
    lines.append("- 复盘指出的数据缺项：" + ("；".join(data_gaps[:10]) if data_gaps else "无"))
    lines.append(f"- 本周完成复盘 {len(reviews)} 次")
    lines += ["", "## 八、AI 本周复盘要点【模型判断】", ""]
    lines += [f"- {r['session_date']}（置信度 {(r.get('review') or {}).get('confidence')}）："
              f"{((r.get('review') or {}).get('summary') or '')[:600]}" for r in reviews] or [NO_DATA]
    lines += ["", "## 九、下周关注的假设【待验证假设】", ""]
    last = (reviews[-1].get("review") if reviews else None) or {}
    hypotheses = [c for key in ("selection_adjustments", "timing_adjustments") for c in last.get(key, [])]
    lines += [f"- {c['hypothesis']}（验证条件：{c['validation']}；失效条件：{c['invalidated_when']}）"
              for c in hypotheses] or [f"- 调整：{NO_DATA}"]
    pending = last.get("predictions") or []
    lines += ([f"- 待打分预测 {len(pending)} 条：" + "；".join(
        f"{p['symbol']} {DIRECTIONS.get(p['direction'], p['direction'])} {p['horizon_sessions']} 日" for p in pending[:10])]
        if pending else [])
    if last.get("strategy_memo"):
        lines += ["", "最新策略备忘（AI 撰写，供下周参考）：", "", last["strategy_memo"][:3000]]
    return f"weekly-{label}.md", "\n".join(lines) + "\n"
