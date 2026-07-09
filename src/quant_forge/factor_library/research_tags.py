"""Research metadata tags for catalog subjects (CP5-2, owner decision D2).

This is OUR designed schema, not a port. WAVE1_REVIEW_RESOLUTION.md (memo
corrections, item 1) verified that Vibe-Trading's real Alpha-Zoo schema is
``id/nickname/theme/formula_latex/columns_required/extras_required/
requires_sector/universe/frequency/decay_horizon/min_warmup_bars/notes`` and
that the memo's proposed fields do not exist there, so "ours must be designed,
not 'ported'". The schema below keeps the useful, observable subset of those
ideas and drops anything we cannot back with on-tree data.

Design constraints:

- Plain data only, catalog-driven (owner decision D7): tags are derived from
  the already-loaded data catalog, operator registry catalog, and factor
  definitions. No loader hooks; declarative extension manifests can later feed
  the same shape (``provenance="manifest"`` is reserved for them).
- FP-4: unobserved values stay ``None`` (or an empty tuple for genuinely
  known-empty collections). Nothing here invents directions, warmups, or
  failure modes that were never measured.
- FP-5: one tag schema for fields, operators, and factors; subjects differ
  only by ``subject_kind``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from quant_forge.core.contracts import FactorDefinition

RESEARCH_TAGS_SCHEMA_VERSION = "qf.research_tags.v1"

_SUBJECT_KINDS = ("field", "operator", "factor")
# "catalog": curated alongside the data catalog entry itself.
# "derived": computed from an authoritative on-tree source (operator registry
#            entry, registered factor definition).
# "manifest": reserved for declarative extension manifests (decision D7).
_PROVENANCES = ("catalog", "derived", "manifest")


@dataclass(frozen=True)
class ResearchTags:
    """Plain-data research metadata for one catalog subject.

    ``columns_required`` is ``None`` when the subject's inputs are not
    observable (for example precomputed factors, whose formulas live outside
    this repository); an empty tuple means "observably requires no panel
    columns". The distinction is deliberate (FP-4 null-not-zero).
    """

    subject_kind: str
    subject_id: str
    themes: tuple[str, ...] = ()
    columns_required: tuple[str, ...] | None = ()
    universe_filters: tuple[str, ...] = ()
    frequency: str | None = None
    decay_horizon_days: int | None = None
    min_warmup_bars: int | None = None
    notes: str | None = None
    provenance: str = "catalog"
    schema_version: str = RESEARCH_TAGS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.subject_kind not in _SUBJECT_KINDS:
            raise ValueError(f"subject_kind must be one of {_SUBJECT_KINDS}: {self.subject_kind}")
        if not self.subject_id.strip():
            raise ValueError("subject_id is required")
        if self.provenance not in _PROVENANCES:
            raise ValueError(f"provenance must be one of {_PROVENANCES}: {self.provenance}")
        if self.schema_version != RESEARCH_TAGS_SCHEMA_VERSION:
            raise ValueError(f"unsupported research tags schema_version: {self.schema_version}")
        for name, value in (
            ("decay_horizon_days", self.decay_horizon_days),
            ("min_warmup_bars", self.min_warmup_bars),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive or None: {value}")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe plain dict; tuples become lists, ``None`` stays ``None``."""

        payload = asdict(self)
        for key in ("themes", "universe_filters"):
            payload[key] = list(payload[key])
        if payload["columns_required"] is not None:
            payload["columns_required"] = list(payload["columns_required"])
        return payload


def research_tags_for_operator(entry: Mapping[str, Any]) -> ResearchTags:
    """Tags derived from one operator registry catalog entry.

    Only registry-backed facts are carried: name, category/family (as themes)
    and the registry description (as notes). Warmup depends on the concrete
    call shape (for example a window argument), so it stays ``None`` here
    rather than being guessed (FP-4). Operators consume expressions, not panel
    columns, so ``columns_required`` is a known-empty tuple.
    """

    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError("operator catalog entry has no name")
    themes = tuple(
        dict.fromkeys(
            str(entry[key]).strip()
            for key in ("category", "family")
            if str(entry.get(key) or "").strip()
        )
    )
    description = str(entry.get("description") or "").strip()
    return ResearchTags(
        subject_kind="operator",
        subject_id=name,
        themes=themes,
        columns_required=(),
        notes=description or None,
        provenance="derived",
    )


def research_tags_for_factor(
    factor: FactorDefinition,
    *,
    input_fields: tuple[str, ...] | None,
) -> ResearchTags:
    """Tags derived from one registered factor definition.

    ``input_fields`` is supplied by the caller (parsed with the same canonical
    formula parser the experiment planner uses — FP-5, one field-extraction
    definition). Pass ``None`` when inputs are not observable, for example for
    precomputed factors.

    ``decay_horizon_days`` semantics for factor subjects: the value carried is
    ``FactorDefinition.horizon_days`` — the holding/signal horizon — because no
    measured decay estimate exists on-tree and inventing one would violate
    FP-4. The key name is frozen by the ``qf.research_tags.v1`` payload
    contract; renderers must label the value as a horizon, not a decay
    parameter (integration finding F-009).
    """

    return ResearchTags(
        subject_kind="factor",
        subject_id=factor.factor_id,
        columns_required=tuple(input_fields) if input_fields is not None else None,
        universe_filters=tuple(factor.universe_filters),
        decay_horizon_days=int(factor.horizon_days) if factor.horizon_days else None,
        notes=factor.description.strip() or None,
        provenance="derived",
    )
