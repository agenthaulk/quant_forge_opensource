"""QF_OS Extension Manifest v1: schema constants, validation, public view.

Decisions D7/D7a: the extensions registry is declarative-only. Validation is
pure stdlib over parsed JSON; manifest content is never imported, executed,
or dynamically loaded. Any contribution carrying a truthy ``executable`` key
rejects the whole manifest, with no built-in exemption of any kind.

Release-safety rules reuse the single redaction definition
(:func:`quant_forge.lineage.store.redact_free_text`): manifests carrying
external URLs or redactable values (absolute local paths, ``file://``/UNC
paths, secret-like assignments) are rejected outright, so unsafe values never
need redacting downstream. Validation issues are closed-set label codes with
field locators (FP-4): never scores, never severity numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from quant_forge.lineage.store import redact_free_text


MVP_CONTRIBUTION_POINTS: tuple[str, ...] = (
    "data.snapshot_source",
    "data.canonical_mapping",
    "data.quality_rule",
    "agent.context_pack",
    "docs.pack",
)

RESERVED_CONTRIBUTION_POINTS: tuple[str, ...] = (
    "data.provider_adapter",
    "data.pit_resolver",
    "report.renderer",
    "agent.workflow",
    "lab.view",
)

ALL_CONTRIBUTION_POINTS: tuple[str, ...] = MVP_CONTRIBUTION_POINTS + RESERVED_CONTRIBUTION_POINTS

EXTENSION_KINDS: tuple[str, ...] = (
    "data-extension",
    "docs-extension",
    "agent-extension",
    "mixed",
)

_LABEL_MAX_CHARS = 64
_LABEL_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class ManifestIssue:
    """One validation finding: a closed-set label code plus a field locator."""

    code: str
    field: str | None


def validate_extension_manifest(payload: object) -> list[ManifestIssue]:
    """Validate one parsed manifest; an empty list means the manifest is valid."""

    if not isinstance(payload, dict):
        return [ManifestIssue("manifest_not_object", None)]
    issues: list[ManifestIssue] = []
    extension_id = payload.get("id")
    if extension_id is None:
        issues.append(ManifestIssue("missing_id", "id"))
    elif not _valid_label(extension_id):
        issues.append(ManifestIssue("invalid_id", "id"))
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(ManifestIssue("missing_name", "name"))
    version = payload.get("version")
    if version is None:
        issues.append(ManifestIssue("missing_version", "version"))
    elif not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        issues.append(ManifestIssue("invalid_version", "version"))
    if payload.get("kind") not in EXTENSION_KINDS:
        issues.append(ManifestIssue("invalid_kind", "kind"))
    issues.extend(_permission_issues(payload.get("permissions")))
    issues.extend(_contribution_issues(payload.get("contributes")))
    issues.extend(_external_url_issues(payload))
    issues.extend(_redactable_value_issues(payload))
    return issues


def public_extension_view(payload: dict) -> dict:
    """Project one validated manifest into its public payload shape.

    The view is a projection: unknown keys, ``builtin``, and ``executable``
    flags are tolerated on read and never echoed. Optional fields absent from
    the manifest are omitted, never defaulted (FP-4).
    """

    view: dict[str, object] = {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "version": payload.get("version"),
        "kind": payload.get("kind"),
    }
    description = payload.get("description")
    if isinstance(description, str) and description:
        view["description"] = description
    engine = payload.get("engine")
    if isinstance(engine, str) and engine:
        view["engine"] = engine
    view["permissions"] = _permissions_view(payload.get("permissions"))
    view["contributions"] = _contributions_view(payload.get("contributes"))
    return view


def _permissions_view(permissions: object) -> dict[str, object]:
    source = permissions if isinstance(permissions, dict) else {}
    view: dict[str, object] = {
        "network_access": source.get("network_access"),
        "secret_access": source.get("secret_access"),
    }
    scopes = source.get("data_scopes")
    if isinstance(scopes, list):
        view["data_scopes"] = [str(scope) for scope in scopes]
    return view


def _contributions_view(contributes: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if not isinstance(contributes, list):
        return rows
    for entry in contributes:
        if not isinstance(entry, dict):
            continue
        row: dict[str, object] = {
            "id": entry.get("id"),
            "point": entry.get("point"),
            "reserved": entry.get("point") in RESERVED_CONTRIBUTION_POINTS,
        }
        config = entry.get("config")
        if isinstance(config, dict):
            row["config"] = config
        docs_label = entry.get("docs")
        if isinstance(docs_label, str) and docs_label:
            row["docs"] = docs_label
        rows.append(row)
    return rows


def _valid_label(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _LABEL_MAX_CHARS
        and _LABEL_RE.fullmatch(value) is not None
    )


def _permission_issues(permissions: object) -> list[ManifestIssue]:
    if not isinstance(permissions, dict):
        return [ManifestIssue("permissions_missing", "permissions")]
    issues: list[ManifestIssue] = []
    # Local-first (D7): both flags must be explicitly present, boolean, and
    # false. Anything else -- missing, truthy, or non-boolean -- is rejected.
    for key, code in (
        ("network_access", "network_access_rejected"),
        ("secret_access", "secret_access_rejected"),
    ):
        value = permissions.get(key)
        if not isinstance(value, bool) or value:
            issues.append(ManifestIssue(code, f"permissions.{key}"))
    scopes = permissions.get("data_scopes")
    if scopes is not None and (
        not isinstance(scopes, list) or not all(_valid_label(scope) for scope in scopes)
    ):
        issues.append(ManifestIssue("data_scopes_invalid", "permissions.data_scopes"))
    return issues


def _contribution_issues(contributes: object) -> list[ManifestIssue]:
    if contributes is None:
        return []
    if not isinstance(contributes, list):
        return [ManifestIssue("contributions_invalid", "contributes")]
    issues: list[ManifestIssue] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(contributes):
        locator = f"contributes[{index}]"
        if not isinstance(entry, dict):
            issues.append(ManifestIssue("contributions_invalid", locator))
            continue
        entry_id = entry.get("id")
        if not _valid_label(entry_id):
            issues.append(ManifestIssue("invalid_contribution_id", f"{locator}.id"))
        elif entry_id in seen_ids:
            issues.append(ManifestIssue("duplicate_contribution_id", f"{locator}.id"))
        else:
            seen_ids.add(entry_id)  # type: ignore[arg-type]
        if entry.get("point") not in ALL_CONTRIBUTION_POINTS:
            issues.append(ManifestIssue("unknown_contribution_point", f"{locator}.point"))
        if entry.get("executable"):
            # D7 unconditional no-exec rule: any truthy ``executable`` rejects
            # the whole manifest; ``builtin`` grants no exemption of any kind.
            issues.append(
                ManifestIssue("executable_contribution_rejected", f"{locator}.executable")
            )
        config = entry.get("config")
        if config is not None and not isinstance(config, dict):
            issues.append(ManifestIssue("contributions_invalid", f"{locator}.config"))
    return issues


def _external_url_issues(payload: dict) -> list[ManifestIssue]:
    issues: list[ManifestIssue] = []

    def _contains_url(text: str) -> bool:
        # URL schemes are case-insensitive, so the rejected-outright contract
        # (module docstring) must catch HTTPS:// the same as https://; the
        # scheme separator keeps names like "httpx" from over-matching.
        folded = text.casefold()
        return "http://" in folded or "https://" in folded

    def _walk(node: object, locator: str) -> None:
        if isinstance(node, str):
            if _contains_url(node):
                issues.append(ManifestIssue("external_url_rejected", locator or None))
        elif isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key)
                if _contains_url(key_text):
                    issues.append(ManifestIssue("external_url_rejected", locator or None))
                _walk(item, f"{locator}.{key_text}" if locator else key_text)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{locator}[{index}]")

    _walk(payload, "")
    return issues


def _redactable_value_issues(payload: dict) -> list[ManifestIssue]:
    # One definition per quantity: a manifest is rejected outright when the
    # lineage redactor would change its canonical serialization (absolute
    # local paths, file://or UNC paths, secret-like KEY=value pairs).
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    if redact_free_text(serialized) != serialized:
        return [ManifestIssue("redactable_value_rejected", None)]
    return []
