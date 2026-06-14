"""`factor_root` source-of-truth repository."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from quant_forge.core.contracts import FactorDefinition, FactorStatus
from quant_forge.factor_library.classification import FACTOR_CATEGORY_DIRS, factor_category
from quant_forge.utils import read_yaml, write_yaml

FACTOR_FILE = "factor.yaml"
STATUS_DIRS: dict[str, str] = {
    "active": "active_factors",
    "draft": "inactive_factors",
    "candidate": "inactive_factors",
    "inactive": "inactive_factors",
    "archived": "inactive_factors",
}


@dataclass(frozen=True)
class FactorRootNormalizationItem:
    factor_id: str
    source_path: Path
    target_path: Path
    action: str


@dataclass(frozen=True)
class FactorRootNormalizationResult:
    factor_root: Path
    dry_run: bool
    discovered_count: int
    created_count: int
    skipped_count: int
    conflicted_count: int
    items: tuple[FactorRootNormalizationItem, ...]


class FactorRepository:
    def __init__(self, factor_root: Path) -> None:
        self.factor_root = factor_root.expanduser()

    def ensure_layout(self) -> None:
        for directory in {"active_factors", "inactive_factors", "manifests"}:
            (self.factor_root / directory).mkdir(parents=True, exist_ok=True)
        for category_dir in FACTOR_CATEGORY_DIRS.values():
            for directory in {"active_factors", "inactive_factors"}:
                (self.factor_root / category_dir / directory).mkdir(parents=True, exist_ok=True)

    def list(self) -> list[FactorDefinition]:
        if not self.factor_root.exists():
            return []
        by_id: dict[str, list[Path]] = {}
        for path in _factor_files(self.factor_root):
            payload = read_yaml(path)
            factor_id = str(payload["factor_id"])
            by_id.setdefault(factor_id, []).append(path)
        definitions: list[FactorDefinition] = []
        for factor_id in sorted(by_id):
            definitions.append(_from_payload(read_yaml(_preferred_factor_file(by_id[factor_id]))))
        return definitions

    def get(self, factor_id: str) -> FactorDefinition:
        matches = _matching_factor_files(self.factor_root, factor_id)
        if not matches:
            raise FileNotFoundError(f"factor not found in factor_root: {factor_id}")
        return _from_payload(read_yaml(_preferred_factor_file(matches)))

    def save(self, factor: FactorDefinition) -> Path:
        self.ensure_layout()
        category_dir = FACTOR_CATEGORY_DIRS[factor_category(factor)]
        target_dir = self.factor_root / category_dir / STATUS_DIRS[factor.status] / factor.factor_id
        target = target_dir / FACTOR_FILE
        write_yaml(target, _to_payload(factor))
        self._remove_duplicate_files(factor.factor_id, keep=target)
        return target

    def delete(self, factor_id: str) -> int:
        """Delete all source definitions for one factor id."""

        deleted = 0
        for path in _matching_factor_files(self.factor_root, factor_id):
            path.unlink()
            deleted += 1
            _remove_empty_factor_dirs(path.parent, stop=self.factor_root)
        return deleted

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
        for path in _matching_factor_files(self.factor_root, factor_id):
            if path != keep:
                path.unlink()
                try:
                    path.parent.rmdir()
                except OSError:
                    pass


def normalize_factor_root_layout(factor_root: Path, *, dry_run: bool = False) -> FactorRootNormalizationResult:
    """Copy factor.yaml files into category/status directories without deleting legacy files."""

    root = factor_root.expanduser()
    repo = FactorRepository(root)
    if not dry_run:
        repo.ensure_layout()
    items: list[FactorRootNormalizationItem] = []
    created_count = 0
    skipped_count = 0
    conflicted_count = 0
    if not root.exists():
        return FactorRootNormalizationResult(root, dry_run, 0, 0, 0, 0, ())
    for source_path in _factor_files(root):
        factor = _from_payload(read_yaml(source_path))
        category_dir = FACTOR_CATEGORY_DIRS[factor_category(factor)]
        target_path = root / category_dir / STATUS_DIRS[factor.status] / factor.factor_id / FACTOR_FILE
        if source_path == target_path:
            action = "categorized"
            skipped_count += 1
        elif target_path.exists():
            if _same_file_content(source_path, target_path):
                action = "skipped"
                skipped_count += 1
            else:
                action = "conflict"
                conflicted_count += 1
        else:
            action = "would_create" if dry_run else "create"
            created_count += 1
            if not dry_run:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
        items.append(
            FactorRootNormalizationItem(
                factor_id=factor.factor_id,
                source_path=source_path,
                target_path=target_path,
                action=action,
            )
        )
    return FactorRootNormalizationResult(
        factor_root=root,
        dry_run=dry_run,
        discovered_count=len(items),
        created_count=created_count,
        skipped_count=skipped_count,
        conflicted_count=conflicted_count,
        items=tuple(items),
    )


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


def _factor_files(factor_root: Path) -> list[Path]:
    paths = list(factor_root.glob(f"*_factors/*/{FACTOR_FILE}"))
    for category_dir in FACTOR_CATEGORY_DIRS.values():
        paths.extend(factor_root.glob(f"{category_dir}/*_factors/*/{FACTOR_FILE}"))
    return sorted(set(paths))


def _matching_factor_files(factor_root: Path, factor_id: str) -> list[Path]:
    matches = list(factor_root.glob(f"*_factors/{factor_id}/{FACTOR_FILE}"))
    for category_dir in FACTOR_CATEGORY_DIRS.values():
        matches.extend(factor_root.glob(f"{category_dir}/*_factors/{factor_id}/{FACTOR_FILE}"))
    return sorted(set(matches))


def _remove_empty_factor_dirs(path: Path, *, stop: Path) -> None:
    stop = stop.expanduser().resolve(strict=False)
    current = path.expanduser().resolve(strict=False)
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _preferred_factor_file(matches: list[Path]) -> Path:
    categorized = [path for path in matches if len(path.parts) >= 4 and path.parts[-4] in FACTOR_CATEGORY_DIRS.values()]
    if len(categorized) == 1:
        return categorized[0]
    if categorized:
        payloads = {path.read_text(encoding="utf-8") for path in categorized}
        if len(payloads) == 1:
            return categorized[0]
        raise ValueError(f"factor appears more than once in categorized factor_root: {categorized[0].parent.name}")
    if len(matches) == 1:
        return matches[0]
    payloads = {path.read_text(encoding="utf-8") for path in matches}
    if len(payloads) == 1:
        return matches[0]
    raise ValueError(f"factor appears more than once in factor_root: {matches[0].parent.name}")


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False
