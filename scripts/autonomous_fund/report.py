"""Rebuild local reports from the durable journal; never fabricate unknown costs."""
import csv
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

from .risk import instant, trading_day


REPORT_KINDS = ("account", "benchmark", "fill", "fill_reconciled", "decision_validated", "fill_details_unavailable",
                "agent", "notification", "startup", "premarket_research", "postmarket_review",
                "agent_call_started", "agent_call_finished", "risk_overlay", "decision_backoff", "scorecard",
                "protective_trigger", "price_alert", "review_triggered", "mandate_initialized",
                "mandate_capital_changed", "operator_reserve_adjusted", "decision_accepted",
                "allocation_complete", "operator_approval")



from .costs import call_cost, cost_summary, rate_for, token_counts  # noqa: F401  (token_counts: rehearsal)


def _historical_state(events, approvals, cutoff):
    """Rebuild mutable report views from evidence already visible at the requested instant."""
    mandate, target = {}, None
    visible, plans = [], {}
    for row in approvals:
        body = json.loads(row["body"])
        if body.get("created_at") and instant(body["created_at"]) <= cutoff:
            visible.append({**row, "state": "pending"})
            plans[row["id"]] = body
    for at, kind, body in events:
        if kind == "mandate_initialized":
            mandate = {"mandate_capital_basis": body.get("capital_usd"),
                       "mandate_reserve_cash": body.get("reserved_cash")}
        elif kind == "mandate_capital_changed":
            mandate.update(mandate_capital_basis=body.get("to"), mandate_reserve_cash=body.get("reserved_cash"))
        elif kind == "operator_reserve_adjusted":
            mandate["mandate_reserve_cash"] = body.get("reserved_cash")
        elif kind == "decision_accepted":
            target = [body["id"], "active", body["until_at"], json.dumps(body["decision"])]
        elif kind == "allocation_complete" and target and body["decision_id"] == target[0]:
            target[1] = "complete"
        if kind not in {"decision_accepted", "operator_approval"} and not (
                kind == "notification" and body.get("kind") == "approval_required"):
            continue
        replacement_id = (body.get("details") or {}).get("id") if kind == "notification" else None
        replacement = plans.get(replacement_id)
        for row in visible:
            plan = plans[row["id"]]
            if instant(plan["created_at"]) > instant(at):
                continue
            superseded = (kind == "decision_accepted" and plan.get("decision_id") != body["id"] or
                          replacement is not None and replacement_id != row["id"] and
                          replacement.get("decision_id") == plan.get("decision_id"))
            if superseded and row["state"] in {"pending", "approved"}:
                row["state"] = "superseded"
            elif kind == "operator_approval" and body["id"] == row["id"]:
                row["state"] = body["state"]
                if body["state"] == "rejected" and target and plan.get("decision_id") == target[0]:
                    target[1] = "rejected"
    return mandate, target, visible


def report(root, prices=None, write_files=True, *, as_of=None):
    if as_of is not None and write_files:
        raise ValueError("Historical report snapshots must not overwrite current reports")
    root = Path(root)
    # sqlite3's own context manager only commits; closing() releases the handle.
    with closing(sqlite3.connect(root / "state.sqlite3")) as db:
        db.execute("BEGIN")  # Events and immutable request bodies come from the same snapshot.
        from .ledger_audit import audit_connection
        integrity = audit_connection(db) if as_of is None else {"status": "not_checked", "reason": "Historical cutoff"}
        # Only the kinds a report uses; per-minute liveness rows are skipped.
        invalid_accounts = set(integrity.get("invalid_account_sequences", []))
        cutoff = instant(as_of) if as_of is not None else None
        kinds = [kind for kind in REPORT_KINDS if kind not in ("account", "benchmark")]
        sequenced = [(sequence, at, kind, json.loads(body)) for sequence, at, kind, body in db.execute(
            "SELECT sequence,at,kind,body FROM events WHERE kind IN (%s) ORDER BY sequence" % ",".join("?" * len(kinds)),
            kinds) if cutoff is None or instant(at) <= cutoff]
        events = [(at, kind, body) for _, at, kind, body in sequenced]
        # NAV marks and benchmarks (one each about every 30 seconds of market
        # time): ordering, the closing-mark rule, the daily curve and drawdown
        # use light fields of every mark; bodies are read only for marks shown.
        from .evaluation import benchmark_events, event_bodies, nav_marks, outcome_inputs
        marks = nav_marks(db, until=cutoff, excluded=invalid_accounts)
        light = sorted(marks, key=lambda mark: mark[2])  # Stable: sequence order within one instant.
        if light and light[-1][4]:
            closing_mark, close_cutoff = light[-1], instant(light[-1][4])
            # Older runners rewrote identical off-hours account observations. A
            # recovered, source-proven close supersedes those observation clones.
            light = [mark for mark in light if mark is closing_mark or
                     not (close_cutoff < mark[2] <= closing_mark[2] and not mark[4])]
        daily = {}
        for mark in light:
            daily[trading_day(instant(mark[5] or mark[1]) if mark[4] else mark[2])] = mark
        shown = {mark[0] for mark in daily.values()} | ({light[0][0], light[-1][0]} if light else set())
        bodies, shown_accounts = event_bodies(db, shown), {}
        for sequence, at, *_ in light:
            if sequence in shown and sequence not in shown_accounts:
                body = bodies[sequence]
                shown_accounts[sequence] = {
                    "at": at, "equity": float(body["equity"]), "cash": float(body["cash"]),
                    "as_of": body.get("as_of") or at, "positions": body["positions"],
                    "position_average_prices": body.get("position_average_prices") or {},
                    "cash_equivalents": body.get("cash_equivalents") or {},
                    "cash_equivalent_value": body.get("cash_equivalent_value", 0),
                    "receivables": float(body.get("receivables", 0)),
                    **({"valuation": body["valuation"]} if "valuation" in body else {})}
        first_account = shown_accounts[light[0][0]] if light else None
        last_account = shown_accounts[light[-1][0]] if light else None
        benchmarks = {}
        for _, at, _, body in benchmark_events(db, {instant(row["at"]) for row in (first_account, last_account) if row},
                                               until=cutoff):
            benchmarks[instant(at)] = body
        outcome_events = [(at, kind, body) for _, at, kind, body in sorted(
            sequenced + outcome_inputs(db, marks, until=cutoff), key=lambda row: row[0])]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        approval_rows = [dict(zip(("id", "digest", "state", "body"), r)) for r in db.execute(
            "SELECT id,digest,state,body FROM fund_approvals ORDER BY rowid")] if "fund_approvals" in tables else []
        target = db.execute("SELECT id,state,until_at,body FROM fund_decisions ORDER BY rowid DESC LIMIT 1").fetchone() if "fund_decisions" in tables else None
        mandate_meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('mandate_reserve_cash','mandate_capital_basis')"))
    if as_of is not None:
        events = [row for row in events if instant(row[0]) <= instant(as_of)]
        # Current meta/table states may have changed after the report's cutoff.
        mandate_meta, target, approval_rows = _historical_state(events, approval_rows, instant(as_of))
    now = instant(as_of) if as_of is not None else datetime.now(timezone.utc)
    for row in approval_rows:
        row["body"] = json.loads(row["body"])
        row["effective_state"] = "expired" if row["state"] in {"pending", "approved"} and instant(row["body"]["expires_at"]) <= now else row["state"]
    from .evaluation import forward_outcomes
    outcomes = forward_outcomes(outcome_events)
    monitor = None
    if as_of is None and (root / "monitor.json").is_file():
        try:
            monitor = json.loads((root / "monitor.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            monitor = {"status": "unavailable"}
    fills, usage, notifications, reconciled = [], [], [], {}
    decisions = 0
    incomplete_fills = False
    startup = next((body for _, kind, body in reversed(events) if kind == "startup"), {})
    premarket = [body for _, kind, body in events if kind == "premarket_research"]
    risk_overlays = [{"at": at, **body} for at, kind, body in events if kind == "risk_overlay"]
    scorecards = [body for _, kind, body in events if kind == "scorecard"]
    triggers = {name: [{"at": at, **body} for at, kind, body in events if kind == name]
                for name in ("protective_trigger", "price_alert", "review_triggered")}
    mandate_events = [{"at": at, "kind": kind, **body} for at, kind, body in events
                      if kind in {"mandate_initialized", "mandate_capital_changed", "operator_reserve_adjusted"}]
    backoffs = sum(kind == "decision_backoff" for _, kind, _ in events)
    reviews = [body for _, kind, body in events if kind == "postmarket_review"]
    for at, kind, body in events:
        if kind == "fill":
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
    curve = [shown_accounts[daily[day][0]] for day in sorted(daily)]
    peak, max_drawdown, previous = 0, 0, None
    for row in curve:
        peak = max(peak, row["equity"])
        if peak > 0:
            max_drawdown = max(max_drawdown, 1 - row["equity"] / peak)
        if previous is None:
            row["daily_return"] = 0 if row["equity"] > 0 else None
        else:
            row["daily_return"] = row["equity"] / previous - 1 if previous > 0 else None
        previous = row["equity"]
    initial = first_account["equity"] if light else None
    observed_peak, observed_drawdown = 0, 0
    for row in light:
        equity = float(row[3])
        observed_peak = max(observed_peak, equity)
        if observed_peak > 0:
            observed_drawdown = max(observed_drawdown, 1 - equity / observed_peak)
    returns = [row["daily_return"] for row in curve[1:]]
    sharpe = None
    if len(returns) > 1 and all(value is not None for value in returns):
        mean = sum(returns) / len(returns)
        variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
        sharpe = mean / math.sqrt(variance) * math.sqrt(252) if variance > 0 else None
    comparison = {}
    for symbol in ("SPY", "QQQ"):
        # Compare the same first/last instants as the account's total return. A mark earlier
        # that day cannot stand in for a missing endpoint after a source failure.
        anchors = [first_account, last_account] if light else []
        endpoints = [benchmarks.get(instant(row["at"]), {}).get(symbol) for row in anchors]
        try:
            benchmark_prices = [float(value) for value in endpoints]
        except (TypeError, ValueError):
            benchmark_prices = []
        comparison[symbol] = (benchmark_prices[-1] / benchmark_prices[0] - 1
                              if len(light) > 1 and len(benchmark_prices) == 2 and
                              all(math.isfinite(value) and value > 0 for value in benchmark_prices) else None)
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
        for key, value in token_counts(raw).items():
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
            # Cached prompt tokens at the operator's cached_input rate (ordinary input when not given).
            estimates_by_model = {model: call_cost(counts, rates[model]) for model, counts in by_model.items()}
            estimate = sum(estimates_by_model.values())
    from .holdings import recent_trades
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
        # Identical consecutive blocks are journaled once per 30 minutes.
        "blocked_iterations": sum(row["kind"] == "blocked" for row in notifications),
        "latest_risk_overlay": risk_overlays[-1] if risk_overlays else None,
        "latest_scorecard": scorecards[-1] if scorecards else None,
        "protective_triggers": triggers["protective_trigger"][-20:],
        "price_alerts": triggers["price_alert"][-20:],
        "review_triggers": {"count": len(triggers["review_triggered"]), "recent": triggers["review_triggered"][-10:]},
        "mandate": {"capital_usd": mandate_meta.get("mandate_capital_basis"),
                    "reserved_cash": mandate_meta.get("mandate_reserve_cash"), "events": mandate_events[-10:]},
        "decision_backoffs": backoffs,
        "observations": len(curve), "starting_equity": initial,
        "ending_equity": last_account["equity"] if light else None,
        "return": last_account["equity"] / initial - 1 if initial is not None and initial > 0 and light else None,
        "max_observed_drawdown": observed_drawdown if observed_peak > 0 else None,
        "max_daily_drawdown": max_drawdown if peak > 0 else None,
        "daily_sharpe_zero_risk_free": sharpe,
        "benchmark_price_returns": comparison, "fills_observed": len(fills),
        "observed_fees": local_fees + sum(fee_by_order.values()) if not unknown_fee else None,
        "fill_details_incomplete": incomplete_fills,
        "model_usage": totals, "model_usage_by_model": by_model,
        "model_costs": cost_summary([{"at": row["at"], "kind": "agent", "body": row} for row in usage],
                                    last_account["equity"] if light else None, as_of or now, prices),
        "unfinished_model_calls": list(unfinished.values()),
        "model_cost_estimate_usd": estimate, "model_cost_estimate_by_model_usd": estimates_by_model,
        "latest_account": last_account,
        "recent_trades": recent_trades(events, last_account["as_of"]) if light else [],
        "ledger_audit": integrity,
        "limitations": "Daily sampled drawdown; benchmark price returns exclude dividends and fees. "
                        "No claim of predictive performance from scripted models. Cash flows are not return-adjusted; use a dedicated unfunded-during-run account."
    }
    if integrity["status"] == "failed":
        # Retain the audit evidence, but do not publish disputed balances or
        # trade prices as usable account facts to operators and research tools.
        for field in ("latest_account", "ending_equity", "return", "observed_fees", "max_observed_drawdown",
                      "max_daily_drawdown", "daily_sharpe_zero_risk_free"):
            summary[field] = None
        summary["recent_trades"] = []
    if not write_files:
        return summary
    (root / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if integrity["status"] == "failed":
        return summary  # Do not overwrite last-good equity/fill exports with disputed data.
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
