"""Offline validation: real-data risk replay and statistics for the learning loop.

risk_replay() answers one question with recorded market data: does the
deterministic risk layer (volatility target, expected shortfall, name/group
caps, drawdown budget, daily loss stop) actually reduce realised risk compared
with holding the same AI targets unconstrained? It is look-ahead free: weights
chosen at the close of day t use only closes up to t and earn the t -> t+1
return. Missing closes are never filled; a day without every close is skipped.

paired_test() is the statistic behind the shadow A/B gate for the AI's own
strategy memo: paired daily differences, their mean, a t statistic and the
sample size. It does not pretend overlapping or tiny samples are significant.
"""
import math
from decimal import Decimal

from .quant import RiskEstimate, construct, drawdown_scale
from .risk import number


def _metrics(daily_returns, exposures):
    equity, peak, drawdown, curve = 1.0, 1.0, 0.0, []
    for value in daily_returns:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        curve.append(equity)
    n = len(daily_returns)
    mean = sum(daily_returns) / n if n else 0.0
    var = sum((r - mean) ** 2 for r in daily_returns) / (n - 1) if n > 1 else 0.0
    vol = math.sqrt(var * 252)
    tail = sorted(daily_returns)[:max(1, math.ceil(0.025 * n))] if n else []
    return {"days": n, "total_return": curve[-1] - 1 if curve else 0.0, "annual_vol": vol,
            "max_drawdown": drawdown, "worst_day": min(daily_returns) if n else 0.0,
            "es_975_1d_realised": -sum(tail) / len(tail) if tail else 0.0,
            "sharpe_zero_rf": mean / math.sqrt(var) * math.sqrt(252) if var > 0 else None,
            "average_gross_exposure": sum(exposures) / len(exposures) if exposures else 0.0}


def risk_replay(closes, targets, policy, limits, *, warmup=None, cost_bps=10, daily_loss=True):
    """Replay constant AI targets through the risk layer on recorded closes.

    closes: {symbol: [(iso_date, close), ...]}; targets: {symbol: weight}.
    Returns metrics for the unconstrained targets and the risk-managed book.
    """
    warmup = warmup or policy.min_history_days + 1
    days = sorted(set.intersection(*(set(d for d, _ in rows) for rows in closes.values())))
    table = {s: dict(rows) for s, rows in closes.items()}
    raw_returns, managed_returns, raw_exposure, managed_exposure, log = [], [], [], [], []
    weights = {s: 0.0 for s in targets}
    equity = peak = 1.0
    previous_equity = 1.0
    for index in range(warmup, len(days) - 1):
        today, tomorrow = days[index], days[index + 1]
        history = {s: [(d, table[s][d]) for d in days[:index + 1]] for s in targets}
        estimate = RiskEstimate(history, policy)
        drawdown = 1 - equity / peak
        scale = drawdown_scale(Decimal(str(drawdown)), policy, limits.drawdown)
        budget = limits.max_exposure * scale
        current = {s: number(str(round(w, 8))) for s, w in weights.items()}
        constructed, overlay = construct({s: number(str(w)) for s, w in targets.items()}, current, estimate,
                                         policy, limits, budget)
        new = {s: float(constructed.get(s, 0)) for s in targets}
        day_loss = 1 - equity / previous_equity if previous_equity else 0.0
        if daily_loss and day_loss >= float(policy.daily_loss_limit):
            # The daily loss stop forbids increases for the rest of the day.
            new = {s: min(new[s], weights[s]) for s in targets}
        if drawdown >= float(limits.drawdown):
            new = {s: min(new[s], weights[s]) for s in targets}  # Hard latch: no new risk.
        turnover = sum(abs(new[s] - weights[s]) for s in targets)
        step = {s: table[s][tomorrow] / table[s][today] - 1 for s in targets}
        raw = sum(float(w) * step[s] for s, w in targets.items())
        managed = sum(new[s] * step[s] for s in targets) - turnover * cost_bps / 10000
        raw_returns.append(raw)
        managed_returns.append(managed)
        raw_exposure.append(sum(float(w) for w in targets.values()))
        managed_exposure.append(sum(new.values()))
        previous_equity = equity
        equity *= 1 + managed
        peak = max(peak, equity)
        # Drift weights with the day's returns, as a real book would.
        grown = {s: new[s] * (1 + step[s]) for s in targets}
        weights = {s: grown[s] / (1 + managed) for s in targets}
        log.append({"date": today, "exposure": sum(new.values()), "binding": overlay["binding"],
                    "ex_ante_vol": overlay["constructed_metrics"]["annual_vol"], "drawdown": drawdown})
    raw, managed = _metrics(raw_returns, raw_exposure), _metrics(managed_returns, managed_exposure)
    binding = {}
    for row in log:
        for name in row["binding"]:
            key = name.split(":")[0]
            binding[key] = binding.get(key, 0) + 1
    return {"window": {"from": days[warmup] if len(days) > warmup else None, "to": days[-1] if days else None},
            "unconstrained": raw, "risk_managed": managed, "binding_days": binding,
            "drawdown_reduction": raw["max_drawdown"] - managed["max_drawdown"],
            "vol_reduction": raw["annual_vol"] - managed["annual_vol"],
            "return_cost": raw["total_return"] - managed["total_return"], "daily": log,
            "limitations": "Close-to-close replay with a flat transaction cost; no intraday stops, gaps inside a "
                           "day or AI decisions. It validates the risk layer's mechanics, not a strategy's alpha."}


def paired_test(differences, min_pairs):
    """Mean paired difference, t statistic and verdict inputs; no significance below min_pairs."""
    n = len(differences)
    if n < 2:
        return {"pairs": n, "mean": differences[0] if n else None, "t_stat": None, "win_rate": None,
                "sufficient": False}
    mean = sum(differences) / n
    var = sum((d - mean) ** 2 for d in differences) / (n - 1)
    t = mean / math.sqrt(var / n) if var > 0 else (math.inf if mean > 0 else -math.inf if mean < 0 else 0.0)
    return {"pairs": n, "mean": mean, "t_stat": t, "win_rate": sum(d > 0 for d in differences) / n,
            "sufficient": n >= min_pairs}


def daily_from_minutes(rows):
    """[(timestamp, close)] minute bars -> [(date, last close)] completed sessions."""
    by_day = {}
    for stamp, close in rows:
        by_day[str(stamp)[:10]] = float(close)
    return sorted(by_day.items())


def load_closes(directory, symbols):
    """SYMBOL.csv files with Date/Close (Yahoo export), timestamp/close or minute time/close columns."""
    import pandas as pd
    from pathlib import Path
    from .risk import RiskError
    result = {}
    for symbol in symbols:
        path = Path(directory) / f"{symbol}.csv"
        if not path.is_file():
            raise RiskError(f"Missing history file {path.name}")
        frame = pd.read_csv(path)
        columns = {c.lower(): c for c in frame.columns}
        stamp = next((columns[c] for c in ("date", "timestamp", "time") if c in columns), None)
        if stamp is None or "close" not in columns:
            raise RiskError(f"{path.name} needs a date/timestamp/time column and a close column")
        rows = [(str(t), float(c)) for t, c in zip(frame[stamp], frame[columns["close"]]) if c == c and float(c) > 0]
        # Several rows per date means intraday bars: keep each session's last close.
        result[symbol] = daily_from_minutes(rows)
    return result


class ValidationSettings:
    """`validation` section: shadow A/B of the learning loop and acceptance criteria."""
    FIELDS = {"shadow_enabled", "shadows_per_session", "min_pairs", "switch_t_stat", "acceptance"}
    ACCEPTANCE = {"min_sessions": 20, "min_iteration_ratio": 0.9, "max_watchdog_stops": 3,
                  "max_unresolved_intents": 0, "max_fill_details_unavailable": 0, "max_model_failure_rate": 0.2,
                  "min_review_ratio": 0.9, "max_mean_slippage_bps": 25, "max_bot_crashes": 0}

    def __init__(self, shadow_enabled=False, shadows_per_session=1, min_pairs=20, switch_t_stat=2.0, acceptance=None):
        from .risk import RiskError
        if type(shadow_enabled) is not bool:
            raise RiskError("validation.shadow_enabled must be true or false")
        if type(shadows_per_session) is not int or not 0 <= shadows_per_session <= 5:
            raise RiskError("validation.shadows_per_session must be 0..5")
        if type(min_pairs) is not int or not 5 <= min_pairs <= 250:
            raise RiskError("validation.min_pairs must be 5..250")
        if not isinstance(switch_t_stat, (int, float)) or not 1 <= float(switch_t_stat) <= 5:
            raise RiskError("validation.switch_t_stat must be 1..5")
        criteria = dict(self.ACCEPTANCE)
        unknown = set(acceptance or {}) - set(criteria)
        if unknown:
            raise RiskError("Unknown acceptance criteria: " + ", ".join(sorted(unknown)))
        criteria.update(acceptance or {})
        self.shadow_enabled, self.shadows_per_session = shadow_enabled, shadows_per_session
        self.min_pairs, self.switch_t_stat, self.acceptance = min_pairs, float(switch_t_stat), criteria

    @classmethod
    def from_config(cls, section):
        from .risk import RiskError
        section = dict(section or {})
        unknown = set(section) - cls.FIELDS
        if unknown:
            raise RiskError("Unknown validation settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def shadow_pairs(shadows, closes, horizon=1):
    """Paired memo-minus-baseline forward excess returns per session (both arms scored identically)."""
    from .scorecard import score_decisions
    pairs = []
    for row in shadows:
        arms = {row["arm"]: row["decision"], row["trading_arm"]: row["trading_decision"]}
        if set(arms) != {"memo", "baseline"} or not row.get("valid"):
            continue
        scored = {arm: score_decisions([(row["decided_at"], body)], closes)["recent"][0]["horizons"][str(horizon)]
                  for arm, body in arms.items()}
        if all(item["status"] == "observed" for item in scored.values()):
            from zoneinfo import ZoneInfo
            from .risk import instant
            day = instant(row["decided_at"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            pairs.append({"session_date": day,
                          "difference": scored["memo"]["excess_return"] - scored["baseline"]["excess_return"]})
    by_day = {}
    for pair in pairs:
        by_day[pair["session_date"]] = pair["difference"]  # One pair per session.
    return [by_day[day] for day in sorted(by_day)]


def session_pairs(events, sessions, zone):
    """Consecutive completed sessions' last account marks, and every benchmark symbol's last mark per session.

    A pair spanning an operator reserve adjustment is dropped: moving cash in or
    out of the sleeve is not a return. Returns (pairs, marks) with pairs as dicts
    (start/end day and time, starting equity, return) and marks {symbol: {day: price}}.
    """
    from .risk import instant
    accounts, marks, adjusted = {}, {}, []
    for at, kind, body in events:
        moment = instant(at)
        day = moment.astimezone(zone).date()
        if kind == "account" and day in sessions:
            accounts[day] = (moment, float(body["equity"]))
        elif kind == "benchmark" and day in sessions:
            for symbol, value in body.items():
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    marks.setdefault(symbol, {})[day.isoformat()] = price
        elif kind == "operator_reserve_adjusted":
            adjusted.append(moment)
    days, pairs = sorted(accounts), []
    for a, b in zip(days, days[1:]):
        (start, before), (end, after) = accounts[a], accounts[b]
        if any(start < t <= end for t in adjusted) or before <= 0:
            continue
        pairs.append({"start_day": a.isoformat(), "end_day": b.isoformat(), "start": start, "end": end,
                      "equity_start": before, "return": after / before - 1})
    return pairs, marks


def session_returns(events, sessions, zone):
    """(returns, SPY returns, aligned): aligned says every pair also has SPY marks on both days."""
    pairs, marks = session_pairs(events, sessions, zone)
    spy = marks.get("SPY", {})
    returns = [p["return"] for p in pairs]
    market = [spy[p["end_day"]] / spy[p["start_day"]] - 1 for p in pairs
              if p["start_day"] in spy and p["end_day"] in spy]
    return returns, market, len(market) == len(returns)


def interval_model_costs(events, pairs):
    """Model cost between marks; an unpriced call stays unknown even without usage."""
    from .costs import scripted
    from .risk import instant
    calls = [(instant(at), body) for at, kind, body in events
             if kind == "agent" and not scripted(body) and not body.get("cache_hit")]
    costs = []
    for pair in pairs:
        inside = [body for at, body in calls if pair["start"] < at <= pair["end"]]
        if any(body.get("cost_usd") is None for body in inside):
            costs.append(None)
        else:
            costs.append(sum(float(body.get("cost_usd") or 0) for body in inside))
    return costs


def benchmark_report(events, pairs, marks, startups):
    """Net returns and the comparison with every benchmark defined so far (old definitions stay reported)."""
    from .performance import (SESSIONS_PER_MONTH, PerformanceSettings, benchmark_comparison, benchmark_series,
                              net_returns)
    configured = [b.get("performance") for b in startups if b.get("performance")]
    definitions, current = {}, configured[-1]["benchmarks"] if configured else PerformanceSettings().definitions()
    for entry in configured:
        for definition in entry["benchmarks"]:
            definitions.setdefault(definition["id"], definition)
    for definition in current:
        definitions.setdefault(definition["id"], definition)
    actions = {}
    for _, kind, body in events:
        if kind == "benchmark_action":
            actions.setdefault(body["symbol"], []).append(body)
    monthly = (configured[-1].get("data_costs_usd_per_month") if configured
               else PerformanceSettings().data_costs_usd_per_month)
    per_session = float(monthly) / SESSIONS_PER_MONTH if monthly is not None else None
    returns = [p["return"] for p in pairs]
    window = [(p["start_day"], p["end_day"]) for p in pairs]
    current_ids = {d["id"] for d in current}
    recorded = {s for _, kind, b in events if kind == "benchmark_actions_checked" for s in b.get("symbols", [])}
    comparisons = []
    for definition in definitions.values():
        row = benchmark_comparison(definition, returns, benchmark_series(definition, window, marks, actions),
                                   definition["id"] in current_ids)
        row["dividends"] = ("included" if set(definition["weights"]) <= recorded else
                            "excluded: no dividend records for " + ", ".join(sorted(set(definition["weights"]) - recorded)))
        comparisons.append(row)
    return {"returns": net_returns(pairs, interval_model_costs(events, pairs), per_session),
            "data_costs_usd_per_month": monthly, "benchmarks": comparisons}


def trial_count(prompt_versions, risk_policies, factor_expressions, construction_runs=0, shadow_runs=0):
    """Configurations tried, for the deflated Sharpe ratio: each kind listed, and their sum.

    Prompt versions x risk policies that traded (as before), plus every factor
    expression tested (rejected ones too), plus one for the alternative portfolio
    construction and one for the learning control arm once those comparisons ran.
    Never fewer than before this count existed.
    """
    traded = max(1, prompt_versions) * max(1, risk_policies)
    parts = {"prompt_versions_x_risk_policies": traded, "factor_expressions": int(factor_expressions),
             "construction_alternative": 1 if construction_runs else 0,
             "learning_control_arm": 1 if shadow_runs else 0}
    return {**parts, "total": sum(parts.values())}


def acceptance(root, criteria=None, now=None):
    """Readiness evidence for a local/paper run, computed only from its journal."""
    import json
    import sqlite3
    from contextlib import closing
    from datetime import datetime, timezone
    from pathlib import Path
    from zoneinfo import ZoneInfo
    import pandas_market_calendars as mcal
    from .risk import instant
    criteria = {**ValidationSettings.ACCEPTANCE, **(criteria or {})}
    root = Path(root)
    now = instant(now or datetime.now(timezone.utc))
    with closing(sqlite3.connect((root / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        from .ledger_audit import audit_connection
        integrity = audit_connection(db)
        def rows(kinds):
            return [(at, kind, json.loads(body)) for at, kind, body in db.execute(
                "SELECT at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(kinds)), kinds)
                if instant(at) <= now]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        unresolved = db.execute("SELECT count(*) FROM intents WHERE status!='terminal'").fetchone()[0]
        from .execution import observed_arrivals
        references = observed_arrivals(db, as_of=now)
        approvals = dict(db.execute("SELECT state,count(*) FROM fund_approvals GROUP BY state")) if "fund_approvals" in tables else {}
        # Every configuration tried counts against the deflated Sharpe ratio, including rejected factors.
        factor_trials = db.execute("SELECT count(*) FROM fund_factors").fetchone()[0] if "fund_factors" in tables else 0
        construction_runs, shadow_runs = (db.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]
                                          for kind in ("construction_variant", "shadow_decision"))
        events = rows(("startup", "account", "iteration_finished", "notification", "operator_approval",
                       "operator_drawdown_reset", "operator_attached_order", "operator_reserve_adjusted",
                       "telegram_command", "order_submitted", "fill", "fill_reconciled", "fill_details_unavailable",
                       "order_reprice_cancel", "buy_chase_limit", "protective_trigger", "agent", "decision_backoff",
                       "postmarket_review", "daily_loss_pause", "drawdown_pause", "learning_evaluation",
                       "auto_resume", "benchmark", "decision_validated", "benchmark_action",
                       "benchmark_actions_checked", "scorecard", "agent_outcome", "review_skipped", "critic_reused"))
    zone = ZoneInfo("America/New_York")
    startups = [b for _, k, b in events if k == "startup"]
    interval = ((startups[-1].get("execution") or {}).get("iteration_seconds") or 60) if startups else 60
    accounts = [(instant(at), b) for at, k, b in events if k == "account"]
    days = sorted({at.astimezone(zone).date() for at, _ in accounts})
    sessions = []
    if days:
        schedule = mcal.get_calendar("NYSE").schedule(start_date=days[0], end_date=days[-1])
        for day, market in schedule.iterrows():
            if day.date() in days and market.market_close <= now:
                sessions.append((day.date(), market.market_open, market.market_close))
    ratios = []
    finished = [instant(at) for at, k, _ in events if k == "iteration_finished"]
    for day, start, end in sessions:
        count = sum(start <= t < end for t in finished)
        ratios.append(min(1.0, count / ((end - start).total_seconds() / interval)))
    notes = [b for _, k, b in events if k == "notification"]
    count = lambda kind: sum(1 for _, k, _ in events if k == kind)  # noqa: E731
    submitted = [b for _, k, b in events if k == "order_submitted"]
    by_broker = {str(b["broker_id"]): b for b in submitted}
    slippage, slippage_unknown = [], 0
    for _, kind, body in events:
        if kind not in {"fill", "fill_reconciled"}:
            continue
        order = by_broker.get(str(body.get("broker_id")))
        if not order:
            slippage_unknown += 1
            continue
        arrival = references.get(order.get("intent_id"))
        if arrival and float(body.get("price") or 0) > 0:
            fill = float(body["price"])
            sign = 1 if order["side"] == "buy" else -1
            slippage.append(sign * (fill - arrival) / arrival * 10000)
        else:
            slippage_unknown += 1
    agents = [b for _, k, b in events if k == "agent"]
    failures = sum(1 for b in agents if b.get("parse_error"))
    equity = [float(b["equity"]) for _, b in accounts]
    peak, drawdown = 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak) if peak else 0.0
    reviews = {b["session_date"] for _, k, b in events if k == "postmarket_review"}
    learning = next((b for _, k, b in reversed(events) if k == "learning_evaluation"), None)
    metrics = {
        "sessions_observed": len(sessions), "iteration_seconds": interval,
        "median_iteration_ratio": sorted(ratios)[len(ratios) // 2] if ratios else None,
        "min_iteration_ratio": min(ratios) if ratios else None,
        "watchdog_stops": sum(1 for n in notes if n.get("kind") in {"watchdog_stop", "server_supervision_stop"}),
        "unresolved_intents": unresolved, "fill_details_unavailable": count("fill_details_unavailable"),
        "operator_interventions": {k: count(k) for k in ("operator_approval", "operator_drawdown_reset",
                                                          "operator_attached_order", "operator_reserve_adjusted")},
        "telegram_commands": count("telegram_command"), "approval_requests": approvals,
        "orders_submitted": len(submitted),
        "protective_orders": sum(1 for b in submitted if b.get("kind") == "protective"),
        "reprice_cancels": count("order_reprice_cancel"), "chase_limited": count("buy_chase_limit"),
        "fills": count("fill") + count("fill_reconciled"),
        "mean_slippage_bps_vs_arrival": sum(slippage) / len(slippage) if slippage else None,
        "agent_runs": len(agents), "agent_failures": failures,
        "model_failure_rate": failures / len(agents) if agents else None, "decision_backoffs": count("decision_backoff"),
        "max_observed_drawdown": drawdown, "daily_loss_pauses": count("daily_loss_pause"),
        "drawdown_latches": count("drawdown_pause"), "protective_triggers": count("protective_trigger"),
        "postmarket_reviews": len(reviews), "review_ratio": len(reviews) / len(sessions) if sessions else None,
        "learning": learning,
        # A crash that escaped every guard ended the process: never acceptable for unattended running.
        "bot_crashes": sum(1 for n in notes if n.get("kind") == "bot_crash"),
        "bookkeeping_errors": sum(1 for n in notes if n.get("kind") == "bookkeeping_error"),
        "auto_resumes": count("auto_resume"),
        "ledger_status": integrity["status"], "fills_with_unknown_slippage": slippage_unknown,
    }
    checks = [
        ("local_ledger_verified", integrity["status"], "verified or not_applicable",
         lambda v, t: v in {"verified", "not_applicable"}),
        ("max_unknown_slippage_fills", slippage_unknown, 0, lambda v, t: v <= t),
        ("min_sessions", metrics["sessions_observed"], criteria["min_sessions"], lambda v, t: v >= t),
        ("min_iteration_ratio", metrics["median_iteration_ratio"], criteria["min_iteration_ratio"],
         lambda v, t: v is not None and v >= t),
        ("max_watchdog_stops", metrics["watchdog_stops"], criteria["max_watchdog_stops"], lambda v, t: v <= t),
        ("max_unresolved_intents", unresolved, criteria["max_unresolved_intents"], lambda v, t: v <= t),
        ("max_fill_details_unavailable", metrics["fill_details_unavailable"], criteria["max_fill_details_unavailable"],
         lambda v, t: v <= t),
        ("max_model_failure_rate", metrics["model_failure_rate"], criteria["max_model_failure_rate"],
         lambda v, t: v is not None and v <= t),
        ("min_review_ratio", metrics["review_ratio"], criteria["min_review_ratio"], lambda v, t: v is not None and v >= t),
        ("max_mean_slippage_bps", metrics["mean_slippage_bps_vs_arrival"], criteria["max_mean_slippage_bps"],
         lambda v, t: v is None or v <= t),
        ("max_bot_crashes", metrics["bot_crashes"], criteria["max_bot_crashes"], lambda v, t: v <= t),
    ]
    results = [{"criterion": name, "value": value, "threshold": threshold, "passed": bool(test(value, threshold))
                if value is not None or name == "max_mean_slippage_bps" else False}
               for name, value, threshold, test in checks]
    mode = startups[-1].get("mode") if startups else None
    verdict = "ready" if all(r["passed"] for r in results) else "not_ready"
    from .performance import capital_stage, evidence
    returns, market, aligned = session_returns(events, {day for day, _, _ in sessions}, zone)
    pairs, marks = session_pairs(events, {day for day, _, _ in sessions}, zone)
    tried = trial_count(
        len({b.get("prompt_version") or "unversioned" for _, k, b in events if k == "decision_validated"}),
        len({b.get("risk_policy_digest") for b in startups}), factor_trials, construction_runs, shadow_runs)
    stats = evidence(returns, market if aligned else None, tried["total"])
    stats["configurations_breakdown"] = tried
    net = benchmark_report(events, pairs, marks, startups)
    limit = ((startups[-1].get("limits") or {}).get("drawdown")) if startups else None
    stage = capital_stage(verdict, stats, drawdown, float(limit) if limit is not None else None)
    from .call_value import from_events
    card = next((b for _, k, b in reversed(events) if k == "scorecard"), None)
    investment = investment_acceptance(stats, returns, market if aligned else None, net, learning,
                                       (card or {}).get("ideas"), from_events(events))
    return {"as_of": now.isoformat(), "mode": mode, "verdict": verdict,
            "criteria": results, "metrics": metrics, "performance_evidence": stats, "capital_stage": stage,
            "net_returns": net["returns"], "benchmarks": net["benchmarks"],
            "data_costs_usd_per_month": net["data_costs_usd_per_month"], "investment": investment,
            "limitations": "Operational readiness evidence from this journal only. It does not prove profitability; "
                           "a local virtual book is not broker evidence, and live trading needs separate approval."}


INVESTMENT_MIN_SESSIONS = 60
SEPARATION = ("运行验收通过不代表投资能力验收通过，反过来也一样。投资能力验收通过也只是初步判断："
              "模拟盘的成交偏理想，样本仍短，不保证未来盈利。")


def _smaller_t(regression):
    values = [t for t in ((regression or {}).get("alpha_t"), (regression or {}).get("alpha_t_newey_west")) if t is not None]
    return min(values) if values else None


def investment_acceptance(stats, returns, market, net, learning, ideas, call_value):
    """Investment-capability acceptance (V2 4.4), separate from the operational verdict.

    Required: enough sessions; beta-adjusted alpha against SPY; beating the predefined
    risk-matched benchmark, before and after model and data costs; the deflated Sharpe
    after every configuration tried; and a positive beta-adjusted return in both halves
    of the record (one lucky stretch is not stability). Reported but not required: the
    learning comparison (gate 4) and the research ideas' calibration.
    """
    from .performance import Z95, regression_hac
    sessions = stats.get("sessions") or 0
    regression = stats.get("benchmark_regression") or {}
    matched = next((row for row in net["benchmarks"] if row.get("current") and row.get("name") == "risk_matched"), None)
    computed = matched is not None and matched.get("status") == "computed"
    system = net["returns"].get("system") or {}
    halves = None
    if market is not None and len(returns) >= 4:
        fit = regression_hac(returns, market)
        if fit is not None:
            residual = [r - fit["beta"] * m for r, m in zip(returns, market)]
            middle = len(residual) // 2
            halves = [sum(part) / len(part) for part in (residual[:middle], residual[middle:])]
    stats_matched = (matched or {}).get("regression") if computed else None
    benchmark_annual = ((matched or {}).get("benchmark") or {}).get("annual_return") if computed else None
    system_excess = (system["annual_return"] - benchmark_annual
                     if system.get("status") == "computed" and benchmark_annual is not None else None)
    evaluation = (learning or {}).get("memo_minus_baseline_1d_excess") or {}
    checks = [
        ("min_sessions", sessions, INVESTMENT_MIN_SESSIONS, sessions >= INVESTMENT_MIN_SESSIONS, True),
        ("alpha_t_vs_spy", _smaller_t(regression), round(Z95, 2),
         (_smaller_t(regression) or 0) >= Z95, True),
        ("risk_matched_excess_annual", (matched or {}).get("annual_excess_return") if computed else None, "> 0 and alpha t ≥ 1.65",
         computed and matched["annual_excess_return"] > 0 and (_smaller_t(stats_matched) or 0) >= Z95, True),
        ("system_net_excess_annual", system_excess if system.get("status") != "unknown_costs" else "unknown_costs", "> 0",
         system_excess is not None and system_excess > 0, True),
        ("deflated_sharpe", stats.get("deflated_sharpe"), 0.90, (stats.get("deflated_sharpe") or 0) >= 0.90, True),
        ("both_halves_beta_adjusted_positive", halves, "> 0 in each half",
         halves is not None and all(h > 0 for h in halves), True),
        ("learning_beats_baseline_t", evaluation.get("t_stat"), 2.0,
         bool(evaluation.get("sufficient")) and (evaluation.get("t_stat") or 0) >= 2.0, False),
        ("research_ideas_informative", (ideas or {}).get("verdict"), "informative",
         (ideas or {}).get("verdict") == "informative", False),
    ]
    criteria = [{"criterion": name, "value": value, "threshold": threshold, "passed": bool(passed), "required": required}
                for name, value, threshold, passed, required in checks]
    if sessions < INVESTMENT_MIN_SESSIONS or stats.get("status") != "computed":
        verdict = "insufficient_evidence"
    elif all(c["passed"] for c in criteria if c["required"]):
        verdict = "preliminary_edge"
    else:
        verdict = "edge_not_shown"
    return {"verdict": verdict, "criteria": criteria, "call_value": call_value, "notice": SEPARATION}


def _number(value, digits=2):
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"


def _net_lines(net, monthly, pct):
    if not net:
        return ["- 无数据"]
    account, system, costs = net["account"], net["system"], net["costs_usd"]
    lines = [f"- 账户净收益（已扣成交费用和滑点）：年化 {pct(account.get('annual_return'))}，"
             f"交易日 {account.get('sessions')}"]
    if system.get("status") == "unknown_costs":
        lines.append(f"- 系统净收益（再扣模型和数据费用）：未知（{system['sessions_with_unknown_costs']} 个交易日的费用未知，"
                     "不按 0 计）")
    else:
        lines.append(f"- 系统净收益（再扣模型和数据费用）：年化 {pct(system.get('annual_return'))}")
    lines.append(f"- 期间模型费用 {_number(costs.get('model'))} 美元；数据费用 "
                 f"{_number(costs.get('data')) if costs.get('data') is not None else '未知'} 美元（月费 {monthly if monthly is not None else '未知'}）")
    return lines


TRIAL_LABELS = {"prompt_versions_x_risk_policies": "提示词版本×风控配置", "factor_expressions": "因子表达式（含未通过）",
                "construction_alternative": "组合构建对照", "learning_control_arm": "学习对照组"}


VERDICT_LABELS = {"ready": "通过", "not_ready": "未通过", "insufficient_evidence": "样本不足，尚不能判断",
                  "edge_not_shown": "尚未证明有扣费后的超额收益", "preliminary_edge": "初步显示扣费后的超额收益"}
INVESTMENT_LABELS = {"min_sessions": "交易日数", "alpha_t_vs_spy": "相对 SPY 的 alpha t（普通与 Newey-West 取小）",
                     "risk_matched_excess_annual": "相对风险匹配基准的年化超额",
                     "system_net_excess_annual": "扣模型和数据费后相对风险匹配基准的年化超额",
                     "deflated_sharpe": "去试错夏普（DSR）", "both_halves_beta_adjusted_positive": "前后两半的 beta 调整后日均收益",
                     "learning_beats_baseline_t": "学习对照：看备忘组减对照组的 t",
                     "research_ideas_informative": "研究想法的预测能力"}


def _shown(value):
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, list):
        return "、".join(_shown(v) for v in value)
    return "—" if value is None else str(value)


def acceptance_markdown(result):
    """The operational acceptance report (acceptance.md): is the system running reliably?"""
    lines = [f"# 运行验收报告（{result['mode']}）", "",
             f"截止：{result['as_of']}；结论：**{result['verdict']}**（{VERDICT_LABELS.get(result['verdict'], '')}）", "",
             "只回答“系统是否可靠地运行”。是否有投资能力见投资能力验收报告（`investment_acceptance.md`）。"
             + SEPARATION, "", "| 标准 | 实际值 | 阈值 | 结果 |", "|---|---|---|---|"]
    for row in result["criteria"]:
        lines.append(f"| {row['criterion']} | {_shown(row['value'])} | {row['threshold']} | {'通过' if row['passed'] else '未通过'} |")
    lines += ["", "## 关键指标", "", "```json", json_dump(result["metrics"]), "```", "", result["limitations"], ""]
    return "\n".join(lines)


def investment_markdown(result):
    """The investment-capability acceptance report (investment_acceptance.md)."""
    investment = result.get("investment") or {}
    stats, stage = result.get("performance_evidence") or {}, result.get("capital_stage") or {}
    regression = stats.get("benchmark_regression") or {}
    def pct(value):
        return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "—"
    verdict = investment.get("verdict")
    lines = [f"# 投资能力验收报告（{result['mode']}）", "",
             f"截止：{result['as_of']}；结论：**{verdict}**（{VERDICT_LABELS.get(verdict, '')}）", "",
             investment.get("notice", SEPARATION), "",
             "| 标准 | 实际值 | 阈值 | 结果 | 是否必需 |", "|---|---|---|---|---|"]
    for row in investment.get("criteria", []):
        lines.append(f"| {INVESTMENT_LABELS.get(row['criterion'], row['criterion'])} | {_shown(row['value'])} | "
                     f"{row['threshold']} | {'通过' if row['passed'] else '未通过'} | {'必需' if row['required'] else '参考'} |")
    lines += ["", "## 业绩证据（是能力还是运气）", "",
              f"- 交易日：{stats.get('sessions')}；年化夏普：{stats.get('sharpe_annualized')}",
              f"- 真实夏普 > 0 的概率（PSR）：{pct(stats.get('probabilistic_sharpe'))}；"
              f"扣除 {stats.get('configurations_tried')} 种配置试错后的概率（DSR）：{pct(stats.get('deflated_sharpe'))}",
              f"- 试错计数：" + ("；".join(f"{TRIAL_LABELS.get(k, k)} {v}" for k, v in
                                        (stats.get("configurations_breakdown") or {}).items() if k != "total") or "—"),
              f"- 达到 95% 置信所需的最短记录：{stats.get('min_track_record_sessions')} 个交易日",
              f"- 相对 SPY：beta {regression.get('beta')}，年化 alpha {regression.get('alpha_annualized')}，"
              f"alpha t 值 {regression.get('alpha_t')}",
              f"- 年化收益 {pct(stats.get('annual_return'))}，最大回撤 {pct(stats.get('max_drawdown'))}，"
              f"Sortino {_number(stats.get('sortino_annualized'))}，按自相关修正的夏普（Lo）"
              f"{_number(stats.get('sharpe_lo_adjusted'))}；alpha t 值（Newey-West）{_number(regression.get('alpha_t_newey_west'))}"]
    lines += ["", "## 净收益口径", ""] + _net_lines(result.get("net_returns"), result.get("data_costs_usd_per_month"), pct)
    lines += ["", "## 事先定义的基准", "", "| 基准 | 定义 | 当前 | 交易日 | 年化超额 | 信息比率 | alpha t（OLS／NW） | 分红 |",
              "|---|---|---|---|---|---|---|---|"]
    for row in result.get("benchmarks") or []:
        weights = " + ".join(f"{float(w) * 100:g}% {s}" for s, w in row["weights"].items())
        reg = row.get("regression") or {}
        lines.append(f"| {row['id']} | {weights} | {'是' if row['current'] else '旧定义'} | {row['sessions']} | "
                     f"{pct(row.get('annual_excess_return'))} | {_number(row.get('information_ratio'))} | "
                     f"{_number(reg.get('alpha_t'))}／{_number(reg.get('alpha_t_newey_west'))} | {row.get('dividends')} |")
    from .call_value import lines as value_lines
    value = value_lines(investment.get("call_value") or {"by_purpose": {}, "calls_saved": {}})
    lines += ["", "## AI 调用价值（全部记录）", ""] + (value or ["- 还没有带调用编号的记录"])
    lines += ["", f"## 分阶段资金就绪度：**{stage.get('stage')}**", ""]
    lines += [f"- {reason}" for reason in stage.get("reasons", [])] or ["- 全部条件满足"]
    lines += ["", stage.get("notice", ""), ""]
    return "\n".join(lines)


def json_dump(value):
    import json
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
