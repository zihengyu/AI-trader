"""Notice when the model behind a configured name changes.

The fund calls models by name (for example deepseek/deepseek-v4-pro); which
model answers is the provider's choice and can change without the name
changing. Every evaluation here assumes the same manager throughout: hit
rates, excess returns, triage calibration, decision noise, and the factor
lab's knowledge cutoff (dates after it are the clean out-of-sample record).
Two signals, both recorded per configured model:

- served versions: the model version the provider reports for each response
  (LumiBot's AgentRunResult.served_models). The first versions seen are the
  baseline; a version never seen before is a change. A provider that only
  echoes the requested name gives no signal here.
- a daily fixed probe, the same question every day with no tools: the model's
  self-reported name and knowledge cutoff and the latest events it knows.
  Answers vary by sampling, so a change needs agreement over several days:
  the median knowledge month of the last 3 probes at least LATER_MONTHS after
  the baseline (and none at or before it), or the same new self-reported name
  on NAME_DAYS consecutive probes that was never given before. Months after
  the probe date are ignored as invented.

A change raises the model's epoch. Epochs are part of prompt_version, so the
scorecards, experiment registry and triage calibration separate the records
before and after it. The probe's knowledge month can only make the factor
lab's configured cutoff later (a shorter clean record), never earlier, and
never creates one for a model with no configured cutoff.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

META = "model_identity"
BASELINE_PROBES = 3
LATER_MONTHS = 3
NAME_DAYS = 3
HISTORY = 30
MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

PROBE_PROMPT = (
    "This is a fixed calibration question, asked every day in the same words, to notice when the model behind "
    "an API name changes. Answer from your own knowledge only; you have no tools. Return model_name (the model "
    "and version you believe you are, or 'unknown'), knowledge_cutoff (YYYY-MM, the last month your training "
    "data covers well) and latest_events: up to five widely reported world events from the latest months you "
    "know, each with its month (YYYY-MM). Only events you are confident about; fewer is fine, never invent.")
PROBE_TASK = "Answer the fixed calibration question."


class ProbeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(min_length=7, max_length=7)
    event: str = Field(min_length=1, max_length=200)


class ModelProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = Field(min_length=1, max_length=80)
    knowledge_cutoff: str = Field(min_length=7, max_length=7)
    latest_events: list[ProbeEvent] = Field(default_factory=list, max_length=5)


def _index(text):
    match = MONTH.match(str(text or ""))
    return int(match.group(1)) * 12 + int(match.group(2)) - 1 if match else None


def _month(index):
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def knowledge_month(answer, day):
    """The latest month the answer shows knowledge of, no later than the probe's own month."""
    today = day.year * 12 + day.month - 1
    months = [_index(answer.get("knowledge_cutoff"))] + [_index(e.get("month")) for e in answer.get("latest_events") or []]
    valid = [m for m in months if m is not None and m <= today]
    return _month(max(valid)) if valid else None


def _name(text):
    name = "".join(ch for ch in str(text or "").lower() if ch.isalnum())
    return None if name in {"", "unknown"} else name


def _entry(state, model):
    return state.setdefault(model, {"served": [], "probes": [], "baseline_month": None, "baseline_name": None,
                                    "epoch": 0, "changes": []})


def _change(entry, model, kind, before, after, at):
    entry["epoch"] += 1
    change = {"model": model, "kind": kind, "before": before, "after": after, "at": at, "epoch": entry["epoch"]}
    entry["changes"] = (entry["changes"] + [change])[-10:]
    return change


def observe_served(state, model, versions, at):
    """Record the provider-reported versions of one call; return the changes found."""
    entry, changes = _entry(state, model), []
    versions = [v for v in versions or [] if isinstance(v, str) and v]
    if not entry["served"]:
        entry["served"] = versions[:20]  # The first versions seen are the baseline.
        return changes
    for version in versions:
        if version not in entry["served"]:
            changes.append(_change(entry, model, "served_version", list(entry["served"]), version, at))
            entry["served"] = (entry["served"] + [version])[-20:]
    return changes


def observe_probe(state, model, answer, day, at):
    """Record one probe answer; return the changes its agreement with earlier probes shows."""
    entry, changes = _entry(state, model), []
    entry["probes"] = (entry["probes"] + [{"day": day.isoformat(), "month": knowledge_month(answer, day),
                                           "name": _name(answer.get("model_name"))}])[-HISTORY:]
    months = [_index(p["month"]) for p in entry["probes"] if p["month"]]
    baseline = _index(entry["baseline_month"])
    if baseline is None:
        if len(months) >= BASELINE_PROBES:
            entry["baseline_month"] = _month(sorted(months[:BASELINE_PROBES])[BASELINE_PROBES // 2])
    elif len(months) >= 3:
        last = sorted(months[-3:])
        if last[1] >= baseline + LATER_MONTHS and last[0] > baseline:
            changes.append(_change(entry, model, "knowledge_later", entry["baseline_month"], _month(last[1]), at))
            entry["baseline_month"] = _month(last[1])
    names = [p["name"] for p in entry["probes"][-NAME_DAYS:]]
    # A phrasing given before the current streak is sampling variety, not a new model.
    seen_names = {p["name"] for p in entry["probes"][:-NAME_DAYS]} | {entry["baseline_name"]}
    if len(names) == NAME_DAYS and names[0] and len(set(names)) == 1:
        if entry["baseline_name"] is None:
            entry["baseline_name"] = names[0]
        elif names[0] != entry["baseline_name"] and names[0] not in seen_names:
            changes.append(_change(entry, model, "self_reported_name", entry["baseline_name"], names[0], at))
            entry["baseline_name"] = names[0]
    return changes


def epochs(state):
    """{model: epoch} for models whose identity changed at least once (part of prompt_version)."""
    return {model: entry["epoch"] for model, entry in sorted(state.items()) if entry.get("epoch")}


def observed_cutoffs(state):
    """{model: last day of its baseline knowledge month}; the factor lab uses it only to make a cutoff later."""
    result = {}
    for model, entry in state.items():
        index = _index(entry.get("baseline_month"))
        if index is not None:
            year, month = index // 12, index % 12 + 1
            result[model] = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    return result
