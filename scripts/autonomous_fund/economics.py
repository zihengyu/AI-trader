"""Read-only economic diagnostics. Scenarios are estimates, never executable portfolios."""
import json
import math
import sqlite3
from contextlib import closing as closing_connection
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .risk import RiskError, instant
from .scorecard import (HORIZONS, ZONE, _exchange_sessions, _forward, beta, beta_cutoff,
                        decision_start, completed_sessions)


def number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean financial input")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite financial input")
    return result


def weights(body):
    result, seen = {}, set()
    for row in body.get("targets", []):
        symbol, weight = row["symbol"], number(row["weight"])
        if symbol in seen or not 0 <= weight <= 1:
            raise ValueError("Invalid target weights")
        seen.add(symbol)
        if weight:
            result[symbol] = weight
    if sum(result.values()) > 1 + 1e-9:
        raise ValueError("Targets exceed unlevered capital")
    return result


def history_sessions(closes):
    """Use exchange sessions, so a gap in every saved series is still a missing trading day."""
    last = max((day for rows in closes.values() for day, _ in rows), default=None)
    cutoff = str(datetime.fromisoformat(last).date() + timedelta(days=1)) if last else None
    return completed_sessions(closes, cutoff) if cutoff else ()


def decision_comparison(decisions, closes, *, bases=None, calls=None, sessions=None, cost_bps=10):
    """New vs prior AI targets at a common future close; cash earns zero in BOTH arms.

    Trading friction is a declared per-side scenario rate, not recorded fees.
    Model costs are subtracted only for the exact recorded decision-call scope.
    No stops, limits, overlay or actual fills are simulated. Legacy missing inputs stay unknown.
    """
    cost_bps = number(cost_bps)
    if cost_bps < 0:
        raise ValueError("Negative trading friction")
    bases, calls = bases or {}, calls or {}
    calendar = sessions if sessions is not None else history_sessions(closes)
    previous, rows = None, []
    for at, body in sorted(decisions, key=lambda r: instant(r[0])):
        chosen = weights(body)
        basis = bases.get(body.get("decision_id")) or {}
        if "previous_decision" in basis:
            prior = weights(basis["previous_decision"]) if isinstance(basis["previous_decision"], dict) else None
        else:
            prior = previous
        previous = chosen
        day = instant(at).astimezone(ZONE).date().isoformat()
        start, pending = decision_start(at, day, calendar)
        through = beta_cutoff(at)
        known = [d for d in calendar if d <= through]
        beta_through = known[-1] if known else None
        row = {"decision_id": body.get("decision_id"), "decided_at": str(at), "start_day": start,
               "beta_through": beta_through, "prompt_version": body.get("prompt_version") or "unversioned",
               "horizons": {}}
        linked = basis.get("evaluation_call_ids")
        prices = [calls.get(key) for key in linked] if isinstance(linked, list) and linked else None
        cost = (sum(prices) if prices is not None and len(set(linked)) == len(linked)
                and all(p is not None and number(p) >= 0 for p in prices) else None)
        equity = basis.get("account_equity")
        equity = number(equity) if equity is not None else None
        if equity is not None and equity <= 0:
            equity = None
        row.update(review_cost_usd=cost, cost_scope=basis.get("evaluation_cost_scope"), account_equity=equity)
        turnover = sum(abs(chosen.get(s, 0) - (prior or {}).get(s, 0)) for s in chosen.keys() | (prior or {}).keys())
        row["turnover"] = turnover if prior is not None else None
        row["estimated_trading_cost"] = turnover * cost_bps / 10000 if prior is not None else None
        betas = {s: (1.0 if s == "SPY" else beta(closes.get(s), closes.get("SPY"), beta_through,
                                              sessions=calendar) if beta_through else None) for s in chosen}
        row["beta_missing"] = sorted(s for s, value in betas.items() if value is None)
        exposure = sum(chosen.values())
        portfolio_beta = sum(chosen[s] * betas[s] for s in chosen) if not row["beta_missing"] else None
        row.update(target_exposure=exposure, target_beta=portfolio_beta)
        for horizon in HORIZONS:
            result = {"status": "unavailable_previous_plan" if prior is None else "pending" if pending else "unavailable"}
            row["horizons"][str(horizon)] = result
            if prior is None or start is None:
                continue
            needed = chosen.keys() | prior.keys()
            paths = {s: _forward(closes.get(s), start, horizon, sessions=calendar) for s in needed}
            market = _forward(closes.get("SPY"), start, horizon, sessions=calendar)
            if any(p is None for p in paths.values()):
                continue
            if calendar.index(start) + horizon >= len(calendar) or any(p == "pending" for p in paths.values()):
                result["status"] = "pending"
                continue
            returns = {s: p[1] / p[0] - 1 for s, p in paths.items()}
            new = sum(w * returns[s] for s, w in chosen.items())
            hold = sum(w * returns[s] for s, w in prior.items())
            friction = row["estimated_trading_cost"]
            benefit = new - hold - friction
            base = market[1] / market[0] - 1 if isinstance(market, tuple) else None
            matched = (0.0 if portfolio_beta == 0 else
                       portfolio_beta * base if portfolio_beta is not None and base is not None else None)
            result.update(status="observed", end_day=calendar[calendar.index(start) + horizon],
                          new_target_return=new, prior_target_return=hold,
                          change_benefit_after_estimated_friction=benefit,
                          change_benefit_usd=benefit * equity if equity is not None else None,
                          benefit_after_linked_review_cost=(benefit - cost / equity)
                              if cost is not None and equity is not None else None,
                          ex_ante_beta_reference=matched,
                          beta_adjusted_return_after_estimated_friction=new - friction - matched if matched is not None else None)
        rows.append(row)
    summary = {}
    for horizon in map(str, HORIZONS):
        observed = [(r, r["horizons"][horizon]) for r in rows if r["horizons"][horizon]["status"] == "observed"]
        independent, end = [], ""
        for row, item in observed:
            if row["start_day"] >= end:
                independent.append(item)
                end = item["end_day"]
        values = [p["change_benefit_after_estimated_friction"] for _, p in observed]
        costed = [p["benefit_after_linked_review_cost"] for _, p in observed if p["benefit_after_linked_review_cost"] is not None]
        summary[horizon] = {"observed": len(values), "non_overlapping": len(independent),
            "mean_change_benefit": sum(values) / len(values) if values else None,
            "positive_share": sum(v > 0 for v in values) / len(values) if values else None,
            "with_linked_review_cost": len(costed), "mean_after_linked_review_cost": sum(costed) / len(costed) if costed else None}
    return {"by_horizon": summary, "recent": rows[-20:], "decisions": len(rows), "cost_bps_per_side": cost_bps,
            "meaning": "Target-price scenarios, common first close after acceptance; no actual execution, dividends, "
                       "cash yield, stops or risk overlay. Linked review costs cover manager, corrections and fresh "
                       "critique only; other operating costs excluded. Overlapping outcomes are not independent; "
                       "neither positive outcomes nor a target change prove causal model value."}


def opening_reference(pairs, accounts, closes, marks, actions, *, cash_symbol=None, cash_policies=None):
    """Freeze each interval's reference beta at its starting closing book, not realized end risk.

    Cash is a declared sweep-ETF proxy, or zero-return USD if no proxy is configured.
    Benchmark marks/actions use the same recorded total-return convention as fixed references.
    """
    from .performance import component_return
    rows = []
    calendar = history_sessions(closes)
    for pair in pairs:
        a, b = pair["start_day"], pair["end_day"]
        row = {"start_day": a, "end_day": b, "status": "unavailable", "beta_through": a}
        rows.append(row)
        book = accounts.get(a)
        if not book:
            row["reason"] = "missing_starting_closing_book"
            continue
        equity = number(book["equity"])
        if equity <= 0:
            row["reason"] = "nonpositive_opening_equity"
            continue
        proxy_known, proxy = cash_policies.get(a, (False, None)) if cash_policies is not None else (True, cash_symbol)
        values, betas, missing = {}, {}, []
        for symbol, quantity in (book.get("positions") or {}).items():
            quantity = number(quantity)
            if quantity == 0:
                continue
            price = (book.get("position_prices") or {}).get(symbol)
            estimate = 1.0 if symbol == "SPY" else beta(closes.get(symbol), closes.get("SPY"), a, sessions=calendar)
            try:
                price = number(price)
            except (ValueError, TypeError):
                price = None
            if price is None or estimate is None or quantity < 0 or price <= 0:
                missing.append(symbol)
                continue
            values[symbol] = quantity * price / equity
            betas[symbol] = estimate
        if missing:
            row.update(reason="missing_opening_marks_or_beta", missing=sorted(missing))
            continue
        exposure, risk = sum(values.values()), sum(w * betas[s] for s, w in values.items())
        # Floating-point OLS can put an exact unit beta a few ulps beyond one.
        if math.isclose(risk, 1, rel_tol=0, abs_tol=1e-12):
            risk = 1.0
        elif math.isclose(risk, 0, rel_tol=0, abs_tol=1e-12):
            risk = 0.0
        row.update(opening_exposure=exposure, opening_beta=risk)
        if exposure > 1 + 1e-12 or not 0 <= risk <= 1:
            row["reason"] = "beta_outside_unlevered_reference"
            continue
        if not proxy_known and risk < 1:
            row["reason"] = "missing_historical_cash_policy"
            continue
        market = component_return(marks.get("SPY", {}), actions.get("SPY", []), a, b) if risk else 0.0
        cash = component_return(marks.get(proxy, {}), actions.get(proxy, []), a, b) if proxy and risk < 1 else 0.0
        if market is None or cash is None:
            row["reason"] = "missing_reference_marks"
            continue
        row.update(status="observed", cash_proxy=proxy or "USD_zero_return",
                   reference_return=risk * market + (1 - risk) * cash,
                   account_return=pair["return"], gross_difference=pair["return"] - risk * market - (1 - risk) * cash)
    complete = bool(rows) and all(r["status"] == "observed" for r in rows)
    mine = math.prod(1 + p["return"] for p in pairs) - 1 if pairs else None
    reference = math.prod(1 + r["reference_return"] for r in rows) - 1 if complete else None
    return {"status": "observed" if complete else "incomplete", "intervals": len(rows),
            "observed": sum(r["status"] == "observed" for r in rows), "reference_return": reference,
            "account_return": mine, "gross_difference": mine - reference if reference is not None else None,
            "recent": rows[-20:],
            "meaning": "Prior closing holdings and betas only, frozen for the next interval. Recorded benchmark "
                       "marks plus recorded actions, not certified exchange closing prices. Frictionless reference; "
                       "intraday exposure changes, selection, costs and cash carry can all affect the difference. "
                       "Unknown betas are not assumed to equal one. Fixed benchmarks are retained separately."}


def saved_closes(root, db, now, *, diagnostics=None):
    """Read recorded caches only; no cache constructors, downloads or writes."""
    from .session_calendar import completed_daily_cutoff
    cutoff = str(completed_daily_cutoff(now))
    day = instant(now).astimezone(ZONE).date().isoformat()
    series = {}
    cache = Path(root) / "cache" / "daily"
    if cache.is_dir():
        for folder in sorted(cache.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name > day or folder.is_symlink():
                continue
            for path in sorted((folder / "bars").glob("*.json")):
                if path.stem in series or path.is_symlink() or not path.resolve().is_relative_to(cache.resolve()):
                    continue
                try:
                    packet = json.loads(path.read_text())
                    if not isinstance(packet, dict) or not isinstance(packet.get("rows"), list):
                        raise ValueError("Invalid daily-history packet")
                    rows, rejected = [], 0
                    for row in packet["rows"]:
                        try:
                            if not isinstance(row, (list, tuple)) or len(row) < 2:
                                raise ValueError("Invalid daily-history row")
                            session = datetime.fromisoformat(str(row[0])).date().isoformat()
                            if str(row[0]) != session:
                                raise ValueError("Daily-history row needs a session date")
                            close = number(row[1])
                            if close <= 0:
                                raise ValueError("Nonpositive historical close")
                        except (ValueError, TypeError):
                            rejected += 1
                            continue
                        if session < cutoff:
                            rows.append((session, close))
                    if rejected and diagnostics is not None:
                        diagnostics.append({"symbol": path.stem, "cache_day": folder.name,
                                            "reason": "invalid_daily_rows", "rejected_rows": rejected})
                    if rows:
                        series[path.stem] = sorted(dict(rows).items())
                except (OSError, ValueError, TypeError, KeyError, IndexError):
                    if diagnostics is not None:
                        diagnostics.append({"symbol": path.stem, "cache_day": folder.name,
                                            "reason": "unreadable_daily_history"})
                    continue
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_universe_snapshots'").fetchone():
        snapshots = {}
        for role, symbol, day, close, recorded in db.execute("SELECT role,symbol,day,close,recorded_at FROM fund_universe_snapshots "
                "WHERE day<? AND julianday(recorded_at)<=julianday(?) ORDER BY day", (cutoff, instant(now).isoformat())):
            try:
                close = number(close)
            except (TypeError, ValueError):
                continue
            if instant(recorded) <= instant(now) and symbol not in series and close > 0:
                snapshots.setdefault((role, symbol), {})[day] = close
        # Separate roles may hold different provider adjustment vintages. Choose
        # one by coverage (then stable role name), never splice their share bases.
        for symbol in {s for _, s in snapshots}:
            role = max((r for r, s in snapshots if s == symbol), key=lambda r: (len(snapshots[r, symbol]), r))
            series[symbol] = sorted(snapshots[role, symbol].items())
    return series


def assess(root, now=None):
    """A snapshot-consistent, read-only report from this account's journal and saved daily history."""
    from .evaluation import nav_marks, event_bodies, session_benchmarks
    from .session_calendar import completed_daily_cutoff
    from .performance import PerformanceSettings
    from .scoreboard import compute, cost_ledger, _cost
    from .costs import scripted
    from .validation import session_pairs
    now = instant(now or datetime.now(timezone.utc))
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        raise RiskError("No journal at the selected account directory")
    with closing_connection(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        events = [(seq, at, kind, json.loads(body)) for seq, at, kind, body in db.execute(
            "SELECT sequence,at,kind,body FROM events WHERE kind IN ('decision_validated','decision_basis','startup',"
            "'benchmark_action','capital_flow','operator_reserve_adjusted') AND julianday(at)<=julianday(?) ORDER BY sequence",
            (now.isoformat(),)) if instant(at) <= now]
        calls = {}
        for at, key, cost, mock, hit, model in db.execute("SELECT at,json_extract(body,'$.call_id'),json_extract(body,'$.cost_usd'),"
            "json_extract(body,'$.mock'),json_extract(body,'$.cache_hit'),json_extract(body,'$.model') FROM events WHERE kind='agent' "
            "AND julianday(at)<=julianday(?) ORDER BY sequence", (now.isoformat(),)):
            if instant(at) <= now and key:
                calls[key] = 0.0 if scripted({"mock": mock, "model": model}) or hit else _cost(cost)
        history_issues = []
        closes = saved_closes(root, db, now, diagnostics=history_issues)
        candidates = {}
        for sequence, at, _, equity, close, _ in nav_marks(db, until=now):
            if close and instant(close) <= now and equity is not None:
                candidates[str(instant(close).astimezone(ZONE).date())] = (sequence, close)
        packets = event_bodies(db, [seq for seq, _ in candidates.values()])
        # Use historical receipts, not a current book audit that can contain later events.
        closing = {d: (seq, at) for d, (seq, at) in candidates.items()
                   if (packets[seq].get("ledger_audit") or {}).get("status") != "failed"
                   and (packets[seq].get("valuation") or {}).get("book_existed_at_close", True)}
        timeline = [(seq, at, kind, body) for seq, at, kind, body in events
                    if kind in {"capital_flow", "operator_reserve_adjusted"}]
        timeline += [(seq, close, "account", packets[seq]) for seq, close in closing.values()]
        marks = session_benchmarks(db, [(seq, closed, packets[seq]) for seq, closed in closing.values()], until=now)
        timeline += [(closing[day][0], closing[day][1], "benchmark", prices) for day, prices in marks.items()]
        if closing:
            sessions = {datetime.fromisoformat(d).date() for d in _exchange_sessions(min(closing), str(completed_daily_cutoff(now)))}
            # Unit scales follow journal sequence, including flows recorded at the same timestamp.
            pairs, market_marks = session_pairs([(at, k, b) for _, at, k, b in sorted(timeline, key=lambda r: r[0])], sessions, ZONE)
        else:
            pairs, market_marks = [], {}
        paid, data = cost_ledger(db, now.isoformat())
        # No data-accrual receipt is not evidence that the subscription was free.
        data += [(pair["end"], None) for pair in pairs
                 if not any(pair["start"] < at <= pair["end"] for at, _ in data)]
    startups = [(seq, instant(at), b) for seq, at, kind, b in events if kind == "startup"]
    definitions = {}
    for _, _, startup in startups:
        for definition in (startup.get("performance") or {}).get("benchmarks", []):
            definitions[definition["id"]] = definition
    definitions = list(definitions.values()) or PerformanceSettings().definitions()
    actions = {}
    for _, _, kind, body in events:
        if kind == "benchmark_action":
            actions.setdefault(body["symbol"], []).append(body)
    fund = compute(pairs, paid, None, definitions, market_marks, actions, data_charges=data)
    cash_policies = {}
    for day, (seq, closed) in closing.items():
        available = [b for n, observed, b in startups if n <= seq and observed <= instant(closed)]
        startup = available[-1] if available else {}
        if "cash_management_symbol" in startup:
            cash_policies[day] = (True, startup["cash_management_symbol"])
        else:
            # Legacy evidence may explicitly name its cash equivalent; never guess from ticker names.
            assets = list(packets[seq].get("cash_equivalents") or {})
            cash_policies[day] = (len(assets) == 1, assets[0] if len(assets) == 1 else None)
    reference = opening_reference(pairs, {d: packets[seq] for d, (seq, _) in closing.items()}, closes,
                                  market_marks, actions, cash_policies=cash_policies)
    expected = max(0, len(_exchange_sessions(min(closing),
        str(datetime.fromisoformat(max(closing)).date() + timedelta(days=1)))) - 1) if closing else 0
    reference["missing_account_intervals"] = expected - len(pairs)
    complete_costs = not fund.get("net_is_upper_bound", True)
    reference["net_difference"] = fund["net_return"] - reference["reference_return"] if (
        complete_costs and fund.get("status") == "computed" and reference["reference_return"] is not None) else None
    return {"status": "computed", "as_of": now.isoformat(), "decisions": decision_comparison(
        [(at, b) for _, at, k, b in events if k == "decision_validated"], closes,
        bases={b["decision_id"]: b for _, _, k, b in events if k == "decision_basis"}, calls=calls,
        sessions=completed_sessions(closes, str(completed_daily_cutoff(now)))),
        "opening_reference": reference, "fixed_benchmarks_and_fund": fund, "history_issues": history_issues,
        "limitations": "Saved research histories can include provider restatements; historical receipt vintages "
                        "are not certified. Missing inputs stay unknown. No new model calls, trades or downloads."}


def lines(report):
    if report.get("status") != "computed":
        return ["经济评估暂不可用：" + str(report.get("reason"))]
    pct = lambda v: "未知" if v is None else f"{v * 100:.3f}%"
    out = ["只读经济评估：新旧目标从决策后首个收盘起算；不衡量触发当日的日内盈亏，也不证明模型的因果贡献。"]
    if report.get("history_issues"):
        out.append(f"历史日线异常：隔离 {sum(r.get('rejected_rows', 0) for r in report['history_issues'])} 行；"
                   f"不可读文件 {sum(r['reason'] == 'unreadable_daily_history' for r in report['history_issues'])} 个。保留有效行，缺口不补。")
    for horizon, row in report["decisions"]["by_horizon"].items():
        out.append(f"{horizon} 日新方案相对维持旧目标：{row['observed']} 次，非重叠 {row['non_overlapping']} 次；"
                   f"扣估算交易摩擦后平均增益 {pct(row['mean_change_benefit'])}；"
                   f"再扣已关联决策链费用 {pct(row['mean_after_linked_review_cost'])}（费用完整 {row['with_linked_review_cost']} 次）")
    ref = report["opening_reference"]
    out.append(f"期初贝塔对照：覆盖 {ref['observed']}/{ref['intervals']} 个收盘区间；"
               f"对照收益 {pct(ref['reference_return'])}，账户差额 {pct(ref['gross_difference'])}，"
               f"扣模型和数据费用后差额 {pct(ref['net_difference'])}。固定基准保留。")
    out.append(f"仅复合可比区间，另有 {ref['missing_account_intervals']} 个区间缺有效收盘估值；不是连续全期收益。贝塔匹配不等于波动率或 ES 相同。")
    fund = report["fixed_benchmarks_and_fund"]
    if fund.get("status") == "computed":
        net = fund["net_return"] if not fund["net_is_upper_bound"] else None
        out.append(f"相同可比区间实际账户收益 {pct(fund['account_return'])}；扣模型及数据费后 {pct(net)}；"
                   f"未定价调用 {fund['unpriced_calls']} 次。")
        for name, value in fund["benchmarks"].items():
            excess = value["net_excess"] if not fund["net_is_upper_bound"] else None
            out.append(f"固定基准 {name}：{pct(value['return'])}；基金扣费后差额 {pct(excess)}。")
    out.append("方案交易成本为情景估算；旧记录缺少调用链或资金证据时不推断。价格方案不含分红、现金收益及实际限价/止损/风控缩量。")
    out.append("交易摩擦假设为每侧 10bp；关联费用仅含经理、纠错和新调用的反方审查。期初贝塔用当时期初账本估计，未知贝塔不按 1 补齐。")
    out.append("研究历史可能被供应商修订；基准是账本记录价及已记录公司行动，不保证为交易所正式收盘价。缺失时显示未知。")
    return out
