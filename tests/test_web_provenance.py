"""Per-value provenance derivation tests (agent_sidecar_frontend.md §5.1, FE-L3).

Pure unit tests over ``quant_forge.apps.web.provenance`` -- no server, no
filesystem. Covers the WORKORDER P1 pin "missing badge = server-side
assertion failure" and the FE-L3 boundary (never trusting a client-supplied
provenance claim).
"""

from __future__ import annotations

import pytest

from quant_forge.apps.web.provenance import (
    CONFIRM_CARD_FIELDS,
    FACTOR_LEVEL_FIELDS,
    PARAMETER_FIELDS,
    PROVENANCE_SOURCES,
    MissingProvenanceError,
    ProvenanceEntry,
    assert_full_coverage,
    derive_confirm_provenance,
    provenance_by_field,
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


def test_confirm_card_fields_is_the_union_of_factor_level_and_parameter_fields() -> None:
    assert set(CONFIRM_CARD_FIELDS) == set(FACTOR_LEVEL_FIELDS) | set(PARAMETER_FIELDS)
    assert len(CONFIRM_CARD_FIELDS) == len(FACTOR_LEVEL_FIELDS) + len(PARAMETER_FIELDS)


def test_rule_parsed_factor_fields_are_fixed_policy() -> None:
    entries = derive_confirm_provenance(parser={"source": "rule", "provider": "rule", "model": "deterministic"}, factor=_factor(), parameters=_parameters())
    by_field = provenance_by_field(entries)
    for field_name in FACTOR_LEVEL_FIELDS:
        assert by_field[field_name].source == "fixed_policy", field_name


def test_llm_parsed_factor_fields_are_agent_inferred() -> None:
    entries = derive_confirm_provenance(parser={"source": "llm", "provider": "deepseek", "model": "x"}, factor=_factor(), parameters=_parameters())
    by_field = provenance_by_field(entries)
    for field_name in FACTOR_LEVEL_FIELDS:
        assert by_field[field_name].source == "agent_inferred", field_name


def test_unknown_parser_source_fails_loud_instead_of_mislabeling() -> None:
    with pytest.raises(MissingProvenanceError, match="no provenance mapping"):
        derive_confirm_provenance(parser={"source": "mystery"}, factor=_factor(), parameters=_parameters())


def test_configured_date_window_is_profile_default_unset_window_is_data_resolved() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        parameters=_parameters(evaluation_start="2025-01-01", evaluation_end=None),
    )
    by_field = provenance_by_field(entries)
    assert by_field["evaluation_start"].source == "profile_default"
    assert by_field["evaluation_end"].source == "data_resolved"
    assert by_field["backtest_start"].source == "data_resolved"


def test_simulation_costs_are_profile_default_when_unedited() -> None:
    entries = derive_confirm_provenance(parser={"source": "rule"}, factor=_factor(), parameters=_parameters(commission_bps=1.5))
    by_field = provenance_by_field(entries)
    assert by_field["commission_bps"].source == "profile_default"
    assert by_field["commission_bps"].value == 1.5


def test_an_edited_field_becomes_human_override_with_parent_value() -> None:
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        parameters=_parameters(holding_days=5),
        overrides={"holding_days": 20},
    )
    by_field = provenance_by_field(entries)
    entry = by_field["holding_days"]
    assert entry.source == "human_override"
    assert entry.value == 20
    assert entry.parent_value == 5
    # Untouched fields keep their original source, not human_override.
    assert by_field["decay_days"].source == "profile_default"


def test_an_override_equal_to_the_default_is_not_treated_as_an_edit() -> None:
    # A no-op round trip (client echoes back the same value) must not
    # manufacture a fake human_override -- provenance reflects VALUE change,
    # not request-body presence.
    entries = derive_confirm_provenance(
        parser={"source": "rule"},
        factor=_factor(),
        parameters=_parameters(holding_days=5),
        overrides={"holding_days": 5},
    )
    by_field = provenance_by_field(entries)
    assert by_field["holding_days"].source == "profile_default"
    assert by_field["holding_days"].parent_value is None


def test_no_overrides_means_every_parameter_keeps_its_original_source() -> None:
    entries = derive_confirm_provenance(parser={"source": "llm"}, factor=_factor(), parameters=_parameters())
    by_field = provenance_by_field(entries)
    assert all(entry.source != "human_override" for entry in entries)
    assert len(by_field) == len(CONFIRM_CARD_FIELDS)


def test_every_confirm_card_field_carries_exactly_one_badge() -> None:
    entries = derive_confirm_provenance(parser={"source": "rule"}, factor=_factor(), parameters=_parameters())
    fields = [entry.field for entry in entries]
    assert sorted(fields) == sorted(CONFIRM_CARD_FIELDS)
    assert len(fields) == len(set(fields))


def test_assert_full_coverage_raises_on_a_missing_field() -> None:
    entries = [ProvenanceEntry(field="formula", value="-rank(market_cap)", source="fixed_policy")]
    with pytest.raises(MissingProvenanceError, match="missing provenance badge"):
        assert_full_coverage(entries)


def test_assert_full_coverage_passes_on_full_coverage() -> None:
    entries = derive_confirm_provenance(parser={"source": "rule"}, factor=_factor(), parameters=_parameters())
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
        parameters=_parameters(),
    )
    by_field = provenance_by_field(entries)
    assert by_field["formula"].source == "fixed_policy"
