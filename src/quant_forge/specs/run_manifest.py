"""RunManifest: deterministic provenance fingerprints for every run.

Closes the Phase A provenance gap ("cached values trusted as-is"): a manifest
binds a run to the exact spec (factor or strategy), request, data snapshot and
registry version via canonical sha256 fingerprints. Provenance fields are
never blank: a caller that genuinely lacks a fingerprint must pass the typed
sentinel ``UNVERIFIED_PROVENANCE`` so downstream gates can see (and reject)
unverified runs, and ``sample_role`` / ``spec_kind`` are drawn from the closed
vocabularies ``SAMPLE_ROLES`` / ``SPEC_KINDS``. All functions here are pure —
timestamps and data fingerprints are caller-supplied, nothing reads the clock
or the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any
import unicodedata

from quant_forge.specs._normalize import set_tuple
from quant_forge.specs._vocab import SAMPLE_ROLES, SPEC_KINDS, UNVERIFIED_PROVENANCE
from quant_forge.specs.factor_spec import FactorSpec
from quant_forge.specs.strategy_spec import StrategySpec

__all__ = [
    "RUN_MANIFEST_SCHEMA_VERSION",
    "SAMPLE_ROLES",
    "SPEC_KINDS",
    "UNVERIFIED_PROVENANCE",
    "RunManifest",
    "canonical_fingerprint",
    "manifest_for",
]

RUN_MANIFEST_SCHEMA_VERSION = "qf.run.manifest.v1"


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    created_at: str
    spec_fingerprint: str
    spec_kind: str
    spec_schema_version: str
    data_fingerprint: str
    registry_version: str
    request_hash: str
    sample_role: str
    input_refs: tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = RUN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported run manifest schema_version: {self.schema_version} "
                f"(expected {RUN_MANIFEST_SCHEMA_VERSION})"
            )
        set_tuple(self, "input_refs")
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
        if self.spec_kind not in SPEC_KINDS:
            raise ValueError(
                f"invalid spec_kind: {self.spec_kind!r} (expected one of {sorted(SPEC_KINDS)})"
            )
        if not self.spec_schema_version.strip():
            raise ValueError("spec_schema_version is required")
        if not self.data_fingerprint.strip():
            raise ValueError(
                "data_fingerprint is required; pass UNVERIFIED_PROVENANCE "
                f"({UNVERIFIED_PROVENANCE!r}) if provenance is genuinely unavailable"
            )
        if not self.registry_version.strip():
            raise ValueError(
                "registry_version is required; pass UNVERIFIED_PROVENANCE "
                f"({UNVERIFIED_PROVENANCE!r}) if provenance is genuinely unavailable"
            )
        if not self.request_hash.strip():
            raise ValueError("request_hash is required")
        if self.sample_role not in SAMPLE_ROLES:
            raise ValueError(
                f"invalid sample_role: {self.sample_role!r} "
                f"(expected one of {sorted(SAMPLE_ROLES)})"
            )


def _nfc(value: Any) -> Any:
    """Recursively NFC-normalize every string (keys and values) in a payload."""

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc(key): _nfc(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_nfc(item) for item in value]
    return value


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    """sha256 over the canonical JSON form of a payload.

    Same payload => same fingerprint; any key or value change => different
    fingerprint. Canonical form: NFC-normalized strings (keys and values),
    sorted keys, compact separators, ASCII-only. Raises ValueError if the
    payload is not canonically serializable — including non-finite floats
    (NaN/Infinity), which have no canonical JSON form (Phase A non-finite
    JSON rule).

    Numeric type identity is caller responsibility: int ``1`` and float
    ``1.0`` serialize differently ("1" vs "1.0") and therefore fingerprint
    differently. No numeric coercion is performed here — callers that want
    them treated as equal must normalize numeric types before fingerprinting.
    """

    try:
        text = json.dumps(
            _nfc(payload),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload is not canonically serializable: {exc}") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_for(
    spec: FactorSpec | StrategySpec,
    *,
    run_id: str,
    created_at: str,
    request: dict[str, Any],
    data_fingerprint: str,
    registry_version: str,
    sample_role: str,
    input_refs: tuple[str, ...] = (),
) -> RunManifest:
    """Build the provenance manifest for one run of one spec.

    Accepts either spec family; ``spec_kind`` and ``spec_schema_version`` are
    derived from the spec itself. ``data_fingerprint``, ``registry_version``
    and ``sample_role`` carry no defaults on purpose: the caller must state
    provenance and sample role explicitly (``UNVERIFIED_PROVENANCE`` when
    genuinely unavailable).
    """

    if isinstance(spec, FactorSpec):
        spec_kind = "factor"
    elif isinstance(spec, StrategySpec):
        spec_kind = "strategy"
    else:
        raise ValueError(
            f"unsupported spec type for manifest: {type(spec).__name__} "
            "(expected FactorSpec or StrategySpec)"
        )

    return RunManifest(
        run_id=run_id,
        created_at=created_at,
        spec_fingerprint=canonical_fingerprint(spec.to_dict()),
        spec_kind=spec_kind,
        spec_schema_version=spec.schema_version,
        data_fingerprint=data_fingerprint,
        registry_version=registry_version,
        request_hash=canonical_fingerprint(request),
        sample_role=sample_role,
        input_refs=tuple(str(item) for item in input_refs),
    )
