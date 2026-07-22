"""Industry (Shenwan level-1) panel catalog column (upstream batch 2, item U4).

The catalog advertises ``industry`` as an optional categorical column: a Shenwan
(SW) level-1 industry classification code, string-typed, used as a grouping key
by industry-aware operators rather than as a numeric factor input.

* the demo workspace backs it with a static deterministic code per instrument
  (the twelve demo instruments split evenly across four SW level-1 sectors), so
  it reports ``available``;
* a legacy panel without the column reports ``missing`` (never a fabricated
  value, FP-7);
* being categorical, a direct numeric factor reference is rejected the same way
  the ``is_st`` grouping flag is — numeric consumption is an operator concern
  (group-aware operators arrive separately), not a data-layer concern.

Scope is the catalog and the surfaces that consult it plus the demo data that
backs it — no factor-engine math changes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_forge.core.contracts import DataValidationResult
from quant_forge.data.local import (
    FIELD_AVAILABLE,
    FIELD_MISSING,
    LocalPanelDataProvider,
    catalog_field_availability,
    create_demo_workspace,
    data_field_catalog,
)
from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.mcp.read_models import list_available_fields
from quant_forge.specs.factor_spec import FactorSpec
from quant_forge.specs.validation_gate import validate_factor_spec

# The four SW level-1 sectors the demo panel labels its instruments with, and
# the deterministic instrument -> code assignment (interleaved, so industry does
# not line up with the instrument's size/cap ordering).
EXPECTED_SW_L1_CODES = ("801080", "801120", "801150", "801780")
EXPECTED_INSTRUMENT_INDUSTRY = {
    "STK001": "801080", "STK005": "801080", "STK009": "801080",
    "STK002": "801120", "STK006": "801120", "STK010": "801120",
    "STK003": "801150", "STK007": "801150", "STK011": "801150",
    "STK004": "801780", "STK008": "801780", "STK012": "801780",
}


def test_industry_is_declared_optional_classification_with_sw_l1_and_as_of_note() -> None:
    by_name = {field.name: field for field in data_field_catalog()}
    industry = by_name["industry"]
    assert industry.role == "optional"
    assert industry.tags.themes == ("classification",)
    # The catalog text pins the Shenwan level-1 semantics.
    assert "level-1" in industry.description.lower()
    assert "sw" in industry.description.lower() or "shenwan" in industry.description.lower()
    # A bilingual note carries the SW level-1 wording and defers as-of history.
    notes = industry.tags.notes or ""
    assert "申万" in notes
    assert "as-of" in notes.lower()


def test_industry_is_advertised_on_the_read_model_surface() -> None:
    advertised = {entry["name"] for entry in list_available_fields()}
    assert "industry" in advertised


def test_validation_gate_resolves_industry_as_a_known_field() -> None:
    """The gate consults the real catalog, so ``industry`` is a declared field
    and is never reported as unresolved. Numeric suitability is an execution
    concern (industry is a categorical grouping key), covered separately."""

    spec = FactorSpec(factor_id="FTR_U4_IND", name="u4 industry probe", formula_dsl="rank(industry)")
    result = validate_factor_spec(spec)
    assert "industry" not in result.unresolved_fields


def test_demo_panel_labels_instruments_with_deterministic_sw_l1_codes(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()

    per_instrument = panel.groupby("instrument")["industry"].agg(lambda series: set(series.dropna()))
    # Each instrument carries exactly one static SW level-1 code (no as-of drift).
    assert all(len(codes) == 1 for codes in per_instrument)
    mapping = {instrument: next(iter(codes)) for instrument, codes in per_instrument.items()}
    assert mapping == EXPECTED_INSTRUMENT_INDUSTRY

    # Four SW level-1 sectors, three instruments each; every code is a 6-digit
    # SW level-1 identifier (the 801xxx family).
    counts = panel.drop_duplicates("instrument")["industry"].value_counts()
    assert set(counts.index) == set(EXPECTED_SW_L1_CODES)
    assert counts.tolist() == [3, 3, 3, 3]
    assert all(code.startswith("801") and len(code) == 6 for code in counts.index)


def test_industry_is_categorical_not_a_numeric_factor_input(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()

    # Present (not a missing field) but categorical: the column survives the
    # parquet round-trip as a string, and a numeric factor reference is rejected
    # the same way the is_st grouping flag is.
    assert "industry" in panel.columns
    assert not pd.api.types.is_numeric_dtype(panel["industry"])
    with pytest.raises(ValueError, match="factor field must be numeric: industry"):
        execute_factor_formula(panel, "rank(industry)")


def test_demo_workspace_reports_industry_available(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    validation = LocalPanelDataProvider(paths["data_root"]).validate()
    by_name = {item.name: item for item in catalog_field_availability(validation)}
    assert by_name["industry"].status == FIELD_AVAILABLE


def test_legacy_panel_without_industry_reports_missing(tmp_path: Path) -> None:
    validation = DataValidationResult(
        data_root=tmp_path,
        ok=True,
        rows=10,
        instruments=2,
        date_count=5,
        panel_path=tmp_path / "panel.parquet",
        optional_columns=("volume",),  # a legacy panel that carries no industry column
    )
    by_name = {item.name: item for item in catalog_field_availability(validation)}
    assert by_name["industry"].status == FIELD_MISSING
