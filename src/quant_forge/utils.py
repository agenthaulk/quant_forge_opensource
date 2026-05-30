"""Small file IO helpers."""

from __future__ import annotations

import json
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
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    _atomic_write(path, text + "\n")


def write_text(path: Path, text: str) -> None:
    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
