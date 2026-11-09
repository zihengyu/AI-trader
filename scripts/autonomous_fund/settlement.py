"""Local book: holdings the market stopped serving (acquired, delisted) and their cash settlement.

A held stock that is acquired for cash or delisted stops receiving daily bars.
The local book cannot be valued without a price for every holding, so marks and
new buys pause (honest, never a guessed price). The fund notices this itself
(`holding_without_market_data`); the operator then settles the position at the
cash amount actually paid per share (0 for a worthless delisting) with the
`settle-position` command while the runner is stopped.

Invariants:
- Only the operator states a settlement amount; the application never invents one.
- Cash, the position, the action record and its evidence commit together, and the
  strict ledger replay must reproduce the book or the whole settlement rolls back.
- Paper accounts are settled by the broker itself; this applies to the local book only.
"""
from __future__ import annotations

from datetime import timedelta

from .risk import RiskError, number

STALE_SESSIONS = 3  # Completed NYSE sessions without a new daily bar before a holding is reported.
SETTLEMENT_KIND = "cash_settlement"


def sessions_after(day, now):
    """Completed NYSE sessions after `day` (YYYY-MM-DD) up to the strategy clock."""
    from datetime import date
    import pandas_market_calendars as mcal
    from .market_data import completed_daily_cutoff
    cutoff = completed_daily_cutoff(now)
    start = date.fromisoformat(str(day)[:10]) + timedelta(days=1)
    if start >= cutoff:
        return 0
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=cutoff - timedelta(days=1))
    return len(schedule.index)


def stale_holding(symbol, rows, error, now):
    """A report when the provider served no bar for STALE_SESSIONS completed sessions; else None.

    rows are completed (date, close, volume) bars. A failed read is not evidence
    (the history-unavailable event already covers it); an empty answer or an old
    last bar is.
    """
    if error:
        return None
    last = str(rows[-1][0])[:10] if rows else None
    missing = sessions_after(last, now) if last else None
    if last is not None and missing <= STALE_SESSIONS:
        return None
    return {"symbol": symbol, "last_bar": last, "sessions_without_bar": missing}


def settle_position(journal, now, symbol, cash_per_share, reason):
    """Close a local holding at the cash paid per share. Returns the settlement record.

    Refused when the account has no local book, the symbol is not held, an order or
    unresolved intent for it is open, or the amount is not a finite number >= 0.
    """
    import json
    from .journal import encode
    from .ledger_audit import require_consistent
    from .risk import trading_day
    symbol = str(symbol or "").strip().upper()
    reason = str(reason or "").strip()
    if not symbol or not reason:
        raise RiskError("Provide --symbol and --reason (for example: acquired for cash at 54.20)")
    try:
        price = number(str(cash_per_share))
    except RiskError:
        raise RiskError("--cash-per-share must be a finite number (0 for a worthless delisting)") from None
    if price < 0:
        raise RiskError("--cash-per-share must be >= 0")
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"local_account", "local_positions", "local_orders", "local_actions"} <= tables:
            raise RiskError("This account has no local book; a paper broker settles acquisitions itself")
        row = db.execute("SELECT quantity FROM local_positions WHERE symbol=?", (symbol,)).fetchone()
        if not row or row[0] <= 0:
            raise RiskError(f"The local book holds no {symbol}")
        quantity = number(row[0])
        for order in db.execute("SELECT body FROM local_orders WHERE status IN ('new','partial_fill')"):
            if json.loads(order[0]).get("symbol") == symbol:
                raise RiskError(f"An open order for {symbol} must finish or be canceled first")
        for intent in db.execute("SELECT body FROM intents WHERE status!='terminal'"):
            if json.loads(intent[0]).get("symbol") == symbol:
                raise RiskError(f"An unresolved order intent for {symbol} must be reconciled first")
        day = trading_day(now)
        key = f"{symbol}:{day}:{SETTLEMENT_KIND}"
        if db.execute("SELECT 1 FROM local_actions WHERE id=?", (key,)).fetchone():
            raise RiskError(f"{symbol} was already settled on {day}")
        cash = number(db.execute("SELECT cash FROM local_account WHERE id=1").fetchone()[0])
        proceeds = quantity * price
        db.execute("UPDATE local_account SET cash=? WHERE id=1", (str(cash + proceeds),))
        db.execute("UPDATE local_positions SET quantity=0 WHERE symbol=?", (symbol,))
        db.execute("INSERT INTO local_actions VALUES(?,?)", (key, str(price)))
        record = {"symbol": symbol, "date": day, "kind": SETTLEMENT_KIND, SETTLEMENT_KIND: str(price),
                  "quantity": str(quantity), "proceeds": str(proceeds), "reason": reason, "source": "operator"}
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "local_corporate_action",
                                                                      encode(record)))
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "operator_position_settled",
                                                                      encode(record)))
        require_consistent(db)  # The replay must reproduce the settled book, or nothing is written.
    return record
