"""Open research proposals and a versioned, capability-limited execution contract.

Mechanisms are free text. An unsupported implementation stays a feasibility task;
it is never converted to a factor or executed through Python eval/import/exec.
"""
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .risk import RiskError, number


class PositionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(min_length=1, max_length=24)
    entry: str = Field(min_length=1, max_length=1000)
    exit: str = Field(min_length=1, max_length=1000)
    weight: str = Field(min_length=1, max_length=1000)


class StrategyParameter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=3, max_length=40)
    value: float = Field(ge=-1000000, le=1000000, allow_inf_nan=False)
    integer: bool = False


class StrategyProgram(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    adapter: str = Field(default="daily_rules_v1", min_length=1, max_length=80)
    rules: list[PositionRule] = Field(default_factory=list, max_length=40)
    # Strict provider schemas require named object properties. An array keeps
    # arbitrary parameter names without an open additionalProperties dictionary.
    parameters: list[StrategyParameter] = Field(default_factory=list, max_length=20)
    # Descriptions / proposed code for a new adapter are inert research data.
    implementation_notes: str = Field(default="", max_length=12000)

    @field_validator("parameters", mode="before")
    @classmethod
    def _prototype_dictionary(cls, value):
        if isinstance(value, dict):
            return [{"name": k, "value": v, "integer": type(v) is int} for k, v in value.items()]
        return value


class ValidationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    method: str = Field(default="daily_forward_v1", min_length=1, max_length=80)
    benchmark: str = Field(min_length=1, max_length=24)
    benchmark_weight: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    holding_sessions: int = Field(ge=1, le=252)
    train_sessions: int = Field(default=180, ge=40, le=5000)
    validation_sessions: int = Field(default=60, ge=20, le=10000)
    final_sessions: int = Field(default=60, ge=20, le=10000)
    forward_sessions: int = Field(default=60, ge=20, le=10000)
    min_independent_observations: int = Field(default=20, ge=10, le=1000)
    max_drawdown: float = Field(default=0.10, gt=0, le=0.50, allow_inf_nan=False)
    min_net_excess: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    walk_forward_splits: int = Field(default=3, ge=2, le=8)
    min_robust_fraction: float = Field(default=2 / 3, ge=0.6, le=1, allow_inf_nan=False)
    monitoring_window_sessions: int = Field(default=60, ge=20, le=1000)
    max_relative_loss: float = Field(default=0.05, gt=0, le=0.50, allow_inf_nan=False)
    purpose: Literal["excess_return", "defensive"] = "excess_return"
    rationale: str = Field(min_length=1, max_length=2000)


def daily_sample_requirements(plan):
    """The daily verifier needs one complete holding-horizon block per observation."""
    evaluation = plan["holding_sessions"] * plan["min_independent_observations"]
    purged = evaluation + plan["holding_sessions"]
    return {"validation_sessions": purged, "final_sessions": purged,
            "forward_sessions": max(plan["forward_sessions"], evaluation)}


class CostAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fee_rate: float = Field(ge=0, le=0.01, allow_inf_nan=False)
    slippage: float = Field(ge=0, le=0.015, allow_inf_nan=False)
    daily_model_usd: float = Field(ge=0, le=1000, allow_inf_nan=False)
    daily_data_usd: float = Field(ge=0, le=1000, allow_inf_nan=False)
    basis: str = Field(min_length=1, max_length=2000)


class StrategyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    parent_version: str | None = Field(default=None, max_length=80)
    change_type: Literal["new", "revision", "retrain", "revalidation"] = "new"
    name: str = Field(min_length=1, max_length=160)
    hypothesis: str = Field(min_length=1, max_length=3000)
    mechanism: str = Field(min_length=1, max_length=2000)
    instruments: list[str] = Field(min_length=1, max_length=40)
    data_symbols: list[str] = Field(default_factory=list, max_length=40)
    market_conditions: str = Field(min_length=1, max_length=2000)
    entry_logic: str = Field(min_length=1, max_length=2000)
    exit_logic: str = Field(min_length=1, max_length=2000)
    sizing_logic: str = Field(min_length=1, max_length=2000)
    holding_period: str = Field(min_length=1, max_length=1000)
    failure_conditions: str = Field(min_length=1, max_length=2000)
    data_requirements: list[str] = Field(min_length=1, max_length=20)
    validation: ValidationPlan
    costs: CostAssumptions
    implementation: StrategyProgram | None = None
    # Extensible research details are inert JSON text, not trusted instructions.
    additional_research: str = Field(default="", max_length=12000)


class ResearchTaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    action: Literal["continue", "pause", "retire", "request_data"]
    findings: str = Field(min_length=1, max_length=2500)
    next_step: str = Field(min_length=1, max_length=2000)


class StrategyAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    digest: str = Field(min_length=64, max_length=64)
    capital_weight: float = Field(gt=0, le=1, allow_inf_nan=False)


@dataclass(frozen=True)
class StrategyLabSettings:
    enabled: bool = True
    initial_capital_usd: str = "10000"
    max_jobs_per_day: int = 2
    job_timeout_seconds: int = 120
    max_history_rows: int = 12000
    max_candidates: int = 100
    max_artifact_bytes: int = 64000000
    min_forward_sessions: int = 60
    # Admission grants a simulated strategy-library entry only. The current
    # manager's operator mandate and all execution guards retain their authority.
    admission: str = "research_only"
    allocation_mode: str = "legacy_mandate"

    def __post_init__(self):
        if type(self.enabled) is not bool or self.admission not in {"research_only", "paper_library"}:
            raise RiskError("Invalid strategy lab mode")
        if self.allocation_mode not in {"legacy_mandate", "validated_only"} or (
                self.allocation_mode == "validated_only" and (not self.enabled or self.admission != "paper_library")):
            raise RiskError("Validated-only allocations require an enabled paper strategy library")
        if not 0 < number(self.initial_capital_usd) <= number("10000000"):
            raise RiskError("Invalid strategy lab initial capital")
        for key, lower, upper in (("max_jobs_per_day", 1, 20), ("job_timeout_seconds", 1, 1200),
                                  ("max_history_rows", 100, 100000), ("max_candidates", 1, 1000),
                                  ("max_artifact_bytes", 32000000, 1000000000),
                                  ("min_forward_sessions", 20, 2000)):
            if type(getattr(self, key)) is not int or not lower <= getattr(self, key) <= upper:
                raise RiskError(f"Invalid strategy_lab.{key}")

    @classmethod
    def from_config(cls, raw):
        raw = dict(raw or {})
        if set(raw) - cls.__dataclass_fields__.keys():
            raise RiskError("Unknown strategy lab setting")
        return cls(**raw)


RESEARCH_GUIDANCE = (
    "strategy_lab is an open strategy research workspace, separate from Factor Lab. You may propose any "
    "economic mechanism, including a previously unimplemented one; never force it into a factor or a fixed "
    "strategy category. No daily proposal quota: empty strategy_proposals/research_tasks are valid. "
    "Review saved tasks and failures before proposing a new version. strategy_proposals records the full "
    "hypothesis, entry/exit/sizing, data needs, costs, falsification and predeclared validation. "
    "Validation method daily_forward_v1 supports the documented daily evidence protocol; propose any other "
    "method when its assumptions do not fit, with an explanation in validation.rationale. It remains a "
    "feasibility task until a suitable independent verifier exists. Never force all mechanisms into one test. "
    "For daily_forward_v1, allow holding_sessions for the purge plus holding_sessions times "
    "min_independent_observations in both validation and final windows. Base revisions on identified evidence gaps "
    "and declare a fresh evaluation before drawing a new conclusion. "
    "For executable daily_rules_v1 use rules with symbol, entry/exit Boolean expressions and weight expression. "
    "Expressions support field(symbol,column,lag), mean(symbol,column,window), ret(symbol,window), "
    "stdev(symbol,column,window), min/max/abs, arithmetic, comparisons, and/or/not, and the names "
    "holding_sessions, position_weight, entry_price, session_index. Historical observations are completed "
    'Declare tunable numeric parameters as implementation.parameters=[{"name":"p_window","value":20,"integer":true}], '
    "using p_ names and integer:true for history windows. Reference those names in "
    "expressions; the verifier perturbs them independently rather than selecting a winning parameter. "
    "data_symbols can name additional non-traded input series; dated events/macro/fundamentals belong in "
    "the dataset's features with their own publication times, never invented price bars. "
    "and only visible after their available_at; targets execute at the next session open. This is a research "
    "adapter. daily_cash_rules_v1 additionally models the configured cash-equivalent funding/parking policy "
    "with raw price and dividend/payment receipts, including its trading costs and idle-session rule. "
    "Neither adapter is a requirement on strategy design. If inadequate, leave implementation null or describe a "
    "new adapter in implementation_notes: it stays a feasibility task. You may still use ordinary SQL and "
    "research tools for novel analysis. Submitted code/text never executes in production. "
    "research_tasks can continue, pause, retire or request data; only independent validation can advance "
    "admission. Do not submit fake test results, reset failed variants under new names, or tune using sealed "
    "holdout outcomes. Historical results are exploratory and never prove alpha. Research budget limits "
    "apply to optional work; no research task can change account permissions, risk limits or existing orders."
)
