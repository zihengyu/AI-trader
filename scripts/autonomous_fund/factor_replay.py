"""Offline paired replay of a declared price/volume factor and benchmark."""
import ast
from collections import defaultdict
from dataclasses import asdict
from datetime import date

import pandas as pd

from .comparison import ONE, ZERO, declaration, project
from .factors import DATA, _factor_warmup, canonical, compute, evaluate_factor, parse
from .quant import QuantPolicy
from .risk import Limits, RiskError, instant, number
from .strategy_validation import (ResearchBook, _daily_sessions, check_snapshot_as_of,
                                  digest, metrics, signal_cutoff, snapshot_limitations, source_artifact_gaps)


FIELDS = ("open", "high", "low", "close", "volume")
ADV_SESSIONS = 20
PLAN_FIELDS = {"instruments", "start", "end", "capital", "direction", "top_n", "rebalance_sessions",
               "benchmark", "benchmark_rationale", "target_exposure", "cash_annual_rate",
               "fee_bps", "spread_bps", "slippage_bps", "signal_delay_minutes"}


def _plan(spec, config):
    if spec.keys() != PLAN_FIELDS:
        missing, unknown = sorted(PLAN_FIELDS - spec.keys()), sorted(spec.keys() - PLAN_FIELDS)
        raise RiskError(f"Invalid factor replay fields (missing={missing}, unknown={unknown})")
    universe = config["universe"]
    instruments, benchmark = spec["instruments"], spec["benchmark"]
    if (not isinstance(instruments, list) or not all(isinstance(s, str) for s in instruments)
            or len(set(instruments)) < 2 or len(set(instruments)) != len(instruments)):
        raise RiskError("Factor replay requires at least two distinct instruments")
    if not isinstance(benchmark, dict) or not benchmark:
        raise RiskError("Factor replay requires declared benchmark weights")
    symbols = set(instruments) | benchmark.keys()
    if any(universe.get(s) not in {"stock", "unlevered_etf", "leveraged_etf"} for s in symbols):
        raise RiskError("Factor replay instruments must be eligible fund securities")
    limits = Limits(**config["limits"])
    quant = QuantPolicy.from_config(config.get("quant_risk"), universe, limits)
    weight = {s: number(v) for s, v in benchmark.items()}
    if any(v <= ZERO for v in weight.values()) or sum(weight.values(), ZERO) != ONE:
        raise RiskError("Factor benchmark weights must be positive and sum to one")
    rationale = spec["benchmark_rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise RiskError("Document the benchmark's risk-matching rationale")
    for key, low, high in (("top_n", 1, len(instruments) - 1), ("rebalance_sessions", 1, 252),
                           ("signal_delay_minutes", 0, 720)):
        if type(spec[key]) is not int or not low <= spec[key] <= high:
            raise RiskError(f"Factor replay {key} must be a whole number from {low} to {high}")
    if type(spec["direction"]) is not int or spec["direction"] not in {-1, 1}:
        raise RiskError("Factor replay direction must be 1 or -1")
    for key in ("start", "end"):
        try:
            if date.fromisoformat(spec[key]).isoformat() != spec[key]:
                raise ValueError
        except (ValueError, TypeError):
            raise RiskError(f"Factor replay {key} must be a YYYY-MM-DD exchange session") from None
    capital, gross, cash_rate = (number(spec[k]) for k in ("capital", "target_exposure", "cash_annual_rate"))
    if capital <= ZERO or not ZERO < gross <= limits.max_exposure or not -ONE < cash_rate <= ONE:
        raise RiskError("Invalid factor replay capital, exposure or cash scenario")
    fee, spread, slip = (number(spec[k]) / 10000 for k in ("fee_bps", "spread_bps", "slippage_bps"))
    if min(fee, spread, slip) < ZERO or fee > number(".01") or slip + spread / 2 > number(".015"):
        raise RiskError("Factor trading costs exceed the shared replay bounds")
    policy = declaration(asdict(limits), asdict(quant), {s: universe[s] for s in sorted(symbols)}, {},
                         fee_rate=fee, slippage=slip + spread / 2)
    return policy, weight, quant.max_adv_participation


def _market_window(data, spec, symbols, depth):
    missing = set(symbols) - data["bars"].keys()
    if missing:
        raise RiskError("Missing factor replay history for " + ", ".join(sorted(missing)))
    first = min(data["bars"][s][0]["day"] for s in symbols)
    sessions = _daily_sessions(first, spec["end"])
    days = [day for day, _, _ in sessions]
    start = spec["start"]
    if start not in days or spec["end"] not in days or days.index(start) < depth or start == spec["end"]:
        raise RiskError("Factor replay requires exchange-session endpoints and complete prior-session warm-up")
    sessions = sessions[days.index(start) - depth:]
    days = [day for day, _, _ in sessions]
    bars = {s: {r["day"]: r for r in data["bars"][s] if days[0] <= r["day"] <= days[-1]} for s in symbols}
    for s, rows in bars.items():
        if list(rows) != days:
            raise RiskError(f"Factor replay has missing exchange sessions for {s}")
    calendar = {day: opened for day, opened, _ in sessions}
    actions = defaultdict(list)
    split_keys = set()
    for row in data.get("actions", ()):
        day, symbol = row["ex_date"], row["symbol"]
        if symbol not in bars or not days[0] <= day <= days[-1]:
            continue
        if (day not in calendar or instant(row["available_at"]) > calendar[day]
                or set(row) - {"symbol", "ex_date", "pay_date", "split", "dividend", "available_at", "id", "source"}
                or not ({"split", "dividend"} & row.keys())):
            raise RiskError(f"Unsupported or late corporate action for {symbol} on {day}")
        if "split" in row:
            if (symbol, day) in split_keys:
                raise RiskError(f"Conflicting split receipts for {symbol} on {day}")
            split_keys.add((symbol, day))
        actions[day].append(row)
    return sessions, bars, actions


def _signal_inputs(sessions, bars, actions, spec):
    panels = {field: [] for field in FIELDS}
    dollar_volume, cutoffs = [], []
    shares = dict.fromkeys(bars, ONE)
    for day, _, closed in sessions:
        cutoff = instant(signal_cutoff(closed, spec))
        cutoffs.append(cutoff.isoformat())
        for row in actions[day]:
            shares[row["symbol"]] *= number(row.get("split", 1))
        adjusted = {field: {} for field in FIELDS}
        liquidity = {}
        for symbol, rows in bars.items():
            row = rows[day]
            published = row.get("feature_available_at") or {}
            # Use only completed bars that were available at this session's signal.
            if max(instant(row["available_at"]), *(instant(published.get(f, row["available_at"])) for f in FIELDS)) > cutoff:
                raise RiskError(f"Factor bar unavailable at the signal cutoff for {symbol} on {day}")
            for field in FIELDS:
                raw = number(row[field])
                adjusted[field][symbol] = float(raw / shares[symbol] if field == "volume" else raw * shares[symbol])
            liquidity[symbol] = row["close"] * row["volume"]
        for field, frame in panels.items():
            frame.append(adjusted[field])
        dollar_volume.append(liquidity)
    index = pd.DatetimeIndex([day for day, _, _ in sessions])
    return ({field: pd.DataFrame(rows, index=index) for field, rows in panels.items()},
            pd.DataFrame(dollar_volume, index=index).rolling(ADV_SESSIONS).mean(), cutoffs)


def _paired_targets(scores, spec, benchmark, policy):
    ordered = (spec["direction"] * scores).dropna().sort_index().sort_values(ascending=False, kind="stable")
    if len(ordered) != len(spec["instruments"]) or ordered.nunique() < 2:
        raise RiskError("The declared factor has missing or indistinguishable ranks")
    gross = number(spec["target_exposure"])
    selected = project(dict.fromkeys(ordered.index[:spec["top_n"]], gross / spec["top_n"]), policy)
    control = project({s: gross * w for s, w in benchmark.items()}, policy)
    totals = [sum(arm.values(), ZERO) for arm in (selected, control)]
    common = min(totals)
    if common <= ZERO:
        raise RiskError("The factor and benchmark have no feasible common exposure")
    return [{s: w * common / total for s, w in arm.items()} for arm, total in zip((selected, control), totals)]


def _book_session(book, day, opened, closed, actions, targets, liquidity,
                  participation, cash_per_session, *, rebalance, signal_as_of):
    interest = book.cash * cash_per_session if book.observations else ZERO
    book.cash += interest + sum((number(r["amount"]) for r in book.receivables if r["pay_date"] <= day), ZERO)
    book.receivables = [r for r in book.receivables if r["pay_date"] > day]
    ratios = {row["symbol"]: number(row["split"]) for row in actions if "split" in row}
    splits = [{"symbol": s, "date": day, "split": ratio} for s, ratio in ratios.items()]
    # Dividends belong to holders entering the ex-date, even when they sell at
    # the open. Shares and dividend rates use the post-split basis that day.
    entry_shares = {s: q * ratios.get(s, ONE) for s, q in book.positions.items()}
    for row in actions:
        symbol = row["symbol"]
        amount = entry_shares.get(symbol, ZERO) * number(row.get("dividend", 0))
        if amount and row["pay_date"] == day:
            book.cash += amount
        elif amount:
            book.receivables.append({"symbol": symbol, "pay_date": row["pay_date"], "amount": str(amount)})
    available = book.cash + sum((q * number(opened[s]) for s, q in entry_shares.items()), ZERO)
    equity = available + sum((number(r["amount"]) for r in book.receivables), ZERO)
    desired = {s: w * available / equity for s, w in targets.items()}
    book.step(day, desired, opened, splits, hold_positions=not rebalance)
    if book.error:
        raise RiskError(book.error["reason"])
    for trade in book.last["trades"]:
        symbol = trade["symbol"]
        adv = number(liquidity[symbol])
        if trade["side"] == "buy" and adv < number(book.policy["limits"]["min_dollar_volume"]):
            raise RiskError(f"Opening purchase is below the lagged liquidity floor for {symbol}")
        if trade["quantity"] * number(opened[symbol]) > participation * adv:
            raise RiskError(f"Opening order exceeds lagged dollar-volume capacity for {symbol}")
        trade["scope"] = "virtual_next_open_scenario"
    equity = book.value(opened)
    weights = {s: q * number(opened[s]) / equity for s, q in book.positions.items()}
    allowed = project(weights, book.policy)
    if any(w - allowed.get(s, ZERO) > number(".0000000001") for s, w in weights.items()):
        raise RiskError("Post-fill holdings exceed the frozen portfolio caps")
    nav = book.value(closed)
    book.peak = max(book.peak, nav)
    book.last.update(equity=str(nav), net_economic_equity=str(nav),
                     position_prices={s: str(closed[s]) for s in book.positions},
                     receivables=list(book.receivables), signal_as_of=signal_as_of, scheduled_rebalance=rebalance,
                     open_exposure=str(sum(weights.values(), ZERO)), cash_interest=str(interest),
                     targets={s: str(w) for s, w in targets.items()})
    book.series[-1] = book.last.copy()


def _run_books(spec, sessions, start_index, bars, actions, scores, adv, cutoffs, benchmark, policy, participation):
    capital = number(spec["capital"])
    cash_per_session = number(spec["cash_annual_rate"]) / 252
    books = {"factor": ResearchBook(capital, policy), "benchmark": ResearchBook(capital, policy)}
    targets = None
    signal_as_of = None
    for i, (day, open_at, _) in enumerate(sessions[start_index:], start=start_index):
        rebalance = (i - start_index) % spec["rebalance_sessions"] == 0
        if instant(cutoffs[i - 1]) >= open_at:
            raise RiskError("Factor information cutoff must precede the next open")
        if rebalance:
            targets = _paired_targets(scores.iloc[i - 1], spec, benchmark, policy)
            signal_as_of = cutoffs[i - 1]
        opened = {s: rows[day]["open"] for s, rows in bars.items()}
        closed = {s: rows[day]["close"] for s, rows in bars.items()}
        for (arm, book), arm_targets in zip(books.items(), targets):
            try:
                _book_session(book, day, opened, closed, actions[day], arm_targets,
                              adv.iloc[i - 1], participation, cash_per_session,
                              rebalance=rebalance, signal_as_of=signal_as_of)
            except RiskError as exc:
                raise RiskError(f"{day} {arm}: {exc}") from exc
    if any(not book.orders for book in books.values()):
        raise RiskError("One or both replay books could not trade a whole-share position")
    return {arm: {"summary": book.summary(), "metrics": metrics(book.series, capital), "daily": book.series}
            for arm, book in books.items()}


def study(expression, horizon, settings, spec, data, config, now, *, artifacts_dir=None):
    """Use one causal input panel for predictive statistics and long-only ranks."""
    expression = canonical(expression)
    tree = parse(expression)
    unsupported = set(DATA) - set(FIELDS)
    if any(isinstance(node, ast.Name) and node.id in unsupported for node in ast.walk(tree)):
        raise RiskError("Factor replay currently supports price and volume expressions")
    policy, benchmark, participation = _plan(spec, config)
    fingerprint = check_snapshot_as_of(data, now)
    gaps = (source_artifact_gaps(data, artifacts_dir) if artifacts_dir is not None
            else snapshot_limitations(data, require_receipts=True) + ["source_artifacts_unchecked"])
    warmup = _factor_warmup(tree)
    depth = max(warmup + 1, ADV_SESSIONS)
    sessions, bars, actions = _market_window(data, spec, policy["universe"], depth)
    panels, adv, cutoffs = _signal_inputs(sessions, bars, actions, spec)
    inputs = {field: panel[spec["instruments"]] for field, panel in panels.items()}
    scores = compute(expression, inputs["close"], inputs["volume"], inputs)
    sample = slice(depth - 1 - warmup, None)
    scored = scores.iloc[sample].copy()
    scored.iloc[:warmup] = float("nan")
    result = evaluate_factor(scored, horizon, inputs["close"].iloc[sample], settings, trials=1, warmup=warmup)
    protocol = {"version": "factor_long_only_v1", "expression": expression, "horizon_sessions": horizon,
                "plan": spec, "policy": policy, "adv_sessions": ADV_SESSIONS, "adv_participation": str(participation),
                "snapshot_sha256": fingerprint, "factor_settings": asdict(settings)}
    replay = {"protocol_sha256": digest(protocol), "protocol": protocol, "source": data["source"],
              "source_receipt_gaps": gaps, "source_artifacts_matched": not gaps, "scope": "exploratory_historical",
              "predictive_direction_matches_plan": (result["direction"] == spec["direction"]
                                                    if result["direction"] is not None else None),
              "limitations": ["fixed_instrument_set", "unmodeled_spinoffs_and_delisting_settlements",
                              "unmodeled_intraday_matching_and_dynamic_risk", "unmodeled_data_and_model_costs",
                              "terminal_positions_marked_at_close_without_liquidation"]}
    try:
        arms = _run_books(spec, sessions, depth, bars, actions, scores, adv, cutoffs, benchmark, policy, participation)
        excess = arms["factor"]["metrics"]["net_return"] - arms["benchmark"]["metrics"]["net_return"]
        replay.update(status="modeled", net_excess_return=excess, **arms)
        result["qualification"]["tradable"] = {"status": "modeled_long_only", "sample": replay["scope"],
                                               "protocol_sha256": replay["protocol_sha256"]}
    except RiskError as exc:
        replay.update(status="incomplete", reason=str(exc))
    return {"expression": expression, "horizon_sessions": horizon, **result, "long_only_replay": replay}
