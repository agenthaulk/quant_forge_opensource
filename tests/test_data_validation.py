from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_forge.data.local import (
    PANEL_FILE,
    LocalPanelDataProvider,
    create_demo_workspace,
)


def _base_panel() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-03"],
            "instrument": ["STK001", "STK001"],
            "close": [10.0, 11.0],
            "market_cap": [1_000.0, 1_100.0],
            "is_st": [False, False],
        }
    )


def test_clean_demo_panel_validates_ok(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    provider = LocalPanelDataProvider(paths["data_root"])

    result = provider.validate()

    assert result.ok is True
    assert result.missing_columns == ()


def test_validate_flags_duplicate_keys(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    panel = _base_panel()
    duplicated = pd.concat([panel, panel.iloc[[0]]], ignore_index=True)
    duplicated.to_parquet(data_root / PANEL_FILE, index=False)

    result = LocalPanelDataProvider(data_root).validate()

    assert result.ok is False
    assert "duplicate_keys" in result.missing_columns


def test_validate_flags_null_in_required_column(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    panel = _base_panel()
    panel.loc[1, "close"] = None
    panel.to_parquet(data_root / PANEL_FILE, index=False)

    result = LocalPanelDataProvider(data_root).validate()

    assert result.ok is False
    assert "null:close" in result.missing_columns


def test_validate_flags_non_numeric_close(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    panel = _base_panel()
    panel["close"] = ["ten", "eleven"]
    panel.to_parquet(data_root / PANEL_FILE, index=False)

    result = LocalPanelDataProvider(data_root).validate()

    assert result.ok is False
    assert "dtype:close" in result.missing_columns
