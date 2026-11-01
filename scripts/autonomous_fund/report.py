"""Rebuild local reports from the durable journal; never fabricate unknown costs."""
import csv
import json
import math
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from .risk import instant


def report(root, prices=None, write_files=True, *, as_of=None):
    if as_of is not None and write_files:
        raise ValueError("Historical report snapshots must not overwrite current reports")
    root = Path(root)
    with sqlite3.connect(root / "state.sqlite3") as db:
        events = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events ORDER BY sequence")]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        approval_rows = [dict(zip(("id", "digest", "state", "body"), r)) for r in db.execute(
            "SELECT id,digest,state,body FROM fund_approvals ORDER BY rowid")] if "fund_approvals" in tables else []
        target = db.execute("SELECT id,state,until_at,body FROM fund_decisions ORDER BY rowid DESC LIMIT 1").fetchone() if "fund_decisions" in tables else None
    if as_of is not None:
        events = [row for row in events if instant(row[0]) <= instant(as_of)]
    now = datetime.now(timezone.utc)
    for row in approval_rows:
        row["body"] = json.loads(row["body"])
        row["effective_state"] = "expired" if row["state"] in {"pending", "approved"} and instant(row["body"]["expires_at"]) <= now else row["state"]
    from .evaluation import forward_outcomes
    outcomes = forward_outcomes(events)
    monitor = None
    if (root / "monitor.json").is_file():
        try:
            monitor = json.loads((root / "monitor.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            monitor = {"status": "unavailable"}
    accounts, benchmarks, fills, usage, notifications, reconciled = [], {}, [], [], [], {}
    decisions = 0
    incomplete_fills = False
    startup = next((body for _, kind, body in reversed(events) if kind == "startup"), {})
    premarket = [body for _, kind, body in events if kind == "premarket_research"]
    reviews = [body for _, kind, body in events if kind == "postmarket_review"]
    for at, kind, body in events:
        if kind == "account":
            accounts.append({"at": at, "equity": float(body["equity"]), "cash": float(body["cash"]),
                             "positions": body["positions"], "receivables": float(body.get("receivables", 0))})
        elif kind == "benchmark":
            benchmarks[at[:10]] = body
        elif kind == "fill":
            fills.append({"at": at, **body, "evidence_kind": body.get("evidence_kind", "native_fill_callback")})
        elif kind == "fill_reconciled":
            reconciled[body["broker_id"]] = {"at": at, **body, "evidence_kind": "broker_cumulative_observation"}
        elif kind == "decision_validated":
            decisions += 1
        elif kind == "fill_details_unavailable":
            incomplete_fills = True
        elif kind == "agent":
            usage.append({"at": at, **body})
        elif kind == "notification":
            notifications.append({"at": at, **body})
    # A recovered cumulative broker fill replaces callbacks for that order.
    # Its timestamp is observation time, not an invented execution timestamp.
    fills = [row for row in fills if row["broker_id"] not in reconciled] + list(reconciled.values())
    daily = {}
    for row in accounts:
        daily[row["at"][:10]] = row
    curve = list(daily.values())
    peak, max_drawdown, previous = 0, 0, None
    for row in curve:
        peak = max(peak, row["equity"])
        max_drawdown = max(max_drawdown, 1 - row["equity"] / peak)
        row["daily_return"] = row["equity"] / previous - 1 if previous else 0
        previous = row["equity"]
    initial = accounts[0]["equity"] if accounts else None
    observed_peak, observed_drawdown = 0, 0
    for row in accounts:
        observed_peak = max(observed_peak, row["equity"])
        observed_drawdown = max(observed_drawdown, 1 - row["equity"] / observed_peak)
    returns = [row["daily_return"] for row in curve[1:]]
    sharpe = None
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
        sharpe = mean / math.sqrt(variance) * math.sqrt(252) if variance > 0 else None
    comparison = {}
    for symbol in ("SPY", "QQQ"):
        observations = [float(benchmarks[row["at"][:10]][symbol]) for row in curve
                        if symbol in benchmarks.get(row["at"][:10], {})]
        comparison[symbol] = observations[-1] / observations[0] - 1 if len(observations) == len(curve) and len(curve) > 1 else None
    # A partially filled order's trade_cost may be cumulative. Never sum repeated
    # cumulative observations. Missing broker fees remain unknown, not zero.
    fee_by_order = {}
    local_fees = 0
    unknown_fee = incomplete_fills
    for row in fills:
        if row["trade_cost"] is None:
            unknown_fee = True
        else:
            if row["evidence_kind"] == "local_completed_bar_simulation":
                local_fees += float(row["trade_cost"])
            else:
                key = row["broker_id"]
                fee_by_order[key] = max(fee_by_order.get(key, 0), float(row["trade_cost"]))
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "unknown_calls": 0}
    unfinished = {}
    for at, kind, body in events:
        if kind == "agent_call_started":
            unfinished[body["id"]] = {"at": at, **body}
        elif kind == "agent_call_finished":
            unfinished.pop(body["id"], None)
    by_model = {}
    for row in usage:
        if row.get("cache_hit"):
            continue
        counts = by_model.setdefault(row.get("model") or "unknown", dict.fromkeys(totals, 0))
        raw = row.get("usage")
        if not raw:
            totals["unknown_calls"] += 1
            counts["unknown_calls"] += 1
            continue
        values = {
            "input_tokens": int(raw.get("prompt_tokens", raw.get("input_tokens", raw.get("prompt_token_count", 0)))),
            "output_tokens": int(raw.get("completion_tokens", raw.get("output_tokens", raw.get("candidates_token_count", 0)))),
            "cached_input_tokens": int(raw.get("cache_read_input_tokens") or raw.get("cached_content_token_count") or
                (raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        }
        for key, value in values.items():
            totals[key] += value
            counts[key] += value
    estimate = None
    estimates_by_model = None
    if prices and not totals["unknown_calls"] and not unfinished:
        # Operator-supplied rates only. This is an estimate, not a provider bill.
        # A legacy single-model rate must never silently price two models alike.
        rates = ({model: prices for model in by_model} if len(by_model) == 1 and "input" in prices
                 else prices)
        if by_model and all(model in rates for model in by_model):
            estimates_by_model = {
                model: (counts["input_tokens"] * float(rates[model]["input"]) +
                        counts["output_tokens"] * float(rates[model]["output"])) / 1_000_000
                for model, counts in by_model.items()}
            estimate = sum(estimates_by_model.values())
    summary = {
        "mode": startup.get("mode"), "scripted_model": startup.get("mock_model"),
        "models": startup.get("models"),
        "execution_revision": startup.get("execution_revision"),
        "active_allocation": {"id": target[0], "state": target[1], "until_at": target[2], "decision": json.loads(target[3])} if target else None,
        "approval_requests": approval_rows,
        "pending_approval_count": sum(r["effective_state"] == "pending" for r in approval_rows),
        "monitor": monitor,
        "strategy_followup": outcomes,
        "observe_only": startup.get("observe_only", False),
        "validated_decisions": decisions,
        "premarket_research_runs": len(premarket),
        "latest_premarket_research": premarket[-1] if premarket else None,
        "postmarket_review_runs": len(reviews),
        "latest_postmarket_review": reviews[-1] if reviews else None,
        "strategy_memory_version": reviews[-1]["version"] if reviews else None,
        "blocked_iterations": sum(row["kind"] == "blocked" for row in notifications),
        "observations": len(curve), "starting_equity": initial,
        "ending_equity": curve[-1]["equity"] if curve else None,
        "return": curve[-1]["equity"] / initial - 1 if initial and curve else None,
        "max_observed_drawdown": observed_drawdown, "max_daily_drawdown": max_drawdown,
        "daily_sharpe_zero_risk_free": sharpe,
        "benchmark_price_returns": comparison, "fills_observed": len(fills),
        "observed_fees": local_fees + sum(fee_by_order.values()) if not unknown_fee else None,
        "fill_details_incomplete": incomplete_fills,
        "model_usage": totals, "model_usage_by_model": by_model,
        "unfinished_model_calls": list(unfinished.values()),
        "model_cost_estimate_usd": estimate, "model_cost_estimate_by_model_usd": estimates_by_model,
        "latest_account": accounts[-1] if accounts else None,
        "limitations": "Daily sampled drawdown; benchmark price returns exclude dividends and fees. "
                        "No claim of predictive performance from scripted models. Cash flows are not return-adjusted; use a dedicated unfunded-during-run account."
    }
    if not write_files:
        return summary
    (root / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if premarket:
        (root / "premarket_research.json").write_text(json.dumps(premarket[-1], indent=2, ensure_ascii=False), encoding="utf-8")
    from .review import export_reviews
    export_reviews(root, reviews)
    (root / "approvals.json").write_text(json.dumps(approval_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "strategy_evaluations.json").write_text(json.dumps(outcomes, indent=2, ensure_ascii=False), encoding="utf-8")
    for name, rows, fields in (
        ("equity.csv", curve, ["at", "equity", "cash", "positions", "receivables", "daily_return"]),
        ("fills.csv", fills, ["at", "broker_id", "symbol", "side", "quantity", "price", "trade_cost", "evidence_kind", "execution_id", "bar_start"]),
    ):
        with (root / name).open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    (root / "notifications.json").write_text(json.dumps(notifications, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
