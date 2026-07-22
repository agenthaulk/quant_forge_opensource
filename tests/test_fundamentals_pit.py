"""Point-in-time correctness for the fundamental data pipeline.

The one non-negotiable property: a statement figure is visible only from its
announcement date (``ann_date``) onward — never from the report period end, and
never a report announced in the future. These tests pin that with synthetic
report tables so they run without the mounted drive.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quant_forge.data.fundamentals import (
    FUNDAMENTAL_FIELD_NAMES,
    _read_parquet_dir,
    as_of_expand,
    build_fundamentals_overlay,
    dedup_announcements,
)


def _keys(dates: list[str], instrument: str = "000001.SZ") -> pd.DataFrame:
    return pd.DataFrame(
        {"trade_date": pd.to_datetime(dates), "instrument": instrument}
    )


def test_as_of_uses_announcement_date_not_period_end() -> None:
    # FY2024 (period ends 2024-12-31) is only ANNOUNCED on 2025-04-20. So on any
    # trade date before 2025-04-20 the value must be NaN (unknown), and from
    # 2025-04-20 onward it must be the announced value -- never known at period end.
    reports = pd.DataFrame(
        {
            "instrument": ["000001.SZ"],
            "ann_date": pd.to_datetime(["2025-04-20"]),
            "end_date": ["20241231"],
            "netprofit_yoy": [12.5],
        }
    )
    keys = _keys(["2025-01-02", "2025-04-19", "2025-04-20", "2025-06-01"])
    out = as_of_expand(reports, keys, ["netprofit_yoy"]).sort_values("trade_date")
    vals = out.set_index(out["trade_date"].dt.strftime("%Y-%m-%d"))["netprofit_yoy"]

    assert pd.isna(vals["2025-01-02"])          # before announcement: unknown
    assert pd.isna(vals["2025-04-19"])          # day before announcement: still unknown
    assert vals["2025-04-20"] == 12.5           # announcement day: now visible
    assert vals["2025-06-01"] == 12.5           # persists until superseded


def test_as_of_never_pulls_a_future_report() -> None:
    # Two reports for one stock. At a date between the two announcements the
    # value must be the FIRST report's, never the later-announced one.
    reports = pd.DataFrame(
        {
            "instrument": ["000001.SZ", "000001.SZ"],
            "ann_date": pd.to_datetime(["2025-04-20", "2025-08-25"]),
            "end_date": ["20241231", "20250630"],
            "netprofit_yoy": [12.5, 30.0],
        }
    )
    keys = _keys(["2025-05-01", "2025-08-24", "2025-08-25", "2025-09-01"])
    out = as_of_expand(reports, keys, ["netprofit_yoy"])
    vals = out.set_index(out["trade_date"].dt.strftime("%Y-%m-%d"))["netprofit_yoy"]

    assert vals["2025-05-01"] == 12.5           # only the first report is known
    assert vals["2025-08-24"] == 12.5           # day before Q2 announcement
    assert vals["2025-08-25"] == 30.0           # Q2 announcement day
    assert vals["2025-09-01"] == 30.0


def test_restatement_supersedes_only_from_its_announcement() -> None:
    # Same period (2024-12-31) restated later with a corrected value. Before the
    # restatement's ann_date the original applies; from it onward, the correction.
    reports = pd.DataFrame(
        {
            "instrument": ["000001.SZ", "000001.SZ"],
            "ann_date": pd.to_datetime(["2025-04-20", "2025-07-10"]),
            "end_date": ["20241231", "20241231"],
            "roe": [8.0, 6.5],
        }
    )
    keys = _keys(["2025-05-01", "2025-07-09", "2025-07-10"])
    out = as_of_expand(reports, keys, ["roe"])
    vals = out.set_index(out["trade_date"].dt.strftime("%Y-%m-%d"))["roe"]

    assert vals["2025-05-01"] == 8.0
    assert vals["2025-07-09"] == 8.0
    assert vals["2025-07-10"] == 6.5            # correction visible from its ann_date


def test_dedup_announcements_keeps_latest_period_per_ann_date() -> None:
    # Two periods announced on the SAME day -> keep the most recent period.
    reports = pd.DataFrame(
        {
            "instrument": ["000001.SZ", "000001.SZ"],
            "ann_date": pd.to_datetime(["2025-04-20", "2025-04-20"]),
            "end_date": ["20241231", "20250331"],
            "netprofit_yoy": [12.5, 20.0],
        }
    )
    deduped = dedup_announcements(reports)
    assert len(deduped) == 1
    assert deduped.iloc[0]["netprofit_yoy"] == 20.0  # 2025Q1 (later end_date) wins


def _write_source(root, dataset: str, frame: pd.DataFrame) -> None:
    dataset_dir = root / dataset
    dataset_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(dataset_dir / "data.parquet")


def test_build_overlay_combines_daily_and_statement_pit(tmp_path) -> None:
    source = tmp_path / "source"
    # daily_basic: trade_date-keyed valuation (already PIT, equality merge).
    _write_source(
        source,
        "daily_basic",
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000001.SZ"],
                "trade_date": ["20250102", "20250601"],
                "pe_ttm": [10.0, 11.0],
                "pb": [1.2, 1.3],
                "ps_ttm": [2.0, 2.1],
                "dv_ttm": [3.0, 3.0],
                "turnover_rate": [1.5, 1.6],
                "volume_ratio": [0.9, 1.1],
            }
        ),
    )
    # financial: ann_date-keyed growth (needs as-of).
    _write_source(
        source,
        "financial",
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "ann_date": ["20250420"],
                "end_date": ["20241231"],
                "netprofit_yoy": [12.5],
                "roe": [8.0],
            }
        ),
    )
    keys = _keys(["2025-01-02", "2025-06-01"])
    overlay = build_fundamentals_overlay(source, keys)

    assert list(overlay["trade_date"]) == list(pd.to_datetime(["2025-01-02", "2025-06-01"]))
    row_jan = overlay[overlay["trade_date"] == "2025-01-02"].iloc[0]
    row_jun = overlay[overlay["trade_date"] == "2025-06-01"].iloc[0]
    # daily valuation lands on its own trade_date
    assert row_jan["pe_ttm"] == 10.0 and row_jun["pe_ttm"] == 11.0
    # statement netprofit_yoy: unknown in January (announced 2025-04-20), known in June
    assert pd.isna(row_jan["netprofit_yoy"])
    assert row_jun["netprofit_yoy"] == 12.5
    assert row_jun["roe"] == 8.0


def test_overlay_absent_field_stays_absent_not_zero(tmp_path) -> None:
    # A source table that lacks a wanted column -> the field is simply not in the
    # overlay (FP-4: never fabricated as 0). Only present columns are produced.
    source = tmp_path / "source"
    _write_source(
        source,
        "financial",
        pd.DataFrame(
            {"ts_code": ["000001.SZ"], "ann_date": ["20250420"], "end_date": ["20241231"], "roe": [8.0]}
        ),
    )
    overlay = build_fundamentals_overlay(source, _keys(["2025-06-01"]))
    assert "roe" in overlay.columns
    assert "netprofit_yoy" not in overlay.columns  # not in source -> absent, not 0


def test_field_names_are_unique_and_nonempty() -> None:
    assert len(FUNDAMENTAL_FIELD_NAMES) == len(set(FUNDAMENTAL_FIELD_NAMES))
    assert all(FUNDAMENTAL_FIELD_NAMES)


def test_as_of_parses_nan_bearing_int_date_column() -> None:
    # A NaN in an int ann_date column upcasts the whole column to float; a naive
    # astype(str) would give "20250420.0" and NaT every row. The parser must
    # still recover the real dates (only the genuinely-missing row is dropped).
    reports = pd.DataFrame(
        {
            "instrument": ["000001.SZ", "000001.SZ"],
            "ann_date": [20250420, None],  # -> float64 [20250420.0, nan]
            "end_date": ["20241231", "20250331"],
            "netprofit_yoy": [12.5, 99.0],
        }
    )
    keys = _keys(["2025-05-01"])
    out = as_of_expand(reports, keys, ["netprofit_yoy"])
    assert out["netprofit_yoy"].iloc[0] == 12.5  # the NaT-ann_date row is excluded


def test_as_of_empty_reports_returns_all_nan_not_crash() -> None:
    reports = pd.DataFrame(
        {"instrument": ["000001.SZ"], "ann_date": ["notadate"], "end_date": ["20241231"], "roe": [8.0]}
    )
    out = as_of_expand(reports, _keys(["2025-05-01"]), ["roe"])
    assert len(out) == 1
    assert pd.isna(out["roe"].iloc[0])


def test_overlay_duplicate_keys_do_not_fan_out_panel(tmp_path) -> None:
    from quant_forge.data.local import create_demo_workspace, LocalPanelDataProvider, resolve_panel_path

    workspace = tmp_path / "demo"
    create_demo_workspace(workspace)
    panel_path = resolve_panel_path(workspace / "data")
    base = pd.read_parquet(panel_path)
    inst = base["instrument"].iloc[0]
    dt = pd.to_datetime(base["trade_date"]).iloc[0]
    # a duplicated (trade_date, instrument) overlay key
    dup = pd.DataFrame(
        {"trade_date": [dt, dt], "instrument": [inst, inst], "netprofit_yoy": [1.0, 2.0]}
    )
    dup.to_parquet(panel_path.parent / "fundamentals.parquet")
    provider = LocalPanelDataProvider(workspace / "data")
    loaded = provider.load_panel(required_fields=("return_5d",))
    # A price-volume factor does not depend on the invalid optional overlay.
    assert not loaded.duplicated(["trade_date", "instrument"]).any()
    assert "netprofit_yoy" not in loaded.columns
    with pytest.raises(ValueError, match="duplicate keys"):
        provider.load_panel(required_fields=("netprofit_yoy",))


def test_missing_overlay_blocks_only_fundamental_dependencies(tmp_path) -> None:
    from quant_forge.data.local import create_demo_workspace, LocalPanelDataProvider

    workspace = tmp_path / "demo"
    create_demo_workspace(workspace)
    (workspace / "data" / "fundamentals.parquet").unlink()
    provider = LocalPanelDataProvider(workspace / "data")

    assert "netprofit_yoy" not in provider.available_field_names()
    assert "return_5d" in provider.available_field_names()
    assert "return_5d" in provider.load_panel(required_fields=("return_5d",)).columns
    with pytest.raises(ValueError, match="not present in the current data"):
        provider.load_panel(required_fields=("netprofit_yoy",))


def test_evaluation_and_backtest_gate_missing_runtime_fields_before_execution(tmp_path) -> None:
    from quant_forge.backtesting.service import run_factor_backtest
    from quant_forge.data.local import create_demo_workspace
    from quant_forge.evaluation.service import evaluate_factor

    paths = create_demo_workspace(tmp_path / "demo")
    (paths["data_root"] / "fundamentals.parquet").unlink()
    kwargs = {
        "factor_id": "FTR_DEMO_EARNINGS_GROWTH",
        "factor_root": paths["factor_root"],
        "data_root": paths["data_root"],
        "artifact_root": paths["artifact_root"],
    }

    with pytest.raises(ValueError, match="not present in the current data"):
        evaluate_factor(**kwargs)
    with pytest.raises(ValueError, match="not present in the current data"):
        run_factor_backtest(**kwargs)


def test_invalid_optional_overlay_is_reported_without_breaking_price_volume(tmp_path) -> None:
    from quant_forge.data.local import create_demo_workspace, LocalPanelDataProvider

    workspace = tmp_path / "demo"
    create_demo_workspace(workspace)
    pd.DataFrame(
        {
            "trade_date": ["not-a-date"],
            "instrument": ["STK001"],
            "netprofit_yoy": [1.0],
        }
    ).to_parquet(workspace / "data" / "fundamentals.parquet", index=False)
    provider = LocalPanelDataProvider(workspace / "data")

    validation = provider.validate()
    assert validation.ok is True
    assert "netprofit_yoy" not in validation.optional_columns
    assert "optional:fundamentals:invalid_trade_date" in validation.missing_columns
    assert "return_5d" in provider.load_panel(required_fields=("return_5d",)).columns
    with pytest.raises(ValueError, match="invalid trade_date"):
        provider.load_panel(required_fields=("netprofit_yoy",))


def test_overlay_without_join_keys_is_not_advertised(tmp_path) -> None:
    from quant_forge.data.local import create_demo_workspace, LocalPanelDataProvider

    workspace = tmp_path / "demo"
    create_demo_workspace(workspace)
    pd.DataFrame({"netprofit_yoy": [1.0]}).to_parquet(
        workspace / "data" / "fundamentals.parquet", index=False
    )
    provider = LocalPanelDataProvider(workspace / "data")

    validation = provider.validate()
    assert "netprofit_yoy" not in validation.optional_columns
    assert "optional:fundamentals:missing_join_keys" in validation.missing_columns
    with pytest.raises(ValueError, match="missing join keys"):
        provider.load_panel(required_fields=("netprofit_yoy",))


def test_overlay_without_matching_panel_keys_is_not_advertised(tmp_path) -> None:
    from quant_forge.data.local import create_demo_workspace, LocalPanelDataProvider

    workspace = tmp_path / "demo"
    create_demo_workspace(workspace)
    pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["1999-01-04"]),
            "instrument": ["NOT_IN_PANEL"],
            "netprofit_yoy": [12.5],
        }
    ).to_parquet(workspace / "data" / "fundamentals.parquet", index=False)
    provider = LocalPanelDataProvider(workspace / "data")

    validation = provider.validate()
    assert validation.ok is True
    assert "netprofit_yoy" not in validation.optional_columns
    assert "optional:fundamentals:no_matching_data" in validation.missing_columns
    assert "netprofit_yoy" not in provider.available_field_names()
    with pytest.raises(ValueError, match="no usable values"):
        provider.load_panel(required_fields=("netprofit_yoy",))


def test_all_null_panel_field_is_not_runtime_available(tmp_path) -> None:
    from quant_forge.data.local import create_demo_workspace, LocalPanelDataProvider, resolve_panel_path

    workspace = tmp_path / "demo"
    create_demo_workspace(workspace)
    panel_path = resolve_panel_path(workspace / "data")
    panel = pd.read_parquet(panel_path)
    panel["roe"] = pd.NA
    panel.to_parquet(panel_path, index=False)
    (workspace / "data" / "fundamentals.parquet").unlink()

    provider = LocalPanelDataProvider(workspace / "data")
    assert "roe" not in provider.available_field_names()
    with pytest.raises(ValueError, match="no usable values"):
        provider.load_panel(required_fields=("roe",))


def test_parquet_projection_reads_each_file_once(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    _write_source(
        source,
        "financial",
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "ann_date": ["20250420"],
                "end_date": ["20241231"],
                "netprofit_yoy": [12.5],
            }
        ),
    )
    real_read_parquet = pd.read_parquet
    calls: list[tuple[str, ...] | None] = []

    def counted_read_parquet(path, *, columns=None, **kwargs):
        calls.append(tuple(columns) if columns is not None else None)
        return real_read_parquet(path, columns=columns, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", counted_read_parquet)
    result = _read_parquet_dir(
        source / "financial",
        columns=["ts_code", "ann_date", "end_date", "netprofit_yoy"],
    )

    assert len(result) == 1
    assert calls == [("ts_code", "ann_date", "end_date", "netprofit_yoy")]
