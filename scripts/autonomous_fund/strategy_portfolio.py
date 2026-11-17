"""Forward studies of authorized research books at equal starting capital."""
import json
import math
import statistics
from datetime import timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from .execution_quality import prior_cost_reference
from .journal import encode
from .risk import RiskError, instant, number
from .scorecard import _exchange_sessions, _idea_session_close, _regime_interval
from .strategy_portfolio_exposure import position_exposure
from .strategy_validation import _daily_sessions, digest, following_session, metrics


STUDY_EVENT = "strategy_portfolio_declared"
PROTOCOL = "daily_sleeve_volatility_match_v1"
CALIBRATION_SESSIONS = 20
PAIRED_BLOCK_SESSIONS = 5
EXTRA_TRANSFER_STRESS_BPS = (5, 10, 25, 50, 100)
RETURN_EPSILON = 1e-12


def _identity(row):
    key = row["strategy_id"], row["version"]
    if any(not isinstance(part, str) or not part.strip() or len(part) > 80 for part in key):
        raise RiskError("Each portfolio sleeve requires an exact strategy ID and version")
    return key


def _plan(spec):
    try:
        if set(spec) != {"study_id", "base", "candidate", "candidate_weight", "fee_bps", "slippage_bps"}:
            raise RiskError("Portfolio study requires a name, explicit sleeves, allocation and cost assumptions")
        name, base, candidate = spec["study_id"], spec["base"], spec["candidate"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or not isinstance(base, list) or not 1 <= len(base) <= 8):
            raise RiskError("Invalid portfolio study name or number of base sleeves")
        if any(set(row) != {"strategy_id", "version", "weight"} for row in base) or set(candidate) != {"strategy_id", "version"}:
            raise RiskError("Base sleeves require explicit weights and every sleeve requires a version")
        keys = [_identity(row) for row in base]
        weights = [number(row["weight"]) for row in base]
        allocation = number(spec["candidate_weight"])
        fee, slippage = number(spec["fee_bps"]), number(spec["slippage_bps"])
        if len(set(keys)) != len(keys) or _identity(candidate) in keys:
            raise RiskError("Portfolio study requires distinct base and candidate versions")
        if any(not 0 < w <= 1 for w in weights) or sum(weights) != 1 or not 0 < allocation < 1:
            raise RiskError("Base weights must sum to one and the candidate must replace a fraction of base capital")
        if not (0 <= fee <= 100 and 0 <= slippage <= 100):
            raise RiskError("Each portfolio transfer cost assumption must be between zero and 100 bps")
        return {"study_id": name, "base": [{**row, "weight": float(weight)} for row, weight in zip(base, weights)],
                "candidate": candidate, "candidate_weight": float(allocation),
                "fee_bps": float(fee), "slippage_bps": float(slippage)}
    except (AttributeError, KeyError, TypeError) as exc:
        raise RiskError("Invalid portfolio study") from exc


def _weights(plan):
    base = {_identity(row): row["weight"] for row in plan["base"]}
    share = plan["candidate_weight"]
    return base, {**{key: weight * (1 - share) for key, weight in base.items()}, _identity(plan["candidate"]): share}


def _returns(db, identity, now):
    """Use the exact, uninterrupted prefix of saved prospective closing receipts."""
    rows = db.execute("SELECT day,at,CASE WHEN json_valid(body) THEN json_extract(body,'$.evidence') END,"
                      "CASE WHEN json_valid(body) THEN json_extract(body,'$.receipt.day') END,"
                      "CASE WHEN json_valid(body) THEN json_extract(body,'$.receipt.net_economic_equity') END "
                      "FROM fund_strategy_forward WHERE id=? AND version=? ORDER BY day", identity).fetchall()
    if not rows:
        return {}
    calendar = _daily_sessions(rows[0][0], rows[-1][0])
    closes = {}
    for (day, at, evidence, receipt_day, equity), (expected, _, closed) in zip(rows, calendar):
        try:
            if day != expected or receipt_day != day or evidence != "prospective" or not closed <= instant(at) <= now:
                break
            value = float(number(equity))
            if value <= 0 or not math.isfinite(value):
                break
        except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
            break
        closes[day] = value
    days = list(closes)
    result = {}
    for previous, day in zip(days, days[1:]):
        daily_return = closes[day] / closes[previous] - 1
        if not math.isfinite(daily_return) or daily_return <= -1:
            break
        result[day] = daily_return
    return result


def _aligned(db, weights, now, cache):
    for key in weights:
        if key not in cache:
            cache[key] = _returns(db, key, now)
    paths = {key: cache[key] for key in weights}
    common = sorted(set.intersection(*(set(path) for path in paths.values())))
    return common, paths


def _versions(db, identities, now):
    versions = []
    for key, version in identities:
        row = db.execute("SELECT created_at,digest,policy_hash FROM fund_strategy_versions WHERE id=? AND version=?",
                         (key, version)).fetchone()
        if not row or instant(row[0]) > now:
            raise RiskError("A declared strategy version does not exist in the selected journal")
        versions.append({"strategy_id": key, "version": version, "strategy_digest": row[1], "policy_hash": row[2]})
    return versions


def _calibration_digest(fit_days, paths):
    return digest([fit_days, [[*key, [path[day] for day in fit_days]] for key, path in paths.items()]])


def declare_study(db, spec, now):
    """Append an operator declaration in the caller's account-local transaction."""
    now = instant(now)
    started = monotonic()
    if not db.in_transaction:
        raise RiskError("Portfolio declaration requires an account-local write transaction")
    plan = _plan(spec)
    existing = db.execute("SELECT body FROM events WHERE kind=? AND "
                          "json_extract(body,'$.plan.study_id')=?", (STUDY_EVENT, plan["study_id"])).fetchone()
    if existing:
        saved = json.loads(existing[0])
        if saved["plan"] != plan:
            raise RiskError("A portfolio study cannot be rewritten. Declare a new study ID")
        return saved

    base, combined = _weights(plan)
    versions = _versions(db, combined, now)
    days, paths = _aligned(db, combined, now, {})
    fit_days = days[-CALIBRATION_SESSIONS:]
    if len(fit_days) < CALIBRATION_SESSIONS:
        raise RiskError("Portfolio study requires 20 common prospective close-to-close returns before declaration")
    vols = {arm: statistics.stdev(math.fsum(weight * paths[key][day] for key, weight in weights.items())
                                 for day in fit_days) * math.sqrt(252)
            for arm, weights in (("base", base), ("combined", combined))}
    target = min(vols.values())
    if target <= 0 or not all(map(math.isfinite, vols.values())):
        raise RiskError("Portfolio study requires nonzero risk in both calibration portfolios")
    execution_reference = prior_cost_reference(db, now)
    declared_at = now + timedelta(seconds=monotonic() - started)
    local_day = declared_at.astimezone(ZoneInfo("America/New_York")).date()
    closes = _exchange_sessions(local_day.isoformat(), (local_day + timedelta(days=15)).isoformat())
    anchor = next(day for day in closes if _idea_session_close(day) > declared_at)
    saved = {"protocol": PROTOCOL, "plan": plan, "versions": versions, "declared_at": declared_at.isoformat(),
             "execution_cost_reference": execution_reference,
             "calibration_digest": _calibration_digest(fit_days, paths), "calibration_start": fit_days[0],
             "calibration_end": fit_days[-1], "calibration_sessions": len(fit_days), "volatility_target": target,
             "calibration_volatility": vols, "scales": {arm: target / value for arm, value in vols.items()},
             "first_evaluation_session": following_session(anchor)[0]}
    db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (declared_at.isoformat(), STUDY_EVENT, encode(saved)))
    return saved


def _simulate(days, paths, weights, fee, slippage, *, keep_daily=False):
    """Transfer capital at each interval's starting close, using both legs' net sleeve NAVs."""
    cash, positions = 1.0, dict.fromkeys(weights, 0.0)
    turnover, cash_fraction, earned = 0.0, 0.0, []
    rate = fee + slippage
    cash_weight = max(0.0, 1 - math.fsum(weights.values()))
    for day in days:
        before = cash + math.fsum(positions.values())
        after = before
        # Solve the self-financing rebalance after transfer costs. With the
        # bounded rates and unlevered weights this is a contraction (<= 3%,
        # including the largest additional transfer stress).
        for _ in range(10 if rate else 1):
            targets = {key: after * w for key, w in weights.items()}
            traded = math.fsum(abs(value - positions[key]) for key, value in targets.items())
            after = before - rate * traded
        turnover += traded
        cash = after * cash_weight
        cash_fraction += cash_weight
        positions = {key: after * w * (1 + paths[key][day]) for key, w in weights.items()}
        earned.append({"net_economic_equity": cash + math.fsum(positions.values())})
    result = metrics(earned, 1)
    if not keep_daily:
        del result["daily_returns"]
    return {**result, "mean_cash_weight": cash_fraction / len(days), "transfer_turnover_fraction": turnover,
            "transfer_fees_fraction": turnover * fee, "transfer_slippage_fraction": turnover * slippage}


def _paired_advantage(days, base, combined):
    width = PAIRED_BLOCK_SESSIONS
    windows = []
    for start in range(0, len(days) - width + 1, width):
        end = start + width
        left = math.prod(1 + value for value in base[start:end])
        right = math.prod(1 + value for value in combined[start:end])
        windows.append((days[start], days[end - 1], right - left))
    differences = [value for _, _, value in windows]
    interval = _regime_interval(windows, "portfolio")
    if differences and max(differences) - min(differences) <= RETURN_EPSILON:
        # Identical books can differ by a few float ulps after proportional
        # transfers. That is no evidence of a tightly estimated advantage.
        interval.update(status="unreliable", interval_95=None)
    return {**interval, "block_sessions": width,
            "positive_blocks": sum(value > RETURN_EPSILON for value in differences),
            "trailing_sessions_excluded": len(days) % width}


def _cost_stress(days, paths, weights, fee, slippage, base_return, combined_return):
    """Hold the base at declared costs and stress transfers in the combined arm."""
    initial = combined_return - base_return
    scenarios = [{"extra_transfer_bps": 0, "incremental_net_return": initial}]
    last_positive, bracket = (0 if initial > RETURN_EPSILON else None), None
    for extra in EXTRA_TRANSFER_STRESS_BPS:
        try:
            stressed = _simulate(days, paths, weights, fee, slippage + extra / 10000)
            difference = stressed["net_return"] - base_return
        except (ArithmeticError, RiskError):
            break
        if not math.isfinite(difference):
            break
        scenarios.append({"extra_transfer_bps": extra, "incremental_net_return": difference})
        if bracket is None and last_positive is not None:
            if difference > RETURN_EPSILON:
                last_positive = extra
            else:
                bracket = {"positive_at_extra_bps": last_positive, "nonpositive_at_extra_bps": extra}
    if initial <= RETURN_EPSILON:
        status = "already_nonpositive"
    elif bracket:
        status = "bracketed"
    elif scenarios[-1]["extra_transfer_bps"] == EXTRA_TRANSFER_STRESS_BPS[-1]:
        status = "above_tested_range"
    else:
        status = "incomplete_stress_path"
    return {"status": status, "basis": "combined_arm_only_per_dollar_of_transfer",
            "breakeven_bracket": bracket, "scenarios": scenarios}


def evaluate_studies(db, now):
    now = instant(now)
    reports, cache = [], {}
    for at, raw in db.execute("SELECT at,body FROM events WHERE kind=? AND julianday(at)<=julianday(?) "
                              "ORDER BY julianday(at) DESC,sequence DESC", (STUDY_EVENT, now.isoformat())):
        if instant(at) > now:
            continue
        saved = json.loads(raw)
        plan = saved["plan"]
        if saved.get("protocol") != PROTOCOL:
            reports.append({"plan": plan, "status": "unsupported_portfolio_study_protocol"})
            if len(reports) == 5:
                break
            continue
        base, combined = _weights(plan)
        common, paths = _aligned(db, combined, now, cache)
        start = saved["first_evaluation_session"]
        days = [day for day in common if day >= start]
        fit_days = [day for day in common if saved["calibration_start"] <= day <= saved["calibration_end"]]
        try:
            matching_versions = _versions(db, combined, now) == saved["versions"]
        except RiskError:
            matching_versions = False
        result = {**saved, "status": "awaiting_prospective_sessions", "evaluation_sessions": 0,
                  "latest_shared_prospective_session": common[-1] if common else None}
        if not matching_versions or _calibration_digest(fit_days, paths) != saved["calibration_digest"]:
            result["status"] = "missing_or_changed_calibration_evidence"
        elif days and days[0] == start:
            fee, slippage = plan["fee_bps"] / 10000, plan["slippage_bps"] / 10000
            scaled = {arm: {key: weight * saved["scales"][arm] for key, weight in weights.items()}
                      for arm, weights in (("base", base), ("combined", combined))}
            try:
                arms = {arm: _simulate(days, paths, weights, fee, slippage, keep_daily=True)
                        for arm, weights in scaled.items()}
            except (ArithmeticError, RiskError):
                result["status"] = "unusable_portfolio_path"
            else:
                paired = _paired_advantage(days, arms["base"].pop("daily_returns"), arms["combined"].pop("daily_returns"))
                stress = _cost_stress(days, paths, scaled["combined"], fee, slippage,
                                      arms["base"]["net_return"], arms["combined"]["net_return"])
                exposure = position_exposure(db, days, paths, scaled, _identity(plan["candidate"]), now)
                result.update(status="observed" if len(days) >= CALIBRATION_SESSIONS else "limited_sample",
                              evaluation_sessions=len(days), evaluation_end=days[-1], **arms,
                              paired_advantage=paired, transfer_cost_stress=stress, position_exposure=exposure,
                              incremental_net_return=arms["combined"]["net_return"] - arms["base"]["net_return"],
                              change_in_max_drawdown=arms["combined"]["max_drawdown"] - arms["base"]["max_drawdown"])
        elif days or now > _idea_session_close(start):
            result["status"] = "missing_first_evaluation_session"
        reports.append(result)
        if len(reports) == 5:
            break
    return {"studies": reports,
            "meaning": "Predeclared research-sleeve transfers at equal starting capital and matched prior estimated volatility. "
                       "Sleeve NAVs include their modeled costs; extra transfer fees and slippage are explicit. Residual "
                       "USD earns zero, end NAV is marked without liquidation, and security-level order netting, actual "
                       "account impact, whole-share effects at a different sleeve size and future risk are unmodeled."}
