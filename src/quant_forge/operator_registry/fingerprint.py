"""Canonical formula fingerprints shared by RD and cache-aware services."""

from __future__ import annotations

import hashlib
import json
import re

from quant_forge.operator_registry.loader import load_default_operator_registry
from quant_forge.operator_registry.models import OperatorRegistry
from quant_forge.operator_registry.resolver import resolve_formula_operators


def canonical_formula_fingerprint(
    formula: str,
    horizon: int,
    universe_filters: tuple[str, ...],
    registry: OperatorRegistry | None = None,
) -> str:
    registry = registry or load_default_operator_registry()
    resolution = resolve_formula_operators(formula, registry)
    canonical_formula = resolution.canonical_formula if resolution.executable else formula
    payload = {
        "schema_version": "qf.formula_fingerprint.v2",
        "formula": _compact_formula(canonical_formula),
        "horizon": int(horizon),
        "universe_filters": sorted(_compact_formula(item) for item in universe_filters),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compact_formula(value: str) -> str:
    return re.sub(r"\s+", "", value.strip()).lower()
