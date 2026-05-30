"""`factor_root` source-of-truth repository."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from quant_forge.core.contracts import FactorDefinition, FactorStatus
from quant_forge.utils import read_yaml, write_yaml

FACTOR_FILE = "factor.yaml"
STATUS_DIRS: dict[str, str] = {
    "active": "active_factors",
    "draft": "inactive_factors",
    "candidate": "inactive_factors",
    "inactive": "inactive_factors",
    "archived": "inactive_factors",
}


class FactorRepository:
    def __init__(self, factor_root: Path) -> None:
        self.factor_root = factor_root.expanduser()

    def ensure_layout(self) -> None:
        for directory in {"active_factors", "inactive_factors", "manifests"}:
            (self.factor_root / directory).mkdir(parents=True, exist_ok=True)

    def list(self) -> list[FactorDefinition]:
        if not self.factor_root.exists():
            return []
        definitions: list[FactorDefinition] = []
        for path in sorted(self.factor_root.glob("*_factors/*/factor.yaml")):
            definitions.append(_from_payload(read_yaml(path)))
        return definitions

    def get(self, factor_id: str) -> FactorDefinition:
        matches = list(self.factor_root.glob(f"*_factors/{factor_id}/{FACTOR_FILE}"))
        if not matches:
            raise FileNotFoundError(f"factor not found in factor_root: {factor_id}")
        if len(matches) > 1:
            raise ValueError(f"factor appears more than once in factor_root: {factor_id}")
        return _from_payload(read_yaml(matches[0]))

    def save(self, factor: FactorDefinition) -> Path:
        self.ensure_layout()
        target_dir = self.factor_root / STATUS_DIRS[factor.status] / factor.factor_id
        target = target_dir / FACTOR_FILE
        write_yaml(target, _to_payload(factor))
        self._remove_duplicate_files(factor.factor_id, keep=target)
        return target

    def promote(self, factor_id: str, to_status: FactorStatus, reason: str) -> FactorDefinition:
        if not reason.strip():
            raise ValueError("promotion reason is required")
        current = self.get(factor_id)
        promoted = FactorDefinition(
            factor_id=current.factor_id,
            name=current.name,
            formula=current.formula,
            status=to_status,
            description=current.description,
            horizon_days=current.horizon_days,
            universe_filters=current.universe_filters,
            source=current.source,
        )
        self.save(promoted)
        return promoted

    def _remove_duplicate_files(self, factor_id: str, keep: Path) -> None:
        for path in self.factor_root.glob(f"*_factors/{factor_id}/{FACTOR_FILE}"):
            if path != keep:
                path.unlink()
                try:
                    path.parent.rmdir()
                except OSError:
                    pass


def parse_idea_to_definition(text: str) -> FactorDefinition:
    """Deterministically parse a public smoke-path factor idea."""

    normalized = text.strip()
    if not normalized:
        raise ValueError("factor idea text is required")
    lowered = normalized.lower()
    contains_non_st = "非st" in lowered or "non-st" in lowered or "non st" in lowered
    filters = ("is_st == false",) if contains_non_st else ()

    if _contains_any(lowered, ["小市值", "小盘", "small cap", "market cap", "市值小"]):
        name = "small_cap_non_st" if contains_non_st else "small_cap"
        formula = "-rank(market_cap)"
        description = "Small market-cap stocks receive higher scores."
    elif _contains_any(lowered, ["动量", "momentum"]):
        name = "momentum_5d"
        formula = "rank(return_5d)"
        description = "Recent five-day momentum receives higher scores."
    elif _contains_any(lowered, ["低波", "波动", "volatility"]):
        name = "low_volatility"
        formula = "-rank(volatility_5d)"
        description = "Lower short-term volatility receives higher scores."
    elif _contains_any(lowered, ["成交量", "交易活跃", "放量", "volume", "liquidity"]):
        name = "volume_strength"
        formula = "rank(volume)"
        description = "Higher trading volume receives higher scores."
    else:
        name = "close_strength"
        formula = "rank(close)"
        description = "Close-price strength receives higher scores."

    digest = hashlib.sha1(f"{name}:{formula}:{filters}:{normalized}".encode("utf-8")).hexdigest()[:8].upper()
    return FactorDefinition(
        factor_id=f"FTR_{digest}",
        name=_slug(name),
        formula=formula,
        status="draft",
        description=description,
        horizon_days=5,
        universe_filters=filters,
        source="idea",
    )


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return value.strip("_") or "factor"


def _to_payload(factor: FactorDefinition) -> dict[str, object]:
    return {
        "factor_id": factor.factor_id,
        "name": factor.name,
        "formula": factor.formula,
        "status": factor.status,
        "description": factor.description,
        "horizon_days": factor.horizon_days,
        "universe_filters": list(factor.universe_filters),
        "source": factor.source,
    }


def _from_payload(payload: dict[str, object]) -> FactorDefinition:
    filters = payload.get("universe_filters", ())
    if filters is None:
        filters = ()
    if not isinstance(filters, list):
        raise ValueError("universe_filters must be a list")
    return FactorDefinition(
        factor_id=str(payload["factor_id"]),
        name=str(payload["name"]),
        formula=str(payload["formula"]),
        status=str(payload.get("status", "draft")),  # type: ignore[arg-type]
        description=str(payload.get("description", "")),
        horizon_days=int(payload.get("horizon_days", 5)),
        universe_filters=tuple(str(item) for item in filters),
        source=str(payload.get("source", "user")),
    )
