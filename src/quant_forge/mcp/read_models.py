"""Read-only catalogs for LLM and agent tooling."""

from __future__ import annotations

from pathlib import Path

from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS
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
        "abs": "Elementwise absolute value.",
        "correlation": "Rolling time-series correlation by instrument: correlation(x, y, window).",
        "covariance": "Rolling time-series covariance by instrument: covariance(x, y, window).",
        "decay_linear": "Rolling linearly weighted average by instrument: decay_linear(x, window).",
        "delay": "Historical value by instrument: delay(x, window).",
        "delta": "Current value minus delayed value by instrument: delta(x, window).",
        "log": "Elementwise natural log for positive values.",
        "rank": "Cross-sectional percentile rank by trade date.",
        "scale": "Cross-sectional scaling so sum(abs(score)) equals the target value.",
        "sign": "Elementwise sign.",
        "signedpower": "Signed power transform: sign(x) * abs(x) ** power.",
        "stddev": "Rolling time-series standard deviation by instrument.",
        "ts_max": "Rolling time-series maximum by instrument.",
        "ts_mean": "Rolling time-series mean by instrument.",
        "ts_min": "Rolling time-series minimum by instrument.",
        "ts_rank": "Rolling time-series percentile rank of the latest value by instrument.",
        "ts_sum": "Rolling time-series sum by instrument.",
        "wq_max": "WorldQuant-style max; scalar second arg maps to ts_max, otherwise pairwise max.",
        "wq_min": "WorldQuant-style min; scalar second arg maps to ts_min, otherwise pairwise min.",
        "zscore": "Cross-sectional z-score by trade date.",
    }
    return [{"name": name, "description": descriptions[name]} for name in sorted(SUPPORTED_OPERATORS)]


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
