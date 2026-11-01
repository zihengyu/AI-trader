"""Read-only Schwab account check: the first, zero-risk step before any live use.

Connects with LumiBot's native Schwab broker (OAuth, account selection by
SCHWAB_ACCOUNT_NUMBER), disables every order method on that instance, and turns
the real account into a preview of how this application would treat it:
reserved cash, the AI capital sleeve, the operator's frozen holdings, symbols
excluded because the operator already holds them, settled versus unsettled
cash, margin, open orders and token renewal. It never submits, cancels or
modifies an order, and this application still has no live execution entry.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .risk import RiskError, number

ORDER_METHODS = ("submit_order", "submit_orders", "_submit_order", "_submit_orders", "cancel_order",
                 "cancel_orders", "_cancel_order", "modify_order", "_modify_order", "cancel_open_orders")


def read_only(broker):
    """Make order methods on THIS broker instance raise; account reads are unaffected."""
    def refuse(*args, **kwargs):
        raise RiskError("Schwab read-only check: order methods are disabled")
    for name in ORDER_METHODS:
        if hasattr(broker, name):
            setattr(broker, name, refuse)
    return broker


def summarize(account, positions, open_orders, config, environ, now=None):
    """Pure analysis of a Schwab account payload for the configured mandate (no I/O)."""
    from .strategy import Mandate
    now = now or datetime.now(timezone.utc)
    securities = account.get("securitiesAccount", {})
    balances = securities.get("currentBalances", {})
    kind = str(securities.get("type", "UNKNOWN")).upper()
    cash = number(balances.get("cashBalance", 0))
    settled = balances.get("cashAvailableForTrading")
    settled = number(settled) if settled is not None else None
    unsettled = number(balances.get("unsettledCash", 0) or 0)
    equity = number((account.get("aggregatedBalance") or {}).get("currentLiquidationValue")
                    or balances.get("liquidationValue") or balances.get("accountValue") or 0)
    mandate = Mandate.from_config(config.get("mandate"))
    universe = set(config["universe"])
    rows, external_value, issues, notices = [], number(0), [], []
    for position in positions:
        symbol = position["symbol"]
        value = number(position.get("market_value") or 0)
        rows.append({**position, "in_ai_universe": symbol in universe})
        external_value += value
    held_universe = sorted({r["symbol"] for r in rows} & universe)
    capital = number(mandate.capital_usd) if mandate.capital_usd is not None else None
    preview = {"account_type": kind, "cash_balance": str(cash), "settled_cash_available": str(settled) if settled is not None else None,
               "unsettled_cash": str(unsettled), "liquidation_value": str(equity), "positions": rows,
               "open_orders": open_orders, "capital_usd": mandate.capital_usd}
    if rows and not mandate.allow_external_holdings:
        issues.append("The account already holds positions: set FUND_ALLOW_EXTERNAL_HOLDINGS=true so they become your "
                      "frozen holdings, or use a separate Schwab account dedicated to the AI (recommended).")
    if cash < 0:
        issues.append("Negative cash balance (margin debt): the AI sleeve would be refused; it never borrows.")
    if capital is not None:
        sleeve_source = cash if mandate.allow_external_holdings else equity
        reserve = sleeve_source - capital
        preview.update(reserved_cash=str(reserve), ai_sleeve_start=str(capital),
                       operator_holdings_value=str(external_value))
        if reserve < 0:
            issues.append(f"Cash {cash} is below FUND_CAPITAL_USD {capital}: lower the capital or add cash.")
        if kind == "CASH" and settled is not None and settled < capital:
            issues.append(f"Settled cash {settled} is below FUND_CAPITAL_USD {capital}: unsettled proceeds cannot "
                          "fund purchases in a cash account without risking good-faith violations.")
    else:
        notices.append("FUND_CAPITAL_USD=account: the whole account would be managed; use only a dedicated account.")
    if held_universe:
        notices.append("You already hold these AI-universe symbols; the AI will not trade them (tax lots and "
                       "attribution stay yours): " + ", ".join(held_universe))
    if open_orders:
        issues.append(f"{len(open_orders)} open order(s) exist; the first mandate start requires none in AI symbols.")
    if kind == "MARGIN":
        notices.append("Margin account: the AI sleeve is cash-only (no borrowing, no shorting); keep margin "
                       "features off for this account if possible.")
    if kind == "CASH":
        notices.append("Cash account: live purchases must use settled cash only (T+1).")
    if not environ.get("SCHWAB_APP_SECRET"):
        notices.append("SCHWAB_APP_SECRET is not set: automatic token refresh is unavailable; Schwab refresh tokens "
                       "expire after about 7 days and then need a new browser login.")
    if not environ.get("SCHWAB_APP_KEY"):
        notices.append("SCHWAB_APP_KEY is not set: LumiBot's default Schwab app and its BotSpot OAuth callback "
                       "(api.botspot.trade) are used. Register your own Schwab developer app for direct control.")
    preview.update(checked_at=now.isoformat(), excluded_shared_symbols=held_universe, issues=issues, notices=notices,
                   verdict="ready_for_read_only_validation" if not issues else "needs_attention",
                   live_trading="not available in this application; requires a separately reviewed entry")
    return preview


def schwab_check(args, config, broker_factory=None, environ=None, wait_seconds=90):
    import os
    environ = os.environ if environ is None else environ
    if not environ.get("SCHWAB_ACCOUNT_NUMBER"):
        raise RiskError("Set SCHWAB_ACCOUNT_NUMBER (the exact account the AI may use) in .env")
    if broker_factory is None:
        from lumibot.brokers import Schwab
        broker_factory = lambda: Schwab(connect_stream=False)  # noqa: E731
    broker = read_only(broker_factory())
    deadline = time.monotonic() + wait_seconds
    while not getattr(broker, "_broker_fully_ready", False) and time.monotonic() < deadline:
        if getattr(broker, "schwab_authorization_error", False):
            break
        time.sleep(1)
    if not getattr(broker, "_broker_fully_ready", False):
        raise RiskError("Schwab login is not complete: finish the OAuth authorization shown above and retry")
    response = broker.client.get_account(broker.hash_value, fields=[broker.client.Account.Fields.POSITIONS])
    if response.status_code != 200:
        raise RiskError(f"Schwab account read failed with HTTP {response.status_code}")
    account = response.json()
    positions = []
    for raw in account.get("securitiesAccount", {}).get("positions", []) or []:
        instrument = raw.get("instrument", {})
        positions.append({"symbol": instrument.get("symbol"), "asset_type": instrument.get("assetType"),
                          "quantity": str(number(raw.get("longQuantity", 0) or 0) - number(raw.get("shortQuantity", 0) or 0)),
                          "market_value": str(raw.get("marketValue", 0))})
    active = getattr(broker, "ACTIVE_SCHWAB_ORDER_STATUSES", set())
    orders = [{"symbol": ((leg.get("instrument") or {}).get("symbol")), "status": order.get("status"),
               "instruction": leg.get("instruction"), "quantity": leg.get("quantity")}
              for order in broker._pull_broker_all_orders() or [] if order.get("status") in active
              for leg in (order.get("orderLegCollection") or [{}])[:1]]
    result = summarize(account, positions, orders, config, environ)
    target = Path(args.run_dir) / "schwab_check.json"
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
