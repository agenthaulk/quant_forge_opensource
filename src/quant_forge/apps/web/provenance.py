"""Per-value provenance derivation for the confirm card (agent_sidecar_frontend.md §5.1, FE-L3).

Server-derived ONLY. Every function here reads the SERVER'S OWN parse-time
artifact (the ``parser`` / ``factor`` / ``parameters`` triple a completed
``parse_idea`` job produced, per :mod:`quant_forge.apps.web.pipeline`) and
confirm-time value fingerprints (comparing the frozen draft against any
user-submitted override); it never trusts a client-supplied provenance
claim.

FE-L3 names the anti-pattern explicitly: ``apps/web/api.py``'s
``_parser_payload_from_request`` echoes whatever ``parser`` dict the client's
HTTP body carries at validate time -- a client could claim
``{"source": "llm"}`` for a hand-typed formula and nothing on the old path
would catch it. This module never calls that function and never reads a
client-supplied ``source``/``provider``/``model`` field into a badge; the
only ``parser``/``factor`` values it ever sees are the ones
:mod:`quant_forge.apps.web.pipeline` captured directly from a completed
parse job's own stored result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "PROVENANCE_SOURCES",
    "ProvenanceEntry",
    "MissingProvenanceError",
    "FACTOR_LEVEL_FIELDS",
    "PARAMETER_FIELDS",
    "CONFIRM_CARD_FIELDS",
    "derive_confirm_provenance",
    "provenance_by_field",
    "assert_full_coverage",
]

# Closed 7-value vocabulary (spec §5.1). Stable identifiers; CN labels are
# resolved client-side (static/views/provenance.js), never here.
PROVENANCE_SOURCES: tuple[str, ...] = (
    "user_explicit",
    "user_answer",
    "profile_default",
    "fixed_policy",
    "data_resolved",
    "agent_inferred",
    "human_override",
)

# The confirm card's factor-level fields (formula + its supporting fields)
# and the 11 absorbed simulation/backtest parameters (former
# apps/web/html.py#validation-controls grid; see WORKORDER P1 减法). Every
# field in CONFIRM_CARD_FIELDS must appear in derive_confirm_provenance's
# output for every confirm-able pipeline -- assert_full_coverage enforces
# this (spec §11 ship gate #3 / WORKORDER pin: "missing badge = fail").
FACTOR_LEVEL_FIELDS: tuple[str, ...] = ("formula", "name", "description", "horizon_days", "universe_filters")
PARAMETER_FIELDS: tuple[str, ...] = (
    "holding_days",
    "decay_days",
    "top_quantile",
    "execution_delay_days",
    "evaluation_start",
    "evaluation_end",
    "backtest_start",
    "backtest_end",
    "commission_bps",
    "slippage_bps",
    "short_borrow_bps_annual",
)
CONFIRM_CARD_FIELDS: tuple[str, ...] = FACTOR_LEVEL_FIELDS + PARAMETER_FIELDS

# parser.source -> the source label for every factor-level field that came
# out of parsing. Deliberately closed (KeyError, not a silent default) --
# `parse_factor_idea` only ever returns "llm" or "rule" (llm_factor_parser.py);
# a third value showing up here means a new parser mode shipped without a
# provenance mapping decision, which must fail loud, not mislabel evidence.
_FACTOR_LEVEL_SOURCE_BY_PARSER: dict[str, str] = {
    "llm": "agent_inferred",
    "rule": "fixed_policy",
}

# parameters.* fields resolved from the research-loop profile when the
# profile carries an explicit value; the DATE-window fields fall back to
# "resolve from the available data range" when the profile leaves them
# unset, which is a data-plane resolution, not a static config value.
_DATA_RESOLVED_WHEN_UNSET: frozenset[str] = frozenset(
    {"evaluation_start", "evaluation_end", "backtest_start", "backtest_end"}
)


class MissingProvenanceError(ValueError):
    """A confirm-card field would render with no provenance badge.

    Spec §11 ship gate #3 / WORKORDER P1 pin: "missing badge = server-side
    assertion failure." Raised, never silently patched over -- a caller that
    hits this has a real gap in derive_confirm_provenance, not a degraded
    but safe state.
    """


@dataclass(frozen=True)
class ProvenanceEntry:
    field: str
    value: Any
    source: str
    parent_value: Any = None
    evidence_ref: str | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("provenance entry field is required")
        if self.source not in PROVENANCE_SOURCES:
            raise ValueError(f"invalid provenance source: {self.source!r} (expected one of {PROVENANCE_SOURCES})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "parent_value": self.parent_value,
            "evidence_ref": self.evidence_ref,
            "superseded_by": self.superseded_by,
        }


def _factor_level_entries(parser: dict[str, Any], factor: dict[str, Any]) -> list[ProvenanceEntry]:
    parser_source = str(parser.get("source", "")).strip().lower()
    try:
        source = _FACTOR_LEVEL_SOURCE_BY_PARSER[parser_source]
    except KeyError as exc:
        raise MissingProvenanceError(
            f"no provenance mapping for parser.source={parser_source!r}; "
            f"expected one of {sorted(_FACTOR_LEVEL_SOURCE_BY_PARSER)}"
        ) from exc
    return [
        ProvenanceEntry(field=field_name, value=factor.get(field_name), source=source)
        for field_name in FACTOR_LEVEL_FIELDS
    ]


def _parameter_default_source(field_name: str, value: Any) -> str:
    if field_name in _DATA_RESOLVED_WHEN_UNSET and (value is None or value == ""):
        return "data_resolved"
    return "profile_default"


def _parameter_entries(
    parameters: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> list[ProvenanceEntry]:
    overrides = overrides or {}
    entries: list[ProvenanceEntry] = []
    for field_name in PARAMETER_FIELDS:
        default_value = parameters.get(field_name)
        if field_name in overrides and overrides[field_name] != default_value:
            entries.append(
                ProvenanceEntry(
                    field=field_name,
                    value=overrides[field_name],
                    source="human_override",
                    parent_value=default_value,
                )
            )
        else:
            entries.append(
                ProvenanceEntry(
                    field=field_name,
                    value=default_value,
                    source=_parameter_default_source(field_name, default_value),
                )
            )
    return entries


def derive_confirm_provenance(
    *,
    parser: dict[str, Any],
    factor: dict[str, Any],
    parameters: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> tuple[ProvenanceEntry, ...]:
    """Per-value provenance for every confirm-card field (spec §5.1).

    ``parser`` / ``factor`` / ``parameters`` must be the SERVER'S OWN parse
    artifact (never a client-echoed claim -- FE-L3). ``overrides`` is the
    optional confirm-time user edit set: a field present there with a value
    that differs from ``parameters`` becomes ``human_override`` with
    ``parent_value`` set to the pre-edit default; an override equal to the
    default is not a real edit and keeps its original source (comparing
    VALUES, not presence, so a no-op round-trip never manufactures a fake
    override).
    """

    entries = _factor_level_entries(parser, factor) + _parameter_entries(parameters, overrides)
    assert_full_coverage(entries)
    return tuple(entries)


def provenance_by_field(entries: tuple[ProvenanceEntry, ...]) -> dict[str, ProvenanceEntry]:
    return {entry.field: entry for entry in entries}


def assert_full_coverage(entries: list[ProvenanceEntry] | tuple[ProvenanceEntry, ...]) -> None:
    """Fail loud if any CONFIRM_CARD_FIELDS field has no badge (WORKORDER P1 pin)."""

    covered = {entry.field for entry in entries}
    missing = [field_name for field_name in CONFIRM_CARD_FIELDS if field_name not in covered]
    if missing:
        raise MissingProvenanceError(f"missing provenance badge for confirm-card field(s): {missing}")
