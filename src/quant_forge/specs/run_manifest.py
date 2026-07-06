"""RunManifest: deterministic provenance fingerprints for every run.

Closes the Phase A provenance gap ("cached values trusted as-is"): a manifest
binds a run to the exact spec, request, data snapshot and registry version via
canonical sha256 fingerprints. All functions here are pure — timestamps and
data fingerprints are caller-supplied, nothing reads the clock or the
filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any

from quant_forge.specs.factor_spec import FactorSpec

RUN_MANIFEST_SCHEMA_VERSION = "qf.run.manifest.v1"


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    created_at: str
    spec_fingerprint: str
    data_fingerprint: str
    registry_version: str
    request_hash: str
    input_refs: tuple[str, ...] = field(default_factory=tuple)
    sample_role: str = "research_evaluation"
    schema_version: str = RUN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported run manifest schema_version: {self.schema_version} "
                f"(expected {RUN_MANIFEST_SCHEMA_VERSION})"
            )
        _set_tuple(self, "input_refs")
        if not self.run_id.strip():
            raise ValueError("run_id is required")
        if not self.created_at.strip():
            raise ValueError("created_at is required")
        try:
            datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("created_at must be an ISO timestamp") from exc
        if not self.spec_fingerprint.strip():
            raise ValueError("spec_fingerprint is required")
        if not self.request_hash.strip():
            raise ValueError("request_hash is required")
        if not self.sample_role.strip():
            raise ValueError("sample_role is required")


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    """sha256 over the canonical JSON form of a payload.

    Same payload => same fingerprint; any key or value change => different
    fingerprint. Canonical form: sorted keys, compact separators, ASCII-only.
    """

    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_for(
    spec: FactorSpec,
    *,
    run_id: str,
    created_at: str,
    request: dict[str, Any],
    data_fingerprint: str = "",
    registry_version: str = "",
    input_refs: tuple[str, ...] = (),
    sample_role: str = "research_evaluation",
) -> RunManifest:
    """Build the provenance manifest for one run of one spec."""

    return RunManifest(
        run_id=run_id,
        created_at=created_at,
        spec_fingerprint=canonical_fingerprint(spec.to_dict()),
        data_fingerprint=data_fingerprint,
        registry_version=registry_version,
        request_hash=canonical_fingerprint(request),
        input_refs=tuple(str(item) for item in input_refs),
        sample_role=sample_role,
    )


def _set_tuple(instance: object, field_name: str) -> None:
    value = getattr(instance, field_name)
    if value is None:
        normalized: tuple[str, ...] = ()
    elif isinstance(value, tuple):
        normalized = value
    elif isinstance(value, list):
        normalized = tuple(value)
    else:
        normalized = (value,)
    object.__setattr__(instance, field_name, tuple(str(item) for item in normalized))
