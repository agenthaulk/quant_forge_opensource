"""Unified read catalog for registered and precomputed factors."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from quant_forge.core.contracts import FactorDefinition
from quant_forge.core.contracts import FactorStatus
from quant_forge.factor_library.repository import FactorRepository

PRECOMPUTED_FORMULA_PREFIX = "precomputed:"
FACTOR_ID_STORE_PREFIX = "factor_id="
_UNPORTABLE_METADATA_VALUE = object()
_MACHINE_PATH_PREFIXES = (
    "file://",
    "/" + "Volumes/",
    "/" + "Users/",
    "/" + "home/",
    "/" + "mnt/",
    "/" + "media/",
)


@dataclass(frozen=True)
class FactorStoreNormalizationItem:
    factor_id: str
    source_dir: Path
    target_dir: Path
    action: str
    files_written: int = 0
    files_skipped: int = 0
    files_conflicted: int = 0
    manifest_written: bool = False


@dataclass(frozen=True)
class FactorStoreNormalizationResult:
    factor_values_root: Path
    manifest_root: Path | None
    source_roots: tuple[Path, ...]
    dry_run: bool
    discovered_count: int
    canonical_count: int
    legacy_count: int
    created_count: int
    merged_count: int
    updated_count: int
    skipped_count: int
    items: tuple[FactorStoreNormalizationItem, ...]


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
        local = [_canonicalize_precomputed_factor(factor) for factor in FactorRepository(self.factor_root).list()]
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
            return _canonicalize_precomputed_factor(FactorRepository(self.factor_root).get(factor_id))
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


def normalize_precomputed_factor_store(
    factor_values_root: Path | None,
    *,
    manifest_root: Path | None = None,
    source_roots: tuple[Path, ...] = (),
    dry_run: bool = False,
    link_files: bool = False,
) -> FactorStoreNormalizationResult:
    """Create canonical factor_id=<FACTOR_ID> directories for mounted factors.

    The operation is non-destructive: legacy directories remain readable, while
    canonical directories become the preferred target for discovery and future
    incremental writes.
    """

    root = resolve_factor_values_root(factor_values_root)
    if root is None:
        raise ValueError("factor_values_root is required")
    if not root.exists() and not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    manifest_root = manifest_root.expanduser() if manifest_root is not None else None
    resolved_source_roots = _resolved_source_roots(root, source_roots)
    factors: list[tuple[Path, dict[str, Any], FactorDefinition]] = []
    skipped_items: list[FactorStoreNormalizationItem] = []
    for source_root in (root, *resolved_source_roots):
        for directory in _factor_value_dirs(source_root):
            metadata = _read_factor_metadata(directory, manifest_root=manifest_root)
            factor = _factor_from_store(directory, metadata)
            if factor is None:
                skipped_items.append(
                    FactorStoreNormalizationItem(
                        factor_id="",
                        source_dir=directory,
                        target_dir=directory,
                        action="skipped_unidentified",
                    )
                )
                continue
            factors.append((directory, metadata, factor))

    items: list[FactorStoreNormalizationItem] = []
    canonical_count = 0
    legacy_count = 0
    created_count = 0
    merged_count = 0
    updated_count = 0
    for directory, metadata, factor in factors:
        target_key = _canonical_store_key(factor.factor_id)
        target_dir = root / target_key
        is_canonical = directory.parent == root and _store_key_matches(directory.name, target_key)
        canonical_count += int(is_canonical)
        legacy_count += int(not is_canonical)
        metadata_payload = _normalized_factor_metadata(
            factor,
            metadata,
            source_dir=directory,
            target_key=target_key,
        )
        if dry_run:
            action = "canonical" if is_canonical else ("would_merge" if target_dir.exists() else "would_create")
            items.append(
                FactorStoreNormalizationItem(
                    factor_id=factor.factor_id,
                    source_dir=directory,
                    target_dir=target_dir,
                    action=action,
                )
            )
            continue

        files_written = 0
        files_skipped = 0
        files_conflicted = 0
        action = "canonical"
        if not is_canonical:
            action = "merge" if target_dir.exists() else "create"
            target_dir.mkdir(parents=True, exist_ok=True)
            for source_file in _store_files(directory):
                result = _copy_store_file(
                    source_file,
                    directory,
                    target_dir,
                    link_files=link_files,
                )
                if result == "written":
                    files_written += 1
                elif result == "conflicted":
                    files_conflicted += 1
                else:
                    files_skipped += 1
            created_count += int(action == "create")
            merged_count += int(action == "merge")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            updated_count += 1
        overwrite_metadata = action != "merge"
        _write_normalized_metadata(target_dir, factor.factor_id, metadata_payload, overwrite=overwrite_metadata)
        manifest_written = _write_normalized_manifest(
            manifest_root,
            factor.factor_id,
            metadata_payload,
            overwrite=overwrite_metadata,
        )
        items.append(
            FactorStoreNormalizationItem(
                factor_id=factor.factor_id,
                source_dir=directory,
                target_dir=target_dir,
                action=action,
                files_written=files_written,
                files_skipped=files_skipped,
                files_conflicted=files_conflicted,
                manifest_written=manifest_written,
            )
        )

    items.extend(skipped_items)
    return FactorStoreNormalizationResult(
        factor_values_root=root,
        manifest_root=manifest_root,
        source_roots=resolved_source_roots,
        dry_run=dry_run,
        discovered_count=len(factors),
        canonical_count=canonical_count,
        legacy_count=legacy_count,
        created_count=created_count,
        merged_count=merged_count,
        updated_count=updated_count,
        skipped_count=len(skipped_items),
        items=tuple(items),
    )


def discover_factor_value_roots(search_root: Path) -> list[Path]:
    """Find mounted factor-value roots under a portable data tree."""

    root = search_root.expanduser()
    if not root.exists():
        return []
    candidates = [root]
    try:
        candidates.extend(
            path
            for path in root.rglob("*")
            if path.is_dir()
            and not path.name.startswith("._")
            and (
                path.name == "factor_values"
                or path.name.startswith("factor=")
                or path.parent.name == "canonical"
            )
        )
    except OSError:
        return []
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = resolve_factor_values_root(candidate)
        if resolved is None or not _looks_like_factor_values_root_for_scan(resolved):
            continue
        key = resolved.resolve()
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


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


def _looks_like_factor_values_root_for_scan(path: Path) -> bool:
    return not _looks_like_factor_dir(path) and _looks_like_factor_values_root(path)


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
            metadata.update(_portable_metadata(loaded))
    if manifest_root is None or not manifest_root.exists():
        return metadata
    for path in _manifest_candidates(directory, manifest_root):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            _fill_missing_metadata(metadata, _portable_metadata(loaded))
    return metadata


def _fill_missing_metadata(metadata: dict[str, Any], supplemental: dict[str, Any]) -> None:
    for key, value in supplemental.items():
        if _metadata_value_missing(metadata.get(key)):
            metadata[key] = value


def _metadata_value_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _manifest_candidates(directory: Path, manifest_root: Path) -> tuple[Path, ...]:
    factor_id = _factor_id_from_name(directory.name)
    names = (
        _canonical_factor_id(factor_id) or factor_id,
        _store_key_from_name(directory.name),
        directory.name,
    )
    ordered_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        ordered_names.append(name)
    return tuple(manifest_root / f"{name}.json" for name in ordered_names)


def _factor_from_store(directory: Path, metadata: dict[str, Any]) -> FactorDefinition | None:
    raw_id = str(metadata.get("factor_id") or "").strip()
    factor_id = _canonical_factor_id(raw_id or _factor_id_from_name(directory.name))
    if not factor_id:
        return None
    name = _safe_store_key(str(metadata.get("factor_name") or _store_key_from_name(directory.name) or factor_id))
    store_key = _canonical_store_key(factor_id)
    description_bits = ["Precomputed factor values loaded from factor_values_root."]
    if metadata.get("schema_version"):
        description_bits.append(f"schema={metadata['schema_version']}.")
    if metadata.get("universe"):
        description_bits.append(f"universe={metadata['universe']}.")
    formula_dsl = metadata.get("formula_dsl") or metadata.get("formula") or metadata.get("expression")
    if formula_dsl:
        description_bits.append("source_formula=metadata.")
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


def _canonicalize_precomputed_factor(factor: FactorDefinition) -> FactorDefinition:
    if not is_precomputed_formula(factor.formula):
        return factor
    formula = precomputed_formula(_canonical_store_key(factor.factor_id))
    if factor.formula == formula:
        return factor
    return replace(factor, formula=formula)


def _dedupe_factors(factors: list[FactorDefinition]) -> list[FactorDefinition]:
    result_by_key: dict[str, FactorDefinition] = {}
    for factor in factors:
        key = _factor_key(factor.factor_id)
        current = result_by_key.get(key)
        if current is None or _factor_priority(factor) > _factor_priority(current):
            result_by_key[key] = factor
    return list(result_by_key.values())


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
    if name.startswith(FACTOR_ID_STORE_PREFIX):
        factor_id = _factor_id_from_name(name)
        return _canonical_store_key(factor_id) if factor_id else _safe_store_key(name)
    return _safe_store_key(name)


def _canonical_store_key(factor_id: str) -> str:
    canonical_id = _canonical_factor_id(factor_id) or _safe_store_key(factor_id)
    return f"{FACTOR_ID_STORE_PREFIX}{canonical_id}"


def _store_key_matches(left: str, right: str) -> bool:
    return _safe_store_key(left).lower() == _safe_store_key(right).lower()


def _factor_priority(factor: FactorDefinition) -> int:
    marker = factor.formula.removeprefix(PRECOMPUTED_FORMULA_PREFIX)
    return 2 if marker.startswith(FACTOR_ID_STORE_PREFIX) else 1


def _safe_store_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.=-]+", "_", value.strip())
    return normalized.strip("_") or "factor"


def _factor_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _factor_id_values(value: str) -> set[str]:
    normalized = value.strip().lower().replace("-", "_")
    values = {normalized}
    match = re.fullmatch(r"(?:(?:worldquant|wq)_)?alpha_?0*(\d+)", normalized)
    if match is not None:
        number = int(match.group(1))
        values.update({f"worldquant_alpha_{number:03d}", f"wq_alpha_{number:03d}", f"alpha_{number:03d}"})
    return values


def _normalized_factor_metadata(
    factor: FactorDefinition,
    metadata: dict[str, Any],
    *,
    source_dir: Path,
    target_key: str,
) -> dict[str, Any]:
    payload = _portable_metadata(metadata)
    payload.setdefault("schema_version", "qf.factor_values.metadata.v1")
    payload["factor_id"] = factor.factor_id
    payload["factor_name"] = factor.name
    payload["factor_store_key"] = target_key
    payload["canonical_store_key"] = target_key
    payload["factor_values_relative_path"] = target_key
    payload["storage_naming"] = "factor_id_partition"
    legacy_key = str(metadata.get("factor_store_key") or _store_key_from_name(source_dir.name) or source_dir.name)
    if not _store_key_matches(legacy_key, target_key):
        payload["legacy_store_key"] = legacy_key
        payload["legacy_directory"] = source_dir.name
    return payload


def _store_files(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        and not _is_metadata_file(path)
        and not any(part.startswith("._") for part in path.relative_to(directory).parts)
    ]


def _is_metadata_file(path: Path) -> bool:
    return path.name == "metadata.json" or path.name.endswith(".metadata.json")


def _copy_store_file(source: Path, source_dir: Path, target_dir: Path, *, link_files: bool) -> str:
    relative_path = source.relative_to(source_dir)
    target = target_dir / relative_path
    if source.resolve() == target.resolve():
        return "skipped"
    if target.exists():
        if _same_file_content(source, target):
            return "skipped"
        conflict = _unique_conflict_path(target_dir / "conflicts" / _safe_store_key(source_dir.name) / relative_path)
        _write_store_file(source, conflict, link_files=link_files)
        return "conflicted"
    _write_store_file(source, target, link_files=link_files)
    return "written"


def _write_store_file(source: Path, target: Path, *, link_files: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if link_files:
        try:
            os.link(source, target)
            return
        except OSError:
            pass
    shutil.copy2(source, target)


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        return left.stat().st_size == right.stat().st_size and _file_digest(left) == _file_digest(right)
    except OSError:
        return False


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_conflict_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}.conflict{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"too many conflict files for {path}")


def _resolved_source_roots(target_root: Path, source_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen = {target_root.resolve()}
    for source_root in source_roots:
        resolved = resolve_factor_values_root(source_root)
        if resolved is None or not resolved.exists():
            continue
        key = resolved.resolve()
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return tuple(roots)


def _portable_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in metadata.items():
        if _is_machine_path_metadata_key(str(key)):
            continue
        portable_value = _portable_metadata_value(value)
        if portable_value is _UNPORTABLE_METADATA_VALUE:
            continue
        payload[str(key)] = portable_value
    return payload


def _portable_metadata_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _portable_metadata(value)
    if isinstance(value, list):
        portable_items = [_portable_metadata_value(item) for item in value]
        return [item for item in portable_items if item is not _UNPORTABLE_METADATA_VALUE]
    if isinstance(value, str) and _looks_like_machine_path(value):
        return _UNPORTABLE_METADATA_VALUE
    return value


def _is_machine_path_metadata_key(key: str) -> bool:
    normalized = key.strip().lower()
    allowed = {
        "canonical_store_key",
        "factor_store_key",
        "factor_values_relative_path",
        "legacy_directory",
        "legacy_store_key",
        "storage_naming",
    }
    if normalized in allowed:
        return False
    return any(token in normalized for token in ("path", "root", "dir", "directory", "uri", "url"))


def _looks_like_machine_path(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith(_MACHINE_PATH_PREFIXES):
        return True
    return re.match(r"^[a-zA-Z]:[\\/]", stripped) is not None


def _write_normalized_metadata(target_dir: Path, factor_id: str, payload: dict[str, Any], *, overwrite: bool) -> None:
    target = target_dir / f"{factor_id}.metadata.json"
    if target.exists() and not overwrite:
        return
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_normalized_manifest(
    manifest_root: Path | None,
    factor_id: str,
    payload: dict[str, Any],
    *,
    overwrite: bool,
) -> bool:
    if manifest_root is None:
        return False
    manifest_root.mkdir(parents=True, exist_ok=True)
    target = manifest_root / f"{factor_id}.json"
    if target.exists() and not overwrite:
        return False
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True
