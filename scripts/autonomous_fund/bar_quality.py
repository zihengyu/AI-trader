"""Cross-check of the primary daily bars (Yahoo) against a second source (Alpaca).

Yahoo is free and unofficial: a bad print, a missed split or a missing session
would flow into risk estimates, factor tests, scorecards and the universe
snapshot. When Alpaca keys are present (local mode), each symbol's completed
daily closes are compared once per day with Alpaca's split/spin-off-adjusted
bars. Cash-dividend adjustment is excluded on both sides.

Neither source is assumed right when they disagree:

- a session whose closes differ by more than TOLERANCE is dropped (missing, never
  replaced by the other source's value or by an interpolation);
- a symbol that disagrees on many sessions (a split applied by one source only),
  or on its latest completed session (the bar decisions rely on most), is
  withheld for the day: its history is treated as unavailable, so the risk
  engine adds nothing to it and factor tests leave it out.

Alpaca's free feed is IEX only: its closes are the last IEX trade, a few basis
points from the consolidated close for large stocks, so the tolerance catches
gross errors, not small differences. Volume is not compared (IEX volume is a
fraction of the market's). Sessions only one source has are reported, not
filled in.
"""
from __future__ import annotations

import hashlib

TOLERANCE = 0.02          # Relative close difference counted as a disagreement.
UNRELIABLE_SHARE = 0.05   # Disputed share of compared sessions that withholds the symbol.
MIN_UNRELIABLE = 5        # ...but never fewer disputed sessions than this.
MIN_OVERLAP = 20          # Sessions in common before a comparison means anything.
PROBLEMS = ("disputed_sessions", "unreliable")
COMPARISON_VERSION = 2  # Earlier verdicts used split-only reference prices.


def _closes(rows):
    return {str(r[0])[:10]: float(r[1]) for r in rows or ()
            if r[1] is not None and r[1] == r[1] and float(r[1]) > 0}


def digest(rows):
    """Identifies the primary rows a verdict was computed from."""
    closes = _closes(rows)
    text = "|".join(f"{day}:{close:.6f}" for day, close in sorted(closes.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def compare(primary, reference, feed=None):
    """Verdict on `primary` rows against `reference` rows ((date, close, ...) completed bars)."""
    mine, theirs = _closes(primary), _closes(reference)
    verdict = {"feed": feed, "tolerance": TOLERANCE, "primary_sessions": len(mine)}
    if not mine:
        return {**verdict, "status": "no_primary", "compared": 0, "disputed": []}
    first, last = min(mine), max(mine)
    common = sorted(set(mine) & set(theirs))
    differences = {day: mine[day] / theirs[day] - 1 for day in common}
    disputed = [day for day in common if abs(differences[day]) > TOLERANCE]
    missing = sorted(day for day in theirs if day >= first and day not in mine)
    missing_latest = missing[-1] if missing and missing[-1] > last else None
    largest = sorted(disputed, key=lambda day: -abs(differences[day]))[:3]
    verdict.update(compared=len(common), disputed=disputed, missing_in_primary=missing[:20],
                   missing_count=len(missing), missing_latest=missing_latest, latest=last,
                   largest=[{"date": day, "primary": mine[day], "reference": theirs[day],
                             "difference": differences[day]} for day in largest])
    if len(common) < MIN_OVERLAP:
        verdict["status"] = "insufficient_overlap"
    elif (last in disputed or
          (len(disputed) >= MIN_UNRELIABLE and len(disputed) > UNRELIABLE_SHARE * len(common))):
        verdict["status"] = "unreliable"
    else:
        verdict["status"] = "disputed_sessions" if disputed else "agree"
    return verdict


def apply(rows, verdict):
    """The rows the fund may use under `verdict`: disputed sessions dropped; none when unreliable."""
    if not verdict:
        return rows
    if verdict.get("status") == "unreliable":
        return []
    if verdict.get("status") != "disputed_sessions":
        return rows
    disputed = set(verdict.get("disputed") or ())
    return [row for row in rows if str(row[0])[:10] not in disputed] if disputed else rows


def cached_verdict(store, day, symbol, rows, cutoff, *, reference=None, keep_previous_rejection=False):
    """A verdict belongs to the exact closes and cutoff, even when the service is down."""
    saved = store.get(day, f"quality/{symbol}") if store is not None else None
    if saved and saved.get("cutoff") == str(cutoff) and saved.get("digest") == digest(rows):
        current = (saved.get("comparison_version") == COMPARISON_VERSION and
                   (reference is None or saved.get("reference_adjustment") == getattr(reference, "adjustment", None)))
        if current or (keep_previous_rejection and saved.get("status") in PROBLEMS):
            return saved
    return None


def check(store, day, symbol, rows, cutoff, reference):
    """The day's verdict for `symbol`, computed once per primary rows and cached in the daily store.

    A symbol the reference does not serve is recorded as `no_reference` (checked, not
    comparable). A network or service failure propagates, so the caller can pause.
    """
    from .history import NoDailyBars
    saved = cached_verdict(store, day, symbol, rows, cutoff, reference=reference)
    if saved:
        return saved
    try:
        other = reference.bars(symbol, len(rows) + 5, cutoff)
        verdict = compare(rows, other, getattr(reference, "feed", None))
    except NoDailyBars as exc:
        verdict = {"status": "no_reference", "reason": str(exc)[:120], "feed": getattr(reference, "feed", None),
                   "compared": 0, "disputed": []}
    verdict.update(symbol=symbol, digest=digest(rows), cutoff=str(cutoff), comparison_version=COMPARISON_VERSION,
                   reference_adjustment=getattr(reference, "adjustment", None))
    if store is not None:
        try:
            store.put(day, f"quality/{symbol}", verdict)
        except OSError:
            pass  # The cache saves a request; the verdict still applies this time.
    return verdict


def check_store(store, day, symbols, length, reference):
    """Data desk / prefetch: check every stored symbol for the day. Returns counts by status."""
    counts, failed = {}, None
    for symbol in symbols:
        rows = store.bars(day, symbol, length)
        if not rows:
            continue
        try:
            verdict = check(store, day, symbol, rows, day, reference)
        except Exception as exc:  # The service is down: stop asking for the rest of this pass.
            failed = type(exc).__name__
            break
        counts[verdict["status"]] = counts.get(verdict["status"], 0) + 1
    return {"statuses": counts, **({"unavailable": failed} if failed else {})}
