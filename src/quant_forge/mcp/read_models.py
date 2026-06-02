"""Read-only catalogs for LLM and agent tooling."""

from __future__ import annotations

from pathlib import Path

from quant_forge.factor_engine.executor import SUPPORTED_OPERATORS
from quant_forge.factor_library.catalog import FactorCatalog

AVAILABLE_FIELDS = {
    "close": "Adjusted close or close-like local demo price.",
    "market_cap": "Point-in-time market capitalization supplied by local data.",
    "return_1d": "One-day trailing return derived from local close data.",
    "return_5d": "Five-day trailing return derived from local close data.",
    "volatility_5d": "Five-day trailing return volatility.",
    "volume": "Local demo trading volume.",
    "is_st": "Boolean risk flag; use as a universe filter, not a numeric factor field.",
}


def list_available_fields() -> list[dict[str, str]]:
    return [{"name": name, "description": description} for name, description in sorted(AVAILABLE_FIELDS.items())]


def list_available_operators() -> list[dict[str, str]]:
    descriptions = {
        "rank": "Cross-sectional percentile rank by trade date.",
        "zscore": "Cross-sectional z-score by trade date.",
    }
    return [{"name": name, "description": descriptions[name]} for name in sorted(SUPPORTED_OPERATORS)]


def list_factors(factor_root: Path, factor_values_root: Path | None = None) -> list[dict[str, object]]:
    return [
        {
            "factor_id": factor.factor_id,
            "name": factor.name,
            "formula": factor.formula,
            "status": factor.status,
            "horizon_days": factor.horizon_days,
            "universe_filters": list(factor.universe_filters),
        }
        for factor in FactorCatalog(factor_root, factor_values_root=factor_values_root).list()
    ]


def list_artifacts(artifact_root: Path) -> list[dict[str, str]]:
    root = artifact_root.expanduser()
    if not root.exists():
        return []
    artifacts: list[dict[str, str]] = []
    for path in sorted(root.glob("*/*.json")):
        artifacts.append({"kind": path.parent.name, "path": str(path)})
    return artifacts
