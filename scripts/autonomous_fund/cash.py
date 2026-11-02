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
"""
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .risk import Intent, RiskError, number

KIND = "cash_equivalent"


@dataclass(frozen=True)
class CashSweep:
    symbol: str
    keep_cash: Decimal = Decimal("0.01")
    band: Decimal = Decimal("0.02")

    def __post_init__(self):
        object.__setattr__(self, "keep_cash", number(self.keep_cash))
        object.__setattr__(self, "band", number(self.band))
        if not Decimal("0") <= self.keep_cash <= Decimal("0.2"):
            raise RiskError("cash_sweep.keep_cash must be 0..0.2 of equity")
        if not Decimal("0") < self.band <= Decimal("0.5"):
            raise RiskError("cash_sweep.band must be above 0 and at most 0.5 of equity")

    @classmethod
    def from_config(cls, section, universe):
        """The configured sweep, or None when disabled or no cash equivalent is admitted."""
        section = dict(section or {})
        enabled = section.pop("enabled", True)
        unknown = set(section) - {"symbol", "keep_cash", "band"}
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


def park_buy(account, quote, limits, sweep, min_trade=Decimal("0")):
    """Buy the sweep with cash above the buffer when the excess exceeds the band; None otherwise."""
    equity = number(account.equity)
    excess = real_cash(account) - sweep.keep_cash * equity
    if excess < sweep.band * equity or excess < number(min_trade):
        return None
    limit = (number(quote.ask) * (1 + limits.limit_buffer)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    quantity = int((excess / (limit * (1 + limits.fee_buffer))).to_integral_value(rounding=ROUND_FLOOR))
    return Intent(sweep.symbol, "buy", quantity, limit) if quantity > 0 else None
