"""Reconstruct forward inputs from immutable source snapshots and append order."""
import json

from .risk import RiskError, instant
from .strategy_validation import digest


def merge_forward_input(seed, incoming):
    """Append new observations to an owned copy of the frozen replay history."""
    for symbol, values in incoming["bars"].items():
        known = seed["bars"].setdefault(symbol, [])
        last = known[-1]["day"] if known else ""
        known.extend(row for row in values if row["day"] > last)
    for symbol, values in incoming.get("features", {}).items():
        known = seed.setdefault("features", {}).setdefault(symbol, [])
        last = instant(known[-1]["at"]) if known else None
        known.extend(row for row in values if last is None or instant(row["at"]) > last)
    existing = {digest(action) for action in seed.get("actions", [])}
    seed.setdefault("actions", []).extend(action for action in incoming.get("actions", []) if digest(action) not in existing)
    seed["coverage"] = {key: value is True and incoming.get("coverage", {}).get(key) is True
                        for key, value in seed.get("coverage", {}).items()}
    seed.pop("provenance", None)
    return seed


def load_frozen_dataset(db, fingerprint):
    """Read a legacy full snapshot or verify a reconstructed forward snapshot."""
    def read(key):
        row = db.execute("SELECT body FROM fund_strategy_datasets WHERE digest=?", (key,)).fetchone()
        if row is None:
            raise RiskError("Frozen strategy dataset is missing")
        return json.loads(row[0])

    data = read(fingerprint)
    sources, seen = [], {fingerprint}
    while "bars" not in data:
        if set(data) != {"forward_base", "forward_source"} or data["forward_base"] in seen:
            raise RiskError("Invalid frozen strategy dataset chain")
        sources.append(data["forward_source"])
        seen.add(data["forward_base"])
        data = read(data["forward_base"])
    for source in reversed(sources):
        incoming = read(source)
        if "bars" not in incoming or digest(incoming) != source:
            raise RiskError("Forward source differs from its frozen identity")
        merge_forward_input(data, incoming)
    if sources and digest(data) != fingerprint:
        raise RiskError("Reconstructed forward data differs from its frozen identity")
    return data
