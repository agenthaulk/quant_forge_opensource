from __future__ import annotations

import pandas as pd

from quant_forge.core.contracts import SimulationProfile
from quant_forge.factor_engine.signal_processing import prepare_factor_scores, prepare_factor_scores_result


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


def test_prepare_factor_scores_reuses_existing_worldquant_daily_values(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "worldquant_alpha_003"
    factor_dir.mkdir()
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
        "missing_field",
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


def test_prepare_factor_scores_computes_and_persists_only_missing_dates(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "FTR_PARTIAL"
    factor_dir.mkdir()
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["AAA", "BBB"],
            "factor_value": [10.0, 20.0],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)

    result = prepare_factor_scores_result(
        panel,
        "rank(market_cap)",
        factor_id="FTR_PARTIAL",
        factor_name="FTR_PARTIAL",
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 2
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [10.0, 20.0, 0.5, 1.0]

    incremental = pd.read_parquet(factor_dir / "incremental" / "2025.parquet")
    assert list(incremental["trade_date"]) == ["2025-01-03", "2025-01-03"]
    assert list(incremental["instrument"]) == ["AAA", "BBB"]
    assert list(incremental["factor_value"]) == [0.5, 1.0]
    assert incremental["formula_signature"].nunique() == 1


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
    canonical_dir = tmp_path / "worldquant_alpha_003"
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
        "missing_field",
        factor_id="WQ_ALPHA_003",
        factor_name="alpha_003",
        factor_values_root=tmp_path,
    )

    assert result.factor_values_path == canonical_dir
    assert result.computed_rows == 0
    assert list(result.scores["score"]) == [0.3, 0.1]


def test_prepare_factor_scores_recomputes_dates_with_bad_cached_values(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "FTR_BAD_CACHE"
    factor_dir.mkdir()
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03"],
            "instrument": ["AAA", "BBB", "AAA", "BBB"],
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
    assert result.cached_rows == 2
    assert result.computed_rows == 2
    assert list(result.scores["score"]) == [0.5, 1.0, 30.0, 40.0]


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
    factor_dir = tmp_path / "FTR_FILTERED_CACHE"
    factor_dir.mkdir()
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02", "2025-01-02"],
            "instrument": ["CCC", "DDD"],
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
    assert result.computed_rows == 4
    assert list(result.scores["instrument"]) == ["AAA", "BBB", "CCC", "DDD"]
    assert list(result.scores["score"].iloc[:2]) == [0.25, 0.5]
    assert result.scores["score"].iloc[2:].isna().all()


def test_prepare_factor_scores_ignores_incremental_cache_with_different_formula(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "FTR_SIGNATURE"
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
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]

    incremental = pd.read_parquet(incremental_dir / "2025.parquet")
    assert set(incremental["factor_value"]) == {0.5, 1.0}
    assert set(incremental["formula_signature"]) != {"old-signature"}


def test_prepare_factor_scores_ignores_legacy_incremental_cache_without_signature(tmp_path) -> None:
    panel = _two_day_panel()
    factor_dir = tmp_path / "FTR_LEGACY_INCREMENTAL"
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
        factor_values_root=tmp_path,
    )

    assert result.source == "factor_values_incremental"
    assert result.cached_rows == 0
    assert result.computed_rows == 4
    assert list(result.scores["score"]) == [0.5, 1.0, 0.5, 1.0]

    incremental = pd.read_parquet(incremental_dir / "2025.parquet")
    assert set(incremental["factor_value"]) == {0.5, 1.0}
    assert incremental["formula_signature"].nunique() == 1


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
