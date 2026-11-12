"""Independent, deterministic risk boundary; no model or broker dependencies."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from zoneinfo import ZoneInfo

# The universe kind for leveraged, inverse and volatility ETFs: admitted only when the operator declares
# them so, and capped separately (quant_risk.max_leveraged_total / max_leveraged_position).
LEVERAGED_KIND = "leveraged_etf"
# Not a complete list: common US-listed leveraged, inverse and volatility products. These may be admitted
# only as LEVERAGED_KIND, so one can never pass as an ordinary stock or ETF by mistake.
# (index, sector, commodity and single-stock). The operator still reviews every admitted symbol.
LEVERAGED_OR_INVERSE = frozenset("""
TQQQ SQQQ QLD QID PSQ UPRO SPXU SPXL SPXS SSO SDS SH UDOW SDOW DDM DXD DOG TNA TZA UWM TWM RWM URTY SRTY
SOXL SOXS USD SSG TECL TECS ROM REW FAS FAZ UYG SKF DPST LABU LABD CURE RXL HIBL HIBS WEBL WEBS FNGU FNGD
BULZ BERZ MVV MZZ SAA SDD TMF TMV TBT TBF UBT TTT PST NUGT DUST JNUG JDST GUSH DRIP ERX ERY BOIL KOLD
UCO SCO AGQ ZSL UGL GLL UVXY SVXY VXX VIXY VIXM UVIX SVIX YINN YANG EDC EDZ EET EEV XPP FXP CWEB KORU
TSLL TSLQ TSLZ TSLT NVDL NVDU NVDD NVDS NVDX CONL MSTU MSTX MSTZ AMZU AAPU AAPD METU GGLL BITX SBIT
""".split())


class RiskError(ValueError):
    pass


class MarketDataUnavailable(RiskError):
    """An unusable source observation: wait for fresh data without changing the book."""

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


class NoCompletedMinute(MarketDataUnavailable):
    """A minute-history response arrived before any of its bars had closed."""


class OpeningValuationPending(MarketDataUnavailable):
    """A holding has only a prior-session mark before the first minute closes."""


class DecisionTimingError(RiskError):
    """A rejected answer with safe clock evidence for one bounded reassessment."""

    def __init__(self, as_of, expires, now):
        age = (now - as_of).total_seconds()
        reason = "future" if age < 0 else "expired" if now >= expires else "stale"
        self.diagnostic = {"reason": reason, "as_of": as_of.isoformat(), "expires_at": expires.isoformat(),
                           "checked_at": now.isoformat(), "age_seconds": age, "max_age_seconds": 300}
        super().__init__("Stale, future or expired decision")


def number(value):
    if isinstance(value, bool):
        raise RiskError("Boolean is not a financial number")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise RiskError("Invalid financial number") from exc
    if not result.is_finite():
        raise RiskError("Non-finite financial number")
    return result


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RiskError("An explicit timezone is required")
    return value


@dataclass(frozen=True)
class Limits:
    max_position: Decimal = Decimal("0.10")
    max_exposure: Decimal = Decimal("0.70")
    daily_orders: int = 5
    drawdown: Decimal = Decimal("0.08")
    quote_max_seconds: int = 60
    min_dollar_volume: Decimal = Decimal("20000000")
    fee_buffer: Decimal = Decimal("0.001")
    limit_buffer: Decimal = Decimal("0.001")

    def __post_init__(self):
        for key in ("max_position", "max_exposure", "drawdown", "min_dollar_volume", "fee_buffer", "limit_buffer"):
            object.__setattr__(self, key, number(getattr(self, key)))
        if not (0 < self.max_position <= self.max_exposure <= 1 and 0 < self.drawdown < 1):
            raise RiskError("Invalid position, exposure or drawdown limits")
        # Counts every submission, including cancel/replace repricing and sales.
        if type(self.daily_orders) is not int or not 1 <= self.daily_orders <= 50:
            raise RiskError("Daily order limit must be an integer in [1, 50]")
        if not 1 <= self.quote_max_seconds <= 300 or self.min_dollar_volume <= 0:
            raise RiskError("Invalid freshness or liquidity limit")
        if not 0 <= self.fee_buffer <= Decimal("0.05") or not 0 <= self.limit_buffer <= Decimal("0.01"):
            raise RiskError("Invalid fee/price buffer")


@dataclass(frozen=True)
class Price:
    bid: Decimal
    ask: Decimal
    as_of: datetime
    dollar_volume: Decimal

    def validate(self, now, limits, historical=False):
        bid, ask = number(self.bid), number(self.ask)
        age = (instant(now) - instant(self.as_of)).total_seconds()
        ceiling = 4 * 86400 if historical else limits.quote_max_seconds
        if not 0 <= age <= ceiling or not 0 < bid <= ask or ask / bid > Decimal("1.02"):
            raise RiskError("Stale, future, crossed or excessively wide quote")
        if number(self.dollar_volume) < limits.min_dollar_volume:
            raise RiskError("Insufficient observed liquidity")


@dataclass(frozen=True)
class ObservedPrice:
    """A read-only mark; freshness matters, trading liquidity is a separate contract."""
    price: Decimal
    as_of: datetime

    def validate(self, now, limits):
        now, stamp = instant(now), instant(self.as_of)
        age = (now - stamp).total_seconds()
        detail = {"source_as_of": stamp.isoformat(), "checked_at": now.isoformat(),
                  "age_seconds": age, "max_age_seconds": limits.quote_max_seconds}
        try:
            price = number(self.price)
        except RiskError:
            raise MarketDataUnavailable("Invalid observed price", diagnostic={**detail, "reason": "invalid_price"}) from None
        reason = "nonpositive" if price <= 0 else "future" if age < 0 else "stale" if age > limits.quote_max_seconds else None
        if reason:
            raise MarketDataUnavailable("Stale, future or nonpositive observation",
                                        diagnostic={**detail, "reason": reason, "price": str(price)})
        return ObservedPrice(price, stamp)


def observed_quote(quote, now, limits):
    # Quote.timestamp can synthesize receipt time. Use retained source evidence,
    # just as the trading path does, without imposing its liquidity requirement.
    raw = (getattr(quote, "raw_data", None) or {}).get("original_response")
    stamp = getattr(raw, "timestamp", None)
    if stamp is None:
        raise MarketDataUnavailable("Missing source quote timestamp")
    price = getattr(quote, "price", None)
    if price is None:
        bid, ask = number(getattr(quote, "bid", None)), number(getattr(quote, "ask", None))
        if not 0 < bid <= ask:
            raise MarketDataUnavailable("Invalid observed bid/ask")
        price = (bid + ask) / 2
    # Validate here so non-finite prices also retain the actual observation clock.
    try:
        return ObservedPrice(price, instant(stamp)).validate(now, limits)
    except MarketDataUnavailable as exc:
        # Retain whether a stale observation was a completed-bar proxy or an
        # actual last trade. Never log a full provider payload for diagnostics.
        metadata = getattr(quote, "raw_data", None) or {}
        exc.diagnostic = {**(exc.diagnostic or {}),
                          **({"price_kind": metadata["price_kind"]} if metadata.get("price_kind") else {})}
        raise


@dataclass(frozen=True)
class Account:
    cash: Decimal
    equity: Decimal
    positions: dict
    as_of: datetime
    open_orders: bool = False
    receivables: Decimal = Decimal("0")
    # The cash sweep holding (see cash.py): already counted in `cash`, never in `positions`.
    cash_equivalents: dict = field(default_factory=dict)
    cash_equivalent_value: Decimal = Decimal("0")
    # Observed position cost only, for operator display; never substitute a target or quote.
    position_average_prices: dict = field(default_factory=dict)
    # Observed position prices at account valuation, separate from entry cost.
    position_prices: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: str
    quantity: int
    limit: Decimal


def bounded_text(limit, min_length=0):
    """A model text field asked to stay within `limit` characters; a longer answer is cut and marked, never rejected.

    A strict maximum would throw away a whole paid answer for a few extra words
    (2026-11-03: a critique summary of 1,600 characters). The schema still shows
    the model the limit.
    """
    from typing import Annotated
    from pydantic import BeforeValidator, Field

    def cut(value):
        if isinstance(value, str) and len(value) > limit:
            return value[:limit - 14].rstrip() + " …[truncated]"
        return value
    return Annotated[str, BeforeValidator(cut), Field(min_length=min_length, max_length=limit)]


def bounded_list(item, limit, min_length=0):
    """A model list asked to hold at most `limit` items; extra items are dropped, never a rejected answer."""
    from typing import Annotated
    from pydantic import BeforeValidator, Field
    return Annotated[list[item], BeforeValidator(lambda v: v[:limit] if isinstance(v, list) else v),
                     Field(min_length=min_length, max_length=limit)]


def evidence_time(value):
    """One aware timestamp from what a model wrote, or None.

    Models sometimes give a span ("A ~ B", "A to B", "A - B"); the LATEST time of
    the span is used, which is the conservative reading for any cutoff check.
    """
    import re
    if value is None:
        return None
    try:
        return instant(value)
    except (TypeError, ValueError, RiskError):
        pass
    times = []
    for part in re.split(r"\s*(?:~|\u2013|\u2014|\bto\b|\s-\s)\s*", str(value)):
        try:
            times.append(instant(part.strip()))
        except (TypeError, ValueError, RiskError):
            continue
    return max(times) if times else None


def validate_evidence(evidence, now, historical):
    """Check research evidence times; live runs repair what can be repaired instead of losing the research.

    Historical runs stay strict: every item needs readable available_at and
    observed_at (the latest time of a span), none after the simulated time. Live
    runs have no publication-date rule: a span is replaced by its latest time and
    an unreadable observation time by the validation time, disclosed in
    estimate_reason, so one badly formatted field never discards a paid answer.
    """
    if not evidence:
        raise RiskError("No dated research evidence")

    def plain(value):
        try:
            instant(value)
            return True
        except (TypeError, ValueError, RiskError):
            return False
    for source in evidence:
        observed = evidence_time(source.get("observed_at"))
        if not historical:
            notes = []
            if observed is None:
                notes.append(f"observed_at {source.get('observed_at')!r} was unreadable; set to the validation time")
                source["observed_at"] = instant(now).isoformat()
            elif not plain(source.get("observed_at")):
                notes.append(f"observed_at span {source.get('observed_at')!r} reduced to its end")
                source["observed_at"] = observed.isoformat()
            available = source.get("available_at")
            if available is not None and not plain(available):
                parsed = evidence_time(available)
                notes.append(f"available_at {available!r} " + ("reduced to its end" if parsed else "was unreadable"))
                source["available_at"] = parsed.isoformat() if parsed else None
            if notes:
                source["estimated"] = True
                source["estimate_reason"] = f"{(source.get('estimate_reason') or '').strip()} ({'; '.join(notes)})".strip()
            continue
        published = evidence_time(source.get("available_at"))
        if published is None or observed is None:
            raise RiskError("Historical evidence needs readable available_at and observed_at times")
        if published > now or observed > now:
            raise RiskError("Research evidence is after the simulated cutoff")
        if source.get("estimated") and not source.get("estimate_reason", "").strip():
            raise RiskError("Estimated publication dates require an explanation")


def validate_decision(decision, now, universe, limits):
    as_of, expires = instant(decision["as_of"]), instant(decision["expires_at"])
    if not as_of <= now < expires or (now - as_of).total_seconds() > 300:
        raise DecisionTimingError(as_of, expires, now)
    if expires - as_of > timedelta(days=1):
        raise RiskError("Decision validity exceeds one day")
    targets = {}
    for row in decision["targets"]:
        symbol, weight = row["symbol"], number(row["weight"])
        if symbol in targets or symbol not in universe or not 0 <= weight <= limits.max_position:
            raise RiskError("Duplicate, ineligible or overweight target")
        targets[symbol] = weight
        _validate_levels(row, weight)
    if sum(targets.values()) > limits.max_exposure:
        raise RiskError("Target exposure exceeds portfolio limit")
    review = instant(decision["next_review_at"])
    if not now + timedelta(minutes=1) <= review <= now + timedelta(days=1):
        raise RiskError("Next review must be within one minute to one day")
    for alert in decision.get("alerts") or []:
        # A watch can observe an index or another non-tradable instrument.
        # Only targets grant order intent; their universe check above stays strict.
        symbol = alert["symbol"]
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 64:
            raise RiskError("A price alert needs a nonempty symbol of at most 64 characters")
        bounds = [number(alert[k]) for k in ("above", "below") if alert.get(k) is not None]
        if not bounds or any(b <= 0 for b in bounds):
            raise RiskError("A price alert needs a positive above or below level")
        if alert.get("above") is not None and alert.get("below") is not None and number(alert["below"]) >= number(alert["above"]):
            raise RiskError("A price alert's below level must be under its above level")
    return targets


def _validate_levels(row, weight):
    """AI-chosen execution levels; executed only by deterministic code."""
    levels = {k: number(row[k]) for k in ("entry_limit", "stop_loss", "take_profit") if row.get(k) is not None}
    if any(value <= 0 for value in levels.values()):
        raise RiskError("Entry, stop and take-profit levels must be positive prices")
    if "stop_loss" in levels and "entry_limit" in levels and levels["stop_loss"] >= levels["entry_limit"]:
        raise RiskError("A stop-loss must be below the entry limit")
    if "stop_loss" in levels and "take_profit" in levels and levels["stop_loss"] >= levels["take_profit"]:
        raise RiskError("A stop-loss must be below the take-profit level")
    if "entry_limit" in levels and "take_profit" in levels and levels["take_profit"] <= levels["entry_limit"]:
        # Otherwise a fill near the entry limit triggers the take-profit at once: a paid round trip.
        raise RiskError("A take-profit must be above the entry limit")
    if row.get("take_profit_weight") is not None:
        if "take_profit" not in levels or not 0 <= number(row["take_profit_weight"]) < weight:
            raise RiskError("take_profit_weight needs a take-profit level and must be below the target weight")


def decision_levels(decision):
    """{symbol: levels} for targets that carry any AI execution level."""
    result = {}
    for row in decision["targets"]:
        levels = {k: str(number(row[k])) for k in ("entry_limit", "stop_loss", "take_profit", "take_profit_weight")
                  if row.get(k) is not None}
        if levels:
            result[row["symbol"]] = levels
    return result


def desired_shares(equity, weight, ask, limits):
    buy_limit = (number(ask) * (1 + limits.limit_buffer)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return int((number(equity) * number(weight) / buy_limit).to_integral_value(rounding=ROUND_FLOOR))


def fresh_account_snapshot(as_of, now, max_age_seconds):
    return 0 <= (instant(now) - instant(as_of)).total_seconds() <= max_age_seconds


def plan(targets, account, prices, universe, limits, now, paused=False, historical=False, *,
         buy_budget=None, min_trade=None, participation=None, blocked_buys=frozenset()):
    """Sell first; return candidates. The caller rechecks before EACH submission.

    No sale proceeds fund a purchase until a fresh broker snapshot confirms them.
    Limits bound submitted risk, not subsequent price appreciation or overnight gaps.

    Optional quantitative execution controls (all only shrink orders):
    - buy_budget: maximum notional of the next purchase (risk-increase envelope).
    - min_trade: no-trade band; smaller adjustments are skipped, but a sale to a
      zero target always proceeds so a position can always be fully exited.
    - participation: fraction of observed 20-day average dollar volume per order.
    - blocked_buys: symbols that must not be bought now (e.g. chase limit).
    """
    now = instant(now)
    if account.open_orders:
        raise RiskError("Open or uncertain account orders require reconciliation")
    if not fresh_account_snapshot(account.as_of, now, limits.quote_max_seconds):
        raise RiskError("Stale account snapshot")
    cash, equity = number(account.cash), number(account.equity)
    if cash < 0 or equity <= 0:
        raise RiskError("Invalid equity or margin debt")
    symbols = set(targets) | set(account.positions)
    values, desired = {}, {}
    for symbol in symbols:
        held = number(account.positions.get(symbol, 0))
        if symbol not in universe or held < 0 or held != int(held):
            raise RiskError("Unsupported holding or short position: operator review required")
        prices[symbol].validate(now, limits, historical)
        mark = number(prices[symbol].ask)
        values[symbol] = held * mark
        desired[symbol] = desired_shares(equity, targets.get(symbol, 0), mark, limits)
    # Account reports and independent marks must describe the same dedicated account.
    if number(account.receivables) < 0:
        raise RiskError("Invalid receivable balance")
    if abs(cash + sum(values.values()) + number(account.receivables) - equity) > max(Decimal("5"), equity * Decimal("0.02")):
        raise RiskError("Account balances and marked positions disagree")
    sells, buys = [], []
    for symbol in sorted(symbols):
        held = number(account.positions.get(symbol, 0))
        delta = desired[symbol] - held
        quote = prices[symbol]
        if delta < 0:
            quantity = int(min(-delta, held))  # Whole shares only; never turn a sell into a short.
            limit = (number(quote.bid) * (1 - limits.limit_buffer)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
            full_exit = desired[symbol] == 0
            if participation is not None:
                quantity = min(quantity, max(1 if full_exit else 0, _participation_cap(quote, limit, participation)))
            if min_trade is not None and not full_exit and quantity * limit < number(min_trade):
                continue
            if quantity and limit > 0:
                sells.append(Intent(symbol, "sell", quantity, limit))
        elif delta >= 1 and not paused and symbol not in blocked_buys:
            limit = (number(quote.ask) * (1 + limits.limit_buffer)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            capacity = min(
                equity * limits.max_position - values[symbol],
                equity * limits.max_exposure - sum(values.values()),
                cash / (1 + limits.fee_buffer),
            )
            if buy_budget is not None:
                capacity = min(capacity, number(buy_budget))
            quantity = min(int(delta), int((max(0, capacity) / limit).to_integral_value(rounding=ROUND_FLOOR)))
            if participation is not None:
                quantity = min(quantity, _participation_cap(quote, limit, participation))
            if min_trade is not None and quantity * limit < number(min_trade):
                continue
            if quantity:
                buys.append(Intent(symbol, "buy", quantity, limit))
    # Only one order is sent before refreshing account/order state. Risk cannot
    # be reused across several buys, and unfilled sales cannot fund buys.
    return sells or buys


def _participation_cap(quote, limit, participation):
    return int((number(quote.dollar_volume) * number(participation) / limit).to_integral_value(rounding=ROUND_FLOOR))


def trading_day(now):
    return instant(now).astimezone(ZoneInfo("America/New_York")).date().isoformat()
