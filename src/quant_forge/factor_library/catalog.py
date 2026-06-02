"""Unified read catalog for registered and precomputed factors."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any

from quant_forge.core.contracts import FactorDefinition
from quant_forge.core.contracts import FactorStatus
from quant_forge.factor_library.repository import FactorRepository

PRECOMPUTED_FORMULA_PREFIX = "precomputed:"


class FactorCatalog:
    """Read factors from local registrations and mounted precomputed stores."""

    def __init__(
        self,
        factor_root: Path,
        *,
        factor_values_root: Path | None = None,
        factor_values_manifest_root: Path | None = None,
    ) -> None:
        self.factor_root = factor_root.expanduser()
        self.factor_values_root = factor_values_root.expanduser() if factor_values_root is not None else None
        self.factor_values_manifest_root = (
            factor_values_manifest_root.expanduser() if factor_values_manifest_root is not None else None
        )

    def list(self) -> list[FactorDefinition]:
        local = FactorRepository(self.factor_root).list()
        seen = {_factor_key(factor.factor_id) for factor in local}
        discovered: list[FactorDefinition] = []
        for factor in discover_precomputed_factors(
            self.factor_values_root,
            manifest_root=self.factor_values_manifest_root,
        ):
            key = _factor_key(factor.factor_id)
            if key in seen:
                continue
            seen.add(key)
            discovered.append(factor)
        return [*local, *discovered]

    def get(self, factor_id: str) -> FactorDefinition:
        try:
            return FactorRepository(self.factor_root).get(factor_id)
        except FileNotFoundError:
            pass
        requested = _factor_id_values(factor_id)
        for factor in discover_precomputed_factors(
            self.factor_values_root,
            manifest_root=self.factor_values_manifest_root,
        ):
            values = _factor_id_values(factor.factor_id)
            values.update(_factor_id_values(factor.name))
            if requested.intersection(values):
                return factor
        raise FileNotFoundError(f"factor not found in factor_root or factor_values_root: {factor_id}")


def discover_precomputed_factors(
    factor_values_root: Path | None,
    *,
    manifest_root: Path | None = None,
) -> list[FactorDefinition]:
    root = resolve_factor_values_root(factor_values_root)
    if root is None or not root.exists():
        return []
    manifest_root = manifest_root.expanduser() if manifest_root is not None else None
    factors: list[FactorDefinition] = []
    for directory in _factor_value_dirs(root):
        metadata = _read_factor_metadata(directory, manifest_root=manifest_root)
        factor = _factor_from_store(directory, metadata)
        if factor is not None:
            factors.append(factor)
    return _dedupe_factors(factors)


def import_precomputed_factors(
    factor_root: Path,
    *,
    factor_values_root: Path | None,
    manifest_root: Path | None = None,
    factor_ids: tuple[str, ...] = (),
    import_all: bool = False,
    status: FactorStatus = "candidate",
) -> list[FactorDefinition]:
    if not import_all and not factor_ids:
        raise ValueError("provide factor ids or pass --all")
    discovered = discover_precomputed_factors(factor_values_root, manifest_root=manifest_root)
    selected = _select_precomputed_factors(discovered, factor_ids=factor_ids, import_all=import_all)
    repo = FactorRepository(factor_root)
    imported: list[FactorDefinition] = []
    for factor in selected:
        registered = replace(factor, status=status, source="precomputed")
        repo.save(registered)
        imported.append(registered)
    return imported


def resolve_factor_values_root(root: Path | None) -> Path | None:
    if root is None:
        return None
    expanded = root.expanduser()
    candidates = (
        expanded,
        expanded / "canonical" / "factor=cn_a",
        expanded / "factor_values",
    )
    for candidate in candidates:
        if _looks_like_factor_values_root(candidate):
            return candidate
    return expanded


def is_precomputed_formula(formula: str) -> bool:
    return formula.strip().lower().startswith(PRECOMPUTED_FORMULA_PREFIX)


def precomputed_formula(store_key: str) -> str:
    return f"{PRECOMPUTED_FORMULA_PREFIX}{_safe_store_key(store_key)}"


def _factor_value_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and not child.name.startswith("._") and _looks_like_factor_dir(child)
    ]


def _looks_like_factor_values_root(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        return any(child.is_dir() and _looks_like_factor_dir(child) for child in path.iterdir())
    except OSError:
        return False


def _looks_like_factor_dir(path: Path) -> bool:
    if path.name.startswith("factor_id="):
        return True
    if any(item.suffix == ".parquet" and not item.name.startswith("._") for item in path.glob("*.parquet")):
        return True
    if any(item.name.endswith(".metadata.json") and not item.name.startswith("._") for item in path.glob("*.json")):
        return True
    incremental = path / "incremental"
    return incremental.exists() and any(
        item.suffix == ".parquet" and not item.name.startswith("._") for item in incremental.glob("*.parquet")
    )


def _read_factor_metadata(directory: Path, *, manifest_root: Path | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for path in sorted(directory.glob("*.metadata.json")):
        if path.name.startswith("._"):
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            metadata.update(loaded)
    if metadata or manifest_root is None or not manifest_root.exists():
        return metadata
    for path in _manifest_candidates(directory, manifest_root):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            metadata.update(loaded)
    return metadata


def _manifest_candidates(directory: Path, manifest_root: Path) -> tuple[Path, ...]:
    names = {directory.name, _factor_id_from_name(directory.name), _store_key_from_name(directory.name)}
    return tuple(manifest_root / f"{name}.json" for name in names if name)


def _factor_from_store(directory: Path, metadata: dict[str, Any]) -> FactorDefinition | None:
    raw_id = str(metadata.get("factor_id") or "").strip()
    factor_id = _canonical_factor_id(raw_id or _factor_id_from_name(directory.name))
    if not factor_id:
        return None
    name = _safe_store_key(str(metadata.get("factor_name") or _store_key_from_name(directory.name) or factor_id))
    store_key = _safe_store_key(str(metadata.get("factor_store_key") or name or directory.name))
    description_bits = ["Precomputed factor values loaded from factor_values_root."]
    if metadata.get("schema_version"):
        description_bits.append(f"schema={metadata['schema_version']}.")
    if metadata.get("universe"):
        description_bits.append(f"universe={metadata['universe']}.")
    return FactorDefinition(
        factor_id=factor_id,
        name=name,
        formula=precomputed_formula(store_key),
        status="candidate",
        description=" ".join(description_bits),
        horizon_days=5,
        universe_filters=(),
        source="precomputed",
    )


def _dedupe_factors(factors: list[FactorDefinition]) -> list[FactorDefinition]:
    result: list[FactorDefinition] = []
    seen: set[str] = set()
    for factor in factors:
        key = _factor_key(factor.factor_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(factor)
    return result


def _select_precomputed_factors(
    factors: list[FactorDefinition],
    *,
    factor_ids: tuple[str, ...],
    import_all: bool,
) -> list[FactorDefinition]:
    if import_all:
        return factors
    requested = {item: _factor_id_values(item) for item in factor_ids}
    selected: list[FactorDefinition] = []
    matched: set[str] = set()
    for factor in factors:
        aliases = _factor_id_values(factor.factor_id)
        aliases.update(_factor_id_values(factor.name))
        for requested_id, requested_aliases in requested.items():
            if requested_id in matched:
                continue
            if aliases.intersection(requested_aliases):
                selected.append(factor)
                matched.add(requested_id)
    missing = [factor_id for factor_id in factor_ids if factor_id not in matched]
    if missing:
        raise ValueError(f"precomputed factors not found: {', '.join(missing)}")
    return selected


def _canonical_factor_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("factor_id="):
        cleaned = cleaned.split("=", 1)[1]
    normalized = cleaned.upper().replace("-", "_")
    match = re.fullmatch(r"(?:WORLDQUANT_)?(?:WQ_)?ALPHA_?0*(\d+)", normalized)
    if match:
        return f"WQ_ALPHA_{int(match.group(1)):03d}"
    if re.fullmatch(r"[A-Z][A-Z0-9_=-]*", normalized):
        return normalized
    return ""


def _factor_id_from_name(name: str) -> str:
    if name.startswith("factor_id="):
        return name.split("=", 1)[1]
    match = re.fullmatch(r"(?:worldquant_)?(?:wq_)?alpha_?0*(\d+)", name.strip().lower())
    if match:
        return f"WQ_ALPHA_{int(match.group(1)):03d}"
    return _canonical_factor_id(name)


def _store_key_from_name(name: str) -> str:
    if name.startswith("factor_id="):
        factor_id = _factor_id_from_name(name)
        match = re.fullmatch(r"WQ_ALPHA_0*(\d+)", factor_id)
        if match:
            return f"worldquant_alpha_{int(match.group(1)):03d}"
        return _safe_store_key(factor_id.lower())
    return _safe_store_key(name)


def _safe_store_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.=-]+", "_", value.strip())
    return normalized.strip("_") or "factor"


def _factor_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _factor_id_values(value: str) -> set[str]:
    normalized = value.strip().lower().replace("-", "_")
    values = {normalized}
    match = re.search(r"(?:worldquant_)?(?:wq_)?alpha_?0*(\d+)", normalized)
    if match is not None:
        number = int(match.group(1))
        values.update({f"worldquant_alpha_{number:03d}", f"wq_alpha_{number:03d}", f"alpha_{number:03d}"})
    return values
