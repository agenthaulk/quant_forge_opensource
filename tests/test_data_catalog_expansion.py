"""CP5-2 / CP5-3 (owner decision D2, WAVE1 memo corrections): research metadata
tags are OUR designed, plain-data, catalog-driven schema (D7/D7a: no loader
hooks), and the field expansion path validates declared vs actually-available
fields with explicit statuses (FP-7), never silent booleans."""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.core.contracts import DataValidationResult, FactorDefinition
from quant_forge.data.local import (
    FIELD_AVAILABLE,
    FIELD_MISSING,
    FIELD_SYNTHESIZED,
    CatalogField,
    LocalPanelDataProvider,
    catalog_field_availability,
    create_demo_workspace,
    data_field_catalog,
    undeclared_panel_columns,
)
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.factor_library.research_tags import (
    RESEARCH_TAGS_SCHEMA_VERSION,
    ResearchTags,
    research_tags_for_operator,
)
from quant_forge.mcp.read_models import (
    list_available_operators,
    list_factor_research_tags,
    list_field_research_tags,
    list_operator_research_tags,
)


# ---------------------------------------------------------------------------
# CP5-3: declared vs actually-available validation
# ---------------------------------------------------------------------------


def test_demo_workspace_reports_every_declared_field_available(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    validation = LocalPanelDataProvider(paths["data_root"]).validate()

    availability = catalog_field_availability(validation)
    declared_non_key = [item.name for item in data_field_catalog() if item.role != "key"]
    assert [item.name for item in availability] == declared_non_key
    assert {item.status for item in availability} == {FIELD_AVAILABLE}


def test_availability_reports_missing_and_synthesized_with_statuses(tmp_path: Path) -> None:
    validation = DataValidationResult(
        data_root=tmp_path,
        ok=True,
        rows=10,
        instruments=2,
        date_count=5,
        panel_path=tmp_path / "panel.parquet",
        optional_columns=("return_1d", "return_5d", "volatility_5d"),
        synthesized_columns=("is_st", "market_cap"),
    )

    by_name = {item.name: item for item in catalog_field_availability(validation)}
    assert by_name["is_st"].status == FIELD_SYNTHESIZED
    assert by_name["market_cap"].status == FIELD_SYNTHESIZED
    assert by_name["close"].status == FIELD_AVAILABLE
    # Declared optional field the data root does not provide: missing, not
    # silently dropped (FP-7 statuses, FP-4 no fabricated availability).
    assert by_name["volume"].status == FIELD_MISSING
    assert by_name["return_1d"].status == FIELD_AVAILABLE


def test_newly_declared_field_reports_missing_until_data_is_provided(monkeypatch, tmp_path: Path) -> None:
    """The documented expansion path: declaring a field must not fabricate
    availability — the declared-vs-actual check reports it missing until the
    data root actually provides it."""

    import quant_forge.data.local as data_local

    extension = CatalogField(
        name="sentiment_score",
        description="Test-only extension field.",
        role="optional",
        tags=ResearchTags(subject_kind="field", subject_id="sentiment_score", provenance="catalog"),
    )
    monkeypatch.setattr(data_local, "PANEL_FIELD_CATALOG", data_field_catalog() + (extension,))

    paths = create_demo_workspace(tmp_path / "demo")
    validation = LocalPanelDataProvider(paths["data_root"]).validate()
    by_name = {item.name: item for item in catalog_field_availability(validation)}
    assert by_name["sentiment_score"].status == FIELD_MISSING
    assert by_name["close"].status == FIELD_AVAILABLE


def test_undeclared_panel_columns_flags_the_reverse_direction() -> None:
    declared = [item.name for item in data_field_catalog()]
    assert undeclared_panel_columns(declared) == ()
    assert undeclared_panel_columns([*declared, "mystery_column"]) == ("mystery_column",)


def test_catalog_field_rejects_unknown_roles_and_mismatched_tags() -> None:
    with pytest.raises(ValueError, match="invalid catalog field role"):
        CatalogField(
            name="bad_role",
            description="x",
            role="mandatory",
            tags=ResearchTags(subject_kind="field", subject_id="bad_role"),
        )
    with pytest.raises(ValueError, match="must describe the field itself"):
        CatalogField(
            name="close",
            description="x",
            role="optional",
            tags=ResearchTags(subject_kind="field", subject_id="volume"),
        )


# ---------------------------------------------------------------------------
# CP5-2: research metadata tags (OUR schema; plain data; FP-4 null-not-zero)
# ---------------------------------------------------------------------------


def test_research_tags_schema_is_validated() -> None:
    with pytest.raises(ValueError, match="subject_kind"):
        ResearchTags(subject_kind="dataset", subject_id="x")
    with pytest.raises(ValueError, match="subject_id"):
        ResearchTags(subject_kind="field", subject_id="  ")
    with pytest.raises(ValueError, match="provenance"):
        ResearchTags(subject_kind="field", subject_id="x", provenance="loader_hook")
    with pytest.raises(ValueError, match="schema_version"):
        ResearchTags(subject_kind="field", subject_id="x", schema_version="qf.research_tags.v0")
    with pytest.raises(ValueError, match="min_warmup_bars"):
        ResearchTags(subject_kind="field", subject_id="x", min_warmup_bars=0)


def test_research_tags_to_dict_keeps_null_not_zero() -> None:
    tags = ResearchTags(subject_kind="factor", subject_id="FTR_X", columns_required=None)
    payload = tags.to_dict()
    # Unobserved inputs stay None; observable-but-empty stays [] (FP-4).
    assert payload["columns_required"] is None
    assert payload["decay_horizon_days"] is None
    assert payload["min_warmup_bars"] is None
    assert payload["themes"] == []
    assert payload["schema_version"] == RESEARCH_TAGS_SCHEMA_VERSION

    observed = ResearchTags(subject_kind="field", subject_id="close", columns_required=())
    assert observed.to_dict()["columns_required"] == []


def test_field_tags_cover_the_advertised_surface() -> None:
    tags = list_field_research_tags()
    declared = [item.name for item in data_field_catalog() if item.role != "key"]
    assert sorted(tag["subject_id"] for tag in tags) == sorted(declared)
    assert {tag["subject_kind"] for tag in tags} == {"field"}
    assert {tag["provenance"] for tag in tags} == {"catalog"}
    by_id = {tag["subject_id"]: tag for tag in tags}
    # Warmup facts of the loader derivations, not estimates.
    assert by_id["return_5d"]["min_warmup_bars"] == 6
    assert by_id["return_5d"]["columns_required"] == ["close"]


def test_operator_tags_derive_from_the_canonical_registry() -> None:
    catalog = list_available_operators()
    tags = list_operator_research_tags()
    assert sorted(tag["subject_id"] for tag in tags) == sorted(entry["name"] for entry in catalog)
    assert {tag["subject_kind"] for tag in tags} == {"operator"}
    assert {tag["provenance"] for tag in tags} == {"derived"}
    by_id = {tag["subject_id"]: tag for tag in tags}
    rank = by_id["rank"]
    # Registry-backed facts only: category/family as themes, description as
    # notes; operators consume expressions, so columns_required is a
    # known-empty list; warmup is call-shape dependent, so it stays None (FP-4).
    entry = next(item for item in catalog if item["name"] == "rank")
    assert rank["themes"] == [entry["category"], entry["family"]]
    assert rank["columns_required"] == []
    assert rank["min_warmup_bars"] is None


def test_operator_tags_reject_nameless_entries() -> None:
    with pytest.raises(ValueError, match="no name"):
        research_tags_for_operator({"description": "orphan"})


def test_factor_tags_use_the_canonical_parser_and_null_for_precomputed(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    repo = FactorRepository(paths["factor_root"])
    repo.save(
        FactorDefinition(
            factor_id="FTR_PRECOMP",
            name="precomputed import",
            formula="precomputed:vendor_alpha_1",
            description="Externally computed factor values.",
        )
    )

    tags = {tag["subject_id"]: tag for tag in list_factor_research_tags(paths["factor_root"])}

    momentum = tags["FTR_DEMO_MOMENTUM"]
    assert momentum["subject_kind"] == "factor"
    assert momentum["provenance"] == "derived"
    assert momentum["columns_required"] == ["return_5d"]
    assert momentum["decay_horizon_days"] == 5

    small_cap = tags["FTR_DEMO_SMALL_CAP"]
    assert small_cap["columns_required"] == ["market_cap"]
    assert small_cap["universe_filters"] == ["is_st == false"]

    # Precomputed formulas live outside this repository: inputs are
    # unobservable, so they are None — never a fabricated empty list (FP-4).
    assert tags["FTR_PRECOMP"]["columns_required"] is None
