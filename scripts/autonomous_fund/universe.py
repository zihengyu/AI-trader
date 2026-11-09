"""Point-in-time universe snapshot: who was in each cross-section on each completed session.

Free price sources only serve today's listed names, so every historical factor
cross-section is survivorship-biased. From the day the fund starts, this table
keeps each member's completed daily bars as they were served while it was a
member. A name that later leaves (acquired, delisted, removed from the lists)
stays in the factor lab's cross-section for its membership period, so the
record from the snapshot's first day on is less exposed to survivorship bias.

Invariants:
- Only real bars the provider served are stored; nothing is filled or carried forward.
- A symbol's rows start at its first recorded session: never before it was a member.
- Provider revisions update the bars the provider still serves. Older prices are
  rescaled only when overlapping sessions corroborate a common adjustment.
"""
from __future__ import annotations

from math import isclose, isfinite

ROLES = ("stocks", "etfs")
REVISION_TOLERANCE = 1e-6
RESCALE_TOLERANCE = 1e-4  # Providers can round a common adjustment differently at different prices.


class UniverseSnapshots:
    def __init__(self, journal):
        self.journal = journal
        with journal.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS fund_universe_snapshots (
                role TEXT NOT NULL, symbol TEXT NOT NULL, day TEXT NOT NULL, close REAL NOT NULL,
                volume REAL, open REAL, high REAL, low REAL, recorded_at TEXT NOT NULL,
                PRIMARY KEY(role, symbol, day))""")

    def record(self, now, role, members):
        """Store the completed bars of today's members of `role`.

        members: {symbol: [(date, close, volume, open, high, low)]}, the provider's
        completed bars. A new member starts with its latest bar only; a known one
        gets every bar from its first recorded session on, refreshed when the
        provider revised it. Returns the number of rows written.
        """
        if role not in ROLES:
            raise ValueError(f"unknown universe role {role!r}")
        stamp, written = now.isoformat(), 0
        with self.journal.connection() as db:
            for symbol, rows in members.items():
                rows = sorted(((str(day)[:10], close, *(_finite(v) for v in fields))
                               for day, close, *fields in rows if close is not None and isfinite(close) and close > 0),
                              key=lambda row: row[0])
                if not rows:
                    continue
                first, = db.execute("SELECT MIN(day) FROM fund_universe_snapshots WHERE role=? AND symbol=?",
                                    (role, symbol)).fetchone()
                start = first or rows[-1][0]
                fresh = [r for r in rows if r[0] >= start]
                if not fresh:
                    continue
                # Read only the overlapping provider window, even after years of archiving.
                known = {r[0]: tuple(r[1:]) for r in db.execute(
                    "SELECT day,close,volume,open,high,low FROM fund_universe_snapshots "
                    "WHERE role=? AND symbol=? AND day BETWEEN ? AND ?", (role, symbol, fresh[0][0], fresh[-1][0]))}
                self._rescale_older(db, role, symbol, known, fresh)
                changes = [r for r in fresh if r[0] not in known or
                           any(_differs(a, b) for a, b in zip(known[r[0]], r[1:]))]
                db.executemany(
                    "INSERT OR REPLACE INTO fund_universe_snapshots(role,symbol,day,close,volume,open,high,low,"
                    "recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(role, symbol, day, close, volume, opened, high, low, stamp)
                     for day, close, volume, opened, high, low in changes])
                written += len(changes)
        return written

    @staticmethod
    def _rescale_older(db, role, symbol, known, fresh):
        """Extend an adjustment to older bars only when both oldest overlapping closes support it."""
        overlap = [r for r in fresh if r[0] in known]
        if len(overlap) < 2:
            return
        oldest, next_oldest = overlap[:2]
        old_close, old_volume, *_ = known[oldest[0]]
        next_close, next_volume, *_ = known[next_oldest[0]]
        if not all(isfinite(v) and v > 0 for v in (old_close, next_close)):
            return
        if not _differs(old_close, oldest[1]) or not _differs(next_close, next_oldest[1]):
            return
        ratio = oldest[1] / old_close
        if not isfinite(ratio) or ratio <= 0 or not isclose(ratio, next_oldest[1] / next_close, rel_tol=RESCALE_TOLERANCE):
            return  # One corrected print is not evidence of a split or dividend adjustment.
        inverse = 1 / ratio
        volume_ratio = inverse if all(
            before and after and isclose(after / before, inverse, rel_tol=RESCALE_TOLERANCE)
            for before, after in ((old_volume, oldest[2]), (next_volume, next_oldest[2]))) else 1
        db.execute("UPDATE fund_universe_snapshots SET close=close*?, open=open*?, high=high*?, low=low*?, "
                   "volume=volume*? WHERE role=? AND symbol=? AND day<?",
                   (ratio, ratio, ratio, ratio, volume_ratio, role, symbol, oldest[0]))

    def rows(self, role, symbols, *, before=None):
        """Stored bars for these symbols, oldest first and strictly before the optional cutoff."""
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        result = {}
        with self.journal.connection() as db:
            marks = ",".join("?" * len(symbols))
            query = (f"SELECT symbol, day, close, volume, open, high, low FROM fund_universe_snapshots "
                     f"WHERE role=? AND symbol IN ({marks})")
            args = (role, *symbols)
            if before is not None:
                query += " AND day<?"
                args += (str(before)[:10],)
            for symbol, day, close, volume, opened, high, low in db.execute(query + " ORDER BY symbol, day", args):
                result.setdefault(symbol, []).append((day, close, volume, opened, high, low))
        return result

    def former(self, role, current, before=None):
        """Symbols recorded for `role` that are not current members (left the universe)."""
        with self.journal.connection() as db:
            query, args = "SELECT DISTINCT symbol FROM fund_universe_snapshots WHERE role=?", [role]
            if before is not None:
                query, args = query + " AND day<?", args + [str(before)[:10]]
            return sorted({row[0] for row in db.execute(query, args)} - set(current))

    def since(self, role=None):
        """First recorded session (YYYY-MM-DD), or None before the first record."""
        with self.journal.connection() as db:
            if role is None:
                row = db.execute("SELECT MIN(day) FROM fund_universe_snapshots").fetchone()
            else:
                row = db.execute("SELECT MIN(day) FROM fund_universe_snapshots WHERE role=?", (role,)).fetchone()
        return row[0] if row else None


def snapshot_since(root):
    """First snapshot session of an account directory, read-only; None when there is none."""
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT MIN(day) FROM fund_universe_snapshots").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def survivorship_text(root):
    """The reports' survivorship note, with when the account's snapshot began."""
    from .factors import SURVIVORSHIP_TEXT
    since = snapshot_since(root)
    if since is None:
        return SURVIVORSHIP_TEXT + "每日股票池快照还没有开始（第一次准备因子数据时开始保存）。"
    return (SURVIVORSHIP_TEXT + f"每日股票池快照从 {since} 开始保存：此后离开股票池的股票（被收购、退市或移出名单）"
            "仍可用已保存的行情参与因子检验。缺失的行情、基本面和退市最终收益仍可能造成偏差。")


def _differs(stored, served):
    if stored is None or served is None:
        return stored != served
    return not isclose(stored, served, rel_tol=REVISION_TOLERANCE, abs_tol=1e-12)


def _finite(value):
    return value if value is not None and isfinite(value) else None
