"""Reconstruct forward inputs from immutable source snapshots and append order."""
import json

from .risk import RiskError, instant
from .strategy_program import validate_program
from .strategy_validation import digest, verify_reference


def load_reference(db, proposal, policy_hash, registered_at):
    reference = proposal["validation"].get("reference_strategy")
    if reference is None:
        return None
    row = db.execute("SELECT proposal,digest,policy_hash,created_at FROM fund_strategy_versions "
                     "WHERE id=? AND version=?", (reference["strategy_id"], reference["version"])).fetchone()
    if (row is None or row[1] != reference["digest"] or row[2] != policy_hash
            or instant(row[3]) > instant(registered_at)):
        raise RiskError("Reference strategy requires an existing exact version under the same frozen policy")
    body = json.loads(row[0])
    verify_reference(proposal, body)
    if body.get("implementation") is None or validate_program(body["implementation"], body["instruments"]) != "ready":
        raise RiskError("Reference strategy requires a supported executable daily implementation")
    return body


def reference_signal_ready(signal, reference, as_of):
    return (isinstance(signal, dict) and signal.get("identity") == reference and bool(signal.get("as_of"))
            and bool(as_of) and instant(signal["as_of"]) == instant(as_of)
            and isinstance(signal.get("raw_targets"), dict))


def reference_signal_matches(signal, receipt, reference):
    return (reference_signal_ready(signal, reference, receipt.get("signal_as_of"))
            and signal["raw_targets"] == receipt.get("raw_signal"))


def can_resume_forward(seed, incoming, day, close):
    """An appended observation must not change any completed leg or its closing signal."""
    for symbol, rows in incoming["bars"].items():
        known = seed["bars"].get(symbol) or []
        last = known[-1]["day"] if known else ""
        if any(last < row["day"] <= day for row in rows):
            return False
    close = instant(close)
    for symbol, rows in incoming.get("features", {}).items():
        known = seed.get("features", {}).get(symbol) or []
        last = instant(known[-1]["at"]) if known else None
        if any((last is None or instant(row["at"]) > last) and instant(row["at"]) <= close
               and instant(row["available_at"]) <= close for row in rows):
            return False
    existing = {digest(action) for action in seed.get("actions", [])}
    return not any(action["ex_date"] <= day and digest(action) not in existing for action in incoming.get("actions", []))


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
