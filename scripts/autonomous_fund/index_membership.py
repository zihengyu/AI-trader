"""When each stock joined the S&P 500, so historical cross-sections count it only from then.

Survivorship bias has two halves. Stocks that died are missing (their prices
need paid data). And today's large caps were selected *because* they grew:
counted in a cross-section before they were large, their later rise makes
size-like or momentum-like factors look better than they were. The second half
is removable for free: the public S&P 500 constituents list gives each current
member's date added. The factor lab masks a member's factor values before that
date; its prices are kept, so look-back windows after it joined stay complete.

Source: the S&P 500 constituents table published as CSV by the
github.com/datasets project (a mirror of the Wikipedia list, public domain). It
is community maintained: dates are as published, never estimated. A stock that
is not a current member has no date and counts throughout (reported as undated).
Cached in the account's cache directory and refreshed every MAX_AGE_DAYS.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta
from pathlib import Path

from .datastore import write_json
from .risk import instant

SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SOURCE = "S&P 500 constituents, date added (github.com/datasets/s-and-p-500-companies)"
HISTORY_URL = "https://raw.githubusercontent.com/lawcal/sp500-components-history/main/data/components_history.csv"
HISTORY_SOURCE = "lawcal/sp500-components-history"
HISTORY_FILE = "index_membership_history.json"
HISTORY_START = "2007-03-05"
MAX_AGE_DAYS = 30
USER_AGENT = "autonomous-paper-fund/1.0 (research data)"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse(text):
    """{symbol: YYYY-MM-DD} from the constituents CSV; rows without a valid date are skipped."""
    return _parse_current(text)[0]


def _parse_current(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Symbol" not in reader.fieldnames or "Date added" not in reader.fieldnames:
        from .risk import RiskError
        raise RiskError("The S&P 500 constituents file has no Symbol and Date added columns")
    added, members = {}, set()
    for row in reader:
        symbol, day = (row.get("Symbol") or "").strip().upper(), (row.get("Date added") or "").strip()
        if symbol:
            members.add(_key(symbol))
        match = DATE.search(day)
        if symbol and match:
            try:
                added[symbol] = date.fromisoformat(match.group(0)).isoformat()
            except ValueError:
                continue
    return added, members


def _key(symbol):
    return symbol.upper().replace("-", ".")


def _read_cache(path, now):
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        fetched = instant(cached["fetched_at"])
        added = cached["added"]
        if not isinstance(added, dict) or not added or fetched > now:
            return None, None
        # Validate before this optional data can affect a factor panel. Keep the
        # canonical ISO form used for date comparisons, including in the report.
        if any(not symbol or date.fromisoformat(day).isoformat() != day for symbol, day in added.items()):
            return None, None
        return cached, fetched
    except (OSError, KeyError, TypeError, ValueError):
        return None, None


def load(cache_dir, now, get=None):
    """{"added", "source", "fetched_at", "stale"?} from the cache, refreshed when older than MAX_AGE_DAYS.

    A failed refresh keeps the cached list (marked stale); without any list it returns
    None and nothing is masked. `get` is requests.get or a test double.
    """
    path = Path(cache_dir) / "index_membership.json"
    now = instant(now)
    cached, fetched = _read_cache(path, now)
    if cached is not None and "members" in cached and fetched > now - timedelta(days=MAX_AGE_DAYS):
        return cached
    try:
        if get is None:
            import requests
            get = requests.get
        response = get(SOURCE_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
        added, members = _parse_current(response.text)
        if len(added) < 400:  # A truncated or changed file: keep what was known.
            raise ValueError(f"only {len(added)} dated constituents")
    except Exception as exc:
        return {**cached, "stale": type(exc).__name__} if cached else None
    fresh = {"added": {_key(s): d for s, d in added.items()}, "members": sorted(members),
             "source": SOURCE, "fetched_at": now.isoformat()}
    try:
        write_json(path, fresh)
    except OSError:
        pass  # Used this time; fetched again next time.
    return fresh


def _exact_day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Noncanonical membership date")
    return value


def parse_history(text):
    """Membership windows with exact dates and one issuer identity per ticker."""
    reader = csv.DictReader(io.StringIO(text))
    if not {"symbol", "cik", "date_added", "date_removed", "created_at"} <= set(reader.fieldnames or ()):
        raise ValueError("Unexpected S&P 500 history columns")
    grouped, uncertain = {}, set()
    for row in reader:
        symbol, cik = _key((row["symbol"] or "").strip()), (row["cik"] or "").strip()
        try:
            start = _exact_day(row["date_added"])
            end = _exact_day(row["date_removed"]) if row["date_removed"] else None
            recorded = _exact_day(row["created_at"])
            if not symbol or not cik.isdecimal() or (end is not None and max(start, recorded) >= end):
                raise ValueError("Ambiguous membership")
        except (TypeError, ValueError):
            uncertain.add(symbol)  # Includes the source's asterisk-marked estimates.
            continue
        grouped.setdefault(symbol, set()).add((cik, start, end, recorded))
    result = {}
    for symbol, rows in grouped.items():
        if symbol in uncertain or len({row[0] for row in rows}) != 1:
            continue
        windows = sorted(((start, end, recorded) for _, start, end, recorded in rows),
                         key=lambda row: (row[0], row[1] or "", row[2]))
        if any(end is None or end > after[0] for (_, end, _), after in zip(windows, windows[1:])):
            continue
        result[symbol] = windows
    return result


def read_history(cache_dir, now):
    now = instant(now)
    try:
        data = json.loads((Path(cache_dir) / HISTORY_FILE).read_text(encoding="utf-8"))
        fetched = instant(data["fetched_at"])
        if not now - timedelta(days=MAX_AGE_DAYS) <= fetched <= now or not isinstance(data["windows"], dict):
            return None
        for windows in data["windows"].values():
            for start, end, recorded in windows:
                _exact_day(start)
                _exact_day(recorded)
                if end is not None:
                    _exact_day(end)
        return data
    except (OSError, KeyError, TypeError, ValueError):
        return None


def refresh_history(cache_dir, now, get=None):
    now = instant(now)
    cached = read_history(cache_dir, now)
    if cached and instant(cached["fetched_at"]) > now - timedelta(days=7):
        return cached
    if get is None:
        import requests
        get = requests.get
    response = get(HISTORY_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
    response.raise_for_status()
    windows = parse_history(response.text)
    if len(windows) < 400:
        raise ValueError("Incomplete S&P 500 membership history")
    payload = {"windows": windows, "fetched_at": now.isoformat(), "source": HISTORY_SOURCE}
    write_json(Path(cache_dir) / HISTORY_FILE, payload)
    return payload


def matching_windows(membership, history, day):
    """Use only histories consistent with the current constituent snapshot."""
    if not membership or not history or "members" not in membership:
        return {}
    members = set(membership["members"])
    added = membership["added"]
    verified = {}
    for symbol, windows in history["windows"].items():
        eligible = [row for row in windows if row[2] <= day and row[0] <= day]
        if not eligible:
            continue
        start, end, _ = eligible[-1]
        active = end is None or end > day
        if active and symbol in members and added.get(symbol) == start:
            verified[symbol] = eligible
        elif not active and symbol not in members:
            verified[symbol] = eligible
    return verified


def mask(close, added, windows=None):
    """Boolean panel like `close`: False before a dated stock joined, True otherwise (undated stocks)."""
    import pandas as pd
    flags = pd.DataFrame(True, index=close.index, columns=close.columns)
    for symbol in close.columns:
        key = _key(symbol)
        day = (added or {}).get(key)
        if day:
            flags.loc[close.index < pd.Timestamp(day, tz=close.index.tz), symbol] = False
        if key in (windows or {}):
            covered = close.index >= pd.Timestamp(HISTORY_START, tz=close.index.tz)
            selected = pd.Series(False, index=close.index)
            for start, end, _ in windows[key]:
                inside = close.index >= pd.Timestamp(start, tz=close.index.tz)
                if end:
                    inside &= close.index < pd.Timestamp(end, tz=close.index.tz)
                selected |= inside
            flags.loc[covered, symbol] = selected.loc[covered]
    return flags


def summary(close, membership):
    """How the membership dates apply to this cross-section, for the AI context and reports."""
    if membership is None:
        return {"applied": False, "reason": "no S&P 500 membership list available yet"}
    added = membership.get("added") or {}
    dated = [s for s in close.columns if _key(s) in added]
    start = close.index[0] if len(close) else None
    joined = sorted(s for s in dated if start is not None and added[_key(s)] > str(start.date()))
    return {"applied": True, "source": membership.get("source"), "fetched_at": membership.get("fetched_at"),
            **({"stale": membership["stale"]} if membership.get("stale") else {}),
            "dated": len(dated), "undated": sorted(set(close.columns) - set(dated))[:20],
            "joined_within_history": {s: added[_key(s)] for s in joined},
            "interval_symbols": sum(_key(s) in (membership.get("windows") or {}) for s in close.columns),
            "history_source": membership.get("history_source")}
