"""Why trading is stopped, so that only a service restart is ever resumed automatically.

The presence of STOP alone stops new orders (unchanged everywhere). Its content
records who stopped trading and why:

- ``operator``: the stop command, a first installation or a restored backup, and
  any older empty STOP file. Never resumed automatically.
- ``watchdog``: the independent monitor or the service supervisor saw a fault.
  Only process-liveness faults that a restart cures may resume automatically.
- ``service``: the service itself (re)started or exited.

A writer never downgrades a stronger class, so a service restart cannot hide an
operator's stop. Reads and writes share a file lock with the automatic resume,
which removes STOP only if it still holds exactly the record it checked.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .risk import RiskError

RANK = {"service": 1, "watchdog": 2, "operator": 3}
# Faults a fresh process cures. Not resumable: low disk space, an old runner
# revision, a matching error or missing notifications need the operator.
RESUMABLE_WATCHDOG = frozenset({"heartbeat_stale", "heartbeat_unavailable", "runner_exited", "iteration_stalled",
                                "agent_call_stalled", "independent_monitor_stale", "independent_monitor_unavailable",
                                "monitor_child_stalled"})


def _lock(root):
    return FileLock(str(Path(root) / "stop.lock"), timeout=30)


def _parse(path):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        record = json.loads(text)
        if isinstance(record, dict) and record.get("class") in RANK and record.get("reason"):
            return record
    except ValueError:
        pass
    return {"class": "operator", "reason": "unrecorded", "at": None, "legacy": True}


def read(root):
    """The STOP record, or None. Empty or unknown content is an operator stop."""
    with _lock(root):
        return _parse(Path(root) / "STOP")


def write(root, kind, reason, now=None, detail=None):
    """Assert STOP; a weaker class never replaces a stronger one. Returns the effective record."""
    if kind not in RANK:
        raise RiskError(f"Unknown stop class {kind}")
    root = Path(root)
    record = {"class": kind, "reason": str(reason), "at": (now or datetime.now(timezone.utc)).isoformat(),
              **({"detail": detail} if detail else {})}
    with _lock(root):
        current = _parse(root / "STOP")
        if current is not None and RANK[current["class"]] > RANK[kind]:
            return current
        if current is not None and current["class"] == kind and current["reason"] == record["reason"]:
            return current  # Keep the original time: how long it has been stopped matters.
        temporary = root / "STOP.tmp"
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(root / "STOP")
        return record


def clear(root, expected=None):
    """Remove STOP. With `expected`, only if it still holds exactly that record."""
    path = Path(root) / "STOP"
    with _lock(root):
        current = _parse(path)
        if current is None:
            return False
        if expected is not None and current != expected:
            return False
        path.unlink()
        return True


def resumable(record):
    if record is None or record.get("legacy"):
        return False
    return record["class"] == "service" or (record["class"] == "watchdog" and record["reason"] in RESUMABLE_WATCHDOG)


@dataclass(frozen=True)
class ResumePolicy:
    """`service` section: automatic resume after a restart, under the monitor's checks."""
    auto_resume: bool = True
    auto_resume_after_seconds: int = 180
    max_auto_resumes_per_day: int = 3

    def __post_init__(self):
        if type(self.auto_resume) is not bool:
            raise RiskError("service.auto_resume must be true/false")
        if type(self.auto_resume_after_seconds) is not int or not 60 <= self.auto_resume_after_seconds <= 3600:
            raise RiskError("service.auto_resume_after_seconds must be 60..3600")
        if type(self.max_auto_resumes_per_day) is not int or not 0 <= self.max_auto_resumes_per_day <= 20:
            raise RiskError("service.max_auto_resumes_per_day must be 0..20")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown service settings: " + ", ".join(sorted(unknown)))
        return cls(**section)
