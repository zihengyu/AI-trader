"""Daily-open cash-management scenario using the existing cash policy and planner.

The fund's asynchronous fills are not reproduced. This explicit scenario funds
risk trades before parking idle cash, charges both legs and preserves real cash.
It never sends an order or constructs a broker connection.
"""
from dataclasses import replace
from decimal import ROUND_CEILING

from .cash import CashSweep, merge, park_buy
from .risk import Account, Limits, Price, RiskError, instant, number


def risk_targets(desired, policy):
    """The same pre-projection cash/friction reserve for replay and next signals."""
    settings = policy.get("cash_management") or {}
    if not settings.get("enabled") or settings.get("symbol") != policy.get("cash_symbol"):
        raise RiskError("The cash-management execution policy is unavailable")
    fee, slip = number(policy["fee_rate"]), number(policy["slippage"])
    scale = (1 - number(settings["keep_cash"])) * max(number(0), 1 - 2 * (fee + slip + fee * slip))
    return {s: number(w) * scale for s, w in desired.items()}


def step(book, day, desired, prices, actions, *, policy, now, session_index, hold_positions=False):
    settings = policy.get("cash_management") or {}
    symbol = policy.get("cash_symbol")
    if not settings.get("enabled") or settings.get("symbol") != symbol:
        raise RiskError("The cash-management execution policy is unavailable")
    sweep = CashSweep(symbol, number(settings["keep_cash"]), number(settings["band"]), settings["min_idle_sessions"])
    fee, slip = number(book.policy["fee_rate"]), number(book.policy["slippage"])
    mark = number(prices[symbol])
    previous = book._save_step_state()
    try:
        held, average = book.positions.pop(symbol, 0), book.averages.pop(symbol, mark)
        for action in actions:
            if action["symbol"] == symbol:
                ratio = number(action.get("split", 1))
                shares = number(held) * ratio
                if shares != shares.to_integral_value():
                    raise RiskError("Cash-equivalent split needs cash-in-lieu evidence")
                held, average = int(shares), average / ratio
        # Temporarily merge the ETF at its raw open mark, as the existing fund
        # cash view does. Additional friction is reserved before risk sizing.
        book.cash += held * mark
        book.step(day, risk_targets(desired, policy), prices,
                  [a for a in actions if a["symbol"] != symbol], hold_positions=hold_positions)
        if book.error:
            raise RiskError(book.error["reason"])
        risk_trades = list(book.last["trades"])
        book.cash -= held * mark
        if held:
            book.positions[symbol], book.averages[symbol] = held, average
        funding = []
        # A net funding sale is modeled before the risk purchases. Its proceeds
        # include slippage and fees; selling at the midpoint would overfund cash.
        buffer = sweep.keep_cash * book.value(prices)
        shortage = max(number(0), buffer - book.cash)
        if shortage and held and (book.cash < 0 or any(t["side"] == "buy" for t in risk_trades)):
            net_unit = mark * (1 - slip) * (1 - fee)
            quantity = min(held, int((shortage / net_unit).to_integral_value(rounding=ROUND_CEILING)))
            funding.append(_trade(book, symbol, -quantity, mark, fee, slip, day))
        if book.cash < 0:
            raise RiskError("Cash-equivalent proceeds cannot fund the risk trades")
        if risk_trades:
            book._last_risk_trade = session_index
        idle = session_index - getattr(book, "_last_risk_trade", session_index - 1)
        parking = []
        if idle >= sweep.min_idle_sessions:
            # The shared cash planner consumes only price/fee buffers. A passive
            # reference may intentionally disable its virtual drawdown stop;
            # that is not an executable Limits object or a production permission.
            limits = Limits()
            limits = replace(limits,
                             fee_buffer=max(number(policy["limits"].get("fee_buffer", limits.fee_buffer)), fee),
                             limit_buffer=number(policy["limits"].get("limit_buffer", limits.limit_buffer)))
            account = merge(Account(book.cash, book.value(prices), dict(book.positions), instant(now)), sweep, mark)
            quote = Price(mark * (1 - slip), mark * (1 + slip), instant(now), number(0))
            order = park_buy(account, quote, limits, sweep, min_trade=policy["min_trade_notional"])
            if order is not None:
                parking.append(_trade(book, symbol, order.quantity, mark, fee, slip, day))
        if len(risk_trades) + len(funding) + len(parking) > policy["limits"]["daily_orders"]:
            raise RiskError("Cash management and risk trades exceed the daily submission allowance")
        book.last.update(trades=funding + risk_trades + parking, cash=str(book.cash), positions=dict(book.positions),
                         position_average_prices={s: str(v) for s, v in book.averages.items()},
                         cash_management_scenario="fund_risk_then_park_next_open_v1")
    except Exception:
        book._restore_step_state(previous)
        raise


def _trade(book, symbol, delta, mark, fee, slip, day):
    if not delta:
        raise RiskError("Empty cash-management trade")
    fill = mark * (1 + slip if delta > 0 else 1 - slip)
    notional, charge = abs(delta) * fill, abs(delta) * fill * fee
    next_cash = book.cash - delta * fill - charge
    held = book.positions.get(symbol, 0)
    if next_cash < 0 or held + delta < 0:
        raise RiskError("Cash-management scenario cannot borrow cash or shares")
    if delta > 0:
        book.averages[symbol] = (held * book.averages.get(symbol, mark) + delta * fill) / (held + delta)
    book.cash = next_cash
    if held + delta:
        book.positions[symbol] = held + delta
    else:
        book.positions.pop(symbol, None)
        book.averages.pop(symbol, None)
    book.fees += charge
    book.friction += abs(delta) * abs(fill - mark)
    book.turnover += notional
    book.orders += 1
    return {"symbol": symbol, "side": "buy" if delta > 0 else "sell", "quantity": abs(delta),
            "price": str(fill), "trade_cost": str(charge), "day": day,
            "scope": "virtual_cash_management_open_scenario"}
