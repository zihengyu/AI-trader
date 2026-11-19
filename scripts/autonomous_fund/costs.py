"""Model cost accounting: what the AI costs per day, and the excess return it must earn to pay for itself.

Prices are the operator's own (`model_prices_per_million` / FUND_MODEL_PRICES_JSON):
{model: {"input": ..., "output": ..., "cached_input": ...}} in USD per million
tokens. `cached_input` is the provider's lower price for prompt tokens served from
its cache; when it is absent, cached tokens are priced as ordinary input (the
conservative choice). A legacy single rate {"input", "output"} applies when only
one model is used. These are estimates, never a provider bill.

Each model call's cost is computed when the call is recorded and stored with it
in the journal (`agent` events: `tokens`, `cost_usd`), so status, reports and the
daily document need no price table. A call with unknown usage or an unpriced
model is counted as unpriced, never as free.
"""
import json
import math
from datetime import timedelta
from zoneinfo import ZoneInfo

from .risk import instant, trading_day

TRADING_DAYS = 252
# The usage spellings LumiBot's runtime and the providers use (LiteLLM, OpenAI, Anthropic, Gemini, DeepSeek).
INPUT_KEYS = ("prompt_token_count", "prompt_tokens", "input_tokens")
OUTPUT_KEYS = ("candidates_token_count", "completion_tokens", "output_tokens")
CACHED_KEYS = ("cached_content_token_count", "cached_input_tokens", "cache_read_input_tokens", "cached_prompt_tokens",
               "cached_tokens", "prompt_cache_hit_tokens", "prompt_tokens_details.cached_tokens",
               "input_tokens_details.cached_tokens")
MISS_KEYS = ("prompt_cache_miss_tokens", "prompt_tokens_details.uncached_tokens")
UNPRICED_LABELS = {"usage_missing": "用量缺失", "usage_incomplete": "用量不全",
                   "usage_invalid": "用量格式异常", "rate_missing": "缺少费率"}


def optional_model_commitments(db, now, *, for_admission=False):
    """Keep unmatched and unpriced call reservations visible without double-counting priced calls."""
    now = instant(now)
    cutoff = None if for_admission else now.isoformat()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_optional_model_reservations'").fetchone():
        return {"reserved_microusd": 0, "running": 0, "unpriced": 0}
    rows = db.execute("""
        SELECT r.id,r.reserved,r.at,e.at,e.body FROM fund_optional_model_reservations r
        LEFT JOIN events e ON e.kind='agent' AND json_valid(e.body) AND json_extract(e.body,'$.call_id')=r.id
            AND (? IS NULL OR julianday(e.at)<=julianday(?))
        WHERE r.day=? AND (? IS NULL OR julianday(r.at)<=julianday(?)) ORDER BY r.id,e.sequence
    """, (cutoff, cutoff, trading_day(now), cutoff, cutoff))
    calls = {}
    for key, amount, reserved_at, observed_at, body in rows:
        if not for_admission and instant(reserved_at) > now:
            continue
        calls.setdefault(key, (amount, None))
        if body and (for_admission or instant(observed_at) <= now):
            calls[key] = amount, json.loads(body)
    pending = [(amount, body) for amount, body in calls.values()
               if body is None or (body.get("cost_usd") is None and not body.get("cache_hit") and not scripted(body))]
    return {"reserved_microusd": sum(amount for amount, _ in pending),
            "running": sum(body is None for _, body in pending), "unpriced": sum(body is not None for _, body in pending)}


def reserve_optional_model_call(journal, now, call_id, purpose, maximum, estimate):
    from .research_budget import micros

    day = trading_day(now)
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS fund_optional_model_reservations (
            id TEXT PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, purpose TEXT NOT NULL, reserved INTEGER NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS optional_model_reservation_day ON fund_optional_model_reservations(day)")
        row = db.execute("SELECT value FROM meta WHERE key='counter:model_cost_microusd'").fetchone()
        counter = json.loads(row[0]) if row else {}
        spent = counter.get("count", 0) if counter.get("day") == day else 0
        # An overlapping caller may have captured its timestamp before the
        # caller that acquired the write lock first. Both reserve the same day.
        pending = optional_model_commitments(db, now, for_admission=True)
        amount = micros(estimate)
        detail = {"day": day, "purpose": purpose, "limit_usd": maximum, "known_usd": spent / 1e6,
                  "requested_usd": amount / 1e6, **pending}
        if spent + pending["reserved_microusd"] + amount > micros(maximum):
            return detail
        db.execute("INSERT INTO fund_optional_model_reservations VALUES(?,?,?,?,?)",
                   (call_id, instant(now).isoformat(), day, purpose, amount))
        return None


def _usage_value(raw, keys):
    for key in keys:
        node = raw
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if (isinstance(node, (int, float)) and not isinstance(node, bool)
                and math.isfinite(node) and node >= 0 and node == int(node)):
            return int(node)
    return None


def _value(raw, keys):
    return _usage_value(raw, keys) or 0


def usage_known(raw):
    """Both token sides must be observed; absent usage is different from an explicit zero."""
    entered = _usage_value(raw, INPUT_KEYS)
    if entered is None:
        cached, missed = _usage_value(raw, CACHED_KEYS), _usage_value(raw, MISS_KEYS)
        entered = cached + missed if cached is not None and missed is not None else None
    return entered is not None and _usage_value(raw, OUTPUT_KEYS) is not None


def token_counts(raw):
    """{input_tokens, output_tokens, cached_input_tokens} from one call's provider usage."""
    raw = raw if isinstance(raw, dict) else {}
    cached, missed = _value(raw, CACHED_KEYS), _value(raw, MISS_KEYS)
    entered = _value(raw, INPUT_KEYS) or (cached + missed)
    return {"input_tokens": entered, "output_tokens": _value(raw, OUTPUT_KEYS),
            "cached_input_tokens": min(cached, entered)}


def rate_for(prices, model, models_used=1):
    """The operator's {input, output, cached_input} rates for `model`, or None when unpriced."""
    if not isinstance(prices, dict):
        return None
    if isinstance(prices.get(model), dict):
        return prices[model]
    if "input" in prices and "output" in prices and models_used == 1:
        return prices  # A legacy single rate never prices two different models alike.
    return None


def call_cost(counts, rate):
    """USD for one call; None without a rate."""
    if not rate:
        return None
    cached = counts["cached_input_tokens"]
    fresh = max(counts["input_tokens"] - cached, 0)
    cached_rate = float(rate.get("cached_input", rate["input"]))
    return (fresh * float(rate["input"]) + cached * cached_rate + counts["output_tokens"] * float(rate["output"])) / 1e6


def scripted(body):
    return bool(body.get("mock")) or str(body.get("model") or "").startswith("scripted/")


def unpriced_reason(body):
    """Why a real, uncached call has no cost; missing usage never implies a free call."""
    if body.get("cost_usd") is not None or scripted(body) or body.get("cache_hit"):
        return None
    saved = body.get("cost_unavailable_reason")
    if isinstance(saved, str) and saved in UNPRICED_LABELS:
        return saved
    if body.get("usage_diagnostic"):
        return "usage_invalid"
    raw = body.get("usage")
    if raw is None or raw == {}:
        return "usage_missing"
    if not isinstance(raw, dict):
        return "usage_invalid"
    return "rate_missing" if usage_known(raw) else "usage_incomplete"


def unpriced_text(reasons):
    return "、".join(f"{label} {reasons[key]}" for key, label in UNPRICED_LABELS.items() if reasons.get(key))


def cost_summary(rows, equity=None, now=None, prices=None, days=30, *, observed_from=None):
    """Recent model cost per day and role, its annual pace and the break-even excess return.

    rows: journal events (dicts with at, kind, body); `agent` rows are model calls.
    Scripted test calls and answers served from LumiBot's own cache cost nothing.
    Keep the legacy priced-call-date pace for report readers. The trading-session
    pace allocates all priced calls, including weekends, across completed NYSE sessions.
    """
    zone = ZoneInfo("America/New_York")
    calls = [r for r in rows if r["kind"] == "agent" and not scripted(r["body"]) and not r["body"].get("cache_hit")]
    now = instant(now) if now is not None else max((instant(row["at"]) for row in calls), default=None)
    start = (now - timedelta(days=days)) if now is not None else None
    # Counts, unknown-price notices and token totals must describe the same window as costs.
    calls = [row for row in calls if start is None or start <= instant(row["at"]) <= now]
    models = {r["body"].get("model") for r in calls}
    by_day, by_role, unpriced, reasons, tokens = {}, {}, 0, {}, {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    today = now.astimezone(zone).date().isoformat() if now is not None else None
    today_purposes, missing_details = {}, {}
    for row in calls:
        at = instant(row["at"])
        body = row["body"]
        counts = body.get("tokens") or token_counts(body.get("usage"))
        for key in tokens:
            tokens[key] += int(counts.get(key) or 0)
        cost = body.get("cost_usd")
        if cost is None and usage_known(body.get("usage")):
            cost = call_cost(counts, rate_for(prices, body.get("model"), len(models)))
        day = at.astimezone(zone).date().isoformat()
        role = body.get("name") or "unknown"
        purpose = body.get("purpose") or body.get("phase") or role
        if day == today:
            item = today_purposes.setdefault(purpose, {"calls": 0, "known_usd": 0.0, "unpriced_calls": 0})
            item["calls"] += 1
            item["unpriced_calls"] += int(cost is None)
            if cost is not None:
                item["known_usd"] += cost
        if cost is None:
            unpriced += 1
            reason = unpriced_reason(body)
            reasons[reason] = reasons.get(reason, 0) + 1
            key = (body.get("model") or "unknown", purpose, reason)
            missing_details[key] = missing_details.get(key, 0) + 1
            continue
        by_day[day] = by_day.get(day, 0.0) + cost
        by_role[role] = by_role.get(role, 0.0) + cost
    total = sum(by_day.values())
    per_day = total / len(by_day) if by_day else None
    annual = per_day * TRADING_DAYS if per_day is not None else None
    equity = float(equity) if equity else None
    sessions = 0
    if start is not None:
        from .scorecard import _exchange_sessions
        from .session_calendar import _session_close, completed_daily_cutoff

        if observed_from is not None:
            observed_start = min((instant(row["at"]) for row in calls), default=instant(observed_from))
            start = max(start, min(instant(observed_from), observed_start))
        first = start.astimezone(zone).date()
        close = _session_close(first)
        if close is not None and close < start:
            first += timedelta(days=1)
        sessions = len(_exchange_sessions(first.isoformat(), completed_daily_cutoff(now).isoformat()))
    per_session = total / sessions if sessions and by_day else None
    session_pace = per_session * TRADING_DAYS if per_session is not None else None
    return {
        "window_days": days, "calls": len(calls), "unpriced_calls": unpriced, "unpriced_by_reason": reasons, "tokens": tokens,
        "today_usd": round(by_day.get(today, 0.0), 4) if today else None,
        "window_usd": round(total, 4), "days_with_calls": len(by_day),
        "by_day_usd": {d: round(v, 4) for d, v in sorted(by_day.items())},
        "by_role_usd": {r: round(v, 4) for r, v in sorted(by_role.items())},
        "today_by_purpose": {key: {**item, "known_usd": round(item["known_usd"], 4)}
                             for key, item in sorted(today_purposes.items())},
        "unpriced_details": [{"model": model, "purpose": purpose, "reason": reason, "calls": count}
                             for (model, purpose, reason), count in sorted(missing_details.items())],
        "average_per_day_usd": round(per_day, 4) if per_day is not None else None,
        "annual_pace_usd": round(annual, 2) if annual is not None else None,
        "break_even_excess_return": round(annual / equity, 5) if annual is not None and equity else None,
        "completed_trading_sessions": sessions,
        "average_per_trading_session_usd": round(per_session, 4) if per_session is not None else None,
        "annual_trading_session_pace_usd": round(session_pace, 2) if session_pace is not None else None,
        "trading_session_break_even_excess_return": round(session_pace / equity, 5)
        if session_pace is not None and equity else None,
        "complete": unpriced == 0 and bool(by_day),
        "note": "Estimate from the operator's rates, not a provider bill. Unpriced calls are not counted as free: "
                "set model_prices_per_million / FUND_MODEL_PRICES_JSON for every model in use.",
    }


def fraction_text(value):
    return "未知" if value is None or not math.isfinite(value) else f"{value * 100:.2f}%"
