"""Read-only, timestamped post-session feedback for this optional application."""
import json
from collections import Counter
from dataclasses import asdict
from typing import Literal
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
from pydantic import BaseModel, ConfigDict, Field

from .journal import encode, event_body
from .risk import RiskError, bounded_list, bounded_text, instant, validate_evidence


class Adjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hypothesis: bounded_text(1200, 1)
    rationale: bounded_text(1500, 1)
    evidence_refs: bounded_list(str, 12, 1)
    validation: bounded_text(1200, 1)
    invalidated_when: bounded_text(1200, 1)


class Prediction(BaseModel):
    """A falsifiable call, scored later against completed closes only."""
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(min_length=1, max_length=12)
    direction: Literal["up", "down", "outperform", "underperform"]
    horizon_sessions: int = Field(ge=1, le=20)
    benchmark: str = Field(default="SPY", min_length=1, max_length=12)
    hypothesis_ref: bounded_text(200) = ""


class FactorProposal(BaseModel):
    """A candidate factor in the factor lab grammar; deterministic code tests it, it never trades."""
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    expression: str = Field(min_length=1, max_length=300)
    horizon_sessions: int = Field(ge=1, le=20)
    hypothesis: bounded_text(600, 1)
    # Which cross-section ranks it: stocks (tradable plus research universe) or ETFs, never both.
    universe: Literal["stocks", "etfs"] = "stocks"


class PostmarketReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_date: str
    as_of: str
    summary: bounded_text(4000, 1)
    selection_adjustments: bounded_list(Adjustment, 8)
    timing_adjustments: bounded_list(Adjustment, 8)
    retained_lessons: bounded_list(bounded_text(600), 12)
    retired_hypotheses: bounded_list(bounded_text(600), 12)
    data_gaps: bounded_list(bounded_text(600), 12)
    confidence: Literal["low", "medium", "high"]
    strategy_memo: bounded_text(6000, 1)
    predictions: bounded_list(Prediction, 10) = Field(default_factory=list)
    mandate_compliance: bounded_text(2000) = ""
    factor_proposals: bounded_list(FactorProposal, 5) = Field(default_factory=list)


def _events(journal, now, kinds=None):
    with journal.connection() as db:
        db.execute("BEGIN")
        from .ledger_audit import audit_connection
        # The audit only decides which account marks to withhold, and it reads every one of them.
        integrity = audit_connection(db) if not kinds or "account" in kinds else {"status": "not_needed"}
        invalid_accounts = set(integrity.get("invalid_account_sequences", []))
        if kinds:
            rows = db.execute("SELECT sequence,at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence"
                              % ",".join("?" * len(kinds)), tuple(kinds)).fetchall()
        else:
            rows = db.execute("SELECT sequence,at,kind,body FROM events ORDER BY sequence").fetchall()
    # SQL string ordering is unsafe when timestamps have different UTC offsets.
    return [{"sequence": row[0], "at": row[1], "kind": row[2], "body": event_body(row[2], row[3])}
            for row in rows if instant(row[1]) <= now and not (row[2] == "account" and
                (row[0] in invalid_accounts or integrity["status"] == "failed"))]


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
            "evidence_catalog": packet.get("evidence_catalog") or [],
            "account_observation_gaps": packet["session"]["account_observation_gaps"],
            "recent_reviews": [{"version": p["version"], "session_date": p["session_date"],
                                "summary": p["review"]["summary"]} for p in reviews[-3:]]}


def _event_day(row, zone):
    valuation = (row.get("body") or {}).get("valuation") or {}
    closing = valuation.get("session_close") if row["kind"] == "account" and valuation.get("book_existed_at_close", True) else None
    return instant(closing or row["at"]).astimezone(zone).date()


SESSION_KINDS = ("account", "decision_validated", "fill", "fill_reconciled")


def _session(rows, now):
    # Never invent a review of a day before the owned account was observed.
    zone = ZoneInfo("America/New_York")
    return _session_from_days({_event_day(row, zone) for row in rows if row["kind"] in SESSION_KINDS}, now)


def _session_from_days(days, now):
    if not days:
        return None
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(start_date=min(days), end_date=max(days))
    for day, market in reversed(list(schedule.iterrows())):
        if day.date() in days and market.market_close <= now:
            return {"session_date": str(day.date()), "market_open": market.market_open.isoformat(),
                    "market_close": market.market_close.isoformat()}
    return None


# Light per-event fields: enough to place every event on its New York day
# (_event_day) and to find a day's saved review stages, without its body.
_LIGHT_EVENT = (
    "SELECT sequence,kind,at,"
    "CASE WHEN kind='account' AND json_valid(body) THEN json_extract(body,'$.valuation.session_close') END,"
    "CASE WHEN kind='account' AND json_valid(body) THEN json_type(body,'$.valuation.book_existed_at_close') END,"
    "CASE WHEN kind='account' AND json_valid(body) THEN json_extract(body,'$.valuation.book_existed_at_close') END,"
    "CASE WHEN kind IN ('postmarket_review','postmarket_research') AND json_valid(body) "
    "THEN json_extract(body,'$.session_date') END,"
    "CASE WHEN kind='premarket_research' AND json_valid(body) THEN json_extract(body,'$.session_open') END "
    "FROM events")


def _light_day(kind, at, close, existed_type, existed, zone):
    """_event_day from light fields: a closing mark belongs to its close's day unless the book did not exist yet."""
    closing = close if kind == "account" and (existed_type is None or existed) else None
    return instant(closing or at).astimezone(zone).date()


def _review_rows(journal, now):
    """(session, rows) for the review at `now`, rows shaped and filtered as _events(journal, now) returns them.

    The rows are only those the review reads: every event of the session's day,
    the last account mark before it, for each benchmark symbol the last
    observation at or before the baseline and ending marks, that day's
    premarket research and saved review stages, and one event for each other
    day with account, decision or fill events (they locate the session). If the
    day is already reviewed only its review is returned. The journal holds a NAV
    mark and a benchmark about every 30 seconds of market time; loading all of
    them at every startup, open and close took memory in proportion to the
    fund's history (about 630 MB at 100,000 marks).
    """
    from .ledger_audit import audit_connection
    from .evaluation import event_bodies
    zone, now = ZoneInfo("America/New_York"), instant(now)
    with journal.connection() as db:
        db.execute("BEGIN")
        integrity = audit_connection(db)
        invalid = set(integrity.get("invalid_account_sequences", []))
        failed = integrity["status"] == "failed"

        def visible(sequence, kind, at):
            moment = instant(at)
            return moment <= now and not (kind == "account" and (sequence in invalid or failed)), moment

        representatives = {}  # day -> the last event that places a session-kind event on it
        for sequence, kind, at, close, existed_type, existed, _, _ in db.execute(
                _LIGHT_EVENT + " WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(SESSION_KINDS)),
                SESSION_KINDS):
            if visible(sequence, kind, at)[0]:
                representatives[_light_day(kind, at, close, existed_type, existed, zone)] = sequence
        session = _session_from_days(set(representatives), now)
        if not session:
            return None, []
        day = session["session_date"]
        reviewed = [sequence for sequence, at in db.execute(
            "SELECT sequence,at FROM events WHERE kind='postmarket_review' AND json_valid(body) AND "
            "json_extract(body,'$.session_date')=? ORDER BY sequence", (day,)) if instant(at) <= now]
        wanted = set(reviewed) | set(representatives.values())
        if not reviewed:
            prior = None  # The last account mark before the session's day.
            for sequence, kind, at, close, existed_type, existed, stage_day, session_open in db.execute(
                    _LIGHT_EVENT + " ORDER BY sequence"):
                shown, moment = visible(sequence, kind, at)
                if not shown:
                    continue
                event_day = str(_light_day(kind, at, close, existed_type, existed, zone))
                if (event_day == day or (stage_day == day and kind in ("postmarket_review", "postmarket_research"))
                        or (session_open and str(instant(session_open).astimezone(zone).date()) == day)):
                    wanted.add(sequence)
                elif kind == "account" and event_day < day:
                    prior = sequence
            if prior is not None:
                wanted.add(prior)
        bodies = event_bodies(db, wanted)
        rows = [{"sequence": sequence, "at": at, "kind": kind, "body": bodies[sequence]}
                for sequence, at, kind in _rows_at(db, wanted)]
        if not reviewed:
            # The benchmarks _inputs compares: per symbol, the last one at or before each anchor mark.
            accounts = [row for row in rows if row["kind"] == "account"]
            prior = [row for row in accounts if str(_event_day(row, zone)) < day]
            today = [row for row in accounts if str(_event_day(row, zone)) == day]
            anchors = [row for row in (prior[-1] if prior else (today[0] if today else None),
                                       today[-1] if today else None) if row]
            last = {}
            for sequence, at, has_spy, has_qqq in db.execute(
                    "SELECT sequence,at,json_type(body,'$.SPY') IS NOT NULL,json_type(body,'$.QQQ') IS NOT NULL "
                    "FROM events WHERE kind='benchmark' AND json_valid(body) ORDER BY sequence"):
                moment = instant(at)
                if moment > now:
                    continue
                for anchor in anchors:
                    if moment <= instant(anchor["at"]):
                        for symbol, present in (("SPY", has_spy), ("QQQ", has_qqq)):
                            if present:
                                last[(anchor["sequence"], symbol)] = sequence
            extra = set(last.values()) - wanted
            if extra:
                bodies = event_bodies(db, extra)
                rows += [{"sequence": sequence, "at": at, "kind": kind, "body": bodies[sequence]}
                         for sequence, at, kind in _rows_at(db, extra)]
                rows.sort(key=lambda row: row["sequence"])
    return session, rows


def _rows_at(db, sequences):
    """(sequence, at, kind) for the given sequences, in sequence order."""
    sequences, found = sorted(sequences), []
    for start in range(0, len(sequences), 500):
        batch = sequences[start:start + 500]
        found += db.execute("SELECT sequence,at,kind FROM events WHERE sequence IN (%s) ORDER BY sequence"
                            % ",".join("?" * len(batch)), batch).fetchall()
    return [tuple(row) for row in found]


def _inputs(rows, session, now):
    zone = ZoneInfo("America/New_York")
    day = session["session_date"]
    owned = [row for row in rows if str(_event_day(row, zone)) == day]
    accounts = [row for row in rows if row["kind"] == "account"]
    today = [row for row in owned if row["kind"] == "account"]
    prior = [row for row in accounts if str(_event_day(row, zone)) < day]
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
        last = [r for r in rows if r["kind"] == "benchmark" and ending and
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


def _evidence_catalog(saved):
    """Short references bound to one saved input snapshot; source text and times stay intact."""
    refs, catalog = [], []
    original = saved["context"]
    for index, row in enumerate(original["session"]["events"]):
        ref = f"event:{row['sequence']}"
        refs.append(ref)
        catalog.append({"ref": ref, "context_path": f"session.events[{index}]"})
    for index, evidence in enumerate(saved["research"]["evidence"], 1):
        ref = f"research:{index}"
        refs.extend((ref, evidence["source"]))  # Exact original names remain valid for older callers.
        catalog.append({"ref": ref, "context_path": f"research.evidence[{index - 1}]", **evidence})
    for key in ("session", "factor_lab", "scorecards", "attribution", "strategy_memory", "strategy_followup",
                "operator_mandate", "limits", "eligible_universe"):
        if original.get(key) is not None:
            ref = f"context:{key}"
            refs.append(ref)
            catalog.append({"ref": ref, "context_path": key, "input_as_of": original["as_of"]})
    return list(dict.fromkeys(refs)), catalog


def _invalid_refs(review, allowed):
    invalid = []
    for group in ("selection_adjustments", "timing_adjustments"):
        for index, change in enumerate(review[group]):
            unmatched = [str(i) for i, ref in enumerate(change["evidence_refs"]) if ref not in allowed]
            if unmatched:
                invalid.append(f"{group}[{index}].evidence_refs[{','.join(unmatched)}]")
    return invalid


def review_after_close(strategy):
    """Two existing read-only roles; no execution or writable configuration tools."""
    if strategy.observe_only or strategy.halt_path.exists():
        return
    now = instant(strategy.get_datetime())
    historical = strategy.mode not in {"local", "paper"}
    end = getattr(getattr(strategy.broker, "data_source", None), "datetime_end", None)
    if historical and end is not None and now > instant(end):
        return  # Native end-of-backtest cleanup can occur beyond the requested cutoff.
    session, rows = _review_rows(strategy.journal, now)
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
               "factor_lab": strategy._factor_context(now) if hasattr(strategy, "_factor_context") else None,
               # Code-computed; the review must not re-estimate where the return came from.
               "attribution": strategy._attribution(session, now) if hasattr(strategy, "_attribution") else None,
               "session": _inputs(rows, session, now)}
    if strategy.mode == "local" and hasattr(getattr(strategy, "broker", None), "ledger_audit"):
        context["ledger_audit"] = strategy.broker.ledger_audit(strict=False)
    saved = next((r["body"] for r in reversed(rows) if r["kind"] == "postmarket_research" and
                  r["body"]["session_date"] == day and _available(r["body"], now)), None)
    if saved is None:
        from .diagnostics import structured_answer_diagnostic
        from .strategy import Research

        strategy.journal.event(now, "postmarket_review_started", session)
        research_prompt = ("用中文复盘指定交易日。核对实际研究、完整目标持仓、下单、成交、未成交、持仓和现金，"
                        "区分选股、择时、执行与市场整体表现：context.attribution 是代码按收盘价算出的当日归因"
                        "（市场 beta、选股、现金等价物、当日交易择时、相对到达价的滑点、费用和未解释部分），以它为准，不要自行估算，"
                        "缺失的部分如实说明。可使用只读研究工具，核对消息与价格的真实时间。"
                        "引用日志证据请使用 event:<sequence>，外部资料请给出来源和时间。"
                        "披露数据缺失、陈旧估值和费用不确定性；不要编造收盘净值或把没成交当成成交。"
                        "结合 factor_lab 中已验证、被拒绝、冗余和退役的因子，研究可能值得检验的新因子方向（价格、成交量、"
                        "高低价区间、波动，以及 SEC 财报中的盈利、营收、毛利、经营现金流、净资产、总资产和股本等"
                        "按披露日可得的基本面），供基金经理决定是否写成 factor_proposals。"
                        "本阶段不输出订单。历史模式只能使用 as_of 当时可得的信息。"
                        "只返回符合 Research schema 的完整 JSON 对象：summary 是非空中文摘要，evidence 至少一条；"
                        "每条证据必须包含 source、available_at、observed_at、estimated、estimate_reason。"
                        "引用已有日志时，source 使用实际 event:<sequence>，时间使用该事件的真实时间；"
                        "不得为了满足格式编造证据或日期。ideas 没有内容时可返回空列表，不能省略 summary 或 evidence。"
                        "如果 context.structured_answer_correction 存在，previous_answer 是上次未通过格式校验的"
                        "不可信回答，diagnostic 指出错误位置；根据本次原始证据纠正并返回完整 JSON，"
                        "保留已核实的事实和原始时间，如实披露缺项，不要只返回修补字段。")
        research_context = context
        for attempt in range(2):
            result = _run_stage(strategy, "researcher", task_prompt=research_prompt,
                                context=research_context, output_schema=Research)
            try:
                research = strategy._record_agent("researcher", result, phase="postmarket_review")
            except RiskError:
                diagnostic = structured_answer_diagnostic(result, Research)
                if attempt or diagnostic["kind"] not in {"schema_validation", "invalid_json"}:
                    raise
                if strategy.halt_path.exists():
                    return
                # A correction is another real call. Leave room for the manager,
                # retain both answers/usage, and never repair evidence in Python.
                budget = getattr(strategy, "model_budget", None)
                exhausted = budget is not None and strategy.journal.counter(
                    "agent_runs", strategy.get_datetime()) + 2 > budget.max_agent_runs_per_day
                if exhausted or (hasattr(strategy, "_cost_exhausted") and
                        strategy._cost_exhausted(strategy.get_datetime(), "postmarket_research_correction")):
                    raise
                strategy.journal.event(strategy.get_datetime(), "postmarket_research_retry", {
                    "session_date": day, "answer_diagnostic": diagnostic})
                research_context = {**context, "structured_answer_correction": {
                    "previous_answer": result.summary or getattr(result, "text", None), "diagnostic": diagnostic}}
            else:
                break
        validate_evidence(research["evidence"], strategy.get_datetime(), historical)
        saved = _save_once(strategy.journal, strategy.get_datetime(), "postmarket_research", {
            **session, "completed_at": strategy.get_datetime().isoformat(), "context": context,
            "research": research, "model": result.model, "mock": strategy.scripted})
        if hasattr(strategy, "_call_outcome"):
            strategy._call_outcome()  # This researcher call's answer is the review's saved research.
    # Persist the successful Flash stage: a Pro error/restart does not pay to
    # repeat that work. Its original as_of and evidence remain visible.
    now = instant(strategy.get_datetime())
    if strategy.halt_path.exists():
        return
    # Use the same snapshot for the copyable list and the exact-match check.
    refs, catalog = _evidence_catalog(saved)
    allowed_refs = set(refs)
    context = {**saved["context"], "as_of": now.isoformat(), "research": saved["research"],
               "research_completed_at": saved["completed_at"], "allowed_evidence_refs": refs,
               "evidence_catalog": catalog}
    if strategy.mode == "local" and hasattr(getattr(strategy, "broker", None), "ledger_audit"):
        context["current_ledger_audit"] = strategy.broker.ledger_audit(strict=False)
    manager_prompt = ("这是盘后复盘，不是可执行持仓决策。按本次 PostmarketReview schema 用中文回答。"
                    "核对研究和实际账户，评估昨日备忘中的假设是否获得支持或反证。"
                    "自主改进选股与择时的判断方法，分别列出可检验的调整、依据、验证条件和失效条件；"
                    "evidence_refs 必须逐字复制 context.allowed_evidence_refs 中的值，保持原有大小写和标点。"
                    "优先使用 evidence_catalog 中的短编号（event:序号、research:序号、context:字段名），"
                    "编号指向原始证据及其时间；不要把计算结果、指标说明或长段转述填进 evidence_refs。"
                    "编号只属于本轮清单，不能把旧备忘中的同名编号当成本轮新证据；引用旧备忘可用 context:strategy_memory。"
                    "如果 context.reference_correction 存在，这是一次引用修正：核对列出的 invalid_paths，"
                    "在同一份原始证据内修正 previous_review 的引用及依赖它的断言，返回完整复盘；"
                    "不得编造对应关系，没有证据支持的调整应撤回。"
                    "不要自行添加前缀或改写来源名称。清单中没有支持某条调整的证据时，省略该条调整。"
                    "给出保留教训、撤回假设、数据缺项及下一交易日完整 strategy_memo，继承仍有效的经验。"
                    "不因单日盈亏宣称策略有效，不追涨杀跌式过拟合，说明样本不足和置信度。"
                    "备忘仅供下一次 AI 研究和完整目标持仓决策参考，不能修改风险上限、投资范围、权限或代码。"
                    "必须阅读 scorecards（已用收盘价客观打分的历史预测命中率、各决策相对同敞口基准的超额收益），"
                    "据此保留、修正或撤回假设；记分差的假设应撤回。scorecards.timing 客观衡量你自己的止损、止盈和入场限价："
                    "离场后价格继续下跌为正贡献，反弹为负；等待入场后未买入而价格上涨为错过的收益，据此校准价位的松紧，"
                    "但不要因此放弃保护性止损。给出最多 10 条可证伪的 predictions"
                    "（symbol、direction=up/down/outperform/underperform、horizon_sessions 1-20、benchmark、"
                    "hypothesis_ref 指向对应调整），之后会被自动打分。"
                    "在 mandate_compliance 中逐条核对当日决策是否遵守 operator_mandate 的约束，如有偏离必须说明。"
                    "阅读 factor_lab：已验证因子的样本内、样本外和验证后的实盘记录，以及被拒绝或退役的因子。"
                    "可在 factor_proposals 中按 factor_lab.grammar 提出最多 5 个新的因子表达式（name、expression、"
                    "horizon_sessions 1-20、hypothesis、universe=stocks 或 etfs），优先基于复盘中发现的选股或择时规律；"
                    "代码会在所选横截面（个股含研究股票池，或 ETF）里用无前视的历史数据做 rank IC 检验，"
                    "要求扣除换手成本后多空收益仍为正、且与已验证因子不高度相关，每多试一个表达式门槛都会提高，"
                    "结果在下一次决策和复盘中提供。"
                    "不要重复已检验的表达式，也不要为了通过检验而反复微调参数。"
                    "返回 session_date=指定交易日以及本次 context.as_of；不要返回订单或 targets。")
    for attempt in range(2):
        now = instant(strategy.get_datetime())
        context = {**context, "as_of": now.isoformat()}
        result = _run_stage(strategy, "manager", task_prompt=manager_prompt, context=context,
                            output_schema=PostmarketReview)
        review = PostmarketReview.model_validate(
            strategy._record_agent("manager", result, phase="postmarket_review")).model_dump()
        if review["session_date"] != day or instant(review["as_of"]) != now:
            raise RiskError("Postmarket review session/as_of mismatch")
        invalid = _invalid_refs(review, allowed_refs)
        if not invalid:
            break
        error = (f"Postmarket adjustment refers to evidence outside the supplied review for {day} "
                 f"({', '.join(invalid)})")
        if strategy.halt_path.exists():
            return
        # Correct a copying mistake once, under the existing budgets. Never accept
        # a fuzzy match, promote model text to evidence, or repeat successful research.
        budget = getattr(strategy, "model_budget", None)
        exhausted = budget is not None and strategy.journal.counter(
            "agent_runs", strategy.get_datetime()) >= budget.max_agent_runs_per_day
        if attempt or exhausted or (hasattr(strategy, "_cost_exhausted") and
                strategy._cost_exhausted(strategy.get_datetime(), "postmarket_reference_correction")):
            raise RiskError(error)
        strategy.journal.event(strategy.get_datetime(), "postmarket_reference_retry", {
            "session_date": day, "invalid_paths": invalid, "research_completed_at": saved["completed_at"]})
        context = {**context, "reference_correction": {"invalid_paths": invalid, "previous_review": review}}
    packet = {**session, "version": (memory["version"] if memory else 0) + 1,
              "previous_version": memory["version"] if memory else None,
              "input_as_of": saved["context"]["as_of"], "completed_at": strategy.get_datetime().isoformat(),
              "models": {"researcher": saved["model"], "manager": result.model},
              "attribution": saved["context"].get("attribution"),
              "evidence_catalog": catalog, "reference_correction_attempts": attempt,
              "mock": saved["mock"] or strategy.scripted,
              "research": saved["research"], "review": review, "session": saved["context"]["session"]}
    _save_once(strategy.journal, strategy.get_datetime(), "postmarket_review", packet)
    if hasattr(strategy, "_call_outcome"):
        strategy._call_outcome(predictions=len(review.get("predictions") or []),
                               memo_changed=review.get("strategy_memo") != ((memory or {}).get("review") or {}).get(
                                   "strategy_memo"))
    lab = getattr(strategy, "factor_lab", None)
    if lab is not None and review["factor_proposals"]:
        try:
            from .factors import as_panels
            panels = as_panels(strategy._factor_panel(now))
            if not panels:
                raise RiskError("Factor history unavailable")
            testable = [p for p in review["factor_proposals"] if (p.get("universe") or "stocks") in panels]
            if len(testable) < len(review["factor_proposals"]):
                strategy.journal.event(strategy.get_datetime(), "factor_lab_unavailable", {
                    "session_date": day, "error": "no history for the cross-section of some proposals",
                    "skipped": [p["name"] for p in review["factor_proposals"] if p not in testable]})
            if testable:
                lab.test(testable, strategy.get_datetime(), day, panels, models=tuple(packet["models"].values()),
                         sectors=strategy._sectors() if hasattr(strategy, "_sectors") else None)
            strategy._factor_context_cache = None  # The next context includes these results.
        except Exception as exc:  # Evidence tooling must never cost the saved review.
            strategy.journal.event(strategy.get_datetime(), "factor_lab_unavailable", {
                "session_date": day, "error": str(exc) if isinstance(exc, RiskError) else type(exc).__name__})
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
