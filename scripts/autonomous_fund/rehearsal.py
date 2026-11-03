"""Pre-open dress rehearsal: one complete live decision on a copy of the account, stopped before any order.

Run while the market is closed (`bash scripts/fund-rehearse.sh`) to find
failures before the session instead of at the open. It copies the configured
local account (consistent SQLite snapshot plus the SEC cache) into
`artifacts/fund-rehearsal/<UTC time>/`, builds the same strategy with order
matching disabled and every submission intercepted, then runs and times each
stage the runner uses: startup and pre-open research, account snapshot, the
risk context, factor research, the earnings calendar, FRED, scorecards, the
critic and the manager with the real models, decision validation and the
quantitative risk construction of the AI's targets.

It never touches the real account directory, STOP, approvals or Telegram (the
copy's notices stay in the copy), and no order reaches any broker. Outside the
session, order planning stops at the quote-freshness check, so planned order
sizes are checked by the runner at the open; the risk construction of the
targets is verified here from real completed history.

Stage thresholds mirror the independent monitor: required work (snapshot and
risk context) over 120 s fails, because the monitor stops a runner after 180 s
without progress; a model call over the 900 s agent limit fails; optional data
over the 45 s per-iteration budget is reported as spread over several iterations.
"""
import json
import os
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .risk import RiskError

REQUIRED_LIMIT_SECONDS = 120
AGENT_LIMIT_SECONDS = 900
KEEP = 5


class Stages:
    def __init__(self, clock=time.monotonic):
        self.clock, self.rows = clock, []

    def run(self, name, function, *, kind="optional", judge=None):
        started = self.clock()
        row = {"stage": name, "kind": kind}
        try:
            value = function()
            row["status"], row["detail"] = judge(value) if judge else ("ok", "")
        except Exception as exc:
            value = None
            row["status"] = "fail"
            row["detail"] = (str(exc)[:300] if isinstance(exc, RiskError) else type(exc).__name__)
            row["where"] = _where(exc)
        row["seconds"] = round(self.clock() - started, 1)
        if row["status"] == "ok" and kind == "required" and row["seconds"] > REQUIRED_LIMIT_SECONDS:
            row["status"] = "fail"
            row["detail"] = f"took {row['seconds']} s; the monitor stops a runner after 180 s without progress"
        elif row["status"] == "ok" and kind == "model" and row["seconds"] > AGENT_LIMIT_SECONDS:
            row["status"] = "fail"
            row["detail"] = f"model call took {row['seconds']} s, over the monitor's {AGENT_LIMIT_SECONDS} s limit"
        elif row["status"] == "ok" and kind == "optional" and row["seconds"] > 45:
            row["detail"] = (row["detail"] + "; " if row["detail"] else "") + (
                f"{row['seconds']} s: in a trading iteration this is spread over several 45 s budgets")
        self.rows.append(row)
        return value


def _where(exc):
    """The nearest non-standard-library frame, without paths, messages or locals."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    for frame in reversed(frames):
        if "site-packages" in frame.filename or "/scripts/" in frame.filename or "lumibot" in frame.filename:
            parts = Path(frame.filename).with_suffix("").parts
            module = ".".join(parts[-3:])
            return f"{module}.{frame.name}:{frame.lineno}"
    return None


def _context_judge(value):
    if not isinstance(value, dict):
        return "ok", "disabled"
    if value.get("unavailable"):
        return "fail", str(value["unavailable"])[:200]
    return "ok", ""


def _desk_judge(view):
    """Running with fresh data is ok; a stopped desk is a warning (the runner then fetches its own data)."""
    if not view.get("running"):
        return "warn", "disabled" if view.get("disabled") else "desk not running"
    ages = [f"{name} {item['updated_minutes_ago']:g} min" + (" (error)" if item.get("error") else "")
            for name, item in sorted(view["datasets"].items()) if item.get("updated_minutes_ago") is not None]
    return "ok", "running; " + (", ".join(ages) or "no dataset finished yet")


def _snapshot_judge(value):
    verdict, detail = _context_judge(value)
    if verdict == "ok" and isinstance(value, dict):
        missing = value.get("no_history") or []
        detail = f"{len(value.get('symbols') or {})} symbols" + (f", no history: {', '.join(missing)}" if missing else "")
    return verdict, detail


def _last_sequence(book):
    """The copy's newest event: everything after it was written by this rehearsal."""
    import sqlite3
    from contextlib import closing
    path = Path(book) / "state.sqlite3"
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT coalesce(max(sequence), 0) FROM events").fetchone()[0]


FAILED_STAGES = {"postmarket_review_error", "premarket_research_error"}


def answer_problems(journal, after=0):
    """Model answers this rehearsal lost or degraded: schema mismatches, unusable critiques, failed stages."""
    problems, located = [], set()
    rows = [r for r in journal.all_events() if r["sequence"] > after]
    for row in rows:
        body = row["body"] if isinstance(row["body"], dict) else {}
        if row["kind"] == "agent" and body.get("parse_error"):
            problems.append(f"{body.get('name')}: answer did not match its schema ({str(body['parse_error'])[:160]})")
        elif row["kind"] == "critic_unavailable":
            problems.append(f"critic unavailable: {body.get('error')}")
        elif row["kind"] == "operation_error" and body.get("notification") in FAILED_STAGES:
            frames = body.get("frames") or []
            where = next((f for f in reversed(frames) if "autonomous_fund" in str(f.get("module", ""))),
                         frames[-1] if frames else {})
            located.add(body["notification"])
            problems.append(f"{body['notification']}: {body.get('error')} at {where.get('module', '?')}."
                            f"{where.get('function', '?')}:{where.get('line', '?')}")
    for row in rows:  # A RiskError has no diagnostic event: its notification carries the reason.
        body = row["body"] if isinstance(row["body"], dict) else {}
        if row["kind"] == "notification" and body.get("kind") in FAILED_STAGES - located:
            problems.append(f"{body['kind']}: {str(body.get('details'))[:160]}")
    return problems


def agent_calls(journal, after=0):
    """Each model call's duration, and its tokens when the provider reported them, from the copy's events.

    The `agent` event recorded right after a finished call carries its usage; a
    reused cached answer used no tokens and is marked cache_hit.
    """
    from .report import token_counts
    from .risk import instant
    started, calls, awaiting = {}, [], {}
    for row in journal.all_events():
        if row["sequence"] <= after:
            continue  # The account's own history, not this run.
        body = row["body"]
        if row["kind"] == "agent_call_started":
            started[body["id"]] = (body["name"], instant(row["at"]))
        elif row["kind"] == "agent_call_finished" and body.get("id") in started:
            name, began = started.pop(body["id"])
            calls.append({"name": name, "seconds": round((instant(row["at"]) - began).total_seconds(), 1)})
            awaiting.setdefault(name, []).append(calls[-1])
        elif row["kind"] == "agent" and awaiting.get(body.get("name")):
            call = awaiting[body["name"]].pop(0)
            if str(body.get("model") or "").startswith("scripted/"):
                call["scripted"] = True  # The free rehearsal's scripted AI: no inference, no tokens.
            elif body.get("cache_hit"):
                call["cache_hit"] = True
            elif body.get("usage"):
                try:
                    call.update({k: v for k, v in token_counts(body["usage"]).items() if k != "cached_input_tokens"})
                except (TypeError, ValueError, AttributeError):
                    pass  # An unfamiliar usage shape: the duration is still reported.
    calls += [{"name": name, "seconds": None, "unfinished": True} for name, _ in started.values()]
    return calls


def _call_text(call):
    if call.get("seconds") is None:
        return f"{call['name']} 未结束"
    tokens = ("（模拟 AI）" if call.get("scripted") else
              f"（输入 {call['input_tokens']} / 输出 {call['output_tokens']} tokens）"
              if "input_tokens" in call else "（缓存命中）" if call.get("cache_hit") else "")
    return f"{call['name']} {call['seconds']} 秒{tokens}"


def latest(root=Path("artifacts/fund-rehearsal")):
    """The newest saved report, or None."""
    reports = sorted(Path(root).glob("*/rehearsal.json")) if Path(root).is_dir() else []
    return json.loads(reports[-1].read_text(encoding="utf-8")) if reports else None


def _prune(root, keep):
    runs = sorted(p for p in Path(root).iterdir() if p.is_dir() and len(p.name) == 15 and p.name[8] == "-")
    for old in runs[:-keep]:
        shutil.rmtree(old)


def rehearse(account_dir, config, *, models=True, runtime=None, source=None, now=None,
             root=Path("artifacts/fund-rehearsal"), keep=KEEP, clock=time.monotonic, live_data=True):
    """Run the rehearsal; returns the report (also written as rehearsal.json in the copy).

    runtime: a scripted AI (the free default from the CLI) answers every model call
    without inference, so the whole flow runs on real data at no model cost; None
    uses the real models. models=False runs the data stages only. live_data=False
    keeps a scripted run off the network (tests).
    """
    ai = "none" if not models else ("scripted" if runtime is not None else "real")
    from . import deploy, stops
    from .local_broker import LocalPaperBroker
    from .market_data import YahooRealtimeData
    from .risk import Limits, number
    from .strategy import AutonomousFund, Mandate, open_journal
    account_dir, root = Path(account_dir), Path(root)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    scratch = root / stamp
    book = scratch / "book"
    stages = Stages(clock)
    # The memory directory the real runner uses must be writable (the 2026-11-02 EROFS).
    memory = Path(os.environ.get("LUMIBOT_MEMORY_DIR") or account_dir / "memory")

    def writable():
        memory.mkdir(parents=True, exist_ok=True)
        probe = memory / ".rehearsal-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    stages.run("memory directory writable", writable, kind="required")

    def caches():
        # Every directory a LumiBot research tool writes to: the service makes the rest read-only.
        problems = []
        for name in ("LUMIBOT_CACHE_FOLDER", "LUMIBOT_SEC_CACHE_DIR", "LUMIBOT_FRED_CACHE_DIR",
                     "LUMIBOT_TRADING_DAYS_CACHE_DIR"):
            value = os.environ.get(name)
            if not value:
                problems.append(f"{name} unset (defaults to the read-only home directory)")
                continue
            try:
                folder = Path(value)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / ".rehearsal-write-test").write_text("ok", encoding="utf-8")
                (folder / ".rehearsal-write-test").unlink()
            except OSError as exc:
                problems.append(f"{name}: {exc.strerror or type(exc).__name__}")
        if problems:
            raise RiskError("; ".join(problems))
    stages.run("research cache directories writable", caches, kind="required")

    def copy():
        root.mkdir(parents=True, exist_ok=True)
        if (account_dir / "state.sqlite3").is_file():
            deploy.backup(account_dir, book)
            # The SEC cache and what the data desk prepared, so the copy reads them like the runner.
            for name in ("sec", "daily"):
                if (account_dir / "cache" / name).is_dir():
                    shutil.copytree(account_dir / "cache" / name, book / "cache" / name)
            desk = account_dir / "cache" / "desk"
            if desk.is_dir():
                try:  # The real desk's live status (only ever read here), not a copy that ages during the run.
                    os.symlink(desk.resolve(), book / "cache" / "desk", target_is_directory=True)
                except OSError:
                    shutil.copytree(desk, book / "cache" / "desk")
            stops.clear(book)  # The copy's own STOP; orders are intercepted below.
            return "copied"
        book.mkdir(parents=True)
        return "fresh"
    origin = stages.run("copy the account", copy, kind="required", judge=lambda v: ("ok", v or ""))
    if origin is None:
        return _finish(scratch, stages, root, keep, extra={"ai": ai})
    from .datadesk import DeskStatus
    stages.run("data desk", lambda: DeskStatus(account_dir).summary(), judge=_desk_judge)
    # Only what happens from here on is this rehearsal's work (the copy holds the account's history).
    start_sequence = _last_sequence(book)
    # The copy's own native memory, so the rehearsal never writes into the real one.
    os.environ["LUMIBOT_MEMORY_DIR"] = str((scratch / "memory").resolve())
    rehearsal_config = config
    submitted = []

    def build():
        journal = open_journal(book, "local", "local-virtual-account", rehearsal_config)
        data = source or YahooRealtimeData(tape=None)
        broker = LocalPaperBroker(data, journal, Limits(**rehearsal_config["limits"]), matching_enabled=False,
                                  connect_stream=False,
                                  starting_cash=Mandate.from_config(rehearsal_config.get("mandate")).local_account_usd)
        strategy = AutonomousFund(broker=broker, name="RehearsalFund", parameters={
            "config": rehearsal_config, "run_dir": str(book), "mode": "local",
            "account_binding": "local-virtual-account", "runtime": runtime, "observe_only": not models,
            "live_data": live_data})
        # Never an order: every submission path only records what it would have sent.
        strategy._submit = lambda intent, now, decision_id, **kw: submitted.append(intent)
        strategy._execute = lambda intent, now, account=None, prices=None: submitted.append(intent)
        strategy._executor._initialize()  # Startup, including the pre-open research.
        strategy.validation = None  # No shadow call: one decision is enough to rehearse.
        if strategy.news is not None:
            strategy.news.standby = lambda: True  # Reads the copied news; never polls from the rehearsal.
        strategy.journal.set_meta("critique_cache", None)  # Rehearse a real critic call, never a reused one.
        return strategy
    strategy = stages.run("start the strategy (startup and pre-open research)", build, kind="model")
    if strategy is None:
        return _finish(scratch, stages, root, keep, extra={"ai": ai})
    research = next((r["body"] for r in reversed(strategy.journal.all_events())
                     if r["kind"] == "premarket_research"), None)
    stages.rows[-1]["detail"] = ("pre-open research " + (research.get("phase", "saved") if research else "not run")
                                 + ("; observe-only, no model calls" if not models else ""))
    at = strategy.get_datetime()
    account = stages.run("account snapshot", strategy._snapshot, kind="required")
    if account is None:
        return _finish(scratch, stages, root, keep, extra={"ai": ai})
    stages.run("risk context (history, volatility, stress windows)", lambda: strategy._risk_context(account, at),
               kind="required", judge=_context_judge)
    stages.run("market snapshot", lambda: strategy._market_snapshot(at), judge=_snapshot_judge)
    stages.run("factor research (history, research universe, SEC)", lambda: strategy._factor_context(at),
               judge=_context_judge)
    stages.run("earnings calendar", lambda: strategy._earnings_caps(at),
               judge=lambda v: ("ok", f"{len(v['caps'])} capped, {len(v['unknown'])} unknown"))
    stages.run("FRED macro context", lambda: strategy._macro_context(at), judge=_context_judge)
    stages.run("scorecards", lambda: strategy._scorecard_summary(at), judge=_context_judge)
    if not models:
        stages.rows.append({"stage": "critic and manager decision", "kind": "model", "status": "skipped",
                            "detail": "--no-models", "seconds": 0.0})
        return _finish(scratch, stages, root, keep, extra={"ai": ai})
    strategy.next_review = None

    def decide():
        try:
            strategy._decide(account, strategy.get_datetime(), False)
            return "planned"
        except RiskError as exc:
            if "quote" in str(exc).lower() or "market session" in str(exc).lower():
                return "quotes"  # Outside the session: order sizes are checked at the open.
            raise

    def judge_decision(value):
        names = [r["body"]["name"] for r in strategy.journal.all_events() if r["kind"] == "agent"]
        detail = "models: " + ", ".join(names[-3:])
        if value == "quotes":
            detail += "; order sizes need live quotes and are planned at the open"
        if not any(r["kind"] == "decision_validated" for r in strategy.journal.all_events()):
            return "fail", detail + "; no validated decision"
        return "ok", detail
    stages.run("critic and manager decision (real models)" if ai == "real" else
               "critic and manager decision (scripted AI)", decide, kind="model", judge=judge_decision)
    decision = strategy.last_decision
    # Paid answers that were lost or degraded (a schema mismatch, an unusable critique, a failed review).
    stages.run("model answers", lambda: answer_problems(strategy.journal, start_sequence),
               judge=lambda found: ("warn", "; ".join(found)[:800]) if found else ("ok", "every answer usable"))

    def construction():
        from .quant import construct, drawdown_scale
        if not decision:
            raise RiskError("No validated decision to construct")
        weights = {t["symbol"]: number(t["weight"]) for t in decision["targets"] if number(t["weight"]) > 0}
        estimate = strategy._risk_estimate(set(weights) | set(account.positions), at)
        session = strategy._session(at, account)
        scale = drawdown_scale(session["drawdown"], strategy.quant, strategy.limits.drawdown)
        result, overlay = construct(weights, {}, estimate, strategy.quant, strategy.limits,
                                    strategy.limits.max_exposure * scale)
        return {"targets": {s: str(w) for s, w in weights.items()},
                "constructed": {s: str(w) for s, w in result.items()}, "binding": overlay["binding"],
                "worst_scenario_loss": overlay["constructed_metrics"]["worst_scenario_loss"]}
    built = stages.run("risk construction of the targets", construction, kind="required",
                       judge=lambda v: ("ok", f"binding: {', '.join(v['binding']) or 'none'}"))
    report = _finish(scratch, stages, root, keep, extra={"ai": ai, "decision": decision, "construction": built,
                                                         "orders_intercepted": [str(i) for i in submitted],
                                                         "agent_calls": agent_calls(strategy.journal,
                                                                                    after=start_sequence)})
    return report


def _finish(scratch, stages, root, keep, extra=None):
    from .strategy import MONITOR_AGENT_SECONDS
    rows = stages.rows
    slow = [c for c in (extra or {}).get("agent_calls", []) if (c.get("seconds") or 0) >= MONITOR_AGENT_SECONDS]
    verdict = "fail" if slow or any(r["status"] == "fail" for r in rows) else "ready"
    report = {"verdict": verdict, "at": datetime.now(timezone.utc).isoformat(), "copy": str(scratch),
              "stages": rows, **(extra or {}),
              "note": "A copy of the account; no order, STOP, approval or Telegram message of the real account "
                      "was touched. Order sizes need live quotes and are planned by the runner at the open."}
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "rehearsal.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                                            encoding="utf-8")
    try:
        _prune(root, keep)
    except OSError:
        pass
    return report


LABELS = {
    "memory directory writable": "记忆库目录可写（昨天的 EROFS）",
    "research cache directories writable": "研究工具缓存目录可写（SEC／FRED／交易日历）",
    "copy the account": "复制账户",
    "start the strategy (startup and pre-open research)": "启动策略（含盘后复盘和盘前研究）",
    "account snapshot": "账户快照",
    "risk context (history, volatility, stress windows)": "风控数据（历史、波动率、危机情景）",
    "data desk": "数据员（常驻数据服务）",
    "model answers": "模型回答是否可用（格式不符、反方审查失败、复盘失败）",
    "market snapshot": "行情快照（收益、波动、beta、均线、成交额）",
    "factor research (history, research universe, SEC)": "因子研究数据（研究股票池、SEC 财报）",
    "earnings calendar": "财报日历",
    "FRED macro context": "FRED 宏观数据",
    "scorecards": "记分卡",
    "critic and manager decision (real models)": "反方审查＋基金经理决策（真实模型）",
    "critic and manager decision (scripted AI)": "反方审查＋基金经理决策（模拟 AI，验证流程）",
    "critic and manager decision": "反方审查＋基金经理决策",
    "risk construction of the targets": "目标组合的量化风控",
}
DETAILS = {"copied": "已复制真实账本", "fresh": "没有账本，用新账户", "disabled": "未启用",
           "desk not running": "没有运行：交易进程会自己拉数据，但盘中更慢；运行 bash scripts/fund-restart.sh -y 启动它",
           "every answer usable": "每个回答都可用",
           "pre-open research not run": "盘前研究未运行（已运行过或在开盘前一小时窗口外）",
           "--no-models": "--no-models，未调用模型"}


# The report keeps English details (machine-readable); the operator's text shows them in Chinese.
PHRASES = (("order sizes need live quotes and are planned at the open", "下单数量需要实时报价，开盘后由交易进程计算"),
           ("pre-open research closed_startup", "盘前研究：休市时启动后已运行"),
           ("pre-open research before_market_opens", "盘前研究：开盘前已运行"),
           ("pre-open research not run", "盘前研究未运行（当天已做过）"), ("observe-only, no model calls", "只观察，不调用模型"),
           ("models: ", "模型："), ("running; ", "运行中；"), ("binding: none", "约束：无"), ("binding: ", "约束："),
           (" capped", " 只财报前受限"), (" unknown", " 只日期未知"), (" symbols", " 个标的"),
           ("no history: ", "无历史："), (" min", " 分钟"), ("(error)", "（出错）"),
           ("calendar ", "经济日历 "), ("earnings ", "财报日历 "), ("global ", "全球市场 "), ("macro ", "宏观 "),
           ("news ", "资讯 "), ("sec ", "SEC 财报 "), ("intraday ", "盘中行情 "), ("premarket ", "盘前行情 "),
           ("snapshot ", "行情快照 "), ("bars ", "日线 "))


def _zh(detail):
    """Known phrases in Chinese; whole words only, so an error message such as "minute" stays intact."""
    import re
    text_ = str(detail or "")
    for english, chinese in PHRASES:
        text_ = re.sub(re.escape(english) + (r"\b" if english[-1].isalpha() else ""), chinese, text_)
    return text_


def text(report):
    marks = {"ok": "通过", "fail": "失败", "skipped": "跳过", "warn": "注意"}
    mode = {"scripted": "模拟 AI：完整流程、真实数据、不调用模型、0 模型费用",
            "real": "真实模型：完整流程，会产生模型费用", "none": "只检查数据，不调用模型"}.get(report.get("ai"))
    lines = [f"盘前彩排：{'全部通过' if report['verdict'] == 'ready' else '有失败项'}（账本副本：{report['copy']}）"]
    lines += [f"方式：{mode}", ""] if mode else [""]
    for row in report["stages"]:
        where = f"；位置 {row['where']}" if row.get("where") else ""
        detail = _zh(DETAILS.get(row.get("detail"), row.get("detail")))
        lines.append(f"[{marks.get(row['status'], row['status'])}] {LABELS.get(row['stage'], row['stage'])}："
                     f"{row['seconds']} 秒" + (f"；{detail}" if detail else "") + where)
    calls = report.get("agent_calls") or []
    if calls:
        lines += ["", "每次模型调用耗时（监控在单次调用超过 900 秒时会停止交易）："
                  + "、".join(_call_text(c) for c in calls)]
    built = report.get("construction")
    if built:
        lines += ["", "AI 目标 → 风控后：" + json.dumps(built["targets"], ensure_ascii=False) + " → "
                  + json.dumps(built["constructed"], ensure_ascii=False)]
    lines += ["", "说明：只用账户副本，真实账户的订单、STOP、审批和 Telegram 都没有被触碰；"
              "具体下单数量需要实时报价，开盘后由交易进程计算。"]
    return "\n".join(lines)
