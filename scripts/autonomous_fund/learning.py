"""Version-bound learning evidence from independent, costed virtual books.

No inference, provider requests or broker mutations. Frozen native risk estimates
retain their receipt time; missing evidence never votes for a trading-arm switch.
"""
import copy
import json

from .risk import Account, Limits, RiskError, instant, number, trading_day
from .scorecard import _exchange_sessions, _idea_session_close

COST_SCOPE = "paired_managers_half_fresh_critic_v1"
ACCOUNT_MODE = "independent_prior_close_v1"


def pack_estimate(estimate):
    return {"symbols": list(estimate.symbols), "missing": sorted(estimate.missing),
        "observations": estimate.observations, "through": estimate.through,
        "vol": estimate.vol, "raw_vol": estimate.raw_vol, "cov": estimate.cov.tolist(),
        "correlation": estimate.correlation.tolist(), "scenarios_history": estimate.scenarios_history.tolist(),
        "scenarios": estimate.scenarios, "styles": estimate.styles}


def unpack_estimate(body, policy):
    import numpy as np
    from .quant import RiskEstimate
    estimate = object.__new__(RiskEstimate)
    estimate.policy, estimate.benchmark = policy, policy.stress_benchmark
    estimate.symbols, estimate.missing = tuple(body["symbols"]), frozenset(body["missing"])
    for name in ("observations", "through", "vol", "raw_vol", "scenarios", "styles"):
        setattr(estimate, name, copy.deepcopy(body[name]))
    n = len(estimate.symbols)
    for name in ("cov", "correlation", "scenarios_history"):
        value = np.asarray(body[name], dtype=float)
        if name == "scenarios_history" and not n:
            value = np.zeros((0, 0))
        if not np.isfinite(value).all() or (name != "scenarios_history" and value.shape != (n, n)) or (
                name == "scenarios_history" and (value.ndim != 2 or value.shape[1] != n)):
            raise RiskError("Invalid frozen risk matrix")
        setattr(estimate, name, value)
    return estimate


def risk_overlay(events, declaration):
    from .quant import QuantPolicy, construct, drawdown_scale
    try:
        if not declaration.get("quant_risk"):
            raise RiskError("No frozen quantitative policy")
        limits = Limits(**declaration["limits"])
        policy = QuantPolicy(**declaration["quant_risk"])
        policy.validate(limits)
    except (RiskError, TypeError, KeyError, ValueError):
        def unavailable(*args):
            raise RiskError("Frozen quantitative policy unavailable; no legacy defaults assumed")
        return unavailable
    rows = [(instant(at), b) for _, at, kind, b in events if kind == "comparison_risk"
            and b.get("protocol_id") == declaration["id"]]
    def overlay(day, desired, current, equity, peak, previous, latched=False):
        available = [(at, b) for at, b in rows if at < _idea_session_close(day) and trading_day(at) == day]
        if not available:
            raise RiskError("Missing same-session frozen quantitative risk evidence")
        at, packet = available[-1]
        if packet["estimate"].get("through") and packet["estimate"]["through"] >= day:
            raise RiskError("Risk history was not completed at capture time")
        estimate = unpack_estimate(packet["estimate"], policy)
        dd = 1 - equity / peak
        affected = {s for s, w in desired.items() if w > 0} | {s for s, w in current.items() if w > 0}
        desired = dict(desired)
        paused = (latched or dd >= limits.drawdown or (previous > 0 and 1 - equity / previous >= policy.daily_loss_limit)
                  or packet.get("stressed"))
        if paused:
            desired = {s: min(w, current.get(s, number(0))) for s, w in desired.items()}
        for symbol, cap in packet.get("earnings_caps", {}).items():
            if symbol in desired:
                desired[symbol] = min(desired[symbol], number(cap))
        budget = limits.max_exposure * drawdown_scale(dd, policy, limits.drawdown)
        result, report = construct(desired, current, estimate, policy, limits, budget)
        if paused:
            report["binding"].append("daily_loss_or_stress_or_drawdown_no_increase")
        def verify(actual):
            projected, _ = construct(actual, actual, estimate, policy, limits, budget)
            if any(w - projected.get(s, number(0)) > number("0.000001") for s, w in actual.items()):
                raise RiskError("Whole-share quantitative post-condition unavailable")
        overlay.verify = verify
        return result, {"binding": report["binding"], "history_through": estimate.through,
                        "complete": bool(packet.get("complete")) and not bool(affected & set(packet.get("earnings_unknown", []))),
                        "risk_receipt_at": at.isoformat(), "constructed_metrics": estimate.metrics(result),
                        "constructed_exposures": estimate.exposures(result)}
    return overlay


def pair_costs(body, calls, charged):
    result = {arm: None for arm in ("memo", "baseline")}
    links = body.get("comparison_calls")
    if body.get("comparison_cost_scope") != COST_SCOPE or not isinstance(links, dict):
        return result
    for arm in result:
        own, shared = links.get(arm, {}).get("own"), links.get(arm, {}).get("shared")
        if not own or not isinstance(shared, list) or len(set(own + shared)) != len(own + shared):
            continue
        amount = number(0)
        for key, fraction in [(k, number(1)) for k in own] + [(k, number("0.5")) for k in shared]:
            cost = calls.get(key)
            if cost is None:
                break
            if (arm, key) not in charged:
                amount += number(cost) * fraction
        else:
            result[arm] = amount
            charged.update((arm, key) for key in own + shared)
    return result


def managed_summary(book):
    rows, peak, drawdown = book.series, book.capital, number(0)
    for row in rows:
        if row["net_economic_equity"] is not None:
            value = number(row["net_economic_equity"])
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
    return {**book.summary(), "model_cost_usd": str(book.model_cost), "costs_complete": not book.cost_unknown and
            book.policy.get("fee_rate") is not None and book.policy.get("slippage") is not None,
        "net_return_after_linked_costs": float((number(book.last["equity"]) - book.model_cost) / book.capital - 1)
            if book.last and not book.error and not book.cost_unknown and book.observations > 1 else None,
        "max_economic_drawdown": float(drawdown) if not book.cost_unknown and not book.error and len(rows) > 1 else None,
        "series": rows, "risk_overlays": book.overlays,
        "cost_scope": COST_SCOPE}


def snapshot(journal, now):
    """Only closing receipts and bounded event classes; no whole-history NAV bodies."""
    from .comparison import evaluate
    from .evaluation import session_benchmarks
    from .session_calendar import completed_daily_cutoff
    from .call_value import _known_cost
    cutoff = instant(now)
    kinds = ("startup", "shadow_decision", "benchmark_action", "local_corporate_action",
             "comparison_corporate_action", "comparison_risk", "comparison_cohort_started")
    with journal.connection() as db:
        db.execute("BEGIN")
        events = [(s, at, kind, json.loads(raw)) for s, at, kind, raw in db.execute(
            "SELECT sequence,at,kind,body FROM events WHERE kind IN (%s) AND julianday(at)<=julianday(?) ORDER BY sequence"
            % ",".join("?" for _ in kinds), (*kinds, cutoff.isoformat())) if instant(at) <= cutoff]
        closing = {}
        for seq, at, raw in db.execute("SELECT sequence,at,body FROM events WHERE kind='account' "
                "AND json_extract(body,'$.valuation.session_close') IS NOT NULL "
                "AND julianday(at)<=julianday(?) ORDER BY sequence", (cutoff.isoformat(),)):
            body = json.loads(raw)
            close = instant(body["valuation"]["session_close"])
            if (instant(at) <= cutoff and close <= cutoff and body["valuation"].get("book_existed_at_close", True)
                    and (body.get("ledger_audit") or {}).get("status") != "failed"):
                closing[trading_day(close)] = seq, close, body
        marks_by_day = session_benchmarks(db, closing.values(), until=cutoff)
        marks = {}
        for day, prices in marks_by_day.items():
            for symbol, value in prices.items():
                marks.setdefault(symbol, {})[day] = value
        calls = {key: _known_cost({"cost_usd": cost, "mock": mock, "cache_hit": hit, "model": model})
            for at, key, cost, mock, hit, model in db.execute("SELECT at,json_extract(body,'$.call_id'),"
                "json_extract(body,'$.cost_usd'),json_extract(body,'$.mock'),json_extract(body,'$.cache_hit'),"
                "json_extract(body,'$.model') FROM events WHERE kind='agent' AND julianday(at)<=julianday(?) ORDER BY sequence",
                (cutoff.isoformat(),)) if instant(at) <= cutoff and key}
    sessions = _exchange_sessions(min(closing), str(completed_daily_cutoff(cutoff))) if closing else ()
    return evaluate(events, marks, {d: row[2] for d, row in closing.items()}, sessions, calls=calls)


def current_cohort(report, protocol, prompt):
    rows = [r for r in report.get("shadow", []) if r.get("declared_at") == report.get("active_epoch_at")]
    row = rows[-1] if rows else None
    starts = [r for r in report.get("cohort_starts", []) if r.get("protocol_id") == protocol and
              r.get("prompt_version") == prompt and instant(r["at"]) >= instant(report["active_epoch_at"])]
    if starts and row and row.get("generation") != starts[-1]["generation"]:
        return None
    return row if row and row["protocol_id"] == protocol and row["prompt_version"] == prompt else None


def evidence(report, protocol, prompt, min_pairs):
    from .validation import paired_test
    row = current_cohort(report, protocol, prompt)
    differences, reason = [], "waiting_for_current_profile_independent_costed_books"
    if row and row.get("independent_context"):
        arms = row["risk_managed"]
        if any(b.get("unavailable") for b in arms.values()):
            reason = "incomplete_quantitative_book"
        elif any(not b["costs_complete"] for b in arms.values()):
            reason = "unpriced_linked_calls"
        elif any(not o.get("complete") for b in arms.values() for o in b["risk_overlays"]):
            reason = "incomplete_frozen_risk_evidence"
        if all(b["status"] == "observed" and b["costs_complete"] and
               all(o.get("complete") for o in b["risk_overlays"]) for b in arms.values()):
            series = {a: b["series"] for a, b in arms.items()}
            days = [r["day"] for r in series["memo"]]
            if days == [r["day"] for r in series["baseline"]]:
                for i in range(1, len(days)):
                    pair = {}
                    for arm in arms:
                        before = number(arms[arm]["capital"]) if i == 1 else number(series[arm][i-1]["net_economic_equity"])
                        after = number(series[arm][i]["net_economic_equity"])
                        if before <= 0 or after <= 0:
                            return paired_test([], min_pairs), [], "nonpositive_economic_nav"
                        pair[arm] = float(after / before - 1)
                    differences.append(pair["memo"] - pair["baseline"])
                reason = "current_profile_risk_managed_linked_costs"
    return paired_test(differences, min_pairs), differences, reason


def independent_context(strategy, context, arm, now):
    """The non-trading manager sees its own prior-close book, never copied holdings."""
    protocol, prompt = strategy.comparison_protocol["id"], strategy.prompt_version
    report = snapshot(strategy.journal, now)
    row = current_cohort(report, protocol, prompt)
    # A data hole remains visible in its old cohort. A fresh forward cohort can
    # start, but must earn its full sample again; no missing day is filled with 0.
    generation = strategy.journal.get_meta("comparison_generation") or {}
    if ((generation.get("protocol_id"), generation.get("prompt_version")) != (protocol, prompt) or
            (row and any(b.get("unavailable") or not b["costs_complete"] or
                any(not o.get("complete") for o in b["risk_overlays"]) for b in row["risk_managed"].values()))):
        from uuid import uuid4
        generation = {"protocol_id": protocol, "prompt_version": prompt, "generation": uuid4().hex,
                      "reason": "incomplete_previous_cohort" if row else "new_profile"}
        strategy.journal.event(now, "comparison_cohort_started", generation)
        strategy.journal.set_meta("comparison_generation", generation)
        row = None
    prior, previous = None, None
    if row:
        book = row["risk_managed"][arm]
        prior = book.get("last")
        previous = row.get("latest_decisions", {}).get(arm)
    capital = number(row["risk_managed"][arm]["capital"] if row else context["account"]["equity"])
    account = {"cash": str(capital), "equity": str(capital), "positions": {}, "as_of": instant(now).isoformat(),
               "open_orders": False, "receivables": "0", "cash_equivalents": {}, "cash_equivalent_value": "0",
               "position_average_prices": {}, "position_prices": {}}
    if prior:
        account.update(cash=prior["cash"], equity=prior["equity"], positions=prior["positions"],
                       as_of=_idea_session_close(prior["day"]).isoformat(),
                       position_average_prices=prior.get("position_average_prices", {}),
                       position_prices=prior.get("position_prices", {}))
    risk = copy.deepcopy(context.get("risk") or {})
    for key in ("drawdown", "drawdown_exposure_budget", "daily_loss", "session_start_exposure", "observed_purchase_cash_today"):
        risk.pop(key, None)
    from .quant import QuantPolicy, drawdown_scale
    policy, limits = QuantPolicy(**strategy.comparison_protocol["quant_risk"]), Limits(**strategy.comparison_protocol["limits"])
    peak = number(row["risk_managed"][arm]["peak_equity"]) if row else capital
    dd = max(number(0), 1 - number(account["equity"]) / peak)
    risk.update(drawdown=str(dd), drawdown_exposure_budget=str(limits.max_exposure * drawdown_scale(dd, policy, limits.drawdown)),
        session_start_exposure=str((number(account["equity"]) - number(account["cash"])) / number(account["equity"])),
        observed_purchase_cash_today="0", marking="independent_prior_close")
    risk.update(scope="independent_shadow_prior_close", purchases_paused=bool(prior and prior["risk_increase_paused"]))
    exposures = risk.get("exposures") or {}
    scores = exposures.get("name_scores") or {}
    weights = {s: number(q) * number(account["position_prices"][s]) / number(account["equity"])
               for s, q in account["positions"].items()}
    names = {"beta"} | {name for per_name in scores.values() for name in per_name}
    exposures["current"] = {name: float(sum((w * number(scores[s].get(name, 0)) for s, w in weights.items()), number(0)))
                            for name in names} if set(weights) <= set(scores) else None
    risk["exposures"] = exposures
    from .trade_value import holding_math
    arithmetic = holding_math(Account(**{**account, "as_of": instant(account["as_of"])}))
    arithmetic["basis"] = "independent_shadow_prior_close_not_executable_quotes"
    result = {**context, "account": account, "holding_math": arithmetic, "previous_decision": previous,
        "recent_trades": prior.get("trades", []) if prior else [], "risk": risk,
        "risk_increase_paused": risk["purchases_paused"], "affordability": None, "day_trades": None,
        "fund_capital": None, "fund_scoreboard": None, "trade_value": None, "ledger_audit": None,
        "comparison_account": {"mode": ACCOUNT_MODE, "as_of": account["as_of"],
            "generation": generation["generation"],
            "linked_model_cost_usd": row["risk_managed"][arm]["model_cost_usd"] if row else "0",
            "linked_costs_complete": row["risk_managed"][arm]["costs_complete"] if row else True,
            "meaning": "Virtual USD/whole-share book at its last completed close; real broker tools describe the trading account. "
                       "Do not substitute its holdings/cash for this shadow account. Research and critique remain shared."}}
    return result
