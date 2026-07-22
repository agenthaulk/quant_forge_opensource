"""Read-only catalogs for LLM and agent tooling.

CP5-1 (owner decision D2): the advertised field surface is backed by the
actually-loaded data catalog (``quant_forge.data.local.data_field_catalog``),
not a static copy, so it cannot drift from what the provider loads (FP-5).
Operators were already registry-backed via ``operator_catalog()``.

The public payload shapes of ``list_available_fields`` /
``list_available_operators`` / ``list_factors`` / ``list_artifacts`` are
consumed by web/CLI/LLM surfaces and stay unchanged. Research metadata tags
(CP5-2) are exposed through the separate ``list_*_research_tags`` functions.
"""

from __future__ import annotations

from pathlib import Path

from quant_forge.data.local import LocalPanelDataProvider, data_field_catalog
from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS, inspect_formula
from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.factor_library.research_tags import (
    research_tags_for_factor,
    research_tags_for_operator,
)
from quant_forge.operator_registry.loader import load_default_operator_registry


def _advertised_fields():
    return sorted(
        (item for item in data_field_catalog() if item.role != "key"),
        key=lambda item: item.name,
    )


def list_available_fields() -> list[dict[str, str]]:
    """Fields supported by the Quant Forge execution capability catalog."""

    return [{"name": item.name, "description": item.description} for item in _advertised_fields()]


def list_runtime_available_fields(data_root: Path) -> list[dict[str, str]]:
    """Capability-catalog fields actually backed by the configured data root."""

    available = set(LocalPanelDataProvider(data_root).available_field_names())
    return [field for field in list_available_fields() if field["name"] in available]


def list_available_operators() -> list[dict[str, object]]:
    return load_default_operator_registry().operator_catalog()


def list_field_research_tags() -> list[dict[str, object]]:
    """Research metadata tags for the advertised catalog fields (CP5-2)."""

    return [item.tags.to_dict() for item in _advertised_fields()]


def list_operator_research_tags() -> list[dict[str, object]]:
    """Research metadata tags derived from the canonical operator registry."""

    return [
        research_tags_for_operator(entry).to_dict()
        for entry in load_default_operator_registry().operator_catalog()
    ]


def list_factor_research_tags(
    factor_root: Path,
    factor_values_root: Path | None = None,
    factor_values_manifest_root: Path | None = None,
) -> list[dict[str, object]]:
    """Research metadata tags derived from the registered factor catalog."""

    catalog = FactorCatalog(
        factor_root,
        factor_values_root=factor_values_root,
        factor_values_manifest_root=factor_values_manifest_root,
    )
    return [
        research_tags_for_factor(factor, input_fields=_factor_input_fields(factor.formula)).to_dict()
        for factor in catalog.list()
    ]


def _factor_input_fields(formula: str) -> tuple[str, ...] | None:
    """Formula input fields via the canonical parser; None when unobservable.

    Precomputed factors carry their formulas outside this repository, so
    their inputs are unknown — ``None``, never a fabricated empty list
    (FP-4). Uses the same parser definition as the experiment planner (FP-5).
    """

    if is_precomputed_formula(formula):
        return None
    inspection = inspect_formula(formula, known_operators=set(SUPPORTED_OPERATORS))
    if not inspection.is_valid:
        return None
    return tuple(inspection.fields)


def list_factors(
    factor_root: Path,
    factor_values_root: Path | None = None,
    factor_values_manifest_root: Path | None = None,
) -> list[dict[str, object]]:
    return [
        {
            "factor_id": factor.factor_id,
            "name": factor.name,
            "formula": factor.formula,
            "status": factor.status,
            "horizon_days": factor.horizon_days,
            "universe_filters": list(factor.universe_filters),
        }
        for factor in FactorCatalog(
            factor_root,
            factor_values_root=factor_values_root,
            factor_values_manifest_root=factor_values_manifest_root,
        ).list()
    ]


def list_artifacts(artifact_root: Path) -> list[dict[str, str]]:
    root = artifact_root.expanduser()
    if not root.exists():
        return []
    artifacts: list[dict[str, str]] = []
    for path in sorted(root.glob("*/*.json")):
        artifacts.append({"kind": path.parent.name, "path": str(path)})
    return artifacts
