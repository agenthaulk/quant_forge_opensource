"""AgentTaskSpec: bounded, typed tasks for the agent plane.

LLM output is always a proposal in a typed schema; tasks carry explicit
budgets and an allowlist of tools drawn from a declared catalog.
``KNOWN_AGENT_TOOLS`` is that catalog — the exact tool names the Phase C
AgentToolPort must implement 1:1. ``allowed_tools`` must be a non-empty
subset of it, so a task naming any other surface (exec, shell, bash, or an
arbitrary string) cannot be constructed at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from quant_forge.specs._normalize import set_tuple
from quant_forge.specs._vocab import SAMPLE_ROLES

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

# The declared agent tool catalog. This is the complete surface the Phase C
# AgentToolPort must implement 1:1; allowed_tools is validated as a subset,
# so no execution surface outside this catalog is representable in a task.
KNOWN_AGENT_TOOLS: frozenset[str] = frozenset(
    {
        "read_catalog",
        "propose_factor",
        "validate_spec",
        "evaluate_factor",
        "run_backtest",
        "review_backtest",
        "run_falsification",
        "assemble_governance_packet",
        "request_promotion",
        "write_artifact_note",
    }
)


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
        set_tuple(self, "allowed_tools")
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
            if tool not in KNOWN_AGENT_TOOLS:
                raise ValueError(
                    f"unknown tool in allowed_tools: {tool!r} "
                    "(not in the declared agent tool catalog KNOWN_AGENT_TOOLS)"
                )
        if self.sample_role_filter not in SAMPLE_ROLES:
            raise ValueError(
                f"invalid sample_role_filter: {self.sample_role_filter!r} "
                f"(expected one of {sorted(SAMPLE_ROLES)})"
            )
