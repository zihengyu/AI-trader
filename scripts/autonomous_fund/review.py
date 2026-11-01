"""Read-only, timestamped post-session feedback for this optional application."""
import json
from collections import Counter
from dataclasses import asdict
from typing import Literal
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
from pydantic import BaseModel, ConfigDict, Field

from .journal import encode
from .risk import RiskError, instant, validate_evidence


class Adjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hypothesis: str = Field(min_length=1, max_length=1200)
    rationale: str = Field(min_length=1, max_length=1500)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    validation: str = Field(min_length=1, max_length=1200)
    invalidated_when: str = Field(min_length=1, max_length=1200)


class Prediction(BaseModel):
    """A falsifiable call, scored later against completed closes only."""
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(min_length=1, max_length=12)
    direction: Literal["up", "down", "outperform", "underperform"]
    horizon_sessions: int = Field(ge=1, le=20)
    benchmark: str = Field(default="SPY", min_length=1, max_length=12)
    hypothesis_ref: str = Field(default="", max_length=200)


class PostmarketReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_date: str
    as_of: str
    summary: str = Field(min_length=1, max_length=4000)
    selection_adjustments: list[Adjustment] = Field(max_length=8)
    timing_adjustments: list[Adjustment] = Field(max_length=8)
    retained_lessons: list[str] = Field(max_length=12)
    retired_hypotheses: list[str] = Field(max_length=12)
    data_gaps: list[str] = Field(max_length=12)
    confidence: Literal["low", "medium", "high"]
    strategy_memo: str = Field(min_length=1, max_length=6000)
    predictions: list[Prediction] = Field(default_factory=list, max_length=10)
    mandate_compliance: str = Field(default="", max_length=2000)


def _events(journal, now, kinds=None):
    with journal.connection() as db:
        if kinds:
            rows = db.execute("SELECT sequence,at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence"
                              % ",".join("?" * len(kinds)), tuple(kinds)).fetchall()
        else:
            rows = db.execute("SELECT sequence,at,kind,body FROM events ORDER BY sequence").fetchall()
    # SQL string ordering is unsafe when timestamps have different UTC offsets.
    return [{"sequence": row[0], "at": row[1], "kind": row[2], "body": json.loads(row[3])}
            for row in rows if instant(row[1]) <= now]


def _available(packet, now):
    return instant(packet["completed_at"]) <= now and instant(packet["market_close"]) <= now


def strategy_memory(journal, now):
    """Only finished reviews already available to this strategy clock may teach it."""
    now = instant(now)
    with journal.connection() as db:
        rows = db.execute("SELECT at,body FROM events WHERE kind='postmarket_review' ORDER BY sequence").fetchall()
    reviews = []
    for at, body in rows:
        if instant(at) <= now:
            packet = json.loads(body)
            if _available(packet, now):
                reviews.append(packet)
    if not reviews:
        return None
    packet = reviews[-1]
    return {"version": packet["version"], "session_date": packet["session_date"],
            "available_at": packet["completed_at"], "model": packet["models"]["manager"],
            "mock": packet["mock"], "review": packet["review"],
            "account_observation_gaps": packet["session"]["account_observation_gaps"],
            "recent_reviews": [{"version": p["version"], "session_date": p["session_date"],
                                "summary": p["review"]["summary"]} for p in reviews[-3:]]}


def _session(rows, now):
    # Never invent a review of a day before the owned account was observed.
    zone = ZoneInfo("America/New_York")
    days = {instant(row["at"]).astimezone(zone).date() for row in rows
            if row["kind"] in {"account", "decision_validated", "fill", "fill_reconciled"}}
    if not days:
        return None
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(start_date=min(days), end_date=max(days))
    for day, market in reversed(list(schedule.iterrows())):
        if day.date() in days and market.market_close <= now:
            return {"session_date": str(day.date()), "market_open": market.market_open.isoformat(),
                    "market_close": market.market_close.isoformat()}
    return None


def _inputs(rows, session, now):
    zone = ZoneInfo("America/New_York")
    day = session["session_date"]
    owned = [row for row in rows if str(instant(row["at"]).astimezone(zone).date()) == day]
    accounts = [row for row in rows if row["kind"] == "account"]
    today = [row for row in owned if row["kind"] == "account"]
    prior = [row for row in accounts if str(instant(row["at"]).astimezone(zone).date()) < day]
    baseline = prior[-1] if prior else (today[0] if today else None)
    ending = today[-1] if today else None
    start_value = float(baseline["body"]["equity"]) if baseline else None
    end_value = float(ending["body"]["equity"]) if ending else None
    pnl = end_value - start_value if start_value is not None and end_value is not None else None
    gaps = []
    if not prior:
        gaps.append("没有前一交易日的账户估值；收益窗口从本日第一次账户观测开始。")
    if not ending:
        gaps.append("没有该交易日的账户观测，无法计算账户收益。")
    elif instant(ending["at"]) < instant(session["market_close"]):
        gaps.append(f"最后账户观测为 {ending['at']}，早于收盘，不能当作收盘净值。")
    benchmarks = {}
    for symbol in ("SPY", "QQQ"):
        first = [r for r in rows if r["kind"] == "benchmark" and baseline and
                 instant(r["at"]) <= instant(baseline["at"]) and symbol in r["body"]]
        last = [r for r in owned if r["kind"] == "benchmark" and ending and
                instant(r["at"]) <= instant(ending["at"]) and symbol in r["body"]]
        # Exact observation times only; do not compare unmatched windows.
        aligned = (first and last and first[-1]["at"] == baseline["at"] and last[-1]["at"] == ending["at"])
        benchmarks[symbol] = float(last[-1]["body"][symbol]) / float(first[-1]["body"][symbol]) - 1 if aligned else None
    kinds = {"account", "decision", "decision_validated", "fill", "fill_reconciled", "order_submitted",
             "order_observed", "notification", "market_observation", "local_corporate_action",
             "decision_accepted", "allocation_complete", "operator_approval"}
    evidence = [r for r in owned if r["kind"] in kinds or
                (r["kind"] == "agent" and r["body"].get("name") == "researcher" and
                 r["body"].get("phase") != "postmarket_review")]
    evidence += [r for r in rows if r["kind"] == "premarket_research" and
                 str(instant(r["body"]["session_open"]).astimezone(zone).date()) == day]
    evidence.sort(key=lambda row: row["sequence"])
    # Keep prompts bounded; full source events stay in SQLite. Disclose omissions.
    selected, size = [], 0
    for row in reversed(evidence):
        length = len(encode(row))
        if len(selected) < 120 and size + length <= 120_000:
            selected.append(row)
            size += length
    selected.reverse()
    return {**session, "input_as_of": now.isoformat(),
            "counts": dict(Counter(r["kind"] for r in owned)),
            "baseline_account": baseline, "last_observed_account": ending,
            "observed_pnl_usd": pnl, "observed_return": pnl / start_value if pnl is not None and start_value else None,
            "account_observation_gaps": gaps,
            "benchmark_observed_price_returns": benchmarks,
            "events": selected, "omitted_events": len(evidence) - len(selected),
            "limitations": "Observed account marks, not guaranteed closing NAV. Source times are preserved. "
                            "Unrealized PnL is not realized profit. Cumulative broker observations are not additional fills. "
                            "Fees can be unknown; use confirmed execution costs only. Cash flows are not adjusted. "
                            "Incomplete or absent data must be disclosed; one day cannot validate a trading strategy."}


def _save_once(journal, now, kind, packet):
    # The runner holds the account lock. Also make each completed stage atomic
    # against duplicate native lifecycle callbacks and interrupted export writes.
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT body FROM events WHERE kind=? AND json_extract(body,'$.session_date')=?",
                         (kind, packet["session_date"])).fetchone()
        if old:
            existing = json.loads(old[0])
            if not _available(existing, instant(now)):
                raise RiskError("A saved review stage belongs to a later strategy time")
            return existing
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), kind, encode(packet)))
    return packet


def _run_stage(strategy, name, **kwargs):
    if hasattr(strategy, "_agent_call"):
        return strategy._agent_call(name, **kwargs)
    try:
        return strategy.agents[name].run(**kwargs)
    except Exception as exc:
        # A transport failure can happen after provider inference. Unknown
        # usage must not quietly become a zero-dollar successful cost estimate.
        strategy.journal.event(strategy.get_datetime(), "agent", {
            "name": name, "model": getattr(strategy, "agent_models", {}).get(name, "unknown"),
            "cache_hit": False, "usage": None, "summary": "Review attempt failed; provider usage unknown.",
            "parse_error": type(exc).__name__, "mock": strategy.scripted, "phase": "postmarket_review"})
        raise


def review_after_close(strategy):
    """Two existing read-only roles; no execution or writable configuration tools."""
    if strategy.observe_only or strategy.halt_path.exists():
        return
    now = instant(strategy.get_datetime())
    historical = strategy.mode not in {"local", "paper"}
    end = getattr(getattr(strategy.broker, "data_source", None), "datetime_end", None)
    if historical and end is not None and now > instant(end):
        return  # Native end-of-backtest cleanup can occur beyond the requested cutoff.
    rows = _events(strategy.journal, now)
    session = _session(rows, now)
    if not session:
        return
    day = session["session_date"]
    if any(r["kind"] == "postmarket_review" and r["body"]["session_date"] == day for r in rows):
        return
    memory = strategy_memory(strategy.journal, now)
    scorecards = None
    if hasattr(strategy, "_scorecards"):
        try:
            scorecards = strategy._scorecards(now)
        except Exception as exc:
            scorecards = {"unavailable": str(exc) if isinstance(exc, RiskError) else type(exc).__name__}
    mandate = getattr(strategy, "mandate", None)
    context = {"as_of": now.isoformat(), "historical": historical, "phase": "postmarket_review",
               "orders_enabled": False, "eligible_universe": sorted(strategy.universe),
               "limits": asdict(strategy.limits), "strategy_memory": memory,
               "strategy_followup": strategy._strategy_evaluations(),
               "scorecards": scorecards, "operator_mandate": mandate.context() if mandate else None,
               "session": _inputs(rows, session, now)}
    saved = next((r["body"] for r in reversed(rows) if r["kind"] == "postmarket_research" and
                  r["body"]["session_date"] == day and _available(r["body"], now)), None)
    if saved is None:
        strategy.journal.event(now, "postmarket_review_started", session)
        result = _run_stage(strategy, "researcher",
            task_prompt="用中文复盘指定交易日。核对实际研究、完整目标持仓、下单、成交、未成交、持仓和现金，"
                        "区分选股、择时、执行与市场整体表现。可使用只读研究工具，核对消息与价格的真实时间。"
                        "引用日志证据请使用 event:<sequence>，外部资料请给出来源和时间。"
                        "披露数据缺失、陈旧估值和费用不确定性；不要编造收盘净值或把没成交当成成交。"
                        "本阶段不输出订单。历史模式只能使用 as_of 当时可得的信息。", context=context)
        research = strategy._record_agent("researcher", result, phase="postmarket_review")
        validate_evidence(research["evidence"], strategy.get_datetime(), historical)
        saved = _save_once(strategy.journal, strategy.get_datetime(), "postmarket_research", {
            **session, "completed_at": strategy.get_datetime().isoformat(), "context": context,
            "research": research, "model": result.model, "mock": strategy.scripted})
    # Persist the successful Flash stage: a Pro error/restart does not pay to
    # repeat that work. Its original as_of and evidence remain visible.
    now = instant(strategy.get_datetime())
    if strategy.halt_path.exists():
        return
    context = {**saved["context"], "as_of": now.isoformat(), "research": saved["research"],
               "research_completed_at": saved["completed_at"]}
    result = _run_stage(strategy, "manager",
        task_prompt="这是盘后复盘，不是可执行持仓决策。按本次 PostmarketReview schema 用中文回答。"
                    "核对研究和实际账户，评估昨日备忘中的假设是否获得支持或反证。"
                    "自主改进选股与择时的判断方法，分别列出可检验的调整、依据、验证条件和失效条件；"
                    "证据不足时可以不调整。evidence_refs 只引用 session 中的 event:<sequence> 或 research.evidence 的 source。"
                    "给出保留教训、撤回假设、数据缺项及下一交易日完整 strategy_memo，继承仍有效的经验。"
                    "不因单日盈亏宣称策略有效，不追涨杀跌式过拟合，说明样本不足和置信度。"
                    "备忘仅供下一次 AI 研究和完整目标持仓决策参考，不能修改风险上限、投资范围、权限或代码。"
                    "必须阅读 scorecards（已用收盘价客观打分的历史预测命中率、各决策相对同敞口基准的超额收益），"
                    "据此保留、修正或撤回假设；记分差的假设应撤回。给出最多 10 条可证伪的 predictions"
                    "（symbol、direction=up/down/outperform/underperform、horizon_sessions 1-20、benchmark、"
                    "hypothesis_ref 指向对应调整），之后会被自动打分。"
                    "在 mandate_compliance 中逐条核对当日决策是否遵守 operator_mandate 的约束，如有偏离必须说明。"
                    "返回 session_date=指定交易日以及本次 context.as_of；不要返回订单或 targets。", context=context,
        output_schema=PostmarketReview)
    review = PostmarketReview.model_validate(strategy._record_agent("manager", result, phase="postmarket_review")).model_dump()
    if review["session_date"] != day or instant(review["as_of"]) != now:
        raise RiskError("Postmarket review session/as_of mismatch")
    refs = {f"event:{r['sequence']}" for r in saved["context"]["session"]["events"]}
    refs.update(e["source"] for e in saved["research"]["evidence"])
    if any(ref not in refs for change in review["selection_adjustments"] + review["timing_adjustments"]
           for ref in change["evidence_refs"]):
        raise RiskError("Postmarket adjustment refers to evidence outside the supplied review")
    packet = {**session, "version": (memory["version"] if memory else 0) + 1,
              "previous_version": memory["version"] if memory else None,
              "input_as_of": saved["context"]["as_of"], "completed_at": strategy.get_datetime().isoformat(),
              "models": {"researcher": saved["model"], "manager": result.model},
              "mock": saved["mock"] or strategy.scripted,
              "research": saved["research"], "review": review, "session": saved["context"]["session"]}
    _save_once(strategy.journal, strategy.get_datetime(), "postmarket_review", packet)
    if scorecards:
        strategy.journal.event(strategy.get_datetime(), "scorecard", {"session_date": day, **scorecards})
    strategy.log_message(f"Post-market review saved for {day}; strategy memory v{packet['version']} is available.")


def export_reviews(root, packets):
    """All artifacts are rebuildable from the append-only account journal."""
    if not packets:
        return
    folder = root / "reviews"
    folder.mkdir(exist_ok=True)
    for packet in packets:
        day, review = packet["session_date"], packet["review"]
        lines = [f"# {day} 盘后复盘 · 策略备忘 v{packet['version']}", "",
                 f"完成时间：{packet['completed_at']}；模型：{packet['models']}；Mock：{packet['mock']}", "",
                 review["summary"], "", "## 当日账户观测", "",
                 f"观测盈亏（美元）：{packet['session']['observed_pnl_usd']}；观测收益率：{packet['session']['observed_return']}",
                 "以源时间为准，可能缺少收盘估值；未实现盈亏不等于已实现利润。", "",
                 *packet["session"]["account_observation_gaps"], ""]
        for title, key in (("选股调整", "selection_adjustments"), ("择时调整", "timing_adjustments")):
            lines.extend([f"## {title}", ""])
            for item in review[key]:
                lines.extend([f"- 假设：{item['hypothesis']}", f"  依据：{item['rationale']}",
                              f"  证据：{', '.join(item['evidence_refs'])}", f"  验证：{item['validation']}",
                              f"  失效：{item['invalidated_when']}", ""])
            if not review[key]:
                lines.extend(["本次没有新增调整。", ""])
        for title, key in (("保留教训", "retained_lessons"), ("撤回假设", "retired_hypotheses"), ("数据缺项", "data_gaps")):
            lines.extend([f"## {title}", "", *[f"- {item}" for item in review[key]], ""])
        lines.extend(["## 约束遵守（AI 自评）", "", review.get("mandate_compliance") or "本次未填写。", "",
                      "## 可证伪预测（之后按收盘价自动打分）", ""])
        for item in review.get("predictions", []):
            lines.append(f"- {item['symbol']} {item['direction']}（{item['horizon_sessions']} 个交易日，"
                         f"基准 {item.get('benchmark', 'SPY')}；对应 {item.get('hypothesis_ref') or '未标注'}）")
        if not review.get("predictions"):
            lines.append("本次没有给出预测。")
        lines.append("")
        lines.extend(["## 下一交易日策略备忘", "", review["strategy_memo"], "",
                      f"置信度：{review['confidence']}。复盘调整是待验证的假设，不保证收益改善。", ""])
        body = json.dumps(packet, indent=2, ensure_ascii=False)
        text = "\n".join(lines)
        # Completed reviews are immutable (one per session); write each archive
        # once, and rebuild only a missing file, instead of every report cycle.
        for path, content in ((folder / f"{day}.json", body), (folder / f"{day}.md", text)):
            if not path.is_file():
                path.write_text(content, encoding="utf-8")
    (root / "postmarket_review.json").write_text(body, encoding="utf-8")
    (root / "postmarket_review.md").write_text(text, encoding="utf-8")
    last = packets[-1]
    (root / "strategy_memory.json").write_text(json.dumps({
        "version": last["version"], "session_date": last["session_date"], "available_at": last["completed_at"],
        "mock": last["mock"], "review": last["review"]}, indent=2, ensure_ascii=False), encoding="utf-8")
