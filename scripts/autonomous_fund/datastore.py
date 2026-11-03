"""Each New York trading day's research data, fetched once and kept on disk.

The scheduled prefetch job (`run prefetch`, systemd timer before the pre-open
research) and the runner share this store under `<run-dir>/cache/daily/<day>/`:

- `bars/<SYMBOL>.json`: completed daily bars (date, close, volume, open, high,
  low) as the data source returned them, sessions BEFORE that day only;
- `<name>.json`: other daily data (FRED series, earnings dates).

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

KEEP_DAYS = 3


class DailyStore:
    def __init__(self, root):
        self.root = Path(root)

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, default=str), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def bars(self, day, symbol, length):
        """Rows [(date, close, volume, open, high, low)] when stored for at least `length` bars; else None."""
        stored = self._read(self.root / str(day) / "bars" / f"{symbol}.json")
        if not stored or stored.get("requested", 0) < length:
            return None
        return [(date.fromisoformat(r[0]), *r[1:]) for r in stored["rows"]]

    def save_bars(self, day, symbol, length, rows):
        """Only completed sessions before `day`, never today's forming bar."""
        kept = [[str(r[0]), *r[1:]] for r in rows if str(r[0]) < str(day)]
        self._write(self.root / str(day) / "bars" / f"{symbol}.json", {"requested": length, "rows": kept})

    def get(self, day, name):
        return self._read(self.root / str(day) / f"{name}.json")

    def put(self, day, name, value):
        self._write(self.root / str(day) / f"{name}.json", value)

    def prune(self, today, keep=KEEP_DAYS):
        """Remove days older than the newest `keep` (this store's own day directories only)."""
        if not self.root.is_dir():
            return
        days = sorted(p for p in self.root.iterdir() if p.is_dir() and len(p.name) == 10 and p.name <= str(today))
        for old in days[:-keep]:
            shutil.rmtree(old)
