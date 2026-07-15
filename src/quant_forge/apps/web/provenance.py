"""Per-value provenance derivation for the confirm card (agent_sidecar_frontend.md §5.1, FE-L3).

Server-derived ONLY. Every function here reads the SERVER'S OWN parse-time
artifact (the ``parser`` / ``factor`` / ``text`` a completed ``parse_idea``
job produced, per :mod:`quant_forge.apps.web.pipeline`) and an IMMUTABLE
parse-time parameter baseline; it never trusts a client-supplied provenance
claim.

FE-L3 names the anti-pattern explicitly: ``apps/web/api.py``'s
``_parser_payload_from_request`` echoes whatever ``parser`` dict the client's
HTTP body carries at validate time -- a client could claim
``{"source": "llm"}`` for a hand-typed formula and nothing on the old path
would catch it. This module never calls that function and never reads a
client-supplied ``source``/``provider``/``model`` field into a badge; the
only ``parser``/``factor``/``text`` values it ever sees are the ones
:mod:`quant_forge.apps.web.pipeline` captured directly from a completed
parse job's own stored result.

Phase-review hardening (F3/F4/F5/F11), replacing the earlier blanket
"parser.source implies every factor-level field's source" design:

* **Per-field, not per-mode (F3).** Each field's source is decided by its
  OWN small function, using whatever is honestly observable about THAT
  field -- not one branch that stamps every factor-level field with the
  same label because the parser happened to run in a given mode.
  ``horizon_days`` really is unconditionally fixed by the rule parser
  (``factor_library/repository.py::parse_idea_to_definition`` hardcodes
  ``horizon_days=5``, never reading the text) so ``fixed_policy`` there is
  a verified fact, not a mode-based guess; ``universe_filters`` is
  evaluated against the ACTUAL idea text independently of ``formula``.
  ``data_resolved`` is never emitted for an unset/``None`` value (a
  genuinely UNRESOLVED value, not a resolved one) -- P1 has no field that
  is filled in from a concretely-observed data-plane value before compute
  runs, so the vocabulary slot exists but nothing in this build emits it
  yet; a later phase that resolves a real evidence-backed value (e.g. an
  auto-detected data start date) can start emitting it without a schema
  change.
* **Immutable baseline (F4).** Attribution compares the CURRENT effective
  value against ``original_parameters`` -- the parse-time snapshot
  ``apps/web/pipeline.py`` captures ONCE and never mutates -- never against
  the mutable, currently-saved ``parameters``. Editing ``decay_days`` can
  therefore never make an untouched ``holding_days`` flip back from
  ``human_override`` to ``profile_default``: every field's comparison is
  independent and against a fixed point in time.
* **Value-fingerprint comparison.** Uses
  ``specs.run_manifest.canonical_fingerprint`` (already the project's one
  canonical-hash helper) rather than a bare ``!=``, so the "did this change"
  check is robust to key-order/type-shape differences, not just accidental
  Python equality.
* **Load-time assertion (F5).** ``assert_provenance_matches_current_values``
  re-derives nothing but checks a persisted provenance array actually
  agrees with the record it is attached to (full coverage, no duplicate
  fields, closed vocabulary, and each entry's stored value equal to the
  field it claims to describe) -- called from
  ``apps/web/pipeline.py::PipelineStore.load`` so a GET can never silently
  serve a confirm card with stale or missing badges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quant_forge.specs.run_manifest import canonical_fingerprint

__all__ = [
    "PROVENANCE_SOURCES",
    "ProvenanceEntry",
    "MissingProvenanceError",
    "InvalidProvenanceError",
    "FACTOR_LEVEL_FIELDS",
    "PARAMETER_FIELDS",
    "CONFIRM_CARD_FIELDS",
    "derive_baseline_provenance",
    "derive_current_provenance",
    "provenance_by_field",
    "assert_full_coverage",
    "assert_provenance_matches_current_values",
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
# field in CONFIRM_CARD_FIELDS must appear in every derivation's output
# (baseline AND current) for every confirm-able pipeline --
# assert_full_coverage enforces this (spec §11 ship gate #3 / WORKORDER
# pin: "missing badge = fail").
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

# Recognizable non-ST-exclusion phrasing, mirroring the ONE existing
# predicate in factor_library/repository.py::parse_idea_to_definition
# ("非st" / "non-st" / "non st"). Kept as a narrow, read-only, display-only
# re-check (never feeds computation) so `universe_filters` can be attributed
# to the user's own words instead of a blanket parser-mode label; if the
# rule parser's own keyword list ever changes, this can drift out of exact
# sync -- acceptable because it only ever affects a badge, never a value.
_NON_ST_PHRASES: tuple[str, ...] = ("非st", "non-st", "non st")

_DATE_FIELDS: frozenset[str] = frozenset({"evaluation_start", "evaluation_end", "backtest_start", "backtest_end"})


class MissingProvenanceError(ValueError):
    """A confirm-card field would render with no provenance badge.

    Spec §11 ship gate #3 / WORKORDER P1 pin: "missing badge = server-side
    assertion failure." Raised, never silently patched over -- a caller that
    hits this has a real gap in its provenance derivation or a malformed
    artifact (re-verify RV-F7: a declared field ABSENT from an artifact is
    this error, never a silent ``None`` badge), not a degraded but safe
    state.
    """


class InvalidProvenanceError(ValueError):
    """A persisted provenance array disagrees with the record it describes.

    Phase-review F5: raised by ``assert_provenance_matches_current_values``
    on duplicate fields, an out-of-vocabulary source, or a stored value that
    no longer matches the field it claims to describe -- a GET must never
    serve a confirm card whose badges could be lying about the numbers next
    to them.
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


def _fingerprint(value: Any) -> str:
    return canonical_fingerprint({"value": value})


def _text_contains_non_st_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _NON_ST_PHRASES)


def _origin_for_horizon_days(parser_source: str) -> str:
    # Verified against the actual parser bodies, not assumed: the rule
    # parser (factor_library/repository.py::parse_idea_to_definition) always
    # hardcodes horizon_days=5, never reading the idea text at all, so
    # fixed_policy is a checked fact for that mode specifically. The LLM
    # parser's own _normalize_horizon_days DOES read the text (e.g. "one
    # month" -> 21), so agent_inferred is the honest label there.
    if parser_source == "rule":
        return "fixed_policy"
    if parser_source == "llm":
        return "agent_inferred"
    raise MissingProvenanceError(
        f"no horizon_days provenance mapping for parser.source={parser_source!r}; expected 'rule' or 'llm'"
    )


def _origin_for_universe_filters(parser_source: str, text: str, filters: Any) -> str:
    has_filters = bool(filters)
    if has_filters and _text_contains_non_st_phrase(text):
        # The user's own words are independently checkable evidence for
        # exactly this filter -- the closest fit in the 7-value vocabulary
        # for "a direct, unambiguous phrase in what the user typed drove
        # this value", regardless of which parser mode read it.
        return "user_explicit"
    if parser_source == "rule":
        # The rule table's only filter is the non-ST exclusion; if it fired
        # without a matching phrase (structurally shouldn't happen) or did
        # not fire, that is the table's fixed (text-independent) behavior.
        return "fixed_policy"
    if parser_source == "llm":
        return "agent_inferred"
    raise MissingProvenanceError(
        f"no universe_filters provenance mapping for parser.source={parser_source!r}; expected 'rule' or 'llm'"
    )


def _origin_for_text_interpreted_field(parser_source: str) -> str:
    # formula / name / description: in BOTH modes these are an automated
    # interpreter's reading of free text into a specific structured choice
    # (the rule parser's fixed keyword-bucket table, or the LLM's
    # generation) -- "agent_inferred" is used for either, since neither is a
    # text-INDEPENDENT default (ruling out fixed_policy) nor a single
    # checkable phrase driving one specific value (ruling out
    # user_explicit, which is reserved for cases like the non-ST filter
    # above where one narrow phrase maps to one narrow value).
    if parser_source not in {"rule", "llm"}:
        raise MissingProvenanceError(
            f"no provenance mapping for parser.source={parser_source!r}; expected 'rule' or 'llm'"
        )
    return "agent_inferred"


def _require_field(container: dict[str, Any], field_name: str, *, artifact: str) -> Any:
    """Presence-checked read (re-verify RV-F7): a DECLARED confirm-card field
    missing from its artifact is a malformed artifact and must raise, never
    silently badge ``None`` with a default origin. ``None`` remains a legal
    VALUE (e.g. an unset date field) -- only key ABSENCE is an error."""

    if field_name not in container:
        raise MissingProvenanceError(f"declared confirm-card field {field_name!r} is missing from the {artifact}")
    return container[field_name]


def _factor_level_entries(parser: dict[str, Any], factor: dict[str, Any], text: str) -> list[ProvenanceEntry]:
    parser_source = str(parser.get("source", "")).strip().lower()
    entries = []
    for field_name in FACTOR_LEVEL_FIELDS:
        value = _require_field(factor, field_name, artifact="parse-time factor artifact")
        if field_name == "horizon_days":
            source = _origin_for_horizon_days(parser_source)
        elif field_name == "universe_filters":
            source = _origin_for_universe_filters(parser_source, text, value)
        else:
            source = _origin_for_text_interpreted_field(parser_source)
        entries.append(ProvenanceEntry(field=field_name, value=value, source=source))
    return entries


def _parameter_default_source(field_name: str, value: Any) -> str:
    # data_resolved is reserved for a CONCRETELY resolved value with
    # evidence (phase-review F3); an unset date field is not resolved at
    # all yet -- "leave it unset, let compute resolve it from the available
    # data range" is itself a fixed platform policy, so it lands there
    # instead.
    if field_name in _DATE_FIELDS and (value is None or value == ""):
        return "fixed_policy"
    return "profile_default"


def _baseline_parameter_entries(parameters: dict[str, Any]) -> list[ProvenanceEntry]:
    entries: list[ProvenanceEntry] = []
    for field_name in PARAMETER_FIELDS:
        value = _require_field(parameters, field_name, artifact="parse-time parameter baseline")
        entries.append(
            ProvenanceEntry(field=field_name, value=value, source=_parameter_default_source(field_name, value))
        )
    return entries


def derive_baseline_provenance(
    *,
    parser: dict[str, Any],
    factor: dict[str, Any],
    parameters: dict[str, Any],
    text: str,
) -> tuple[ProvenanceEntry, ...]:
    """The IMMUTABLE per-field origin artifact, computed ONCE at record
    creation (re-verify RV-F8).

    ``parser`` / ``factor`` / ``parameters`` / ``text`` must be the SERVER'S
    OWN parse artifact (never a client-echoed claim -- FE-L3). This is the
    only function that reads the idea ``text`` or the parser mode: it runs
    exactly when both are guaranteed available (the parse just completed),
    and its output is persisted on the record so no later derivation ever
    depends on volatile job-manager memory again. Every declared
    confirm-card field must be present in its artifact -- absence raises
    ``MissingProvenanceError`` (RV-F7), never a silent ``None`` badge.
    """

    entries = _factor_level_entries(parser, factor, text) + _baseline_parameter_entries(parameters)
    assert_full_coverage(entries)
    return tuple(entries)


def derive_current_provenance(
    *,
    baseline: tuple[dict[str, Any], ...],
    factor: dict[str, Any],
    current_parameters: dict[str, Any],
) -> tuple[ProvenanceEntry, ...]:
    """Current badges as a PURE function of (persisted baseline, current
    values) -- re-verify RV-F8.

    A field whose current value's fingerprint equals its baseline entry's
    keeps the baseline's origin verbatim (including an inherited
    ``human_override`` + ``parent_value`` from a fork baseline); a differing
    fingerprint is ``human_override`` with ``parent_value`` set to the
    baseline value. Restarts cannot move any badge, because nothing here
    reads parser mode, idea text, or any other re-derivable input. A
    declared field missing from the baseline artifact or from the current
    values raises ``MissingProvenanceError`` (RV-F7).
    """

    baseline_by_field = {str(entry.get("field", "")): entry for entry in baseline}
    entries: list[ProvenanceEntry] = []
    for field_name in CONFIRM_CARD_FIELDS:
        baseline_entry = baseline_by_field.get(field_name)
        if baseline_entry is None:
            raise MissingProvenanceError(
                f"declared confirm-card field {field_name!r} is missing from the baseline provenance artifact"
            )
        if field_name in FACTOR_LEVEL_FIELDS:
            current_value = _require_field(factor, field_name, artifact="factor artifact")
        else:
            current_value = _require_field(current_parameters, field_name, artifact="current parameters")
        baseline_value = baseline_entry.get("value")
        if _fingerprint(current_value) != _fingerprint(baseline_value):
            entries.append(
                ProvenanceEntry(
                    field=field_name,
                    value=current_value,
                    source="human_override",
                    parent_value=baseline_value,
                )
            )
        else:
            entries.append(
                ProvenanceEntry(
                    field=field_name,
                    value=current_value,
                    source=str(baseline_entry.get("source", "")),
                    parent_value=baseline_entry.get("parent_value"),
                )
            )
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


def assert_provenance_matches_current_values(
    entries: tuple[dict[str, Any], ...],
    *,
    factor: dict[str, Any],
    parameters: dict[str, Any],
) -> None:
    """Load-time guard (phase-review F5): a persisted provenance array must
    genuinely describe the record it is attached to.

    Checks, in order: exact field coverage (no missing, no unknown extras),
    no duplicate fields, every source is in the closed vocabulary, and every
    entry's stored ``value`` still equals the CURRENT value of the field it
    names. Called from ``apps/web/pipeline.py::PipelineStore.load`` -- a GET
    must never serve a confirm card whose badges have silently gone stale
    relative to the values they are attached to.
    """

    fields = [str(entry.get("field", "")) for entry in entries]
    if len(fields) != len(set(fields)):
        duplicates = sorted({name for name in fields if fields.count(name) > 1})
        raise InvalidProvenanceError(f"duplicate provenance field(s): {duplicates}")
    covered = set(fields)
    expected = set(CONFIRM_CARD_FIELDS)
    missing = expected - covered
    if missing:
        raise InvalidProvenanceError(f"missing provenance badge for confirm-card field(s): {sorted(missing)}")
    unknown = covered - expected
    if unknown:
        raise InvalidProvenanceError(f"provenance entry for unknown field(s): {sorted(unknown)}")

    current_by_field: dict[str, Any] = {name: factor.get(name) for name in FACTOR_LEVEL_FIELDS}
    current_by_field.update({name: parameters.get(name) for name in PARAMETER_FIELDS})

    for entry in entries:
        field_name = str(entry.get("field", ""))
        source = entry.get("source")
        if source not in PROVENANCE_SOURCES:
            raise InvalidProvenanceError(f"invalid provenance source for field {field_name!r}: {source!r}")
        stored_value = entry.get("value")
        actual_value = current_by_field.get(field_name)
        if _fingerprint(stored_value) != _fingerprint(actual_value):
            raise InvalidProvenanceError(
                f"stale provenance for field {field_name!r}: badge describes {stored_value!r}, "
                f"record currently holds {actual_value!r}"
            )
