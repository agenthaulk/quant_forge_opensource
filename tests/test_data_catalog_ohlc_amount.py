"""OHLC + amount panel catalog columns (upstream batch 1, item U2).

The catalog advertises ``open`` / ``high`` / ``low`` on the same adjusted price
basis as ``close`` and ``amount`` as daily traded turnover. They are optional
columns:

* a data root that carries them exposes them to formulas and reports them
  ``available`` (the demo workspace backs all four);
* a legacy panel without them reports ``missing`` (never a fabricated value,
  FP-7) and any formula that references them fails with the standard
  missing-field error.

Scope is the catalog and the surfaces that consult it (the advertised field
list and the specs ValidationGate) plus the demo data that backs it — no
factor-engine math changes.
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

NEW_FIELDS = ("open", "high", "low", "amount")


def test_new_fields_are_declared_optional_with_pinned_basis() -> None:
    by_name = {field.name: field for field in data_field_catalog()}
    for name in NEW_FIELDS:
        assert by_name[name].role == "optional"
    # Price columns are pinned to the same adjusted basis as close so complex
    # price factors cannot silently mix bases; amount is the turnover value that
    # backs the vwap proxy (amount / volume).
    for name in ("open", "high", "low"):
        assert "same price basis as close" in by_name[name].description
        assert by_name[name].tags.themes == ("price",)
    assert "turnover" in by_name["amount"].description.lower()
    assert by_name["amount"].tags.themes == ("liquidity",)


def test_new_fields_are_advertised_on_the_read_model_surface() -> None:
    advertised = {entry["name"] for entry in list_available_fields()}
    assert set(NEW_FIELDS) <= advertised


def test_formula_referencing_new_fields_resolves_ready() -> None:
    """New columns referenced by a formula now resolve: the ValidationGate
    consults the real catalog, so open/high/low/amount are known fields."""

    for formula in (
        "rank(amount)",
        "rank((high - low) / close)",
        "correlation(close, amount, 20)",
    ):
        spec = FactorSpec(factor_id="FTR_U2_PROBE", name="u2 probe", formula_dsl=formula)
        result = validate_factor_spec(spec)
        assert result.status == "ready", (formula, result.unresolved_fields)
        assert result.unresolved_fields == ()


def test_field_outside_the_catalog_stays_blocked() -> None:
    # vwap is a derived proxy, not a catalog column: it must still block so the
    # gate does not fabricate capability it does not have.
    spec = FactorSpec(factor_id="FTR_U2_VWAP", name="u2 vwap", formula_dsl="rank(vwap)")
    result = validate_factor_spec(spec)
    assert result.status == "blocked"
    assert "vwap" in result.unresolved_fields


def test_intraday_range_and_amount_factors_execute_on_demo_panel(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()

    intraday_range = execute_factor_formula(panel, "(high - low) / close")
    scores = intraday_range["score"].dropna()
    assert not scores.empty
    # high and low bracket close on the same basis, so the range stays positive.
    assert (scores > 0).all()

    # amount backs an Amihud-style illiquidity factor end to end.
    illiquidity = execute_factor_formula(panel, "abs(return_1d) / amount")
    assert illiquidity["score"].notna().any()


def test_legacy_panel_without_new_columns_raises_missing_field_error() -> None:
    """A panel that predates the extension reports the standard missing-field
    error rather than a silent NaN score."""

    legacy = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "instrument": ["A", "A"],
            "close": [10.0, 11.0],
        }
    )
    with pytest.raises(ValueError, match="unsupported or missing factor field: amount"):
        execute_factor_formula(legacy, "rank(amount)")
    with pytest.raises(ValueError, match="unsupported or missing factor field: high"):
        execute_factor_formula(legacy, "high - low")


def test_availability_reports_missing_when_data_root_lacks_new_columns(tmp_path: Path) -> None:
    validation = DataValidationResult(
        data_root=tmp_path,
        ok=True,
        rows=10,
        instruments=2,
        date_count=5,
        panel_path=tmp_path / "panel.parquet",
        optional_columns=("volume",),  # a legacy panel that carries no OHLC/amount
    )
    by_name = {item.name: item for item in catalog_field_availability(validation)}
    for name in NEW_FIELDS:
        assert by_name[name].status == FIELD_MISSING


def test_demo_workspace_reports_new_fields_available(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    validation = LocalPanelDataProvider(paths["data_root"]).validate()
    by_name = {item.name: item for item in catalog_field_availability(validation)}
    for name in NEW_FIELDS:
        assert by_name[name].status == FIELD_AVAILABLE
