"""Event triage: a fast model decides whether news and price moves need the portfolio manager.

The minute-level watcher wakes the manager early for alerts, large moves, market
stress and material news. On a busy news day the news watch alone wakes it at
every review floor, and almost none of those calls change the portfolio (on
2026-11-09: 19 manager calls, 0 target changes). Triage puts the fast research
model between the soft triggers (news, price moves) and the manager:

- it reads the batch of triggers with the current plan and answers only whether
  the manager should reassess now (never a trade);
- hard triggers (market stress, the AI's own price alerts, stop and take-profit
  exits) and a failed triage still go straight to the manager, and the manager
  still reviews at least every max_quiet_minutes;
- it starts in the shadow: the manager runs as before and every dismissal is
  scored by whether that manager call changed the targets (a miss). Only when the
  one-sided 95% Clopper-Pearson upper bound of the miss rate over the latest
  `window` dismissals is at most `max_miss_rate` does it start dismissing; then
  every `audit_every`-th dismissal still runs the manager to keep measuring, and
  a miss rate above `max_miss_rate` returns it to the shadow.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from math import comb
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .risk import RiskError, bounded_list, bounded_text

TRIAGED = frozenset({"news", "price_move"})  # Soft triggers; every other reason goes straight to the manager.
MODES = ("off", "shadow", "auto", "enforce")


class Triage(BaseModel):
    """Whether the manager should reassess the portfolio now, and why."""
    model_config = ConfigDict(extra="forbid", strict=True)
    escalate: bool
    importance: Literal["low", "medium", "high"]
    affected_symbols: bounded_list(str, 10) = Field(default_factory=list)
    reason: bounded_text(600, 1)


TRIAGE_PROMPT = (
    "You screen intraday events for a long-only paper portfolio's manager, who is expensive to call. "
    "context.triggers lists what happened since the manager last decided (headlines with their source and time, or "
    "price moves against the decision's reference price); context.plan is the manager's current targets, rationale "
    "and the news it asked to watch; context.positions are the holdings. Decide only whether the manager should "
    "reassess the portfolio NOW. Escalate for: company-specific material news on a held or targeted symbol "
    "(earnings, guidance, mergers, regulatory or legal action, management change, a rating action that moves the "
    "price); a macro surprise or central-bank decision; anything that contradicts the plan's rationale; a large "
    "move on a holding with no explanation. Do not escalate for routine commentary, opinion, previews, repeated "
    "coverage of a story the plan already reflects, or headlines on symbols neither held nor targeted unless they "
    "signal a broad market shift. When genuinely unsure, escalate. You never choose trades or weights. Answer with "
    "the Triage JSON object only: escalate, importance, affected_symbols and a short reason naming the headlines or "
    "moves that decided it.")


@dataclass(frozen=True)
class TriageSettings:
    """`triage` section. mode: off, shadow (score only), auto (shadow until proven, then enforce), enforce."""
    mode: str = "auto"
    model: str | None = None  # Default: the research model.
    window: int = 40
    max_miss_rate: float = 0.15
    audit_every: int = 5

    def __post_init__(self):
        if self.mode not in MODES:
            raise RiskError("triage.mode must be off, shadow, auto or enforce")
        if self.model is not None and (not isinstance(self.model, str) or self.model.count("/") != 1):
            raise RiskError("triage.model must be an exact provider/model ID or null")
        if type(self.window) is not int or not 10 <= self.window <= 500:
            raise RiskError("triage.window must be 10..500")
        if isinstance(self.max_miss_rate, bool) or not isinstance(self.max_miss_rate, (int, float)) or \
                not 0.01 <= self.max_miss_rate <= 0.5:
            raise RiskError("triage.max_miss_rate must be 0.01..0.5")
        if type(self.audit_every) is not int or not 2 <= self.audit_every <= 50:
            raise RiskError("triage.audit_every must be 2..50")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - {f.name for f in fields(cls)}
        if unknown:
            raise RiskError("Unknown triage settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def upper_bound(misses, n, confidence=0.95):
    """One-sided Clopper-Pearson upper bound of a binomial rate (exact, by bisection)."""
    if n <= 0:
        return 1.0
    if misses >= n:
        return 1.0
    target = 1 - confidence
    low, high = misses / n, 1.0
    for _ in range(60):
        p = (low + high) / 2
        tail = sum(comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(misses + 1))  # P(X <= misses)
        low, high = (p, high) if tail > target else (low, p)
    return high


def evaluate(pairs, settings, enforcing):
    """(enforcing, stats) after the latest scored dismissals. pairs: [miss bool], oldest first."""
    recent = list(pairs)[-settings.window:]
    n, misses = len(recent), sum(bool(m) for m in recent)
    bound = upper_bound(misses, n)
    stats = {"dismissals_scored": n, "misses": misses, "miss_rate": misses / n if n else None,
             "miss_rate_upper_95": round(bound, 4), "window": settings.window, "max_miss_rate": settings.max_miss_rate}
    if settings.mode == "enforce":
        return True, stats
    if settings.mode != "auto":
        return False, stats
    if not enforcing and n >= settings.window and bound <= settings.max_miss_rate:
        return True, stats
    if enforcing and n >= 10 and misses / n > settings.max_miss_rate:
        return False, stats
    return enforcing, stats


def soft_only(triggers):
    """True when every pending trigger is one triage may screen (and there is at least one)."""
    return bool(triggers) and all(t.get("reason") in TRIAGED for t in triggers)
