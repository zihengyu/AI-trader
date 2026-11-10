"""Idle cash earns the short-term Treasury rate: a deterministic sweep into one cash-equivalent ETF.

The operator admits exactly one `cash_equivalent` symbol (for example an
ultra-short Treasury bill ETF). It is never an AI target. Every account view the
AI and the risk rules see treats its holding as cash (`merge`), so the exposure
budget, the daily risk-increase envelope, approvals and the daily buy turnover
are unchanged by sweeping. The executor then:

- before an AI purchase that real cash cannot fund, sells just enough of the
  sweep first (`funding_sell`); the purchase follows after that sale settles in
  a fresh snapshot, through the unchanged allocation checks;
- once the AI allocation is complete, buys the sweep with cash above a small
  buffer when the excess exceeds a band (`park_buy`).

Sweep orders are ordinary limit orders on the same durable submission path
(STOP, monitor, market hours, one unresolved intent, daily caps).

Parking is the system's cash management, not part of an AI decision: an open
parking order is never canceled because the decision it followed expired (it is
still repriced when stale, and canceled when a newer AI allocation takes force).
After an AI trade, cash waits for the next session before it is parked
(`min_idle_sessions`), so cash the AI may soon spend again does not pay a round
trip. At most SWEEP_BUYS_PER_DAY parking orders are sent a day, so parking can
never use up the purchase budget the AI's own orders need.
"""
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .risk import Intent, RiskError, number

KIND = "cash_equivalent"
SWEEP_BUYS_PER_DAY = 3


def policy_context(sweep):
    """Explicit operator policy for read-only reviews; no new trading permission."""
    if sweep is None:
        return {"enabled": False}
    return {"enabled": True, "symbol": sweep.symbol, "keep_cash": str(sweep.keep_cash), "band": str(sweep.band),
            "min_idle_sessions": sweep.min_idle_sessions, "max_parking_orders_per_day": SWEEP_BUYS_PER_DAY,
            "execution_owner": "system_cash_management", "ai_target_allowed": False, "counts_as_cash": True,
            "ordinary_position_and_exposure_limits_apply": False,
            "order_controls": ["real_cash", "cash_buffer", "fees", "limit_price", "fresh_source_data",
                               "market_session", "STOP", "monitor_if_required", "daily_submissions"]}


@dataclass(frozen=True)
class CashSweep:
    symbol: str
    keep_cash: Decimal = Decimal("0.01")
    band: Decimal = Decimal("0.02")
    # 1: after an AI trade, wait for the next session before parking; 0: park at once.
    min_idle_sessions: int = 1

    def __post_init__(self):
        object.__setattr__(self, "keep_cash", number(self.keep_cash))
        object.__setattr__(self, "band", number(self.band))
        if type(self.min_idle_sessions) is not int or self.min_idle_sessions not in (0, 1):
            raise RiskError("cash_sweep.min_idle_sessions must be 0 or 1")
        if not Decimal("0") <= self.keep_cash <= Decimal("0.2"):
            raise RiskError("cash_sweep.keep_cash must be 0..0.2 of equity")
        if not Decimal("0") < self.band <= Decimal("0.5"):
            raise RiskError("cash_sweep.band must be above 0 and at most 0.5 of equity")

    @classmethod
    def from_config(cls, section, universe):
        """The configured sweep, or None when disabled or no cash equivalent is admitted."""
        section = dict(section or {})
        enabled = section.pop("enabled", True)
        unknown = set(section) - {"symbol", "keep_cash", "band", "min_idle_sessions"}
        if unknown:
            raise RiskError("Unknown cash_sweep settings: " + ", ".join(sorted(unknown)))
        if type(enabled) is not bool:
            raise RiskError("cash_sweep.enabled must be true/false")
        equivalents = sorted(s for s, kind in dict(universe).items() if kind == KIND)
        if not enabled:
            return None
        symbol = section.pop("symbol", None)
        if symbol is None:
            if not equivalents:
                return None
            if len(equivalents) > 1:
                raise RiskError("Several cash_equivalent symbols are admitted; name one in cash_sweep.symbol")
            symbol = equivalents[0]
        if dict(universe).get(symbol) != KIND:
            raise RiskError(f"cash_sweep.symbol {symbol} must be admitted in the universe as {KIND}")
        if len(equivalents) > 1:
            raise RiskError("Admit exactly one cash_equivalent symbol")
        return cls(symbol, **section)


def merge(account, sweep, mark):
    """The account every risk rule sees: the sweep holding counted as cash at `mark`."""
    held = number(account.positions.get(sweep.symbol, 0))
    if not held:
        return account
    if number(mark) <= 0:
        raise RiskError("Cash sweep price unavailable; the account cannot be valued")
    value = held * number(mark)
    positions = {s: q for s, q in account.positions.items() if s != sweep.symbol}
    return replace(account, cash=number(account.cash) + value, positions=positions,
                   cash_equivalents={sweep.symbol: held}, cash_equivalent_value=value)


def real_cash(account):
    return number(account.cash) - number(account.cash_equivalent_value)


def funding_needed(account, order, limits, sweep):
    """Cash the purchase still lacks after the buffer; 0 when real cash already funds it."""
    need = (order.quantity * number(order.limit) * (1 + limits.fee_buffer)
            + sweep.keep_cash * number(account.equity) - real_cash(account))
    return max(Decimal("0"), need)


def funding_sell(account, order, quote, limits, sweep):
    """Sell enough of the sweep for `order` (a purchase) plus the cash buffer; None when cash suffices."""
    need = (order.quantity * number(order.limit) * (1 + limits.fee_buffer)
            + sweep.keep_cash * number(account.equity) - real_cash(account))
    held = int(number(account.cash_equivalents.get(sweep.symbol, 0)))
    if need <= 0 or held <= 0:
        return None
    limit = (number(quote.bid) * (1 - limits.limit_buffer)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    if limit <= 0:
        return None
    quantity = min(held, int((need / limit).to_integral_value(rounding=ROUND_CEILING)))
    return Intent(sweep.symbol, "sell", quantity, limit) if quantity else None


def idle_excess(account, sweep, min_trade=Decimal("0")):
    """Real cash above the buffer when it exceeds the band (and the minimum trade); None otherwise."""
    equity = number(account.equity)
    excess = real_cash(account) - sweep.keep_cash * equity
    if excess < sweep.band * equity or excess < number(min_trade):
        return None
    return excess


def park_buy(account, quote, limits, sweep, min_trade=Decimal("0")):
    """Buy the sweep with cash above the buffer when the excess exceeds the band; None otherwise."""
    excess = idle_excess(account, sweep, min_trade)
    if excess is None:
        return None
    limit = (number(quote.ask) * (1 + limits.limit_buffer)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    quantity = int((excess / (limit * (1 + limits.fee_buffer))).to_integral_value(rounding=ROUND_FLOOR))
    return Intent(sweep.symbol, "buy", quantity, limit) if quantity > 0 else None
