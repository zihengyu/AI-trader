"""Explicit secret-file loading; never discover a parent directory's dotenv."""
import os
import stat
from pathlib import Path

from .risk import RiskError


def load_environment(filename):
    path = Path(filename)
    if not path.is_file():
        raise RiskError("The explicitly selected environment file does not exist")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RiskError("Secret environment file must be private: chmod 600 <env-file>")
    from dotenv import load_dotenv
    load_dotenv(path, override=False)
