"""Each New York trading day's research data, fetched once and kept on disk.

The data desk (datadesk.py, always on), the one-shot `run prefetch` and the
runner share this store under `<run-dir>/cache/daily/<day>/`:

- `bars/<SYMBOL>.json`: completed daily bars (date, close, volume, open, high,
  low), with an exclusive date cutoff. Intraday readers use earlier sessions;
  post-close readers may refresh through the session that just finished;
- `<name>.json`: other data (FRED series, earnings dates, the market snapshot and
  the intraday pulse), rewritten by the desk whenever it refreshes them.

The runner reads the store first and uses the network only for what is missing,
writing what it fetched back. A restart therefore never repeats the day's
downloads, and research no longer waits for them. Nothing is estimated or
carried forward: a missing entry simply means "fetch it now". Writes are
atomic (temporary file, then rename) so a reader never sees a partial file.
"""
import json
import os
import shutil
from datetime import date
from pathlib import Path
from uuid import uuid4

KEEP_DAYS = 3
SETTLED_HOUR = 6  # New York: a new day's splits and dividends are applied in the daily history by then.


def bars_settled(now):
    """True once a trading day's completed daily bars may be stored for the whole day."""
    from zoneinfo import ZoneInfo
    from .risk import instant
    return instant(now).astimezone(ZoneInfo("America/New_York")).hour >= SETTLED_HOUR


class DailyStore:
    def __init__(self, root):
        self.root = Path(root)

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, default=str)
        # A PID alone collides when two threads refresh the same entry. Each
        # writer owns its staging file; publication remains one atomic rename.
        temporary = path.with_name(path.name + f".{os.getpid()}.{uuid4().hex}.tmp")
        created = False
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                created = True
                handle.write(payload)
            temporary.replace(path)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def bars(self, day, symbol, length, *, cutoff=None):
        """Rows [(date, close, volume, open, high, low)] when stored for at least `length` bars; else None."""
        stored = self._read(self.root / str(day) / "bars" / f"{symbol}.json")
        cutoff = str(cutoff or day)
        if (not stored or stored.get("requested", 0) < length or
                stored.get("cutoff", str(day)) < cutoff):
            return None
        return [(date.fromisoformat(r[0]), *r[1:]) for r in stored["rows"] if r[0] < cutoff]

    def save_bars(self, day, symbol, length, rows, *, cutoff=None):
        """Keep sessions before the caller's verified cutoff (before `day` by default)."""
        cutoff = str(cutoff or day)
        kept = [[str(r[0]), *r[1:]] for r in rows if str(r[0]) < cutoff]
        self._write(self.root / str(day) / "bars" / f"{symbol}.json",
                    {"requested": length, "cutoff": cutoff, "rows": kept})

    def get(self, day, name):
        return self._read(self.root / str(day) / f"{name}.json")

    def put(self, day, name, value):
        self._write(self.root / str(day) / f"{name}.json", value)

    def version(self, day, name):
        """Changes whenever `name` is rewritten (the data desk refreshes during the day); None when absent."""
        try:
            stat = (self.root / str(day) / f"{name}.json").stat()
        except OSError:
            return None
        return stat.st_ino, stat.st_mtime_ns  # Every atomic rewrite is a new file (new inode).

    def prune(self, today, keep=KEEP_DAYS):
        """Remove days older than the newest `keep` (this store's own day directories only)."""
        if not self.root.is_dir():
            return
        days = sorted(p for p in self.root.iterdir() if p.is_dir() and len(p.name) == 10 and p.name <= str(today))
        for old in days[:-keep]:
            shutil.rmtree(old)
