"""Cross-check corporate actions for the local fund's research histories."""
from datetime import date, timedelta

from .risk import number

KEY = "corporate_action_quality"
LOOKBACK_DAYS = 120
SPLIT_RELATIVE = number("0.0001")
DIVIDEND_RELATIVE = number("0.01")
DIVIDEND_ABSOLUTE = number("0.005")


def _events(rows, start, end):
    events = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        if not start <= day <= end:
            continue
        for kind in ("split", "dividend"):
            amount = number(row.get(kind, 0))
            if amount > 0:
                key = (day, kind)
                events[key] = (events.get(key, number(1)) * amount if kind == "split"
                               else events.get(key, number(0)) + amount)
    return events


def compare(primary, reference, start, end):
    yahoo, alpaca = (_events(rows, start, end) for rows in (primary, reference))
    discrepancies, unconfirmed = [], []
    splits = {key for key in yahoo.keys() | alpaca.keys() if key[1] == "split"}
    for day, kind in sorted(yahoo.keys() | alpaca.keys()):
        key = (day, kind)
        a, b = yahoo.get(key), alpaca.get(key)
        item = {"date": day.isoformat(), "kind": kind, "yahoo": str(a) if a is not None else None,
                "alpaca": str(b) if b is not None else None}
        if a is None or b is None:
            unconfirmed.append({**item, "reason": "one_source_only"})
            continue
        if kind == "dividend":
            # Yahoo restates earlier dividends for later splits. Restore their
            # ex-date share basis only when the providers agree on the splits.
            later = [split for split in splits if split[0] > day]
            if (day, "split") in splits or any(yahoo.get(split) != alpaca.get(split) for split in later):
                unconfirmed.append({**item, "reason": "uncertain_share_basis"})
                continue
            for split in later:
                a *= yahoo[split]
            item["yahoo_ex_date_basis"] = str(a)
        relative = SPLIT_RELATIVE if kind == "split" else DIVIDEND_RELATIVE
        absolute = number(0) if kind == "split" else DIVIDEND_ABSOLUTE
        if abs(a - b) > max(absolute, relative * max(a, b)):
            discrepancies.append(item)
    return {"status": "unreliable" if discrepancies else "unconfirmed" if unconfirmed else "agree",
            "discrepancies": discrepancies, "unconfirmed": unconfirmed}


def blocked(verdicts):
    return {symbol for symbol, row in (verdicts or {}).items() if row["status"] == "unreliable"}


def refresh(store, day, symbols, length, source, reference):
    """Data desk/prefetch job. Cache comparable verdicts; retry missing sources on the next pass."""
    primary = getattr(source, "history_actions", None)
    if primary is None:
        return {"unavailable": "primary_actions_not_supported"}
    from lumibot.entities import Asset

    previous = store.get(day, KEY) or {}
    end = date.fromisoformat(str(day))
    start = end - timedelta(days=LOOKBACK_DAYS)
    saved = {symbol: previous[symbol] for symbol in symbols
             if symbol in previous and previous[symbol].get("status") in {"agree", "unreliable"}}
    pending = sorted(set(symbols) - saved.keys())
    failure = None
    if pending:
        try:
            external = reference.corporate_actions(pending, start, end)
        except Exception as exc:
            failure = type(exc).__name__
            for symbol in pending:
                saved[symbol] = previous.get(symbol) or {"status": "unavailable", "error": failure}
        else:
            for symbol in pending:
                try:
                    saved[symbol] = compare(primary(Asset(symbol, asset_type="stock"), length),
                                            external[symbol], start, end)
                except Exception as exc:
                    saved[symbol] = {"status": "unavailable", "error": type(exc).__name__}
        if saved != previous:
            try:
                store.put(day, KEY, saved)
            except OSError as exc:
                failure = type(exc).__name__
    counts = {}
    for verdict in saved.values():
        status = verdict["status"]
        counts[status] = counts.get(status, 0) + 1
    return {"statuses": counts, "window_calendar_days": LOOKBACK_DAYS,
            **({"unavailable": failure} if failure else {})}
