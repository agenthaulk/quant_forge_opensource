"""Append-only metadata trace store for local RD runs."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from quant_forge.research_loop.contracts import ResearchTraceEntry

FORBIDDEN_TRACE_KEYS = {
    "dataframe",
    "factor_values",
    "frame",
    "label_values",
    "market_rows",
    "raw_rows",
}


class ResearchTraceStore:
    """Store small RD metadata without raw market or factor-value rows."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser()

    @property
    def runs_root(self) -> Path:
        return self.output_root / "runs"

    def run_dir(self, run_id: str) -> Path:
        if not run_id.strip():
            raise ValueError("run_id is required")
        return self.runs_root / run_id

    def ensure_run_dirs(self, run_id: str) -> Path:
        root = self.run_dir(run_id)
        for child in (root, root / "plans", root / "reports"):
            child.mkdir(parents=True, exist_ok=True)
        return root

    def write_run(self, run_id: str, payload: dict[str, Any]) -> Path:
        self.ensure_run_dirs(run_id)
        return self._write_json(self.run_dir(run_id) / "run.json", payload)

    def write_config_snapshot(self, run_id: str, payload: Any) -> Path:
        self.ensure_run_dirs(run_id)
        return self._write_json(self.run_dir(run_id) / "config_snapshot.json", payload)

    def write_context(self, run_id: str, payload: Any) -> Path:
        self.ensure_run_dirs(run_id)
        return self._write_json(self.run_dir(run_id) / "context.json", payload)

    def append_trace(self, entry: ResearchTraceEntry | dict[str, Any]) -> Path:
        payload = _jsonable(entry.to_dict() if isinstance(entry, ResearchTraceEntry) else dict(entry))
        _reject_forbidden_trace_payload(payload)
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("trace entry requires run_id")
        self.ensure_run_dirs(run_id)
        path = self.run_dir(run_id) / "trace.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return path

    def read_trace_entries(self, run_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        path = self.run_dir(run_id) / "trace.jsonl"
        if not path.exists():
            return []
        rows = [_jsonable(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if limit is None:
            return rows
        return rows[-max(limit, 0) :]

    def read_recent_entries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not self.runs_root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.runs_root.glob("*/trace.jsonl"), key=lambda item: item.stat().st_mtime):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(_jsonable(json.loads(line)))
        return rows[-max(limit, 0) :]

    def _write_json(self, path: Path, payload: Any) -> Path:
        value = _jsonable(payload)
        _reject_forbidden_trace_payload(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(path)
        return path


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return _redacted_path(value)
    if isinstance(value, str) and _looks_like_absolute_path(value):
        return _redacted_path(Path(value))
    return value


def _reject_forbidden_trace_payload(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key) in FORBIDDEN_TRACE_KEYS:
                raise ValueError(f"trace metadata must not store raw or large data key: {key}")
            _reject_forbidden_trace_payload(value)
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            _reject_forbidden_trace_payload(item)


def _looks_like_absolute_path(value: str) -> bool:
    return value.startswith("/") or bool(len(value) > 2 and value[1:3] == ":\\")


def _redacted_path(path: Path) -> str:
    name = path.name or "path"
    return f"<redacted:path:{name}>"
