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
                  "min_review_ratio": 0.9, "max_mean_slippage_bps": 25}

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
        def rows(kinds):
            return [(at, kind, json.loads(body)) for at, kind, body in db.execute(
                "SELECT at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(kinds)), kinds)
                if instant(at) <= now]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        unresolved = db.execute("SELECT count(*) FROM intents WHERE status!='terminal'").fetchone()[0]
        references = {(r[0], r[1], r[2]): float(r[3]) for r in db.execute(
            "SELECT decision_id,symbol,side,reference FROM fund_orders ORDER BY rowid DESC")} if "fund_orders" in tables else {}
        approvals = dict(db.execute("SELECT state,count(*) FROM fund_approvals GROUP BY state")) if "fund_approvals" in tables else {}
        events = rows(("startup", "account", "iteration_finished", "notification", "operator_approval",
                       "operator_drawdown_reset", "operator_attached_order", "operator_reserve_adjusted",
                       "telegram_command", "order_submitted", "fill", "fill_reconciled", "fill_details_unavailable",
                       "order_reprice_cancel", "buy_chase_limit", "protective_trigger", "agent", "decision_backoff",
                       "postmarket_review", "daily_loss_pause", "drawdown_pause", "learning_evaluation"))
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
    slippage = []
    for _, kind, body in events:
        if kind not in {"fill", "fill_reconciled"}:
            continue
        order = by_broker.get(str(body.get("broker_id")))
        if not order:
            continue
        arrival = references.get((order.get("decision_id"), order["symbol"], order["side"]))
        if arrival and float(body.get("price") or 0) > 0:
            fill = float(body["price"])
            sign = 1 if order["side"] == "buy" else -1
            slippage.append(sign * (fill - arrival) / arrival * 10000)
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
    }
    checks = [
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
    ]
    results = [{"criterion": name, "value": value, "threshold": threshold, "passed": bool(test(value, threshold))
                if value is not None or name == "max_mean_slippage_bps" else False}
               for name, value, threshold, test in checks]
    mode = startups[-1].get("mode") if startups else None
    return {"as_of": now.isoformat(), "mode": mode, "verdict": "ready" if all(r["passed"] for r in results) else "not_ready",
            "criteria": results, "metrics": metrics,
            "limitations": "Operational readiness evidence from this journal only. It does not prove profitability; "
                           "a local virtual book is not broker evidence, and live trading needs separate approval."}


def acceptance_markdown(result):
    lines = [f"# 运行验收报告（{result['mode']}）", "", f"截止：{result['as_of']}；结论：**{result['verdict']}**", "",
             "| 标准 | 实际值 | 阈值 | 结果 |", "|---|---|---|---|"]
    for row in result["criteria"]:
        value = row["value"]
        shown = f"{value:.4g}" if isinstance(value, float) else str(value)
        lines.append(f"| {row['criterion']} | {shown} | {row['threshold']} | {'通过' if row['passed'] else '未通过'} |")
    metrics = result["metrics"]
    lines += ["", "## 关键指标", "", "```json", json_dump(metrics), "```", "", result["limitations"], ""]
    return "\n".join(lines)


def json_dump(value):
    import json
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
