"""Per-value provenance derivation tests (agent_sidecar_frontend.md §5.1, FE-L3,
phase-review qf-fe-p1-review-20260714 Cluster B: F3, F4, F5).

Pure unit tests over ``quant_forge.apps.web.provenance`` -- no server, no
filesystem. Covers the WORKORDER P1 pin "missing badge = server-side
assertion failure", the FE-L3 boundary (never trusting a client-supplied
provenance claim), and the phase-review hardening:

- F3: each field's source comes from ITS OWN honest per-field rule, not a
  blanket parser-mode label -- rule-parser fields are not automatically
  fixed_policy, LLM fields are not automatically agent_inferred.
- F4: attribution compares against an IMMUTABLE baseline, using a value
  fingerprint rather than bare ``!=``.
- F5: a persisted provenance array's coverage/vocabulary/value-agreement is
  checked at load time, independent of how it was derived.
"""

from __future__ import annotations

import pytest

from quant_forge.apps.web.provenance import (
    CONFIRM_CARD_FIELDS,
    FACTOR_LEVEL_FIELDS,
    PARAMETER_FIELDS,
    PROVENANCE_SOURCES,
    InvalidProvenanceError,
    MissingProvenanceError,
    ProvenanceEntry,
    assert_full_coverage,
    assert_provenance_matches_current_values,
    derive_baseline_provenance,
    derive_current_provenance,
    provenance_by_field,
)


def derive_confirm_provenance(*, parser, factor, baseline_parameters, current_parameters=None, text=""):
    """Test-local composition of the two REAL derivation stages, exactly as
    apps/web/pipeline.py composes them (create computes the immutable
    baseline once; every render derives current badges from it -- re-verify
    RV-F8). Keeps the pre-rework characterization tests exercising the same
    production code paths through their original call shape."""

    baseline = derive_baseline_provenance(parser=parser, factor=factor, parameters=baseline_parameters, text=text)
    effective = current_parameters if current_parameters is not None else baseline_parameters
    return derive_current_provenance(
        baseline=tuple(entry.to_dict() for entry in baseline), factor=factor, current_parameters=effective
    )


def _factor(**overrides):
    base = {
        "factor_id": "FTR_ABCDEFGH",
        "name": "small_cap_non_st",
        "formula": "-rank(market_cap)",
        "description": "Small market-cap stocks receive higher scores.",
        "horizon_days": 5,
        "universe_filters": ["is_st == false"],
        "source": "idea",
        "status": "draft",
    }
    base.update(overrides)
    return base


def _parameters(**overrides):
    base = {
        "holding_days": 5,
        "decay_days": 0,
        "top_quantile": 0.3,
        "execution_delay_days": 1,
        "evaluation_start": None,
        "evaluation_end": None,
        "backtest_start": None,
        "backtest_end": None,
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "short_borrow_bps_annual": 0.0,
    }
    base.update(overrides)
    return base


IDEA_TEXT_WITH_NON_ST = "非ST的小市值股票未来表现更好"
IDEA_TEXT_WITHOUT_FILTER_PHRASE = "小市值股票未来表现更好"


def test_confirm_card_fields_is_the_union_of_factor_level_and_parameter_fields() -> None:
    assert set(CONFIRM_CARD_FIELDS) == set(FACTOR_LEVEL_FIELDS) | set(PARAMETER_FIELDS)
    assert len(CONFIRM_CARD_FIELDS) == len(FACTOR_LEVEL_FIELDS) + len(PARAMETER_FIELDS)


# ---------------------------------------------------------------------------
# F3: per-field, not per-mode
# ---------------------------------------------------------------------------


def test_horizon_days_is_fixed_policy_under_rule_and_agent_inferred_under_llm() -> None:
    # Verified fact, not a mode-based guess: the rule parser hardcodes
    # horizon_days=5 unconditionally; the LLM parser actually reads the text.
    rule_entries = derive_confirm_provenance(
        parser={"source": "rule"}, factor=_factor(), baseline_parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
    )
    assert provenance_by_field(rule_entries)["horizon_days"].source == "fixed_policy"

    llm_entries = derive_confirm_provenance(
        parser={"source": "llm"}, factor=_factor(), baseline_parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
    )
    assert provenance_by_field(llm_entries)["horizon_days"].source == "agent_inferred"


def test_formula_name_description_are_agent_inferred_in_both_modes() -> None:
    # phase-review F3: rule-parser fields are NOT automatically fixed_policy
    # -- the rule table's keyword-bucket formula selection is itself an
    # automated interpretation of free text, same as the LLM's generation.
    for source in ("rule", "llm"):
        entries = derive_confirm_provenance(
            parser={"source": source}, factor=_factor(), baseline_parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
        )
        by_field = provenance_by_field(entries)
        assert by_field["formula"].source == "agent_inferred", source
        assert by_field["name"].source == "agent_inferred", source
        assert by_field["description"].source == "agent_inferred", source


def test_universe_filters_is_user_explicit_when_the_idea_text_names_the_filter() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"}, factor=_factor(), baseline_parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
    )
    assert provenance_by_field(entries)["universe_filters"].source == "user_explicit"


def test_universe_filters_is_fixed_policy_under_rule_without_a_matching_phrase() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(universe_filters=[]),
        baseline_parameters=_parameters(),
        text=IDEA_TEXT_WITHOUT_FILTER_PHRASE,
    )
    assert provenance_by_field(entries)["universe_filters"].source == "fixed_policy"


def test_universe_filters_is_agent_inferred_under_llm_without_a_matching_phrase() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "llm"},
        factor=_factor(universe_filters=["is_st == false"]),
        baseline_parameters=_parameters(),
        text=IDEA_TEXT_WITHOUT_FILTER_PHRASE,
    )
    assert provenance_by_field(entries)["universe_filters"].source == "agent_inferred"


def test_universe_filters_english_non_st_phrasing_is_also_recognized() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=_parameters(),
        text="non-ST small-cap stocks outperform",
    )
    assert provenance_by_field(entries)["universe_filters"].source == "user_explicit"


def test_unknown_parser_source_fails_loud_instead_of_mislabeling() -> None:
    with pytest.raises(MissingProvenanceError, match="provenance mapping"):
        derive_confirm_provenance(parser={"source": "mystery"}, factor=_factor(), baseline_parameters=_parameters())


def test_unset_date_window_is_fixed_policy_not_data_resolved() -> None:
    # phase-review F3: data_resolved is reserved for a CONCRETELY resolved
    # value with evidence -- an unset date is not resolved at all, so "leave
    # it unset, let compute resolve it" is itself a fixed platform policy.
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=_parameters(evaluation_start=None, evaluation_end=None),
    )
    by_field = provenance_by_field(entries)
    assert by_field["evaluation_start"].source == "fixed_policy"
    assert by_field["evaluation_end"].source == "fixed_policy"
    assert by_field["backtest_start"].source == "fixed_policy"


def test_configured_date_window_is_profile_default() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=_parameters(evaluation_start="2025-01-01"),
    )
    by_field = provenance_by_field(entries)
    assert by_field["evaluation_start"].source == "profile_default"


def test_simulation_costs_are_profile_default_when_unedited() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"}, factor=_factor(), baseline_parameters=_parameters(commission_bps=1.5)
    )
    by_field = provenance_by_field(entries)
    assert by_field["commission_bps"].source == "profile_default"
    assert by_field["commission_bps"].value == 1.5


# ---------------------------------------------------------------------------
# F4: immutable baseline + value-fingerprint comparison
# ---------------------------------------------------------------------------


def test_a_field_that_differs_from_baseline_becomes_human_override_with_parent_value() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=_parameters(holding_days=5),
        current_parameters=_parameters(holding_days=20),
    )
    by_field = provenance_by_field(entries)
    entry = by_field["holding_days"]
    assert entry.source == "human_override"
    assert entry.value == 20
    assert entry.parent_value == 5
    # Untouched fields keep their original source, not human_override.
    assert by_field["decay_days"].source == "profile_default"


def test_current_equal_to_baseline_is_not_treated_as_an_override() -> None:
    # A no-op round trip (client echoes back the same value) must not
    # manufacture a fake human_override -- provenance reflects VALUE change,
    # not request-body presence.
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=_parameters(holding_days=5),
        current_parameters=_parameters(holding_days=5),
    )
    by_field = provenance_by_field(entries)
    assert by_field["holding_days"].source == "profile_default"
    assert by_field["holding_days"].parent_value is None


def test_omitting_current_parameters_means_current_equals_baseline() -> None:
    entries = derive_confirm_provenance(parser={"source": "llm"}, factor=_factor(), baseline_parameters=_parameters())
    by_field = provenance_by_field(entries)
    assert all(entry.source != "human_override" for entry in entries)
    assert len(by_field) == len(CONFIRM_CARD_FIELDS)


def test_editing_one_field_never_moves_a_different_fields_badge_or_parent_value() -> None:
    # phase-review F4's core regression: comparing against a MUTABLE
    # "current" baseline (instead of the fixed original) would let editing
    # field B accidentally change field A's badge. Simulates two sequential
    # edits by re-deriving with the SAME fixed baseline each time (exactly
    # how apps/web/pipeline.py calls this -- baseline_parameters is always
    # original_parameters, never the previous call's current_parameters).
    baseline = _parameters(holding_days=5, decay_days=0)

    after_first_edit = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=baseline,
        current_parameters=_parameters(holding_days=9, decay_days=0),
    )
    by_field_1 = provenance_by_field(after_first_edit)
    assert by_field_1["holding_days"].source == "human_override"
    assert by_field_1["holding_days"].parent_value == 5

    after_second_edit = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=baseline,  # SAME fixed baseline, not the first edit's current
        current_parameters=_parameters(holding_days=9, decay_days=3),
    )
    by_field_2 = provenance_by_field(after_second_edit)
    assert by_field_2["holding_days"].source == "human_override"
    assert by_field_2["holding_days"].parent_value == 5  # unchanged by decay_days's edit
    assert by_field_2["holding_days"].value == 9
    assert by_field_2["decay_days"].source == "human_override"
    assert by_field_2["decay_days"].parent_value == 0


def test_value_fingerprint_comparison_is_not_fooled_by_key_order() -> None:
    # F4: uses canonical_fingerprint, not a bare !=, so equivalent structures
    # that merely differ in key order/representation are correctly seen as
    # UNCHANGED (a naive dict `!=` could disagree depending on how each side
    # was constructed for a nested/list-shaped parameter value).
    baseline = _parameters(evaluation_start="2025-01-01")
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        baseline_parameters=baseline,
        current_parameters=dict(baseline),  # a fresh dict, same content
    )
    by_field = provenance_by_field(entries)
    assert by_field["evaluation_start"].source != "human_override"


def test_every_confirm_card_field_carries_exactly_one_badge() -> None:
    entries = derive_confirm_provenance(parser={"source": "rule"}, factor=_factor(), baseline_parameters=_parameters())
    fields = [entry.field for entry in entries]
    assert sorted(fields) == sorted(CONFIRM_CARD_FIELDS)
    assert len(fields) == len(set(fields))


def test_assert_full_coverage_raises_on_a_missing_field() -> None:
    entries = [ProvenanceEntry(field="formula", value="-rank(market_cap)", source="fixed_policy")]
    with pytest.raises(MissingProvenanceError, match="missing provenance badge"):
        assert_full_coverage(entries)


def test_assert_full_coverage_passes_on_full_coverage() -> None:
    entries = derive_confirm_provenance(parser={"source": "rule"}, factor=_factor(), baseline_parameters=_parameters())
    assert_full_coverage(entries)  # must not raise


def test_provenance_entry_rejects_an_unknown_source() -> None:
    with pytest.raises(ValueError, match="invalid provenance source"):
        ProvenanceEntry(field="formula", value="x", source="not_a_real_source")


def test_provenance_sources_is_the_closed_seven_value_vocabulary() -> None:
    assert PROVENANCE_SOURCES == (
        "user_explicit",
        "user_answer",
        "profile_default",
        "fixed_policy",
        "data_resolved",
        "agent_inferred",
        "human_override",
    )


def test_derive_confirm_provenance_never_reads_a_client_side_source_field() -> None:
    # FE-L3 boundary regression: even if a caller accidentally passes a
    # `factor` dict carrying its OWN "source" key (e.g. FactorDefinition's
    # `source` field, which is unrelated to parser provenance), the badge
    # source comes only from the `parser` argument's `source`, never from
    # anything inside `factor`/`parameters`.
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(source="llm"),  # a decoy, unrelated field on the factor dict
        baseline_parameters=_parameters(),
    )
    by_field = provenance_by_field(entries)
    assert by_field["formula"].source == "agent_inferred"  # from parser.source="rule" via the text-interpreted rule


# ---------------------------------------------------------------------------
# F5: load-time assertion that a persisted provenance array is genuine
# ---------------------------------------------------------------------------


def _valid_entries_as_dicts() -> tuple[dict, ...]:
    entries = derive_confirm_provenance(
        parser={"source": "rule"}, factor=_factor(), baseline_parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
    )
    return tuple(entry.to_dict() for entry in entries)


def test_assert_provenance_matches_current_values_passes_for_a_genuine_array() -> None:
    entries = _valid_entries_as_dicts()
    assert_provenance_matches_current_values(entries, factor=_factor(), parameters=_parameters())  # must not raise


def test_assert_provenance_matches_current_values_raises_on_duplicate_field() -> None:
    entries = _valid_entries_as_dicts()
    duplicated = entries + (entries[0],)
    with pytest.raises(InvalidProvenanceError, match="duplicate provenance field"):
        assert_provenance_matches_current_values(duplicated, factor=_factor(), parameters=_parameters())


def test_assert_provenance_matches_current_values_raises_on_missing_field() -> None:
    entries = _valid_entries_as_dicts()
    truncated = entries[1:]
    with pytest.raises(InvalidProvenanceError, match="missing provenance badge"):
        assert_provenance_matches_current_values(truncated, factor=_factor(), parameters=_parameters())


def test_assert_provenance_matches_current_values_raises_on_unknown_field() -> None:
    entries = _valid_entries_as_dicts()
    extra = entries + ({"field": "not_a_real_field", "value": 1, "source": "fixed_policy"},)
    with pytest.raises(InvalidProvenanceError, match="unknown field"):
        assert_provenance_matches_current_values(extra, factor=_factor(), parameters=_parameters())


def test_assert_provenance_matches_current_values_raises_on_invalid_source() -> None:
    entries = tuple(
        {**entry, "source": "not_a_real_source"} if entry["field"] == "formula" else entry
        for entry in _valid_entries_as_dicts()
    )
    with pytest.raises(InvalidProvenanceError, match="invalid provenance source"):
        assert_provenance_matches_current_values(entries, factor=_factor(), parameters=_parameters())


def test_assert_provenance_matches_current_values_raises_on_stale_value() -> None:
    entries = _valid_entries_as_dicts()
    with pytest.raises(InvalidProvenanceError, match="stale provenance"):
        # The array describes holding_days=5 (from _parameters()'s default);
        # claiming the record currently holds 999 must be caught.
        assert_provenance_matches_current_values(entries, factor=_factor(), parameters=_parameters(holding_days=999))


# ---------------------------------------------------------------------------
# Re-verify RV-F7: declared fields missing from an artifact RAISE -- never a
# silent None badge with a defaulted origin.
# ---------------------------------------------------------------------------


def test_baseline_raises_when_a_declared_factor_field_is_absent() -> None:
    factor = _factor()
    del factor["description"]
    with pytest.raises(MissingProvenanceError, match="description"):
        derive_baseline_provenance(
            parser={"source": "llm"}, factor=factor, parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
        )


def test_baseline_raises_when_a_declared_parameter_is_absent() -> None:
    parameters = _parameters()
    del parameters["holding_days"]
    with pytest.raises(MissingProvenanceError, match="holding_days"):
        derive_baseline_provenance(
            parser={"source": "llm"}, factor=_factor(), parameters=parameters, text=IDEA_TEXT_WITH_NON_ST
        )


def test_current_raises_when_baseline_artifact_lacks_a_declared_field() -> None:
    baseline = tuple(
        entry.to_dict()
        for entry in derive_baseline_provenance(
            parser={"source": "llm"}, factor=_factor(), parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
        )
    )
    truncated = tuple(entry for entry in baseline if entry["field"] != "formula")
    with pytest.raises(MissingProvenanceError, match="formula"):
        derive_current_provenance(baseline=truncated, factor=_factor(), current_parameters=_parameters())


def test_current_raises_when_current_parameters_lack_a_declared_field() -> None:
    baseline = tuple(
        entry.to_dict()
        for entry in derive_baseline_provenance(
            parser={"source": "llm"}, factor=_factor(), parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
        )
    )
    current = _parameters()
    del current["decay_days"]
    with pytest.raises(MissingProvenanceError, match="decay_days"):
        derive_current_provenance(baseline=baseline, factor=_factor(), current_parameters=current)


# ---------------------------------------------------------------------------
# Re-verify RV-F8: current badges are a pure function of (persisted baseline,
# current values) -- no parser mode, no idea text, so a restart cannot move
# a single badge.
# ---------------------------------------------------------------------------


def test_current_badges_survive_a_restart_without_the_idea_text() -> None:
    # The sharp case the re-verify demonstrated live: universe_filters earns
    # user_explicit from the idea text at BASELINE time; after a restart the
    # text no longer exists anywhere volatile, and the badge must not decay
    # to fixed_policy. derive_current_provenance never sees text at all, so
    # the property holds structurally -- this pins it.
    baseline = tuple(
        entry.to_dict()
        for entry in derive_baseline_provenance(
            parser={"source": "rule"}, factor=_factor(), parameters=_parameters(), text=IDEA_TEXT_WITH_NON_ST
        )
    )
    entries = derive_current_provenance(
        baseline=baseline, factor=_factor(), current_parameters=_parameters(decay_days=3)
    )
    by_field = provenance_by_field(entries)
    assert by_field["universe_filters"].source == "user_explicit"  # unchanged field keeps its baseline origin
    assert by_field["decay_days"].source == "human_override"  # only the edited field moves
    assert by_field["decay_days"].parent_value == 0


def test_current_preserves_an_inherited_human_override_baseline_entry() -> None:
    # Fork semantics: a fork's baseline may carry human_override entries
    # inherited from the parent's run. Unchanged values keep that origin AND
    # its parent_value; a further edit overrides against the FORK baseline.
    baseline = tuple(
        entry.to_dict()
        for entry in derive_baseline_provenance(
            parser={"source": "llm"}, factor=_factor(), parameters=_parameters(), text=""
        )
    )
    inherited = tuple(
        {**entry, "source": "human_override", "value": 9, "parent_value": 5}
        if entry["field"] == "holding_days"
        else entry
        for entry in baseline
    )
    unchanged = derive_current_provenance(
        baseline=inherited, factor=_factor(), current_parameters=_parameters(holding_days=9)
    )
    entry = provenance_by_field(unchanged)["holding_days"]
    assert entry.source == "human_override"
    assert entry.parent_value == 5

    edited = derive_current_provenance(
        baseline=inherited, factor=_factor(), current_parameters=_parameters(holding_days=11)
    )
    entry = provenance_by_field(edited)["holding_days"]
    assert entry.source == "human_override"
    assert entry.parent_value == 9  # overridden against the fork baseline value
