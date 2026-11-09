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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SOURCE = "S&P 500 constituents, date added (github.com/datasets/s-and-p-500-companies)"
MAX_AGE_DAYS = 30
USER_AGENT = "autonomous-paper-fund/1.0 (research data)"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse(text):
    """{symbol: YYYY-MM-DD} from the constituents CSV; rows without a valid date are skipped."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Symbol" not in reader.fieldnames or "Date added" not in reader.fieldnames:
        from .risk import RiskError
        raise RiskError("The S&P 500 constituents file has no Symbol and Date added columns")
    added = {}
    for row in reader:
        symbol, day = (row.get("Symbol") or "").strip().upper(), (row.get("Date added") or "").strip()
        match = DATE.search(day)
        if symbol and match:
            try:
                added[symbol] = date.fromisoformat(match.group(0)).isoformat()
            except ValueError:
                continue
    return added


def _key(symbol):
    return symbol.upper().replace("-", ".")


def load(cache_dir, now, get=None):
    """{"added", "source", "fetched_at", "stale"?} from the cache, refreshed when older than MAX_AGE_DAYS.

    A failed refresh keeps the cached list (marked stale); without any list it returns
    None and nothing is masked. `get` is requests.get or a test double.
    """
    path = Path(cache_dir) / "index_membership.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    now = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if cached and datetime.fromisoformat(cached["fetched_at"]) > now - timedelta(days=MAX_AGE_DAYS):
        return cached
    try:
        if get is None:
            import requests
            get = requests.get
        response = get(SOURCE_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
        added = parse(response.text)
        if len(added) < 400:  # A truncated or changed file: keep what was known.
            raise ValueError(f"only {len(added)} dated constituents")
    except Exception as exc:
        return {**cached, "stale": type(exc).__name__} if cached else None
    fresh = {"added": {_key(s): d for s, d in added.items()}, "source": SOURCE, "fetched_at": now.isoformat()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fresh), encoding="utf-8")
    except OSError:
        pass  # Used this time; fetched again next time.
    return fresh


def mask(close, added):
    """Boolean panel like `close`: False before a dated stock joined, True otherwise (undated stocks)."""
    import pandas as pd
    flags = pd.DataFrame(True, index=close.index, columns=close.columns)
    for symbol in close.columns:
        day = (added or {}).get(_key(symbol))
        if day:
            flags.loc[close.index < pd.Timestamp(day, tz=close.index.tz), symbol] = False
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
            "joined_within_history": {s: added[_key(s)] for s in joined}}
