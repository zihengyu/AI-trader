"""Trade value check (V2 spec 4.5): does a purchase's expected edge cover its expected cost?

Until the research ideas are shown to be calibrated (their scorecard verdict is
"informative": enough scored ideas, a hit rate above a coin and above "always
up", Brier score under 0.25), the fund only shows the manager what a round trip
costs. After that, an add (a target above the current weight) of a symbol with
an idea registered for today is skipped when the idea's calibrated edge does not
cover the round-trip cost:

- calibrated hit rate p: the hit rate of the scored ideas in the idea's
  confidence bucket when that bucket has at least MIN_BUCKET of them, else the
  hit rate of all scored ideas;
- edge = (2p - 1) x the mean absolute move of the scored ideas over their
  horizons, with the sign of the idea's direction (adding to a symbol the
  research expects to fall or lag has a negative edge);
- round-trip cost = 2 x (half the quoted bid-ask spread + fee + slippage).

Reductions, sells and protective exits are never limited. A symbol without an
idea for today is not judged: there is nothing to estimate its edge from.
"""
from decimal import Decimal

MIN_BUCKET = 10
RISING = {"up": True, "outperform": True, "down": False, "underperform": False}


def model(ideas, fee_rate, slippage):
    """What the check uses, built once per decision from the ideas scorecard; active only when calibrated."""
    fee, slip = float(fee_rate), float(slippage)
    base = {"fee_bps": round(fee * 1e4, 2), "slippage_bps": round(slip * 1e4, 2),
            "round_trip_without_spread_bps": round(2 * (fee + slip) * 1e4, 2),
            "spread": "plus the quoted bid-ask spread (half per side) at order time"}
    if not ideas:
        return {**base, "active": False, "reason": "no research ideas scored yet"}
    if ideas.get("verdict") != "informative":
        return {**base, "active": False, "reason": f"ideas not shown to be calibrated ({ideas.get('verdict')})"}
    if not ideas.get("scored") or ideas.get("hit_rate") is None or ideas.get("mean_abs_move") is None:
        return {**base, "active": False, "reason": "no scored idea outcomes"}
    bounds = ideas.get("confidence_bounds") or {}
    buckets = [{"low": bounds[label][0], "high": bounds[label][1], "hit_rate": b["hit_rate"], "scored": b["scored"]}
               for label, b in (ideas.get("by_confidence") or {}).items()
               if label in bounds and b.get("scored", 0) >= MIN_BUCKET and b.get("hit_rate") is not None]
    return {**base, "active": True, "reason": "ideas are calibrated (scorecard verdict informative)",
            "hit_rate": ideas["hit_rate"], "mean_abs_move": ideas["mean_abs_move"], "buckets": buckets,
            "scored": ideas["scored"]}


def judge(current, idea, bid, ask):
    """{"edge", "cost", "covered", "hit_rate"} for one add backed by `idea`, under the active model `current`."""
    confidence = float(idea["confidence"])
    rate = next((b["hit_rate"] for b in current["buckets"] if b["low"] <= confidence < b["high"]), current["hit_rate"])
    sign = 1 if RISING.get(idea.get("direction"), True) else -1
    edge = sign * (2 * rate - 1) * current["mean_abs_move"]
    bid, ask = float(bid), float(ask)
    half_spread = (ask - bid) / (ask + bid) if ask > 0 and bid > 0 and ask >= bid else 0.0
    cost = 2 * (half_spread + current["fee_bps"] / 1e4 + current["slippage_bps"] / 1e4)
    return {"edge": round(edge, 6), "cost": round(cost, 6), "covered": edge > cost, "hit_rate": rate,
            "idea_id": idea.get("idea_id")}


def limit_adds(weights, current_weights, ideas, prices, current):
    """(weights with uncovered adds held at the current weight, {symbol: judgment} for each held add)."""
    if not current or not current.get("active"):
        return weights, {}
    latest = {}
    for idea in ideas or []:
        latest[idea["symbol"]] = idea  # The session's latest idea for each symbol.
    held, result = {}, dict(weights)
    for symbol, weight in weights.items():
        now = current_weights.get(symbol, Decimal(0))
        if weight <= now or symbol not in latest or symbol not in prices:
            continue
        verdict = judge(current, latest[symbol], prices[symbol].bid, prices[symbol].ask)
        if not verdict["covered"]:
            result[symbol] = now  # Never below the holding: this only declines the purchase.
            held[symbol] = verdict
    return result, held


NEAR_CEILING = 0.10  # Share weights within this fraction of the ceiling are re-checked with a live quote.
COARSE_SHARES = 4  # An affordable symbol of which at most this many whole shares fit is listed as coarse.


def affordability(last_closes, equity, max_position, groups, held=(), live=None):
    """Symbols whose single share costs more than the fund may hold of them, and those that fit only a few shares.

    The fund buys whole shares. A symbol's ceiling is the smallest of the single-position
    limit and the cap of every risk group it belongs to (a leveraged ETF's own cap, a
    sector's combined cap). Prices are the last completed close; a symbol whose share
    weight there is within NEAR_CEILING of its ceiling is re-checked with `live(symbol)`
    (a current quote) when given, so a move during the session counts at once. A live
    quote that cannot be read leaves the close in use.

    - unaffordable: one share is above the ceiling; no target can buy it.
    - coarse: affordable, but at most COARSE_SHARES whole shares fit under the ceiling; a
      target is rounded down to whole shares, so one below one share's weight buys none.
    A held symbol can still be held or sold.
    """
    equity = float(equity or 0)
    if equity <= 0:
        return {"equity": equity, "unaffordable": [], "coarse": []}
    unaffordable, coarse = [], []
    for symbol, price in sorted(last_closes.items()):
        if price is None or float(price) <= 0:
            continue
        ceiling = min([float(max_position)] + [float(cap) for _, cap, members in groups if symbol in members])
        if ceiling <= 0:
            continue
        price, basis = float(price), "last_close"
        if live is not None and abs(price / equity / ceiling - 1) <= NEAR_CEILING:
            try:
                quoted = float(live(symbol))
                if quoted > 0:
                    price, basis = quoted, "live_quote"
            except Exception:
                basis = "last_close (live quote unavailable)"
        weight = price / equity
        row = {"symbol": symbol, "share_price": round(price, 2), "one_share_weight": round(weight, 4),
               "max_weight": round(ceiling, 4), "basis": basis}
        if weight > ceiling:
            unaffordable.append({**row, "held": symbol in held})
        else:
            shares = int(ceiling / weight + 1e-9)
            if shares <= COARSE_SHARES:
                coarse.append({**row, "max_shares": shares})
    return {"equity": round(equity, 2), "unaffordable": unaffordable, "coarse": coarse,
            "meaning": "Whole shares only. unaffordable: one share is worth more than the most this account may hold "
                       "of the symbol, so a target cannot buy it at all; do not target these for purchase. coarse: at "
                       "most max_shares whole shares fit under the ceiling; a target weight is rounded down to whole "
                       "shares, so a target below one_share_weight buys nothing and one between steps buys the lower "
                       "step. Holding or selling an existing position is unaffected, and research on these symbols "
                       "remains useful. Prices are the last completed close, or a live quote (basis live_quote) for "
                       "symbols near their ceiling, re-checked at every decision."}
