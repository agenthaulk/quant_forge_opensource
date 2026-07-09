"""Append-only artifact lineage index and run index under ``artifact_root``.

Both indexes are JSONL files that are only ever appended to, never rewritten.
No row may contain an absolute path: artifact locations are stored relative to
``artifact_root`` (POSIX form) or as ``null`` when the artifact lives outside
``artifact_root`` (FP-4: an unknown location is surfaced as null, not guessed).
Free-text metadata is passed through :func:`redact_free_text` before writing so
user-home prefixes, absolute paths, and env-var-like secret assignments never
reach disk.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Mapping

try:  # pragma: no cover - fcntl is available on every POSIX platform
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows) fallback
    fcntl = None  # type: ignore[assignment]

from quant_forge.core.contracts import MetricValue
from quant_forge.factor_library.classification import FACTOR_CATEGORY_DIRS

logger = logging.getLogger(__name__)

LINEAGE_SCHEMA_VERSION = "qf.lineage.v1"
RUN_INDEX_SCHEMA_VERSION = "qf.run_index.v1"
RUN_KINDS = ("evaluate", "backtest", "bench", "rd", "falsification")
DATA_WINDOW_STATUSES = ("available", "unavailable")
METRIC_HIGHLIGHT_STATUSES = (
    "available",
    "insufficient_sample",
    "not_applicable",
    "unavailable_source_series",
    "invalid",
)

_LINEAGE_INDEX_RELATIVE = Path("lineage") / "artifact_index.jsonl"
_RUN_INDEX_RELATIVE = Path("runs") / "index.jsonl"

_REDACTED_PATH = "<redacted-path>"
_REDACTED_VALUE = "<redacted>"
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
# The alternation keeps literal user-home markers out of this public source
# file (release-safety scan) while still matching them in redacted text.
_POSIX_PATH_RE = re.compile(
    r"(?:~|/(?:Users|home|root|private|var|tmp|opt|mnt|srv|etc|usr|Volumes|Library))(?:/[^\s'\"`;,)\]}]+)+"
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s'\"`;,)\]}]+")
# UNC network paths (double-backslash server, share, and at least one further
# separator-delimited component). Built structurally so no literal
# user-home-like marker appears in this public source file.
_UNC_PATH_RE = re.compile(r"\\\\[^\s\\'\"`;,)\]}]+(?:\\[^\s\\'\"`;,)\]}]+)+")
# file:// URLs in both host form (file://host/share/...) and local form
# (file:///abs/path); applied before the POSIX rule so the scheme prefix is
# redacted together with the path.
_FILE_URL_RE = re.compile(r"\bfile://[^\s'\"`;,)\]}]+", re.IGNORECASE)
_ENV_SECRET_RE = re.compile(
    r"\b(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:key|token|secret|password|passwd|credential)s?[A-Za-z0-9_]*)"
    r"\s*=\s*(?P<value>[^\s'\"]+)",
    re.IGNORECASE,
)


def redact_free_text(text: str) -> str:
    """Strip user-home/absolute path prefixes and env-var-like secret values."""

    redacted = text
    home = str(Path.home())
    if len(home) > 1:
        redacted = redacted.replace(home, _REDACTED_PATH)
    redacted = _ENV_SECRET_RE.sub(lambda match: f"{match.group('key')}={_REDACTED_VALUE}", redacted)
    redacted = _FILE_URL_RE.sub(_REDACTED_PATH, redacted)
    redacted = _UNC_PATH_RE.sub(_REDACTED_PATH, redacted)
    redacted = _WINDOWS_PATH_RE.sub(_REDACTED_PATH, redacted)
    redacted = _POSIX_PATH_RE.sub(_REDACTED_PATH, redacted)
    return redacted


@contextmanager
def _advisory_file_lock(lock_path: Path) -> Iterator[None]:
    """Advisory ``fcntl.flock`` exclusive lock on a sidecar ``.lock`` file.

    Serializes the read-then-append critical sections of the lineage and run
    indexes against concurrent same-host writers. On platforms without
    ``fcntl`` (non-POSIX, e.g. Windows) this degrades to a no-op: appends stay
    append-only but the read+append dedup window is not serialized there.
    """

    if fcntl is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """sha256 over a canonical JSON encoding (sorted keys, compact separators)."""

    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def artifact_id_for(*, path: Path | None = None, payload: Mapping[str, Any] | None = None) -> str:
    """sha256 of the file bytes when the file exists, else of the canonical payload."""

    if path is not None:
        candidate = path.expanduser()
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    if payload is not None:
        return canonical_fingerprint(payload)
    raise ValueError("artifact_id requires an existing file or a canonical payload")


def relative_artifact_path(artifact_root: Path, path: Path | None) -> str | None:
    """Path relative to ``artifact_root`` (POSIX), or None when outside it.

    Never returns an absolute path or a ``..`` traversal.
    """

    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=False)
    root = artifact_root.expanduser().resolve(strict=False)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def locate_factor_definition_file(factor_root: Path, factor_id: str) -> Path | None:
    """Find the factor.yaml for one factor id inside ``factor_root``, if any.

    Mirrors the repository layout (legacy ``*_factors`` plus categorized
    directories) without importing repository internals. Categorized entries
    are preferred, matching the repository's read preference.
    """

    root = factor_root.expanduser()
    if not root.exists():
        return None
    matches = list(root.glob(f"*_factors/{factor_id}/factor.yaml"))
    for category_dir in FACTOR_CATEGORY_DIRS.values():
        matches.extend(root.glob(f"{category_dir}/*_factors/{factor_id}/factor.yaml"))
    ordered = sorted(set(matches))
    if not ordered:
        return None
    categorized = [
        candidate
        for candidate in ordered
        if len(candidate.parts) >= 4 and candidate.parts[-4] in FACTOR_CATEGORY_DIRS.values()
    ]
    return (categorized or ordered)[0]


def metric_highlight(value: MetricValue) -> dict[str, Any]:
    """Run-index highlight entry: value WITH status, null-not-zero preserved."""

    return {
        "value": value.value,
        "unit": value.unit,
        "status": value.status,
        "observation_count": value.observation_count,
    }


def new_run_id(kind: str, created_at: datetime, config_fingerprint: str) -> str:
    if kind not in RUN_KINDS:
        raise ValueError(f"unknown run kind: {kind}")
    if len(config_fingerprint) < 8:
        raise ValueError("config_fingerprint must be at least 8 characters")
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    stamped = created_at.astimezone(timezone.utc)
    return f"{kind}-{stamped.strftime('%Y%m%dT%H%M%S%f')}Z-{config_fingerprint[:8]}"


@dataclass(frozen=True)
class LineageRecord:
    artifact_id: str
    artifact_type: str
    path_rel: str | None
    created_at: str
    generated_by: str
    parents: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _SHA256_HEX_RE.fullmatch(self.artifact_id):
            raise ValueError("artifact_id must be a sha256 hex digest")
        if not self.artifact_type.strip():
            raise ValueError("artifact_type is required")
        if not self.generated_by.strip():
            raise ValueError("generated_by is required")
        _require_iso_timestamp(self.created_at)
        if self.path_rel is not None:
            _require_relative_path(self.path_rel)
        for parent in self.parents:
            if not _SHA256_HEX_RE.fullmatch(parent):
                raise ValueError("parent artifact ids must be sha256 hex digests")


class LineageStore:
    """Append-only JSONL artifact lineage index under ``artifact_root/lineage``."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.expanduser()
        self.index_path = self.artifact_root / _LINEAGE_INDEX_RELATIVE
        self.lock_path = self.index_path.with_suffix(".lock")

    def record_artifact(
        self,
        *,
        artifact_type: str,
        created_at: str,
        generated_by: str,
        path: Path | None = None,
        payload: Mapping[str, Any] | None = None,
        parents: Iterable[str] = (),
        metadata: Mapping[str, str] | None = None,
    ) -> LineageRecord:
        record = LineageRecord(
            artifact_id=artifact_id_for(path=path, payload=payload),
            artifact_type=artifact_type,
            path_rel=relative_artifact_path(self.artifact_root, path),
            created_at=created_at,
            generated_by=redact_free_text(generated_by),
            parents=tuple(parents),
            metadata={
                redact_free_text(str(key)): redact_free_text(str(value)) for key, value in (metadata or {}).items()
            },
        )
        # The read-then-append dedup below is a critical section: two writers
        # interleaving between the read and the append would both append the
        # same edge. The advisory lock serializes same-host writers.
        with _advisory_file_lock(self.lock_path):
            if not self._edge_recorded(record):
                self._append(self._row_for(record))
        return record

    def read_rows(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.index_path)

    def _edge_recorded(self, record: LineageRecord) -> bool:
        for row in self.read_rows():
            if (
                row.get("artifact_id") == record.artifact_id
                and tuple(row.get("parents") or ()) == record.parents
                and row.get("generated_by") == record.generated_by
                and row.get("artifact_type") == record.artifact_type
            ):
                return True
        return False

    def _row_for(self, record: LineageRecord) -> dict[str, Any]:
        row: dict[str, Any] = {
            "schema_version": LINEAGE_SCHEMA_VERSION,
            "artifact_id": record.artifact_id,
            "artifact_type": record.artifact_type,
            "path_rel": record.path_rel,
            "created_at": record.created_at,
            "generated_by": record.generated_by,
            "parents": list(record.parents),
        }
        if record.metadata:
            row["metadata"] = dict(record.metadata)
        return row

    def _append(self, row: dict[str, Any]) -> None:
        _append_jsonl(self.index_path, row)


class RunIndex:
    """Append-only JSONL run history under ``artifact_root/runs``."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.expanduser()
        self.index_path = self.artifact_root / _RUN_INDEX_RELATIVE
        self.lock_path = self.index_path.with_suffix(".lock")

    def append_run(
        self,
        *,
        run_id: str,
        kind: str,
        factor_ids: Iterable[str],
        created_at: str,
        data_window: Mapping[str, Any],
        config_fingerprint: str,
        metric_highlights: Mapping[str, Mapping[str, Any]],
        artifact_paths_rel: Iterable[str],
        warnings_count: int,
    ) -> dict[str, Any]:
        ids = [str(item) for item in factor_ids]
        paths_rel = [str(item) for item in artifact_paths_rel]
        if not run_id.strip():
            raise ValueError("run_id is required")
        if kind not in RUN_KINDS:
            raise ValueError(f"unknown run kind: {kind}")
        if not ids:
            raise ValueError("factor_ids must not be empty")
        _require_iso_timestamp(created_at)
        _require_data_window(data_window)
        if not _SHA256_HEX_RE.fullmatch(config_fingerprint):
            raise ValueError("config_fingerprint must be a sha256 hex digest")
        for path_rel in paths_rel:
            _require_relative_path(path_rel)
        if warnings_count < 0:
            raise ValueError("warnings_count must be non-negative")
        highlights = {str(name): _validated_highlight(name, entry) for name, entry in metric_highlights.items()}
        row: dict[str, Any] = {
            "schema_version": RUN_INDEX_SCHEMA_VERSION,
            "run_id": run_id,
            "kind": kind,
            "factor_ids": ids,
            "created_at": created_at,
            "data_window": {
                "start_date": data_window.get("start_date"),
                "end_date": data_window.get("end_date"),
                "status": data_window.get("status"),
            },
            "config_fingerprint": config_fingerprint,
            "metric_highlights": highlights,
            "artifact_paths_rel": paths_rel,
            "warnings_count": int(warnings_count),
        }
        with _advisory_file_lock(self.lock_path):
            _append_jsonl(self.index_path, row)
        return row

    def read_rows(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.index_path)

    def find(self, run_id: str) -> dict[str, Any] | None:
        for row in self.read_rows():
            if row.get("run_id") == run_id:
                return row
        return None

    def search(self, *, factor_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.read_rows()
        if factor_id is not None:
            rows = [row for row in rows if factor_id in (row.get("factor_ids") or [])]
        if kind is not None:
            rows = [row for row in rows if row.get("kind") == kind]
        return rows


def _validated_highlight(name: object, entry: Mapping[str, Any]) -> dict[str, Any]:
    status = entry.get("status")
    if status not in METRIC_HIGHLIGHT_STATUSES:
        raise ValueError(f"metric highlight {name!r} has unknown status: {status!r}")
    value = entry.get("value")
    if status == "available" and value is None:
        raise ValueError(f"metric highlight {name!r} is 'available' but has no value")
    if status != "available" and value is not None:
        # Null-not-zero: a non-available metric must not carry a number.
        raise ValueError(f"metric highlight {name!r} has status {status!r} but a numeric value")
    return {
        "value": value,
        "unit": str(entry.get("unit", "")),
        "status": status,
        "observation_count": int(entry.get("observation_count", 0)),
    }


def _require_data_window(data_window: Mapping[str, Any]) -> None:
    status = data_window.get("status")
    if status not in DATA_WINDOW_STATUSES:
        raise ValueError(f"data_window status must be one of {DATA_WINDOW_STATUSES}, got {status!r}")
    start = data_window.get("start_date")
    end = data_window.get("end_date")
    if status == "available" and not (start and end):
        raise ValueError("data_window 'available' requires both start_date and end_date")
    if status == "unavailable" and (start or end):
        raise ValueError("data_window 'unavailable' must not carry dates")


def _require_relative_path(path_rel: str) -> None:
    if not path_rel.strip():
        raise ValueError("relative artifact path must not be empty")
    candidate = Path(path_rel)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", path_rel) or path_rel.startswith("~"):
        raise ValueError(f"artifact path must be relative to artifact_root: {path_rel}")
    if ".." in candidate.parts:
        raise ValueError(f"artifact path must not traverse outside artifact_root: {path_rel}")


def _require_iso_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"created_at must be an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        # Consistent with the memory/goal stores and new_run_id (F6): naive
        # timestamps are ambiguous evidence and are rejected instead of being
        # silently assumed UTC.
        raise ValueError(f"created_at must be timezone-aware: {value!r}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError:
            # One torn/partial line (e.g. a writer killed mid-append or a
            # final line read concurrently) must not poison the whole index:
            # skip it and keep the successfully-parsed rows in order.
            logger.warning("skipping malformed JSONL line %d in %s", line_number, path)
            continue
    return rows


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
