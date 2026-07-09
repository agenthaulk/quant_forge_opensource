from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from quant_forge.evaluation.falsification import (
    BELOW_FALSIFICATION_SAMPLE_FLOOR,
    FALSIFICATION_SAMPLE_ROLE,
    FALSIFICATION_SCHEMA_VERSION,
    FalsificationReport,
    WEAK_BLOCK_SIGN_CONSISTENCY,
    run_falsification,
)


def _planted_panel(
    n_dates: int = 60,
    n_instruments: int = 25,
    rho: float = 0.7,
    data_seed: int = 7,
) -> pd.DataFrame:
    """Panel where score == next-period return rank, with AR(1)-persistent returns.

    The AR(1) state makes lagged scores decay against future labels at ~rho**lag,
    so the log-linear IC half-life is finite and well identified.
    """
    rng = np.random.default_rng(data_seed)
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    state = rng.normal(size=n_instruments)
    rows: list[dict[str, object]] = []
    for date in dates:
        state = rho * state + rng.normal(scale=math.sqrt(1.0 - rho**2), size=n_instruments)
        forward = state + rng.normal(scale=0.05, size=n_instruments)
        score = pd.Series(forward).rank().to_numpy()
        for index in range(n_instruments):
            rows.append(
                {
                    "trade_date": date,
                    "instrument": f"INST{index:03d}",
                    "score": float(score[index]),
                    "forward_return": float(forward[index]),
                }
            )
    return pd.DataFrame(rows)


def _noise_panel(
    n_dates: int = 60,
    n_instruments: int = 25,
    data_seed: int = 123,
) -> pd.DataFrame:
    rng = np.random.default_rng(data_seed)
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    rows: list[dict[str, object]] = []
    for date in dates:
        scores = rng.normal(size=n_instruments)
        forward = rng.normal(scale=0.02, size=n_instruments)
        for index in range(n_instruments):
            rows.append(
                {
                    "trade_date": date,
                    "instrument": f"INST{index:03d}",
                    "score": float(scores[index]),
                    "forward_return": float(forward[index]),
                }
            )
    return pd.DataFrame(rows)


def test_planted_signal_survives_placebo_and_has_finite_half_life() -> None:
    report = run_falsification(_planted_panel(), seed=17)

    assert report.schema_version == FALSIFICATION_SCHEMA_VERSION
    assert report.sample_role == FALSIFICATION_SAMPLE_ROLE
    assert report.evaluable_dates == 60
    assert report.placebo_percentile.status == "available"
    assert report.placebo_percentile.value is not None
    assert report.placebo_percentile.value >= 0.98
    assert report.real_rank_ic_mean.value == pytest.approx(1.0)

    assert report.ic_half_life_days.status == "available"
    assert report.ic_half_life_days.value is not None
    assert math.isfinite(report.ic_half_life_days.value)
    assert 0.5 < report.ic_half_life_days.value < 10.0
    assert report.ic_decay_rate.status == "available"
    assert report.ic_decay_rate.value is not None
    assert report.ic_decay_rate.value > 0.0

    assert len(report.ic_lag_metrics) == 5
    assert [metric.segment for metric in report.ic_lag_metrics] == [f"lag_{lag}" for lag in range(1, 6)]
    assert all(metric.status == "available" for metric in report.ic_lag_metrics)

    assert len(report.block_metrics) == 3
    assert all(metric.status == "available" for metric in report.block_metrics)
    assert all(metric.start_date and metric.end_date for metric in report.block_metrics)
    assert report.block_ic_spread.value == pytest.approx(0.0)
    assert report.block_sign_consistency.value == pytest.approx(1.0)
    assert report.warnings == ()


def test_pure_noise_panel_is_not_flattered() -> None:
    report = run_falsification(_noise_panel(), seed=11)

    assert report.placebo_percentile.status == "available"
    assert report.placebo_percentile.value is not None
    assert 0.02 < report.placebo_percentile.value < 0.98
    assert report.block_sign_consistency.status == "available"
    assert report.block_sign_consistency.value is not None
    assert report.block_sign_consistency.value < 1.0
    assert WEAK_BLOCK_SIGN_CONSISTENCY in report.warning_codes
    assert WEAK_BLOCK_SIGN_CONSISTENCY in report.block_sign_consistency.warning_codes
    assert all(code not in report.warnings for code in report.warning_codes)
    assert report.warnings
    assert report.block_ic_spread.value is not None
    assert report.block_ic_spread.value > 0.0


def test_below_floor_input_reports_insufficient_sample_everywhere() -> None:
    report = run_falsification(_noise_panel(n_dates=10), seed=11)

    assert report.evaluable_dates == 10
    assert BELOW_FALSIFICATION_SAMPLE_FLOOR in report.warning_codes
    all_metrics = (
        report.real_rank_ic_mean,
        report.placebo_percentile,
        report.ic_half_life_days,
        report.ic_decay_rate,
        report.block_ic_spread,
        report.block_sign_consistency,
        *report.ic_lag_metrics,
        *report.block_metrics,
    )
    for metric in all_metrics:
        assert metric.status == "insufficient_sample"
        assert metric.value is None
        assert metric.minimum_required == 30
        assert BELOW_FALSIFICATION_SAMPLE_FLOOR in metric.warning_codes


def test_same_seed_produces_identical_report() -> None:
    first = run_falsification(_planted_panel(), seed=5)
    second = run_falsification(_planted_panel(), seed=5)
    assert first == second

    noisy_first = run_falsification(_noise_panel(), seed=11)
    noisy_second = run_falsification(_noise_panel(), seed=11)
    assert noisy_first == noisy_second


def test_missing_column_rejected() -> None:
    frame = _noise_panel().drop(columns=["forward_return"])
    with pytest.raises(ValueError, match="missing required columns"):
        run_falsification(frame, seed=1)


def test_duplicate_rows_rejected() -> None:
    frame = _noise_panel(n_dates=35)
    duplicated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        run_falsification(duplicated, seed=1)


def test_invalid_parameters_rejected() -> None:
    frame = _noise_panel(n_dates=35)
    with pytest.raises(ValueError, match="placebo_permutations"):
        run_falsification(frame, seed=1, placebo_permutations=0)
    with pytest.raises(ValueError, match="half_life_max_lag"):
        run_falsification(frame, seed=1, half_life_max_lag=0)
    with pytest.raises(ValueError, match="min_evaluable_dates"):
        run_falsification(frame, seed=1, min_evaluable_dates=0)
    with pytest.raises(TypeError):
        run_falsification(frame)  # type: ignore[call-arg]  # seed is required


def test_report_is_frozen_and_validates_schema() -> None:
    report = run_falsification(_noise_panel(n_dates=35), seed=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.seed = 99  # type: ignore[misc]
    with pytest.raises(ValueError, match="schema version"):
        dataclasses.replace(report, schema_version="qf.falsification.v0")
