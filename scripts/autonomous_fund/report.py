"""Rebuild local reports from the durable journal; never fabricate unknown costs."""
import csv
import json
import math
import sqlite3
from pathlib import Path


def report(root, prices=None, write_files=True):
    root = Path(root)
    with sqlite3.connect(root / "state.sqlite3") as db:
        events = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events ORDER BY sequence")]
    accounts, benchmarks, fills, usage, notifications, reconciled = [], {}, [], [], [], {}
    decisions = 0
    incomplete_fills = False
    startup = next((body for _, kind, body in reversed(events) if kind == "startup"), {})
    premarket = [body for _, kind, body in events if kind == "premarket_research"]
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
    for row in usage:
        if row.get("cache_hit"):
            continue
        raw = row.get("usage")
        if not raw:
            totals["unknown_calls"] += 1
            continue
        totals["input_tokens"] += int(raw.get("prompt_tokens", raw.get("input_tokens", raw.get("prompt_token_count", 0))))
        totals["output_tokens"] += int(raw.get("completion_tokens", raw.get("output_tokens", raw.get("candidates_token_count", 0))))
        totals["cached_input_tokens"] += int(raw.get("cache_read_input_tokens", (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)))
    estimate = None
    if prices and not totals["unknown_calls"]:
        # Operator-supplied rates only. This is an estimate, not a provider bill.
        estimate = (totals["input_tokens"] * float(prices["input"]) +
                    totals["output_tokens"] * float(prices["output"])) / 1_000_000
    summary = {
        "mode": startup.get("mode"), "scripted_model": startup.get("mock_model"),
        "observe_only": startup.get("observe_only", False),
        "validated_decisions": decisions,
        "premarket_research_runs": len(premarket),
        "latest_premarket_research": premarket[-1] if premarket else None,
        "blocked_iterations": sum(row["kind"] == "blocked" for row in notifications),
        "observations": len(curve), "starting_equity": initial,
        "ending_equity": curve[-1]["equity"] if curve else None,
        "return": curve[-1]["equity"] / initial - 1 if initial and curve else None,
        "max_observed_drawdown": observed_drawdown, "max_daily_drawdown": max_drawdown,
        "daily_sharpe_zero_risk_free": sharpe,
        "benchmark_price_returns": comparison, "fills_observed": len(fills),
        "observed_fees": local_fees + sum(fee_by_order.values()) if not unknown_fee else None,
        "fill_details_incomplete": incomplete_fills,
        "model_usage": totals, "model_cost_estimate_usd": estimate,
        "latest_account": accounts[-1] if accounts else None,
        "limitations": "Daily sampled drawdown; benchmark price returns exclude dividends and fees. "
                        "No claim of predictive performance from scripted models. Cash flows are not return-adjusted; use a dedicated unfunded-during-run account."
    }
    if not write_files:
        return summary
    (root / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if premarket:
        (root / "premarket_research.json").write_text(json.dumps(premarket[-1], indent=2, ensure_ascii=False), encoding="utf-8")
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
