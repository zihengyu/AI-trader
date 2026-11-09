"""Point-in-time universe snapshot: who was in each cross-section on each completed session.

Free price sources only serve today's listed names, so every historical factor
cross-section is survivorship-biased. From the day the fund starts, this table
keeps each member's completed daily bars as they were served while it was a
member. A name that later leaves (acquired, delisted, removed from the lists)
stays in the factor lab's cross-section for its membership period, so the
record from the snapshot's first day on has no survivorship bias.

Invariants:
- Only real bars the provider served are stored; nothing is filled or carried forward.
- A symbol's rows start at its first recorded session: never before it was a member.
- While a symbol is a member its stored rows follow the provider's latest adjustment
  (splits and dividends revise history), so a later read never mixes two adjustments.
"""
from __future__ import annotations

ROLES = ("stocks", "etfs")
REVISION_TOLERANCE = 1e-6  # Relative change of a stored close that counts as a provider revision.


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
            stored = {}
            for symbol, day, close in db.execute("SELECT symbol, day, close FROM fund_universe_snapshots "
                                                 "WHERE role=?", (role,)):
                stored.setdefault(symbol, {})[day] = close
            for symbol, rows in members.items():
                rows = sorted((str(r[0])[:10], *r[1:]) for r in rows if r[1] is not None and r[1] > 0)
                if not rows:
                    continue
                known = stored.get(symbol) or {}
                start = min(known) if known else rows[-1][0]
                fresh = [r for r in rows if r[0] >= start]
                revised = self._rescale_older(db, role, symbol, known, fresh)
                changes = [r for r in fresh if r[0] not in known or revised or _differs(known[r[0]], r[1])]
                db.executemany(
                    "INSERT OR REPLACE INTO fund_universe_snapshots(role,symbol,day,close,volume,open,high,low,"
                    "recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(role, symbol, day, close, volume, opened, high, low, stamp)
                     for day, close, volume, opened, high, low in changes])
                written += len(changes)
        return written

    @staticmethod
    def _rescale_older(db, role, symbol, known, fresh):
        """When the provider revised the oldest overlapping bar, scale the stored bars before it alike.

        The provider's history is finite: bars older than it can no longer be refreshed
        directly, so they take the revision ratio of the oldest bar it still serves.
        """
        overlap = [r for r in fresh if r[0] in known]
        if not overlap or not _differs(known[overlap[0][0]], overlap[0][1]):
            return False
        ratio = overlap[0][1] / known[overlap[0][0]]
        db.execute("UPDATE fund_universe_snapshots SET close=close*?, open=open*?, high=high*?, low=low*?, "
                   "volume=volume/? WHERE role=? AND symbol=? AND day<?",
                   (ratio, ratio, ratio, ratio, ratio, role, symbol, overlap[0][0]))
        return True

    def rows(self, role, symbols):
        """{symbol: [(date, close, volume, open, high, low)]} for the given symbols, oldest first."""
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        result = {}
        with self.journal.connection() as db:
            marks = ",".join("?" * len(symbols))
            for symbol, day, close, volume, opened, high, low in db.execute(
                    f"SELECT symbol, day, close, volume, open, high, low FROM fund_universe_snapshots "
                    f"WHERE role=? AND symbol IN ({marks}) ORDER BY symbol, day", (role, *symbols)):
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
    from pathlib import Path
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
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
            "仍按在池期间留在因子检验里，这段记录没有幸存者偏差。")


def _differs(stored, served):
    return abs(served - stored) > REVISION_TOLERANCE * max(abs(stored), 1e-12)
