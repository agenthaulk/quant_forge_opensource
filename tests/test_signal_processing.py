from __future__ import annotations

from pathlib import Path
import warnings

import pandas as pd
import pytest

from quant_forge.core.contracts import SimulationProfile
from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.factor_engine.formula_parser import formula_lookback_rows
from quant_forge.factor_engine.signal_processing import prepare_factor_scores, prepare_factor_scores_result
from quant_forge.factor_engine.value_store import _formula_signature, _panel_with_lookback, _plan_score_computation


def test_prepare_factor_scores_applies_test_period_and_ewma_decay() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            "instrument": ["AAA", "AAA", "AAA"],
            "close": [1.0, 1.0, 1.0],
            "market_cap": [10.0, 20.0, 30.0],
            "is_st": [False, False, False],
        }
    )
    profile = SimulationProfile(decay_days=3, test_period_start="2025-01-02")

    scores = prepare_factor_scores(panel, "market_cap", profile=profile)

    assert list(scores["trade_date"].dt.strftime("%Y-%m-%d")) == ["2025-01-02", "2025-01-03"]
    assert list(scores["score"]) == [20.0, 25.0]


def test_prepare_factor_scores_uses_pre_period_lookback_context() -> None:
    dates = pd.bdate_range("2025-01-02", periods=90)
    panel = pd.DataFrame(
        {
            "trade_date": [date for date in dates for _ in range(2)],
            "instrument": ["AAA", "BBB"] * len(dates),
            "close": [float(index + 1) for index in range(len(dates) * 2)],
            "market_cap": [100.0, 200.0] * len(dates),
            "is_st": [False] * len(dates) * 2,
        }
    )
    profile = SimulationProfile(test_period_start=dates[59].date().isoformat())

    result = prepare_factor_scores_result(panel, "ts_mean(close, 60)", profile=profile)

    assert result.lookback_rows == 59
    assert result.context_rows == len(panel)
    assert result.computed_rows == len(result.scores)
    assert result.scores["trade_date"].min() == dates[59]
    first_visible = result.scores[result.scores["trade_date"] == dates[59]]
    assert first_visible["score"].notna().all()


def test_prepare_factor_scores_preserves_universe_filter_missing_after_decay() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            "instrument": ["AAA", "AAA", "AAA"],
            "close": [1.0, 1.0, 1.0],
            "market_cap": [10.0, 20.0, 30.0],
            "is_st": [False, True, False],
        }
    )

    scores = prepare_factor_scores(panel, "market_cap", ("is_st == false",), profile=SimulationProfile(decay_days=3))

    assert pd.isna(scores.loc[1, "score"])
    assert scores.loc[2, "score"] > scores.loc[0, "score"]


def test_execute_factor_formula_supports_time_series_operator_subset() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [
                    "2025-01-01",
                    "2025-01-01",
                    "2025-01-02",
                    "2025-01-02",
                    "2025-01-03",
                    "2025-01-03",
                ]
            ),
            "instrument": ["AAA", "BBB", "AAA", "BBB", "AAA", "BBB"],
            "close": [10.0, 20.0, 11.0, 18.0, 13.0, 21.0],
            "volume": [100.0, 90.0, 110.0, 95.0, 130.0, 105.0],
        }
    )

    delta = execute_factor_formula(panel, "delta(close, 1)")
    nested = execute_factor_formula(panel, "rank(ts_sum(volume, 2))")
    corr = execute_factor_formula(panel, "correlation(close, volume, 2)")

    assert list(delta["score"].iloc[:2].isna()) == [True, True]
    assert list(delta["score"].iloc[2:]) == [1.0, -2.0, 2.0, 3.0]
    assert list(nested["score"].iloc[:2].isna()) == [True, True]
    assert list(nested["score"].iloc[2:]) == [1.0, 0.5, 1.0, 0.5]
    assert list(corr["score"].iloc[:2].isna()) == [True, True]
    assert corr["score"].iloc[2:].notna().all()


def test_execute_factor_formula_ts_rank_matches_last_window_rank_pct() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"]),
            "instrument": ["AAA", "AAA", "AAA", "AAA"],
            "close": [1.0, 3.0, 2.0, 2.0],
        }
    )

    result = execute_factor_formula(panel, "ts_rank(close, 3)")

    assert list(result["score"].iloc[:2].isna()) == [True, True]
    assert result["score"].iloc[2:].tolist() == pytest.approx([2 / 3, 0.5])


def test_execute_factor_formula_decay_linear_weights_recent_values_more() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"]),
            "instrument": ["AAA", "AAA", "AAA", "AAA"],
            "close": [1.0, 2.0, 3.0, 6.0],
        }
    )

    result = execute_factor_formula(panel, "decay_linear(close, 3)")

    assert list(result["score"].iloc[:2].isna()) == [True, True]
    assert result["score"].iloc[2:].tolist() == pytest.approx([14 / 6, 26 / 6])


def test_prepare_factor_scores_executes_safe_alias_without_value_store() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            "instrument": ["AAA", "AAA", "AAA"],
            "close": [10.0, 11.0, 13.0],
        }
    )

    alias = prepare_factor_scores_result(panel, "ts_stddev(close, 2)")
    canonical = prepare_factor_scores_result(panel, "stddev(close, 2)")

    assert alias.source == "computed_formula"
    assert alias.factor_values_path is None
    pd.testing.assert_frame_equal(alias.scores, canonical.scores)


def test_prepare_factor_scores_blocks_non_executable_alias_without_value_store() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02"]),
            "instrument": ["AAA", "AAA"],
            "close": [10.0, 11.0],
        }
    )

    with pytest.raises(ValueError, match="factor formula failed operator registry gate"):
        prepare_factor_scores_result(panel, "rolling_std(close, 2)")


def test_formula_lookback_rows_tracks_nested_time_series_requirements() -> None:
    assert formula_lookback_rows("rank(market_cap)") == 0
    assert formula_lookback_rows("delta(close, 2)") == 2
    assert formula_lookback_rows("ts_mean(close, 5)") == 4
    assert formula_lookback_rows("correlation(close, volume, 10)") == 9
    assert formula_lookback_rows("ts_mean(delta(close, 2), 3)") == 4


def test_formula_window_arguments_capped_at_named_bound() -> None:
    # P5: boundary accepted, boundary+1 rejected with a clear ValueError.
    assert formula_lookback_rows("ts_mean(close, 750)") == 749
    with pytest.raises(ValueError, match="750"):
        formula_lookback_rows("ts_mean(close, 751)")
    with pytest.raises(ValueError, match="750"):
        formula_lookback_rows("correlation(close, volume, 999999999)")
    with pytest.raises(ValueError, match="750"):
        formula_lookback_rows("rank(delay(close, 100000))")


def test_formula_inspection_reports_window_bound_violation() -> None:
    from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS, inspect_formula

    within = inspect_formula("ts_mean(close, 750)", known_operators=SUPPORTED_OPERATORS)
    assert within.is_valid

    beyond = inspect_formula("ts_mean(close, 751)", known_operators=SUPPORTED_OPERATORS)
    assert not beyond.is_valid
    assert any("750" in error for error in beyond.errors)


def test_wq_min_max_scalar_window_capped_at_named_bound() -> None:
    # FIX 2 / P5: wq_min/wq_max take EITHER a scalar rolling window
    # (wq_max(x, 20) -> ts_max) OR a series (wq_max(x, volume) -> pairwise max).
    # The scalar-window form must honor MAX_WINDOW_ROWS at the resolver/inspect
    # gate (resolve_formula_operators / inspect_formula), matching the lookback
    # path; the pairwise (series) form carries no window and must stay valid.
    from quant_forge.factor_engine.formula_parser import (
        MAX_WINDOW_ROWS,
        SUPPORTED_OPERATORS,
        inspect_formula,
    )
    from quant_forge.operator_registry.resolver import (
        resolve_executable_formula,
        resolve_formula_operators,
    )

    assert MAX_WINDOW_ROWS == 750

    for operator in ("wq_max", "wq_min"):
        # Oversized scalar window: rejected by the resolver gate with a clear
        # named-bound error.
        oversized = resolve_formula_operators(f"{operator}(close, 999999999)")
        assert not oversized.executable
        assert any("750" in reason for reason in oversized.blocking_errors)

        # Boundary accepted, boundary+1 rejected.
        assert resolve_formula_operators(f"{operator}(close, {MAX_WINDOW_ROWS})").executable
        assert not resolve_formula_operators(f"{operator}(close, {MAX_WINDOW_ROWS + 1})").executable

        # Legitimate small scalar window and the pairwise series form still resolve.
        assert resolve_formula_operators(f"{operator}(close, 20)").executable
        assert resolve_formula_operators(f"{operator}(close, volume)").executable

        # inspect_formula (used by the resolver) reports the same bound violation.
        beyond = inspect_formula(f"{operator}(close, {MAX_WINDOW_ROWS + 1})", known_operators=SUPPORTED_OPERATORS)
        assert not beyond.is_valid
        assert any("750" in error for error in beyond.errors)
        assert inspect_formula(f"{operator}(close, {MAX_WINDOW_ROWS})", known_operators=SUPPORTED_OPERATORS).is_valid
        assert inspect_formula(f"{operator}(close, volume)", known_operators=SUPPORTED_OPERATORS).is_valid

    # End-to-end resolver gate (resolve_executable_formula) raises for the
    # oversized form and passes the legitimate scalar-window form through.
    with pytest.raises(ValueError, match="750"):
        resolve_executable_formula("wq_max(close, 999999999)")
    assert resolve_executable_formula("wq_min(close, 20)")


def test_formula_length_capped_at_named_bound() -> None:
    # P5: 2000-char boundary parses; one char more is rejected.
    prefix, suffix = "rank(close + 0.", ")"
    boundary = prefix + "1" * (2000 - len(prefix) - len(suffix)) + suffix
    assert len(boundary) == 2000
    assert formula_lookback_rows(boundary) == 0

    beyond = prefix + "1" * (2001 - len(prefix) - len(suffix)) + suffix
    assert len(beyond) == 2001
    with pytest.raises(ValueError, match="2000"):
        formula_lookback_rows(beyond)


def test_execute_factor_formula_supports_worldquant_style_transforms() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-02"]),
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "close": [2.0, 4.0, 8.0, 16.0],
            "market_cap": [10.0, 20.0, 30.0, 40.0],
        }
    )

    signed = execute_factor_formula(panel, "signedpower(rank(close), 2)")
    scaled = execute_factor_formula(panel, "scale(rank(market_cap))")

    assert list(signed["score"]) == [0.25, 1.0, 0.25, 1.0]
    assert scaled.groupby("trade_date")["score"].sum().tolist() == [1.0, 1.0]


def test_execute_factor_formula_supports_safe_binary_arithmetic() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-02"]),
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "market_cap": [10.0, 20.0, 30.0, 40.0],
            "return_5d": [-0.1, 0.2, 0.1, 0.4],
        }
    )

    result = execute_factor_formula(panel, "zscore(rank(market_cap) * -rank(return_5d))")

    assert result["score"].notna().all()
    assert list(result.groupby("trade_date")["score"].mean().round(12)) == [0.0, 0.0]


def test_execute_factor_formula_supports_grouped_arithmetic() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-01"]),
            "instrument": ["AAA", "BBB", "CCC"],
            "close": [10.0, 20.0, 30.0],
            "volume": [300.0, 100.0, 200.0],
            "market_cap": [30.0, 20.0, 10.0],
            "return_5d": [0.1, -0.2, 0.3],
        }
    )

    grouped = execute_factor_formula(panel, "(rank(close) + rank(volume)) / 2")
    nested = execute_factor_formula(panel, "((rank(close) + rank(volume)) / 2)")
    wrapped = execute_factor_formula(panel, "zscore((rank(market_cap) * -rank(return_5d)))")
    dotted = execute_factor_formula(panel, "rank(local.close)")

    assert list(grouped["score"].round(6)) == [0.666667, 0.5, 0.833333]
    assert list(nested["score"].round(6)) == list(grouped["score"].round(6))
    assert wrapped["score"].notna().all()
    assert list(dotted["score"].round(6)) == [0.333333, 0.666667, 1.0]


@pytest.mark.parametrize(
    "formula",
    [
        "rank(close).__class__",
        "1 < close < 3",
        "close > True",
        "[close]",
        "rank(close, window=2)",
        "rank(close ** 2)",
        "rank(close // 2)",
        "close if volume else market_cap",
        "np.log(close)",
        "+rank(close)",
        "precomputed:factor_id=FTR_PRE + rank(close)",
    ],
)
def test_execute_factor_formula_rejects_unsafe_ast_syntax(formula: str) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01"]),
            "instrument": ["AAA"],
            "close": [10.0],
            "volume": [100.0],
        }
    )

    with pytest.raises(ValueError):
        execute_factor_formula(panel, formula)


def test_prepare_factor_scores_reuses_existing_worldquant_daily_values(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "worldquant_alpha_003"
    factor_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "instrument_id": ["AAA", "BBB", "AAA", "BBB"],
            "trade_date": [20250102, 20250102, 20250103, 20250103],
            "factor_id": ["WQ_ALPHA_003"] * 4,
            "factor_value": [0.3, 0.1, 0.4, 0.2],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "precomputed:worldquant_alpha_003",
        factor_id="WQ_ALPHA_003",
        factor_name="alpha_003",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_cached"
    assert result.cached_rows == 4
    assert result.computed_rows == 0
    assert result.factor_values_path == factor_dir
    assert list(result.scores["score"]) == [0.3, 0.1, 0.4, 0.2]
    assert not (factor_dir / "incremental").exists()


def test_precomputed_scores_use_formula_store_key_directory(tmp_path) -> None:
    panel = _two_day_panel().iloc[:2]
    factor_dir = tmp_path / "vendor_alpha_777"
    factor_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "factor_id": ["FTR_VENDOR_ALPHA"] * 2,
            "factor_value": [0.7, 0.2],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "precomputed:vendor_alpha_777",
        factor_id="FTR_VENDOR_ALPHA",
        factor_name="friendly_factor_name",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_cached"
    assert result.factor_values_path == factor_dir
    assert list(result.scores["score"]) == [0.7, 0.2]


def test_prepare_factor_scores_ignores_root_period_parquet_files(tmp_path) -> None:
    panel = _two_day_panel().iloc[:2]
    factor_dir = tmp_path / "factor_id=WQ_ALPHA_003"
    factor_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "factor_value": [0.3, 0.1],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "factor_value": [9.0, 9.0],
        }
    ).to_parquet(factor_dir / "2025Q1.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "precomputed:factor_id=WQ_ALPHA_003",
        factor_id="WQ_ALPHA_003",
        factor_name="alpha_003",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_cached"
    assert list(result.scores["score"]) == [0.3, 0.1]


def test_prepare_factor_scores_computes_and_persists_only_missing_dates(tmp_path) -> None:
    panel = _two_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_PARTIAL"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_PARTIAL", "rank(market_cap)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "formula_signature": [signature, signature],
            "factor_value": [10.0, 20.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_PARTIAL",
        factor_name="FTR_PARTIAL",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 2
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [10.0, 20.0, 0.5, 1.0]

    categorized_dir = overlay_root / "原始因子" / "factor_id=FTR_PARTIAL"
    incremental = pd.read_parquet(categorized_dir / "incremental" / "2025.parquet")
    assert list(incremental["trade_date"]) == ["2025-01-03", "2025-01-03"]
    assert list(incremental["instrument"]) == ["AAA", "BBB"]
    assert list(incremental["factor_value"]) == [0.5, 1.0]
    assert incremental["formula_signature"].nunique() == 1
    assert not (factor_dir / "incremental").exists()


def test_prepare_factor_scores_reuses_canonical_cache_for_safe_alias(tmp_path) -> None:
    panel = _three_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"

    first = prepare_factor_scores_result(
        panel,
        "stddev(close, 2)",
        factor_id="FTR_ALIAS_CACHE",
        factor_name="FTR_ALIAS_CACHE",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )
    second = prepare_factor_scores_result(
        panel,
        "ts_stddev(close, 2)",
        factor_id="FTR_ALIAS_CACHE",
        factor_name="FTR_ALIAS_CACHE",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert first.source == "factor_values_incremental"
    assert first.computed_rows > 0
    assert second.source == "factor_values_cached"
    assert second.cached_rows == len(panel)
    assert second.computed_rows == 0
    factor_dir = overlay_root / "原始因子" / "factor_id=FTR_ALIAS_CACHE" / "incremental"
    incremental = pd.read_parquet(factor_dir / "2025.parquet")
    assert set(incremental["formula_signature"]) == {
        _formula_signature("FTR_ALIAS_CACHE", "stddev(close, 2)", ())
    }


def test_prepare_factor_scores_reads_legacy_raw_alias_signature(tmp_path) -> None:
    panel = _three_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "原始因子" / "factor_id=FTR_ALIAS_LEGACY"
    factor_dir.mkdir(parents=True)
    legacy_signature = _formula_signature("FTR_ALIAS_LEGACY", "ts_stddev(close, 2)", ())
    pd.DataFrame(
        {
            "trade_date": panel["trade_date"].dt.strftime("%Y-%m-%d"),
            "instrument": panel["instrument"],
            "formula_signature": [legacy_signature] * len(panel),
            "factor_value": [0.0, 0.0, 1.414213562, 2.121320344, 2.121320344, 3.535533906],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "ts_stddev(close, 2)",
        factor_id="FTR_ALIAS_LEGACY",
        factor_name="FTR_ALIAS_LEGACY",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_cached"
    assert result.cached_rows == len(panel)
    assert result.computed_rows == 0
    assert not (overlay_root / "原始因子" / "factor_id=FTR_ALIAS_LEGACY" / "incremental").exists()


def test_prepare_factor_scores_prefers_canonical_cache_over_legacy_alias_signature(tmp_path) -> None:
    panel = _three_day_panel()
    read_root = tmp_path / "canonical"
    factor_dir = read_root / "原始因子" / "factor_id=FTR_ALIAS_CONFLICT"
    factor_dir.mkdir(parents=True)
    canonical_signature = _formula_signature("FTR_ALIAS_CONFLICT", "stddev(close, 2)", ())
    legacy_signature = _formula_signature("FTR_ALIAS_CONFLICT", "ts_stddev(close, 2)", ())
    pd.DataFrame(
        {
            "trade_date": list(panel["trade_date"].dt.strftime("%Y-%m-%d")) * 2,
            "instrument": list(panel["instrument"]) * 2,
            "formula_signature": [legacy_signature] * len(panel) + [canonical_signature] * len(panel),
            "factor_value": [9.0] * len(panel) + [1.0] * len(panel),
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "ts_stddev(close, 2)",
        factor_id="FTR_ALIAS_CONFLICT",
        factor_name="FTR_ALIAS_CONFLICT",
        factor_values_root=read_root,
    )

    assert result.source == "factor_values_cached"
    assert set(result.scores["score"]) == {1.0}


def test_prepare_factor_scores_skips_macos_appledouble_entries_before_stat(tmp_path, monkeypatch) -> None:
    panel = _two_day_panel().iloc[:2]
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    read_root.mkdir()
    (read_root / "._原始因子").write_bytes(b"appledouble")

    original_is_dir = Path.is_dir

    def guarded_is_dir(path: Path) -> bool:
        if path.name.startswith("._"):
            raise PermissionError(f"operation not permitted: {path}")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_DOCKER_SMOKE",
        factor_name="docker_smoke",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [0.5, 1.0]


def test_prepare_factor_scores_preserves_lookback_when_filling_cache_gaps(tmp_path) -> None:
    panel = _three_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_DELTA_CACHE"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_DELTA_CACHE", "delta(close, 1)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "formula_signature": [signature, signature, signature, signature],
            "factor_value": [-99.0, -88.0, 2.0, 3.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "delta(close, 1)",
        factor_id="FTR_DELTA_CACHE",
        factor_name="FTR_DELTA_CACHE",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 4
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [-99.0, -88.0, 2.0, 3.0, 3.0, 5.0]

    incremental = pd.read_parquet(
        overlay_root / "原始因子" / "factor_id=FTR_DELTA_CACHE" / "incremental" / "2025.parquet"
    )
    assert list(incremental["trade_date"]) == ["2025-01-06", "2025-01-06"]
    assert list(incremental["instrument"]) == ["AAA", "BBB"]
    assert list(incremental["factor_value"]) == [3.0, 5.0]
    assert not (factor_dir / "incremental").exists()


def test_panel_with_lookback_uses_full_panel_for_dense_missing_cache() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [f"2025-01-{day:02d}" for day in range(2, 12) for _ in range(3)]
            ),
            "instrument": ["AAA", "BBB", "CCC"] * 10,
            "close": [float(value) for value in range(30)],
            "volume": [float(value * 100) for value in range(30)],
            "market_cap": [float(value * 10) for value in range(30)],
            "is_st": [False] * 30,
        }
    )

    result = _panel_with_lookback(
        panel,
        panel[["trade_date", "instrument"]],
        "rank(volume) * sign(delta(close, 5))",
    )

    assert result is panel


def test_score_computation_plan_reports_dense_lookback_full_recompute() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [f"2025-01-{day:02d}" for day in range(2, 12) for _ in range(3)]
            ),
            "instrument": ["AAA", "BBB", "CCC"] * 10,
            "close": [float(value) for value in range(30)],
            "volume": [float(value * 100) for value in range(30)],
            "market_cap": [float(value * 10) for value in range(30)],
            "is_st": [False] * 30,
        }
    )

    plan = _plan_score_computation(
        panel,
        required_keys=panel[["trade_date", "instrument"]],
        missing_keys=panel[["trade_date", "instrument"]],
        formula="rank(volume) * sign(delta(close, 5))",
    )

    assert plan.panel is panel
    assert plan.mode == "full_recompute"
    assert plan.missing_ratio == 1.0
    assert plan.lookback_rows == 5
    assert plan.context_rows == len(panel)


def test_score_computation_plan_uses_sparse_lookback_context_dates() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [f"2025-01-{day:02d}" for day in range(2, 12) for _ in range(3)]
            ),
            "instrument": ["AAA", "BBB", "CCC"] * 10,
            "close": [float(value) for value in range(30)],
            "volume": [float(value * 100) for value in range(30)],
            "market_cap": [float(value * 10) for value in range(30)],
            "is_st": [False] * 30,
        }
    )
    missing = pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2025-01-10")],
            "instrument": ["AAA"],
        }
    )

    plan = _plan_score_computation(
        panel,
        required_keys=panel[["trade_date", "instrument"]],
        missing_keys=missing,
        formula="rank(volume) * sign(delta(close, 5))",
    )

    assert plan.mode == "sparse_incremental"
    assert plan.lookback_rows == 5
    assert set(plan.panel["trade_date"]) == {
        pd.Timestamp("2025-01-05"),
        pd.Timestamp("2025-01-06"),
        pd.Timestamp("2025-01-07"),
        pd.Timestamp("2025-01-08"),
        pd.Timestamp("2025-01-09"),
        pd.Timestamp("2025-01-10"),
    }
    assert plan.context_rows == 18


def test_score_computation_plan_does_not_overestimate_clustered_sparse_lookback() -> None:
    dates = pd.bdate_range("2025-01-02", periods=100)
    panel = pd.DataFrame(
        {
            "trade_date": [date for date in dates for _ in range(3)],
            "instrument": ["AAA", "BBB", "CCC"] * len(dates),
            "close": [float(value) for value in range(len(dates) * 3)],
            "volume": [float(value * 100) for value in range(len(dates) * 3)],
            "market_cap": [float(value * 10) for value in range(len(dates) * 3)],
            "is_st": [False] * len(dates) * 3,
        }
    )
    missing = pd.DataFrame(
        {
            "trade_date": dates[60:70],
            "instrument": ["AAA"] * 10,
        }
    )

    plan = _plan_score_computation(
        panel,
        required_keys=panel[["trade_date", "instrument"]],
        missing_keys=missing,
        formula="delta(close, 50)",
    )

    assert plan.mode == "sparse_incremental"
    assert plan.context_rows == 180
    assert set(plan.panel["trade_date"]) == set(dates[10:70])


def test_prepare_factor_scores_does_not_count_cached_lookback_warmup_nans_as_missing(
    tmp_path,
) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [f"2025-01-{day:02d}" for day in range(2, 12) for _ in range(3)]
            ),
            "instrument": ["AAA", "BBB", "CCC"] * 10,
            "close": [float(value) for value in range(30)],
            "volume": [float(value * 100) for value in range(30)],
            "market_cap": [float(value * 10) for value in range(30)],
            "is_st": [False] * 30,
        }
    )
    formula = "rank(volume) * sign(delta(close, 5))"
    factor_id = "FTR_SPARSE_LOOKBACK"
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / f"factor_id={factor_id}"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature(factor_id, formula, ())
    cached = execute_factor_formula(panel, formula)
    cached = cached[~((cached["trade_date"] == pd.Timestamp("2025-01-10")) & (cached["instrument"] == "AAA"))]
    payload = cached.rename(columns={"score": "factor_value"}).copy()
    payload["trade_date"] = pd.to_datetime(payload["trade_date"]).dt.strftime("%Y-%m-%d")
    payload["formula_signature"] = signature
    payload.to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        formula,
        factor_id=factor_id,
        factor_name=factor_id,
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.compute_mode == "sparse_incremental"
    assert result.missing_rows == 1
    assert result.computed_rows == 1
    assert result.context_rows == 18


def test_prepare_factor_scores_preserves_rolling_lookback_when_filling_cache_gaps(tmp_path) -> None:
    panel = _three_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_ROLLING_CACHE"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_ROLLING_CACHE", "ts_mean(close, 2)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "formula_signature": [signature, signature, signature, signature],
            "factor_value": [-99.0, -88.0, 11.0, 21.5],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "ts_mean(close, 2)",
        factor_id="FTR_ROLLING_CACHE",
        factor_name="FTR_ROLLING_CACHE",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 4
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [-99.0, -88.0, 11.0, 21.5, 13.5, 25.5]

    incremental = pd.read_parquet(
        overlay_root / "原始因子" / "factor_id=FTR_ROLLING_CACHE" / "incremental" / "2025.parquet"
    )
    assert list(incremental["trade_date"]) == ["2025-01-06", "2025-01-06"]
    assert list(incremental["instrument"]) == ["AAA", "BBB"]
    assert list(incremental["factor_value"]) == [13.5, 25.5]
    assert not (factor_dir / "incremental").exists()


def test_prepare_factor_scores_writes_only_missing_instrument_rows(tmp_path) -> None:
    panel = _two_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_PARTIAL_ROW"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_PARTIAL_ROW", "rank(market_cap)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA"],
            "formula_signature": [signature, signature, signature],
            "factor_value": [10.0, 20.0, 30.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_PARTIAL_ROW",
        factor_name="FTR_PARTIAL_ROW",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 3
    assert result.computed_rows == 1
    assert list(result.scores["score"]) == [10.0, 20.0, 30.0, 1.0]

    incremental = pd.read_parquet(
        overlay_root / "原始因子" / "factor_id=FTR_PARTIAL_ROW" / "incremental" / "2025.parquet"
    )
    assert list(incremental["trade_date"]) == ["2025-01-03"]
    assert list(incremental["instrument"]) == ["BBB"]
    assert list(incremental["factor_value"]) == [1.0]


def test_prepare_factor_scores_uses_per_instrument_lookback_for_sparse_history(tmp_path) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-06"]),
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "close": [10.0, 20.0, 13.0, 25.0],
            "market_cap": [100.0, 200.0, 300.0, 400.0],
            "is_st": [False, False, False, False],
        }
    )
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_SPARSE_DELTA"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_SPARSE_DELTA", "delta(close, 1)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-03"],
            "instrument": ["AAA", "BBB"],
            "formula_signature": [signature, signature],
            "factor_value": [-99.0, -88.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "delta(close, 1)",
        factor_id="FTR_SPARSE_DELTA",
        factor_name="FTR_SPARSE_DELTA",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 2
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [-99.0, -88.0, 3.0, 5.0]

    incremental = pd.read_parquet(
        overlay_root / "原始因子" / "factor_id=FTR_SPARSE_DELTA" / "incremental" / "2025.parquet"
    )
    assert list(incremental["trade_date"]) == ["2025-01-06", "2025-01-06"]
    assert list(incremental["instrument"]) == ["AAA", "BBB"]
    assert list(incremental["factor_value"]) == [3.0, 5.0]


def test_prepare_factor_scores_does_not_write_to_root_without_overlay(tmp_path) -> None:
    panel = _two_day_panel()

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_READ_ONLY",
        factor_name="FTR_READ_ONLY",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert result.factor_values_write_path is None
    assert not (tmp_path / "原始因子" / "factor_id=FTR_READ_ONLY" / "incremental").exists()


def test_prepare_factor_scores_ignores_unsigned_root_cache_for_formula_factor(tmp_path) -> None:
    panel = _two_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_UNSIGNED_ROOT"
    factor_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "factor_value": [9.0, 9.0, 9.0, 9.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_UNSIGNED_ROOT",
        factor_name="FTR_UNSIGNED_ROOT",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]

    categorized_dir = overlay_root / "原始因子" / "factor_id=FTR_UNSIGNED_ROOT"
    incremental = pd.read_parquet(categorized_dir / "incremental" / "2025.parquet")
    assert incremental["formula_signature"].nunique() == 1
    assert set(incremental["factor_value"]) == {0.5, 1.0}
    assert not (factor_dir / "incremental").exists()


def test_prepare_factor_scores_writes_missing_dates_to_overlay(tmp_path) -> None:
    panel = _two_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    read_factor_dir = read_root / "factor_id=FTR_PARTIAL"
    read_factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_PARTIAL", "rank(market_cap)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "formula_signature": [signature, signature],
            "factor_value": [10.0, 20.0],
        }
    ).to_parquet(read_factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_PARTIAL",
        factor_name="FTR_PARTIAL",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    overlay_factor_dir = overlay_root / "原始因子" / "factor_id=FTR_PARTIAL"
    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 2
    assert result.computed_rows == 2
    assert result.factor_values_path == read_factor_dir
    assert result.factor_values_write_path == overlay_factor_dir
    assert not (read_factor_dir / "incremental").exists()
    assert (overlay_factor_dir / "incremental" / "2025.parquet").exists()


def test_prepare_factor_scores_writes_rd_synthetic_values_to_synthetic_category(tmp_path) -> None:
    panel = _two_day_panel()
    overlay_root = tmp_path / "overlay"

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="RD_SYN_TEST",
        factor_name="synthetic_test",
        factor_values_overlay_root=overlay_root,
    )

    factor_dir = overlay_root / "合成因子" / "factor_id=RD_SYN_TEST"
    assert result.source == "factor_values_incremental"
    assert result.factor_values_write_path == factor_dir
    assert (factor_dir / "incremental" / "2025.parquet").exists()


def test_prepare_factor_scores_prefers_overlay_values_over_canonical(tmp_path) -> None:
    panel = _two_day_panel().iloc[:2]
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    read_factor_dir = read_root / "factor_id=FTR_OVERLAY"
    overlay_factor_dir = overlay_root / "factor_id=FTR_OVERLAY"
    categorized_overlay_factor_dir = overlay_root / "原始因子" / "factor_id=FTR_OVERLAY"
    read_factor_dir.mkdir(parents=True)
    overlay_factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_OVERLAY", "rank(market_cap)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "formula_signature": [signature, signature],
            "factor_value": [1.0, 1.0],
        }
    ).to_parquet(read_factor_dir / "2025.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "formula_signature": [signature, signature],
            "factor_value": [2.0, 3.0],
        }
    ).to_parquet(overlay_factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_OVERLAY",
        factor_name="FTR_OVERLAY",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_cached"
    assert result.factor_values_path == overlay_factor_dir
    assert result.factor_values_write_path == categorized_overlay_factor_dir
    assert list(result.scores["score"]) == [2.0, 3.0]


def test_prepare_factor_scores_prefers_canonical_worldquant_alias(tmp_path) -> None:
    panel = _two_day_panel().iloc[:2]
    alpha_dir = tmp_path / "alpha_003"
    alpha_dir.mkdir()
    pd.DataFrame(
        {
            "instrument": ["AAA", "BBB"],
            "trade_date": ["2025-01-02", "2025-01-02"],
            "factor_value": [9.0, 9.0],
        }
    ).to_parquet(alpha_dir / "2025.parquet", index=False)
    canonical_dir = tmp_path / "factor_id=WQ_ALPHA_003"
    canonical_dir.mkdir()
    pd.DataFrame(
        {
            "instrument_id": ["AAA", "BBB"],
            "trade_date": [20250102, 20250102],
            "factor_id": ["WQ_ALPHA_003", "WQ_ALPHA_003"],
            "factor_value": [0.3, 0.1],
        }
    ).to_parquet(canonical_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "precomputed:factor_id=WQ_ALPHA_003",
        factor_id="WQ_ALPHA_003",
        factor_name="alpha_003",
        factor_values_root=tmp_path,
    )

    assert result.factor_values_path == canonical_dir
    assert result.computed_rows == 0
    assert list(result.scores["score"]) == [0.3, 0.1]


def test_prepare_factor_scores_keeps_legacy_worldquant_readable_without_canonical_partition(tmp_path) -> None:
    panel = _two_day_panel().iloc[:2]
    legacy_dir = tmp_path / "worldquant_alpha_003"
    legacy_dir.mkdir()
    pd.DataFrame(
        {
            "instrument_id": ["AAA", "BBB"],
            "trade_date": [20250102, 20250102],
            "factor_id": ["WQ_ALPHA_003", "WQ_ALPHA_003"],
            "factor_value": [0.3, 0.1],
        }
    ).to_parquet(legacy_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "precomputed:worldquant_alpha_003",
        factor_id="WQ_ALPHA_003",
        factor_name="alpha_003",
        factor_values_root=tmp_path,
    )

    assert result.factor_values_path == legacy_dir
    assert result.computed_rows == 0
    assert list(result.scores["score"]) == [0.3, 0.1]


def test_prepare_factor_scores_recomputes_dates_with_bad_cached_values(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "factor_id=FTR_BAD_CACHE"
    factor_dir.mkdir()
    signature = _formula_signature("FTR_BAD_CACHE", "rank(market_cap)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "formula_signature": [signature, signature, signature, signature],
            "factor_value": [None, 20.0, 30.0, 40.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_BAD_CACHE",
        factor_name="FTR_BAD_CACHE",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 3
    assert result.computed_rows == 1
    assert list(result.scores["score"]) == [0.5, 20.0, 30.0, 40.0]


def test_prepare_factor_scores_recomputes_when_filtered_instruments_are_missing(tmp_path) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02"] * 4),
            "instrument": ["AAA", "BBB", "CCC", "DDD"],
            "close": [10.0, 11.0, 12.0, 13.0],
            "market_cap": [100.0, 200.0, 300.0, 400.0],
            "is_st": [False, False, True, True],
        }
    )
    factor_dir = tmp_path / "factor_id=FTR_FILTERED_CACHE"
    factor_dir.mkdir()
    signature = _formula_signature("FTR_FILTERED_CACHE", "rank(market_cap)", ("is_st == false",))
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["CCC", "DDD"],
            "formula_signature": [signature, signature],
            "factor_value": [30.0, 40.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        universe_filters=("is_st == false",),
        factor_id="FTR_FILTERED_CACHE",
        factor_name="FTR_FILTERED_CACHE",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 2
    assert list(result.scores["instrument"]) == ["AAA", "BBB", "CCC", "DDD"]
    assert list(result.scores["score"].iloc[:2]) == [0.25, 0.5]
    assert result.scores["score"].iloc[2:].isna().all()


def test_prepare_factor_scores_ignores_incremental_cache_with_different_formula(tmp_path) -> None:
    panel = _two_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_SIGNATURE"
    incremental_dir = factor_dir / "incremental"
    incremental_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "factor_id": ["FTR_SIGNATURE"] * 4,
            "factor_name": ["FTR_SIGNATURE"] * 4,
            "formula_signature": ["old-signature"] * 4,
            "factor_value": [9.0, 9.0, 9.0, 9.0],
        }
    ).to_parquet(incremental_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_SIGNATURE",
        factor_name="FTR_SIGNATURE",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]

    legacy_incremental = pd.read_parquet(incremental_dir / "2025.parquet")
    categorized_incremental_dir = overlay_root / "原始因子" / "factor_id=FTR_SIGNATURE" / "incremental"
    incremental = pd.read_parquet(categorized_incremental_dir / "2025.parquet")
    assert set(legacy_incremental["factor_value"]) == {9.0}
    assert set(legacy_incremental["formula_signature"]) == {"old-signature"}
    assert set(incremental["factor_value"]) == {0.5, 1.0}
    assert set(incremental["formula_signature"]) != {"old-signature"}


def test_prepare_factor_scores_ignores_legacy_incremental_cache_without_signature(tmp_path) -> None:
    panel = _two_day_panel()
    read_root = tmp_path / "canonical"
    overlay_root = tmp_path / "overlay"
    factor_dir = read_root / "factor_id=FTR_LEGACY_INCREMENTAL"
    incremental_dir = factor_dir / "incremental"
    incremental_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "factor_id": ["FTR_LEGACY_INCREMENTAL"] * 4,
            "factor_name": ["FTR_LEGACY_INCREMENTAL"] * 4,
            "factor_value": [9.0, 9.0, 9.0, 9.0],
        }
    ).to_parquet(incremental_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_LEGACY_INCREMENTAL",
        factor_name="FTR_LEGACY_INCREMENTAL",
        factor_values_root=read_root,
        factor_values_overlay_root=overlay_root,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]

    legacy_incremental = pd.read_parquet(incremental_dir / "2025.parquet")
    categorized_incremental_dir = overlay_root / "原始因子" / "factor_id=FTR_LEGACY_INCREMENTAL" / "incremental"
    incremental = pd.read_parquet(categorized_incremental_dir / "2025.parquet")
    assert set(legacy_incremental["factor_value"]) == {9.0}
    assert set(incremental["factor_value"]) == {0.5, 1.0}
    assert incremental["formula_signature"].nunique() == 1


def test_prepare_factor_scores_computes_from_empty_cache_without_concat_futurewarning(tmp_path) -> None:
    panel = _two_day_panel()

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        result = prepare_factor_scores_result(
            panel,
            "rank(market_cap)",
            factor_id="FTR_CONCAT_WARNING_EMPTY_CACHE",
            factor_name="FTR_CONCAT_WARNING_EMPTY_CACHE",
            factor_values_root=tmp_path,
        )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]


def test_prepare_factor_scores_reads_full_cache_without_concat_futurewarning(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "factor_id=FTR_CONCAT_WARNING_FULL_CACHE"
    factor_dir.mkdir(parents=True)
    signature = _formula_signature("FTR_CONCAT_WARNING_FULL_CACHE", "rank(market_cap)", ())
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "formula_signature": [signature] * 4,
            "factor_value": [0.5, 1.0, 0.5, 1.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        result = prepare_factor_scores_result(
            panel,
            "rank(market_cap)",
            factor_id="FTR_CONCAT_WARNING_FULL_CACHE",
            factor_name="FTR_CONCAT_WARNING_FULL_CACHE",
            factor_values_root=tmp_path,
        )

    assert result.source == "factor_values_cached"
    assert result.cached_rows == 4
    assert result.computed_rows == 0
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]


def _two_day_panel() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"]),
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
            "close": [10.0, 11.0, 12.0, 13.0],
            "market_cap": [100.0, 200.0, 300.0, 400.0],
            "is_st": [False, False, False, False],
        }
    )


def _three_day_panel() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [
                    "2025-01-02",
                    "2025-01-02",
                    "2025-01-03",
                    "2025-01-03",
                    "2025-01-06",
                    "2025-01-06",
                ]
            ),
            "instrument": ["AAA", "BBB", "AAA", "BBB", "AAA", "BBB"],
            "close": [10.0, 20.0, 12.0, 23.0, 15.0, 28.0],
            "market_cap": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0],
            "is_st": [False, False, False, False, False, False],
        }
    )
