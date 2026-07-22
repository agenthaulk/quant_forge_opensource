"""CP5-1 / CP5-4 (owner decision D2): the advertised field surface and the
specs ValidationGate consult the actually-loaded data catalog — one authority
(FP-5), not a static copy that can drift."""

from __future__ import annotations

from pathlib import Path

import quant_forge.data.local as data_local
from quant_forge.data.local import (
    CatalogField,
    LocalPanelDataProvider,
    create_demo_workspace,
    data_field_catalog,
    undeclared_panel_columns,
)
from quant_forge.factor_library.research_tags import ResearchTags
from quant_forge.mcp.read_models import list_available_fields, list_field_research_tags
from quant_forge.specs.factor_spec import FactorSpec
from quant_forge.specs.validation_gate import validate_factor_spec


def _extra_field(name: str) -> CatalogField:
    return CatalogField(
        name=name,
        description="Test-only extension field.",
        role="optional",
        tags=ResearchTags(subject_kind="field", subject_id=name, provenance="catalog"),
    )


def test_advertised_fields_derive_from_the_loaded_catalog() -> None:
    advertised = {entry["name"] for entry in list_available_fields()}
    declared = {item.name for item in data_field_catalog() if item.role != "key"}
    assert advertised == declared
    # Key columns are schema plumbing, not factor inputs.
    assert "trade_date" not in advertised
    assert "instrument" not in advertised


def test_field_payload_shape_is_unchanged_for_web_and_cli() -> None:
    entries = list_available_fields()
    assert entries == sorted(entries, key=lambda entry: entry["name"])
    for entry in entries:
        assert set(entry) == {"name", "description"}
        assert isinstance(entry["name"], str) and entry["name"]
        assert isinstance(entry["description"], str) and entry["description"]


def test_advertised_fields_are_actually_loaded_in_the_demo_panel(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()
    for entry in list_available_fields():
        assert entry["name"] in panel.columns
    # Reverse direction: the loaded panel carries no column the catalog does
    # not declare (FP-5 — nothing usable is hidden from the advertised surface).
    assert undeclared_panel_columns(panel.columns) == ()


def test_catalog_extension_is_visible_without_editing_read_models(monkeypatch) -> None:
    extended = data_field_catalog() + (_extra_field("sentiment_score"),)
    monkeypatch.setattr(data_local, "PANEL_FIELD_CATALOG", extended)
    assert "sentiment_score" in {entry["name"] for entry in list_available_fields()}
    assert "sentiment_score" in {tag["subject_id"] for tag in list_field_research_tags()}


def test_validation_gate_consults_the_real_catalog(monkeypatch) -> None:
    """CP5-4: gate capability = actually declared catalog, not assumptions."""

    spec = FactorSpec(
        factor_id="FTR_GATE_CATALOG",
        name="gate catalog probe",
        formula_dsl="rank(sentiment_score)",
    )

    blocked = validate_factor_spec(spec)
    assert blocked.status == "blocked"
    assert "sentiment_score" in blocked.unresolved_fields

    extended = data_field_catalog() + (_extra_field("sentiment_score"),)
    monkeypatch.setattr(data_local, "PANEL_FIELD_CATALOG", extended)

    ready = validate_factor_spec(spec)
    assert ready.status == "ready"
    assert ready.unresolved_fields == ()


def test_validation_gate_distinguishes_supported_from_runtime_available_fields() -> None:
    spec = FactorSpec(
        factor_id="FTR_RUNTIME_FIELD_GATE",
        name="runtime field gate",
        formula_dsl="rank(netprofit_yoy)",
    )

    supported = validate_factor_spec(spec)
    assert supported.status == "ready"

    unavailable = validate_factor_spec(spec, available_fields={"close", "return_5d"})
    assert unavailable.status == "blocked"
    assert unavailable.unresolved_fields == ()
    assert unavailable.blocking_reasons == (
        "field not available in current data: netprofit_yoy",
    )
