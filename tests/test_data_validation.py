from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_forge.data.local import (
    PANEL_FILE,
    LocalPanelDataProvider,
    create_demo_workspace,
)
from quant_forge.factor_engine.executor import execute_factor_formula


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


def _write_snapshot(root: Path, *, price: pd.DataFrame, daily_basic: pd.DataFrame) -> Path:
    snapshot = root / "source_snapshot" / "provider=test" / "market=cn_a"
    price_dir = snapshot / "price"
    basic_dir = snapshot / "daily_basic"
    price_dir.mkdir(parents=True)
    basic_dir.mkdir(parents=True)
    price.to_parquet(price_dir / "2025.parquet", index=False)
    daily_basic.to_parquet(basic_dir / "2025.parquet", index=False)
    return snapshot


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


def test_snapshot_validation_reports_synthesized_is_st_and_market_cap(tmp_path: Path) -> None:
    dates = ["20250102", "20250103"]
    price = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA"],
            "trade_date": dates,
            "close": [10.0, 11.0],
            "vol": [100.0, 110.0],
        }
    )
    daily_basic = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA"],
            "trade_date": dates,
            "total_mv": [1000.0, None],
            "circ_mv": [None, None],
        }
    )
    _write_snapshot(tmp_path / "lakehouse", price=price, daily_basic=daily_basic)

    provider = LocalPanelDataProvider(tmp_path / "lakehouse")
    result = provider.validate()
    panel = provider.load_panel()

    assert result.ok is True
    assert result.synthesized_columns == ("is_st", "market_cap")
    assert panel["is_st"].tolist() == [False, False]
    assert panel["market_cap"].iloc[0] == 1000.0
    assert pd.isna(panel["market_cap"].iloc[1])


def test_snapshot_validation_reports_only_is_st_when_market_cap_covered(tmp_path: Path) -> None:
    dates = ["20250102", "20250103"]
    price = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA"],
            "trade_date": dates,
            "close": [10.0, 11.0],
            "vol": [100.0, 110.0],
        }
    )
    daily_basic = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA"],
            "trade_date": dates,
            "total_mv": [1000.0, 1100.0],
            "circ_mv": [900.0, 990.0],
        }
    )
    _write_snapshot(tmp_path / "lakehouse", price=price, daily_basic=daily_basic)

    result = LocalPanelDataProvider(tmp_path / "lakehouse").validate()

    assert result.ok is True
    assert result.synthesized_columns == ("is_st",)


def test_snapshot_derived_warmup_rows_stay_nan_and_drop_under_nan_policy(tmp_path: Path) -> None:
    dates = [f"2025010{day}" for day in range(2, 10)]
    instruments = ["AAA", "BBB"]
    price_rows: list[dict[str, object]] = []
    basic_rows: list[dict[str, object]] = []
    for code_index, code in enumerate(instruments):
        for day_index, day in enumerate(dates):
            price_rows.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "close": 10.0 + code_index + 0.1 * day_index + 0.01 * ((day_index * (code_index + 3)) % 5),
                    "vol": 100.0,
                }
            )
            basic_rows.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "total_mv": 1000.0 * (code_index + 1) + day_index,
                    "circ_mv": 900.0 * (code_index + 1) + day_index,
                }
            )
    _write_snapshot(
        tmp_path / "lakehouse",
        price=pd.DataFrame(price_rows),
        daily_basic=pd.DataFrame(basic_rows),
    )

    panel = LocalPanelDataProvider(tmp_path / "lakehouse").load_panel()

    for code in instruments:
        rows = panel[panel["instrument"] == code].sort_values("trade_date")
        assert pd.isna(rows["return_1d"].iloc[0])
        assert rows["return_1d"].iloc[1:].notna().all()
        assert rows["return_5d"].iloc[:5].isna().all()
        assert rows["return_5d"].iloc[5:].notna().all()
        assert rows["volatility_5d"].iloc[:2].isna().all()
        assert rows["volatility_5d"].iloc[2:].notna().all()

    scores = execute_factor_formula(panel, "rank(return_5d)")
    usable = scores.dropna(subset=["score"])
    usable_dates = set(usable["trade_date"].dt.strftime("%Y%m%d"))

    assert usable_dates == set(dates[5:])
    assert len(usable) == len(dates[5:]) * len(instruments)


def test_snapshot_conflicting_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    price = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA", "AAA"],
            "trade_date": ["20250102", "20250102", "20250103"],
            "close": [10.0, 12.0, 11.0],
            "vol": [100.0, 100.0, 110.0],
        }
    )
    daily_basic = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA"],
            "trade_date": ["20250102", "20250103"],
            "total_mv": [1000.0, 1100.0],
            "circ_mv": [900.0, 990.0],
        }
    )
    _write_snapshot(tmp_path / "lakehouse", price=price, daily_basic=daily_basic)

    provider = LocalPanelDataProvider(tmp_path / "lakehouse")
    result = provider.validate()

    assert result.ok is False
    assert "duplicate_price_keys" in result.missing_columns
    with pytest.raises(ValueError, match="conflicting duplicate"):
        provider.load_panel()


def test_snapshot_exact_duplicate_rows_are_deduplicated(tmp_path: Path) -> None:
    price = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA", "AAA"],
            "trade_date": ["20250102", "20250102", "20250103"],
            "close": [10.0, 10.0, 11.0],
            "vol": [100.0, 100.0, 110.0],
        }
    )
    daily_basic = pd.DataFrame(
        {
            "ts_code": ["AAA", "AAA"],
            "trade_date": ["20250102", "20250103"],
            "total_mv": [1000.0, 1100.0],
            "circ_mv": [900.0, 990.0],
        }
    )
    _write_snapshot(tmp_path / "lakehouse", price=price, daily_basic=daily_basic)

    provider = LocalPanelDataProvider(tmp_path / "lakehouse")
    result = provider.validate()
    panel = provider.load_panel()

    assert result.ok is True
    assert len(panel) == 2
