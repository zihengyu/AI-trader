"""Portfolio construction A/B: the AI's own weights versus risk budgets proportional to them.

Every validated decision is recorded in both forms (`construction_variant`):
`ai` is the AI's weights, `risk_budget` re-weights the same names so each one's
ex-ante risk contribution is proportional to its AI weight at the same gross
exposure (quant.risk_budget). Both are scored identically on completed closes
(one-session beta-matched excess return, one pair per session), so no extra
model call is needed. In `auto` mode the construction that trades switches only
after `min_pairs` sessions with |t| >= `switch_t_stat`; the risk engine's limits
apply to either one.
"""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .risk import RiskError, instant

MODES = ("ai", "risk_budget", "auto")
ZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ConstructionSettings:
    mode: str = "auto"
    min_pairs: int = 20
    switch_t_stat: float = 2.0

    def __post_init__(self):
        if self.mode not in MODES:
            raise RiskError("construction.mode must be one of: " + ", ".join(MODES))
        if type(self.min_pairs) is not int or not 5 <= self.min_pairs <= 500:
            raise RiskError("construction.min_pairs must be 5..500")
        if isinstance(self.switch_t_stat, bool) or not isinstance(self.switch_t_stat, (int, float)) or not (
                1.0 <= self.switch_t_stat <= 5.0):
            raise RiskError("construction.switch_t_stat must be 1..5")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown construction settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def pairs(variants, closes, benchmark="SPY"):
    """[(session_date, risk_budget minus ai one-session excess)], one pair per session (its last variant)."""
    from .scorecard import score_decisions
    by_day = {}
    for at, body in variants:
        by_day[instant(at).astimezone(ZONE).date().isoformat()] = (at, body)
    result = []
    for day in sorted(by_day):
        at, body = by_day[day]
        scored = {}
        for arm in ("ai", "risk_budget"):
            targets = [{"symbol": s, "weight": float(w)} for s, w in (body.get(arm) or {}).items()]
            scored[arm] = score_decisions([(at, {"targets": targets})], closes, benchmark)["recent"][0]["horizons"]["1"]
        if all(item["status"] == "observed" for item in scored.values()):
            result.append((day, scored["risk_budget"]["excess_return"] - scored["ai"]["excess_return"]))
    return result


def evaluate(variants, closes, settings, current="ai"):
    """Paired evidence and which construction should trade next (only `auto` ever switches)."""
    from .validation import paired_test
    observed = pairs(variants, closes)
    test = paired_test([d for _, d in observed], settings.min_pairs)
    champion = current if settings.mode == "auto" else settings.mode
    if settings.mode == "auto" and test["sufficient"] and test["t_stat"] is not None:
        if test["t_stat"] >= settings.switch_t_stat:
            champion = "risk_budget"
        elif test["t_stat"] <= -settings.switch_t_stat:
            champion = "ai"
    return {"mode": settings.mode, "champion": champion, "previous": current, "test": test,
            "last_session": observed[-1][0] if observed else None,
            "interpretation": "mean > 0: risk budgets beat the AI's own weights by that much beta-matched excess "
                              "per session. Switching needs min_pairs sessions and |t| >= switch_t_stat."}
