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
from time import sleep
from uuid import uuid4

KEEP_DAYS = 3
SETTLED_HOUR = 6  # New York: a new day's splits and dividends are applied in the daily history by then.


def bars_settled(now):
    """True once a trading day's completed daily bars may be stored for the whole day."""
    from zoneinfo import ZoneInfo
    from .risk import instant
    return instant(now).astimezone(ZoneInfo("America/New_York")).hour >= SETTLED_HOUR


def write_json(path, value):
    """Publish a complete JSON file using a staging path unique to this writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, default=str)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid4().hex}.tmp")
    created = False
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(payload)
        # Concurrent renames or an open reader can briefly lock the destination on Windows.
        for delay in (0.01, 0.02, 0.04, None):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                if delay is None or getattr(exc, "winerror", None) not in (5, 32):
                    raise
                sleep(delay)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


class DailyStore:
    def __init__(self, root):
        self.root = Path(root)

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
        # Older writers could store a nominal history whose closes were all unusable.
        rows = [(date.fromisoformat(r[0]), *r[1:]) for r in stored["rows"]
                if r[0] < cutoff and isinstance(r[1], (int, float)) and 0 < r[1] < float("inf")]
        return rows or None

    def save_bars(self, day, symbol, length, rows, *, cutoff=None):
        """Keep sessions before the caller's verified cutoff (before `day` by default)."""
        cutoff = str(cutoff or day)
        kept = [[str(r[0]), *r[1:]] for r in rows if str(r[0]) < cutoff]
        write_json(self.root / str(day) / "bars" / f"{symbol}.json",
                   {"requested": length, "cutoff": cutoff, "rows": kept})

    def get(self, day, name):
        return self._read(self.root / str(day) / f"{name}.json")

    def put(self, day, name, value):
        write_json(self.root / str(day) / f"{name}.json", value)

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
