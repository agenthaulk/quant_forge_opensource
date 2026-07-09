"""Small file IO helpers."""

from __future__ import annotations

from contextlib import suppress
import json
import math
import os
from pathlib import Path
from typing import Any
import uuid

import yaml


def read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return value


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    # Non-finite floats (unmarkable NAV days etc.) must serialize as null:
    # json.dumps would otherwise emit bare NaN/Infinity, which is not JSON.
    text = json.dumps(_finite_json(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    _atomic_write(path, text + "\n")


def _finite_json(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return value


def write_text(path: Path, text: str) -> None:
    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    """Atomically publish ``text`` at ``path``; safe under concurrent writers.

    Each writer stages its payload in a private, uniquely named temp file
    (``.{name}.{unique}.tmp``, same directory so the rename stays within one
    filesystem) and then atomically renames it over ``path`` via
    ``os.replace``. Semantics:

    - Readers only ever observe a complete payload from exactly one writer,
      never a partial or interleaved file.
    - Concurrent writers to the same path are safe; the surviving content is
      that of the writer whose rename lands last (last-writer-wins).
    - A writer killed between staging and rename may leave its private temp
      file behind; it is never visible at ``path`` and never corrupts it.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        # "x" (exclusive create) guards the never-expected temp-name collision:
        # failing closed beats silently sharing a staging file again.
        with open(tmp, "x", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
