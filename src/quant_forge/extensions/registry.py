"""Filesystem discovery for the declarative extensions registry (D7/D7a).

Discovery is read-only: immediate subdirectories of the extensions root are
scanned in directory-name order for one ``extension.json`` each, read with
``json.loads`` only (JSON, not YAML -- stdlib-only per D8). No manifest
content is ever imported, executed, or dynamically loaded, and manifests
that fail safety validation are never echoed beyond
``directory``/``status``/``issues``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quant_forge.extensions.manifest import (
    ManifestIssue,
    public_extension_view,
    validate_extension_manifest,
)


MANIFEST_FILENAME = "extension.json"

# Fixed contribution-point catalog (order pinned; notes are literal payload
# strings). A contribution to a reserved point is valid but inert.
_CONTRIBUTION_POINT_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "data.snapshot_source",
        "supported",
        "declarative data source metadata; feeds the CP5 data catalog path",
    ),
    (
        "data.canonical_mapping",
        "supported",
        "declarative field mapping metadata; feeds the CP5 data catalog path",
    ),
    (
        "data.quality_rule",
        "supported",
        "declarative quality rule metadata; feeds the CP5 validation path",
    ),
    ("agent.context_pack", "supported", "declarative knowledge pack; no workflow execution"),
    ("docs.pack", "supported", "declarative documentation set"),
    (
        "data.provider_adapter",
        "reserved",
        "reserved; in-repo adapter implementation permitted per D7a, no dynamic loading",
    ),
    (
        "data.pit_resolver",
        "reserved",
        "reserved; in-repo resolver implementation permitted per D7a, no dynamic loading",
    ),
    ("report.renderer", "reserved", "reserved stub"),
    ("agent.workflow", "reserved", "reserved stub; commercial boundary per D6/D7a"),
    ("lab.view", "reserved", "reserved stub"),
)


def contribution_points_payload() -> list[dict[str, Any]]:
    """The static contribution-point catalog as payload-ready rows."""

    return [
        {"point": point, "status": status, "note": note}
        for point, status, note in _CONTRIBUTION_POINT_ROWS
    ]


def scan_extensions(root: Path) -> list[dict[str, Any]]:
    """Scan one extensions root into payload-ready rows (no ``Path`` values).

    Directories are visited in name-ascending order (this order is also the
    duplicate-id claim order). Directories without a manifest file and
    dot-directories are skipped silently. Rejected rows carry only
    ``directory``/``status``/``issues``: manifest content that failed safety
    validation is never echoed.
    """

    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    claimed_ids: set[str] = set()
    for directory in sorted(root.iterdir(), key=lambda path: path.name):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # I/O failure, non-UTF-8 bytes, or a JSON parse error.
            rows.append(_rejected_row(directory.name, [ManifestIssue("manifest_unreadable", None)]))
            continue
        issues = validate_extension_manifest(payload)
        if not issues:
            manifest_id = str(payload["id"])
            if manifest_id in claimed_ids:
                issues = [ManifestIssue("duplicate_extension_id", "id")]
            else:
                claimed_ids.add(manifest_id)
        if issues:
            rows.append(_rejected_row(directory.name, issues))
            continue
        row: dict[str, Any] = {"directory": directory.name, "status": "valid", "issues": []}
        row.update(public_extension_view(payload))
        rows.append(row)
    return rows


def _rejected_row(directory: str, issues: list[ManifestIssue]) -> dict[str, Any]:
    return {
        "directory": directory,
        "status": "rejected",
        "issues": [{"code": issue.code, "field": issue.field} for issue in issues],
    }
