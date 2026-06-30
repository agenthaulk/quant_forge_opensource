"""Typed models for the read-only operator registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AliasMatchType = Literal[
    "canonical_equivalent",
    "likely_equivalent",
    "near_match_but_different",
    "unknown_requires_draft",
    "forbidden_alias",
]
AliasBehavior = Literal[
    "rewrite_then_execute",
    "repair_then_validate",
    "block_and_review",
    "draft_only",
    "reject",
]


@dataclass(frozen=True)
class OperatorArgSpec:
    name: str
    type: str
    role: str = ""
    required: bool = True
    default: object | None = None
    constraints: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "type": self.type,
            "role": self.role,
            "required": self.required,
            "constraints": dict(self.constraints),
        }
        if self.default is not None:
            payload["default"] = self.default
        return payload


@dataclass(frozen=True)
class OperatorSpec:
    name: str
    signature: str
    description: str
    category: str
    family: str
    args: tuple[OperatorArgSpec, ...]
    return_type: str = "series"
    examples: tuple[str, ...] = ()
    lookback_rule: dict[str, object] = field(default_factory=dict)
    execution_status: str = "implemented"
    audit_status: str = "core_reviewed"
    numeric_behavior: dict[str, object] = field(default_factory=dict)
    pit_safety: dict[str, object] = field(default_factory=dict)
    llm: dict[str, object] = field(default_factory=dict)

    def to_dict(self, *, aliases_for_recognition_only: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "signature": self.signature,
            "category": self.category,
            "family": self.family,
            "args": [arg.to_dict() for arg in self.args],
            "return_type": self.return_type,
            "examples": list(self.examples),
            "lookback_rule": dict(self.lookback_rule),
            "execution_status": self.execution_status,
            "audit_status": self.audit_status,
            "numeric_behavior": dict(self.numeric_behavior),
            "pit_safety": dict(self.pit_safety),
            "llm": dict(self.llm),
            "aliases_for_recognition_only": list(aliases_for_recognition_only),
        }


@dataclass(frozen=True)
class AliasSpec:
    alias: str
    canonical: str | None
    match_type: AliasMatchType
    confidence: Literal["high", "medium", "low"]
    behavior: AliasBehavior
    notes: str = ""


@dataclass(frozen=True)
class OperatorRegistry:
    schema_version: str
    operators: dict[str, OperatorSpec]
    aliases: dict[str, AliasSpec]

    @property
    def canonical_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.operators))

    def aliases_for(self, canonical_name: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                alias.alias
                for alias in self.aliases.values()
                if alias.canonical == canonical_name and alias.match_type == "canonical_equivalent"
            )
        )

    def operator_catalog(self) -> list[dict[str, object]]:
        return [
            spec.to_dict(aliases_for_recognition_only=self.aliases_for(spec.name))
            for spec in (self.operators[name] for name in sorted(self.operators))
        ]


@dataclass(frozen=True)
class OperatorResolutionItem:
    original_name: str
    canonical_name: str | None
    status: str
    semantic_match: str
    confidence: str
    signature_valid: bool
    rewrite_applied: bool = False
    blocking_reason: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_name": self.original_name,
            "canonical_name": self.canonical_name,
            "status": self.status,
            "semantic_match": self.semantic_match,
            "confidence": self.confidence,
            "signature_valid": self.signature_valid,
            "rewrite_applied": self.rewrite_applied,
            "blocking_reason": self.blocking_reason,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class OperatorResolutionResult:
    raw_formula: str
    canonical_formula: str
    executable: bool
    requires_operator_draft_review: bool
    items: tuple[OperatorResolutionItem, ...] = ()
    used_fields: tuple[str, ...] = ()
    blocking_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_formula": self.raw_formula,
            "canonical_formula": self.canonical_formula,
            "executable": self.executable,
            "requires_operator_draft_review": self.requires_operator_draft_review,
            "items": [item.to_dict() for item in self.items],
            "used_fields": list(self.used_fields),
            "blocking_errors": list(self.blocking_errors),
            "warnings": list(self.warnings),
        }
