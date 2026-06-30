"""Load and validate the repo-shipped read-only operator registry."""

from __future__ import annotations

from importlib import resources
import re
from pathlib import Path
from typing import Any

import yaml

from quant_forge.operator_registry.errors import OperatorRegistryError
from quant_forge.operator_registry.models import AliasSpec, OperatorArgSpec, OperatorRegistry, OperatorSpec

SECRET_PATTERNS = (
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"bearer\s+[a-z0-9._-]{12,}", re.IGNORECASE),
    re.compile(r"sk-[a-z0-9_-]{12,}", re.IGNORECASE),
)
ALLOWED_ALIAS_MATCH_TYPES = {
    "canonical_equivalent",
    "likely_equivalent",
    "near_match_but_different",
    "unknown_requires_draft",
    "forbidden_alias",
}
ALLOWED_ALIAS_BEHAVIORS = {
    "rewrite_then_execute",
    "repair_then_validate",
    "block_and_review",
    "draft_only",
    "reject",
}


def load_default_operator_registry() -> OperatorRegistry:
    data_root = resources.files("quant_forge.operator_registry.data")
    return load_operator_registry(data_root / "core_operators.yaml", data_root / "aliases.yaml")


def load_operator_registry(operators_path: Path | str, aliases_path: Path | str | None = None) -> OperatorRegistry:
    operators_payload = _read_yaml_mapping(Path(str(operators_path)))
    aliases_payload = _read_yaml_mapping(Path(str(aliases_path))) if aliases_path is not None else {"aliases": []}
    _scan_for_forbidden_values(operators_payload, path="operators")
    _scan_for_forbidden_values(aliases_payload, path="aliases")
    schema_version = str(operators_payload.get("schema_version", "qf.operator_registry.v1"))
    operators = _load_operator_specs(operators_payload)
    aliases = _load_alias_specs(aliases_payload)
    _validate_registry(schema_version, operators, aliases)
    return OperatorRegistry(schema_version=schema_version, operators=operators, aliases=aliases)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise OperatorRegistryError(f"registry YAML must contain a mapping: {path}")
    return payload


def _load_operator_specs(payload: dict[str, Any]) -> dict[str, OperatorSpec]:
    raw_operators = payload.get("operators", [])
    if not isinstance(raw_operators, list):
        raise OperatorRegistryError("operators must be a list")
    operators: dict[str, OperatorSpec] = {}
    for raw in raw_operators:
        if not isinstance(raw, dict):
            raise OperatorRegistryError("operator entry must be a mapping")
        name = _required_text(raw, "name")
        if name in operators:
            raise OperatorRegistryError(f"duplicate operator name: {name}")
        args = tuple(_operator_arg_spec(item) for item in raw.get("args", []))
        operators[name] = OperatorSpec(
            name=name,
            signature=_required_text(raw, "signature"),
            description=_required_text(raw, "description"),
            category=_required_text(raw, "category"),
            family=_required_text(raw, "family"),
            args=args,
            return_type=str(raw.get("return_type", "series")),
            examples=tuple(str(item) for item in raw.get("examples", []) or ()),
            lookback_rule=dict(raw.get("lookback_rule", {}) or {}),
            execution_status=str(raw.get("execution_status", "implemented")),
            audit_status=str(raw.get("audit_status", "core_reviewed")),
            numeric_behavior=dict(raw.get("numeric_behavior", {}) or {}),
            pit_safety=dict(raw.get("pit_safety", {}) or {}),
            llm=dict(raw.get("llm", {}) or {}),
        )
    return operators


def _operator_arg_spec(raw: Any) -> OperatorArgSpec:
    if not isinstance(raw, dict):
        raise OperatorRegistryError("operator arg must be a mapping")
    return OperatorArgSpec(
        name=_required_text(raw, "name"),
        type=_required_text(raw, "type"),
        role=str(raw.get("role", "")),
        required=bool(raw.get("required", True)),
        default=raw.get("default"),
        constraints=dict(raw.get("constraints", {}) or {}),
    )


def _load_alias_specs(payload: dict[str, Any]) -> dict[str, AliasSpec]:
    raw_aliases = payload.get("aliases", [])
    if not isinstance(raw_aliases, list):
        raise OperatorRegistryError("aliases must be a list")
    aliases: dict[str, AliasSpec] = {}
    for raw in raw_aliases:
        if not isinstance(raw, dict):
            raise OperatorRegistryError("alias entry must be a mapping")
        alias = _required_text(raw, "alias")
        if alias in aliases:
            raise OperatorRegistryError(f"duplicate alias: {alias}")
        match_type = _required_text(raw, "match_type")
        behavior = _required_text(raw, "behavior")
        if match_type not in ALLOWED_ALIAS_MATCH_TYPES:
            raise OperatorRegistryError(f"unsupported alias match_type: {match_type}")
        if behavior not in ALLOWED_ALIAS_BEHAVIORS:
            raise OperatorRegistryError(f"unsupported alias behavior: {behavior}")
        canonical = raw.get("canonical")
        aliases[alias] = AliasSpec(
            alias=alias,
            canonical=str(canonical) if canonical is not None else None,
            match_type=match_type,  # type: ignore[arg-type]
            confidence=str(raw.get("confidence", "low")),  # type: ignore[arg-type]
            behavior=behavior,  # type: ignore[arg-type]
            notes=str(raw.get("notes", "")),
        )
    return aliases


def _validate_registry(schema_version: str, operators: dict[str, OperatorSpec], aliases: dict[str, AliasSpec]) -> None:
    from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS

    if schema_version != "qf.operator_registry.v1":
        raise OperatorRegistryError(f"unsupported operator registry schema_version: {schema_version}")
    missing_from_executor = sorted(
        name
        for name, spec in operators.items()
        if spec.execution_status == "implemented" and spec.audit_status == "core_reviewed" and name not in SUPPORTED_OPERATORS
    )
    if missing_from_executor:
        raise OperatorRegistryError(f"implemented registry operators are not supported by executor: {missing_from_executor}")
    missing_from_registry = sorted(SUPPORTED_OPERATORS.difference(operators))
    if missing_from_registry:
        raise OperatorRegistryError(f"supported operators missing from registry: {missing_from_registry}")
    for name, spec in operators.items():
        if spec.execution_status == "implemented" and spec.audit_status == "core_reviewed":
            if not spec.signature or not spec.args or not spec.examples:
                raise OperatorRegistryError(f"implemented operator lacks required metadata: {name}")
    for alias in aliases.values():
        if alias.canonical is not None and alias.canonical not in operators:
            raise OperatorRegistryError(f"alias canonical target is missing: {alias.alias} -> {alias.canonical}")
        if alias.match_type == "unknown_requires_draft" and alias.canonical is not None:
            raise OperatorRegistryError(f"unknown_requires_draft alias must not set canonical: {alias.alias}")


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise OperatorRegistryError(f"operator registry field is required: {key}")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise OperatorRegistryError(f"operator registry field must be portable metadata, not a path: {key}")
    return value


def _scan_for_forbidden_values(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_for_forbidden_values(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_for_forbidden_values(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    text = value.strip()
    if ".." in text or text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", text):
        raise OperatorRegistryError(f"operator registry must not contain local paths: {path}")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise OperatorRegistryError(f"operator registry must not contain secret-like text: {path}")
    if "def " in text or "lambda " in text or "import " in text:
        raise OperatorRegistryError(f"operator registry must not contain executable code: {path}")
