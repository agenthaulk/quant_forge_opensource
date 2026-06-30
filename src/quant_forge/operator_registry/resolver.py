"""Resolve raw formula operators into canonical executable operators."""

from __future__ import annotations

import ast
import re

from quant_forge.factor_engine.formula_parser import inspect_formula, parse_formula_node
from quant_forge.operator_registry.loader import load_default_operator_registry
from quant_forge.operator_registry.models import OperatorRegistry, OperatorResolutionItem, OperatorResolutionResult


def resolve_operator_name(name: str, registry: OperatorRegistry | None = None) -> OperatorResolutionItem:
    registry = registry or load_default_operator_registry()
    if name in registry.operators:
        return OperatorResolutionItem(
            original_name=name,
            canonical_name=name,
            status="exact_supported",
            semantic_match="canonical",
            confidence="high",
            signature_valid=True,
        )
    alias = registry.aliases.get(name)
    if alias is None:
        return OperatorResolutionItem(
            original_name=name,
            canonical_name=None,
            status="unknown_operator",
            semantic_match="unknown_requires_draft",
            confidence="low",
            signature_valid=False,
            blocking_reason=f"unknown operator requires draft review: {name}",
        )
    if alias.match_type == "canonical_equivalent" and alias.behavior == "rewrite_then_execute":
        canonical = alias.canonical
        spec = registry.operators.get(canonical or "")
        executable = spec is not None and spec.execution_status == "implemented" and spec.audit_status == "core_reviewed"
        return OperatorResolutionItem(
            original_name=name,
            canonical_name=canonical,
            status="canonical_alias" if executable else "unsupported_alias_target",
            semantic_match=alias.match_type,
            confidence=alias.confidence,
            signature_valid=executable,
            rewrite_applied=executable,
            blocking_reason="" if executable else f"alias target is not executable: {canonical}",
            notes=(alias.notes,) if alias.notes else (),
        )
    if alias.match_type == "unknown_requires_draft":
        return OperatorResolutionItem(
            original_name=name,
            canonical_name=None,
            status="unknown_requires_draft",
            semantic_match=alias.match_type,
            confidence=alias.confidence,
            signature_valid=False,
            blocking_reason=f"unknown operator requires draft review: {name}",
            notes=(alias.notes,) if alias.notes else (),
        )
    if alias.match_type == "forbidden_alias":
        return OperatorResolutionItem(
            original_name=name,
            canonical_name=alias.canonical,
            status="forbidden_alias",
            semantic_match=alias.match_type,
            confidence=alias.confidence,
            signature_valid=False,
            blocking_reason=f"forbidden operator alias: {name}",
            notes=(alias.notes,) if alias.notes else (),
        )
    return OperatorResolutionItem(
        original_name=name,
        canonical_name=alias.canonical,
        status=alias.match_type,
        semantic_match=alias.match_type,
        confidence=alias.confidence,
        signature_valid=False,
        blocking_reason=f"{name} is {alias.match_type}; repair with canonical operator before execution",
        notes=(alias.notes,) if alias.notes else (),
    )


def resolve_formula_operators(formula: str, registry: OperatorRegistry | None = None) -> OperatorResolutionResult:
    registry = registry or load_default_operator_registry()
    raw_formula = formula.strip()
    try:
        node = parse_formula_node(raw_formula)
    except Exception as exc:
        return OperatorResolutionResult(
            raw_formula=raw_formula,
            canonical_formula=raw_formula,
            executable=False,
            requires_operator_draft_review=False,
            blocking_errors=(str(exc),),
        )

    operator_names = _operator_names(node)
    items = tuple(resolve_operator_name(name, registry) for name in operator_names)
    replacements = {
        item.original_name: item.canonical_name
        for item in items
        if item.status == "canonical_alias" and item.canonical_name is not None
    }
    canonical_formula = _canonicalize_node(node, replacements)
    signature = inspect_formula(canonical_formula, known_operators=set(registry.canonical_names))
    blocking = [item.blocking_reason for item in items if item.blocking_reason]
    if not signature.is_valid:
        blocking.extend(signature.errors)
    requires_draft = any(item.status in {"unknown_operator", "unknown_requires_draft"} for item in items)
    executable = not blocking and signature.is_valid
    return OperatorResolutionResult(
        raw_formula=raw_formula,
        canonical_formula=canonical_formula,
        executable=executable,
        requires_operator_draft_review=requires_draft,
        items=items,
        used_fields=signature.fields,
        blocking_errors=tuple(dict.fromkeys(blocking)),
        warnings=tuple(
            dict.fromkeys(
                item.blocking_reason
                for item in items
                if item.status in {"likely_equivalent", "near_match_but_different"} and item.blocking_reason
            )
        ),
    )


def resolve_executable_formula(formula: str, registry: OperatorRegistry | None = None) -> str:
    if formula.strip().lower().startswith("precomputed:"):
        return formula
    resolution = resolve_formula_operators(formula, registry)
    if resolution.executable:
        return resolution.canonical_formula
    reason = "; ".join(resolution.blocking_errors) or "formula is not executable"
    raise ValueError(f"factor formula failed operator registry gate: {reason}")


def _operator_names(node: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            names.append(child.func.id)
    return tuple(dict.fromkeys(names))


def _canonicalize_node(node: ast.AST, replacements: dict[str, str]) -> str:
    copied = ast.fix_missing_locations(_OperatorNameRewriter(replacements).visit(node))
    return _stable_formula_text(ast.unparse(copied))


def _stable_formula_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    text = re.sub(r"\s*,\s*", ", ", text)
    return text


class _OperatorNameRewriter(ast.NodeTransformer):
    def __init__(self, replacements: dict[str, str]) -> None:
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in self.replacements:
            node.func = ast.copy_location(ast.Name(id=self.replacements[node.func.id], ctx=ast.Load()), node.func)
        return node
