"""AgentTaskSpec: bounded, typed tasks for the agent plane.

LLM output is always a proposal in a typed schema; tasks carry explicit
budgets and an allowlist of tools. The LLM-boundary rule "no exec() of
generated code" is enforced structurally: a task whose tool allowlist names an
exec/shell surface cannot be constructed at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AGENT_TASK_SCHEMA_VERSION = "qf.agent_task.v1"

AgentTaskType = Literal[
    "propose_factor",
    "evaluate",
    "backtest",
    "review_backtest",
    "assemble_governance_packet",
]
_ALLOWED_TASK_TYPES: tuple[str, ...] = (
    "propose_factor",
    "evaluate",
    "backtest",
    "review_backtest",
    "assemble_governance_packet",
)
# No codegen execution surface may ever be handed to an agent (boundary rule 6).
_FORBIDDEN_TOOLS: frozenset[str] = frozenset({"exec", "shell"})


@dataclass(frozen=True)
class AgentTaskSpec:
    task_id: str
    task_type: AgentTaskType
    objective: str
    max_rounds: int
    allowed_tools: tuple[str, ...]
    sample_role_filter: str = "research_evaluation"
    spec_ref: str = ""
    schema_version: str = AGENT_TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != AGENT_TASK_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported agent task schema_version: {self.schema_version} "
                f"(expected {AGENT_TASK_SCHEMA_VERSION})"
            )
        _set_tuple(self, "allowed_tools")
        if not self.task_id.strip():
            raise ValueError("task_id is required")
        if self.task_type not in _ALLOWED_TASK_TYPES:
            raise ValueError(f"invalid agent task_type: {self.task_type}")
        if not self.objective.strip():
            raise ValueError("agent task objective is required")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if not self.allowed_tools:
            raise ValueError("allowed_tools must name at least one tool")
        for tool in self.allowed_tools:
            if not tool.strip():
                raise ValueError("allowed_tools entries must be non-empty")
            if tool.strip().lower() in _FORBIDDEN_TOOLS:
                raise ValueError(
                    f"forbidden tool in allowed_tools: {tool} (no codegen execution surface for agents)"
                )
        if not self.sample_role_filter.strip():
            raise ValueError("sample_role_filter is required")


def _set_tuple(instance: object, field_name: str) -> None:
    value = getattr(instance, field_name)
    if value is None:
        normalized: tuple[str, ...] = ()
    elif isinstance(value, tuple):
        normalized = value
    elif isinstance(value, list):
        normalized = tuple(value)
    else:
        normalized = (value,)
    object.__setattr__(instance, field_name, tuple(str(item) for item in normalized))
