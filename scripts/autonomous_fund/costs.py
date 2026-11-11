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
import math
from datetime import timedelta
from zoneinfo import ZoneInfo

from .risk import instant

TRADING_DAYS = 252
# The usage spellings LumiBot's runtime and the providers use (LiteLLM, OpenAI, Anthropic, Gemini, DeepSeek).
INPUT_KEYS = ("prompt_token_count", "prompt_tokens", "input_tokens")
OUTPUT_KEYS = ("candidates_token_count", "completion_tokens", "output_tokens")
CACHED_KEYS = ("cached_content_token_count", "cached_input_tokens", "cache_read_input_tokens", "cached_prompt_tokens",
               "cached_tokens", "prompt_cache_hit_tokens", "prompt_tokens_details.cached_tokens",
               "input_tokens_details.cached_tokens")
MISS_KEYS = ("prompt_cache_miss_tokens", "prompt_tokens_details.uncached_tokens")


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


def cost_summary(rows, equity=None, now=None, prices=None, days=30):
    """Recent model cost per day and role, its annual pace and the break-even excess return.

    rows: journal events (dicts with at, kind, body); `agent` rows are model calls.
    Scripted test calls and answers served from LumiBot's own cache cost nothing.
    The pace is the mean over the trading days with model calls in the window,
    times 252; break_even_excess_return is that pace as a fraction of equity: the
    AI must beat its benchmark by at least this much a year to cover its models.
    """
    zone = ZoneInfo("America/New_York")
    calls = [r for r in rows if r["kind"] == "agent" and not scripted(r["body"]) and not r["body"].get("cache_hit")]
    now = instant(now) if now is not None else (instant(calls[-1]["at"]) if calls else None)
    start = (now - timedelta(days=days)) if now is not None else None
    # Counts, unknown-price notices and token totals must describe the same window as costs.
    calls = [row for row in calls if start is None or start <= instant(row["at"]) <= now]
    models = {r["body"].get("model") for r in calls}
    by_day, by_role, unpriced, tokens = {}, {}, 0, {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    for row in calls:
        at = instant(row["at"])
        body = row["body"]
        counts = body.get("tokens") or token_counts(body.get("usage"))
        for key in tokens:
            tokens[key] += int(counts.get(key) or 0)
        cost = body.get("cost_usd")
        if cost is None and usage_known(body.get("usage")):
            cost = call_cost(counts, rate_for(prices, body.get("model"), len(models)))
        if cost is None:
            unpriced += 1
            continue
        day = at.astimezone(zone).date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + cost
        role = body.get("name") or "unknown"
        by_role[role] = by_role.get(role, 0.0) + cost
    today = now.astimezone(zone).date().isoformat() if now is not None else None
    per_day = sum(by_day.values()) / len(by_day) if by_day else None
    annual = per_day * TRADING_DAYS if per_day is not None else None
    equity = float(equity) if equity else None
    return {
        "window_days": days, "calls": len(calls), "unpriced_calls": unpriced, "tokens": tokens,
        "today_usd": round(by_day.get(today, 0.0), 4) if today else None,
        "window_usd": round(sum(by_day.values()), 4), "days_with_calls": len(by_day),
        "by_day_usd": {d: round(v, 4) for d, v in sorted(by_day.items())},
        "by_role_usd": {r: round(v, 4) for r, v in sorted(by_role.items())},
        "average_per_day_usd": round(per_day, 4) if per_day is not None else None,
        "annual_pace_usd": round(annual, 2) if annual is not None else None,
        "break_even_excess_return": round(annual / equity, 5) if annual is not None and equity else None,
        "complete": unpriced == 0 and bool(by_day),
        "note": "Estimate from the operator's rates, not a provider bill. Unpriced calls are not counted as free: "
                "set model_prices_per_million / FUND_MODEL_PRICES_JSON for every model in use.",
    }


def fraction_text(value):
    return "未知" if value is None or not math.isfinite(value) else f"{value * 100:.2f}%"
