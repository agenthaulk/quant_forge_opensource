"""Small file IO helpers."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
