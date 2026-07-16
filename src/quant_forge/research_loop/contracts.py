"""Typed contracts for structured local RD runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal

ExpectedDirection = Literal["positive", "negative", "unknown"]
HypothesisSource = Literal[
    "operator_mcp",
    "financial_analyst",
    "effective_idea",
    "parameter_search",
    "local",
    "llm",
]
PlanStatus = Literal[
    "ready",
    "blocked_duplicate_formula",
    "blocked_candidate_diversity",
    "blocked_missing_formula",
    "blocked_missing_field",
    "blocked_ambiguous_field",
    "blocked_missing_operator",
    "requires_operator_draft_review",
    "blocked_pit_event_feature_required",
    "blocked_direction_unknown",
    "blocked_formula_invalid",
]
GateStatus = Literal["passed", "blocked"]
ResearchRunStatus = Literal[
    "queued",
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "no_optimization_performed",
]


@dataclass(frozen=True)
class ResearchContext:
    market: str = "cn_a"
    data_root: str = ""
    factor_root: str = ""
    objective: str = "balanced"
    run_date_range: str = ""
    available_fields: tuple[str, ...] = ()
    available_operators: tuple[str, ...] = ()
    field_catalog: tuple[dict[str, Any], ...] = ()
    operator_catalog: tuple[dict[str, Any], ...] = ()
    available_filters: tuple[str, ...] = ()
    seed_factor_summary: tuple[dict[str, Any], ...] = ()
    effective_ideas: tuple[dict[str, Any], ...] = ()
    recent_successes: tuple[dict[str, Any], ...] = ()
    recent_failures: tuple[dict[str, Any], ...] = ()
    unresolved_items: tuple[str, ...] = ()
    next_focus_hints: tuple[str, ...] = ()
    prompt_context: str = ""
    # SE-iv: bounded, human-activated steering rules (cap 5, exact-scope
    # before global, activation-recency desc; see context_builder._active_rules
    # and llm.py's dedicated closed-template re-authentication). Additive,
    # default-on, empty-by-default so zero activated rules is zero effect.
    active_rules: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _set_tuple(self, "available_fields")
        _set_tuple(self, "available_operators")
        _set_tuple(self, "field_catalog", mapper=dict)
        _set_tuple(self, "operator_catalog", mapper=dict)
        _set_tuple(self, "available_filters")
        _set_tuple(self, "seed_factor_summary", mapper=dict)
        _set_tuple(self, "effective_ideas", mapper=dict)
        _set_tuple(self, "recent_successes", mapper=dict)
        _set_tuple(self, "recent_failures", mapper=dict)
        _set_tuple(self, "unresolved_items")
        _set_tuple(self, "next_focus_hints")
        _set_tuple(self, "active_rules", mapper=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class StructuredResearchHypothesis:
    hypothesis_id: str
    text: str
    rationale: str = ""
    formula_dsl: str = ""
    input_fields: tuple[str, ...] = ()
    expected_direction: ExpectedDirection = "unknown"
    universe_constraints: tuple[str, ...] = ()
    source: HypothesisSource = "local"
    source_detail: str = ""
    priority: int = 0
    parameter_search_fallback: bool = False
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.hypothesis_id.strip():
            raise ValueError("hypothesis_id is required")
        if not self.text.strip():
            raise ValueError("hypothesis text is required")
        _set_tuple(self, "input_fields")
        _set_tuple(self, "universe_constraints")
        _set_tuple(self, "risks")
        _set_tuple(self, "unknowns")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class FactorExperimentPlan:
    plan_id: str
    hypothesis_id: str
    status: PlanStatus
    factor_name: str
    formula_dsl: str
    raw_formula_dsl: str = ""
    canonical_formula_dsl: str = ""
    inputs: tuple[str, ...] = ()
    universe_filters: tuple[str, ...] = ()
    expected_direction: ExpectedDirection = "unknown"
    field_resolution: dict[str, Any] = field(default_factory=dict)
    operator_validation: dict[str, Any] = field(default_factory=dict)
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("plan_id is required")
        _set_tuple(self, "inputs")
        _set_tuple(self, "universe_filters")
        _set_tuple(self, "blocking_reasons")
        _set_tuple(self, "warnings")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ObjectiveScore:
    score: float
    objective: str = "balanced"
    components: dict[str, float | None] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    accepted: bool
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    should_transition_to_candidate: bool = False
    should_promote_active: bool = False
    transition_reason: str = ""

    def __post_init__(self) -> None:
        _set_tuple(self, "blocking_reasons")
        _set_tuple(self, "warnings")
        if self.should_promote_active:
            raise ValueError("RD gates must never promote factors to active automatically")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class FactorExperimentResult:
    plan: FactorExperimentPlan
    candidate_ref: str = ""
    evaluation_status: str = "pending"
    evaluation_metrics: dict[str, Any] = field(default_factory=dict)
    backtest_status: str = "pending"
    backtest_metrics: dict[str, Any] = field(default_factory=dict)
    correlation_summary: dict[str, Any] = field(default_factory=dict)
    objective_score: ObjectiveScore | None = None
    gate_decision: GateDecision | None = None
    artifact_refs: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    error: str = ""

    def __post_init__(self) -> None:
        _set_tuple(self, "notes")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ResearchFeedback:
    status: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    next_hypothesis_hint: str = ""
    unresolved_items: tuple[str, ...] = ()
    requires_user_decision: bool = False

    def __post_init__(self) -> None:
        _set_tuple(self, "unresolved_items")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ResearchTraceEntry:
    run_id: str
    lane_id: str
    phase: str
    timestamp: str
    hypothesis: dict[str, Any] | None = None
    experiment_plan: dict[str, Any] | None = None
    candidate_ref: str = ""
    formula_dsl: str = ""
    inputs: tuple[str, ...] = ()
    universe_filters: tuple[str, ...] = ()
    field_resolution: dict[str, Any] = field(default_factory=dict)
    operator_validation: dict[str, Any] = field(default_factory=dict)
    evaluation_summary: dict[str, Any] = field(default_factory=dict)
    backtest_summary: dict[str, Any] = field(default_factory=dict)
    correlation_summary: dict[str, Any] = field(default_factory=dict)
    objective_score: dict[str, Any] = field(default_factory=dict)
    gate_decision: dict[str, Any] = field(default_factory=dict)
    feedback: dict[str, Any] = field(default_factory=dict)
    next_hypothesis_hint: str = ""
    unresolved_items: tuple[str, ...] = ()
    artifact_refs: dict[str, str] = field(default_factory=dict)
    error: str = ""
    schema_version: str = "qf.research_loop.trace.v1"

    def __post_init__(self) -> None:
        _set_tuple(self, "inputs")
        _set_tuple(self, "universe_filters")
        _set_tuple(self, "unresolved_items")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ResearchRun:
    run_id: str
    status: ResearchRunStatus
    results: tuple[FactorExperimentResult, ...] = ()
    trace_root: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _set_tuple(self, "results")
        _set_tuple(self, "notes")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _set_tuple(instance: object, field_name: str, mapper: Any = str) -> None:
    value = getattr(instance, field_name)
    if value is None:
        normalized = ()
    elif isinstance(value, tuple):
        normalized = value
    elif isinstance(value, list):
        normalized = tuple(value)
    else:
        normalized = (value,)
    if mapper is not None:
        normalized = tuple(mapper(item) for item in normalized)
    object.__setattr__(instance, field_name, normalized)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
