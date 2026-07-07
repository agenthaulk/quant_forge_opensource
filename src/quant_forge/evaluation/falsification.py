"""Advisory falsification diagnostics for factor evaluations.

Secondary anti-overfit checks (QuantGPT-inspired) per the adopted Phase C memo.
This module is standalone by design: nothing here wires into the evaluation
service or any promotion gate — every output is an advisory ``MetricValue``.

Axiom compliance (docs/reviews/quantitative_core_audit.md, "First principles"):

- FP-2 / FP-4: below-floor or unfittable diagnostics report
  ``insufficient_sample`` / ``invalid`` statuses with ``None`` values; numbers
  are never fabricated.
- FP-7: every statistic carries its observation count, floor, and status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
import pandas as pd

from quant_forge.core.contracts import MetricValue

FALSIFICATION_SCHEMA_VERSION = "qf.falsification.v1"
FALSIFICATION_SAMPLE_ROLE = "falsification_advisory"
REQUIRED_COLUMNS = ("trade_date", "instrument", "score", "forward_return")
DEFAULT_PLACEBO_PERMUTATIONS = 50
DEFAULT_HALF_LIFE_MAX_LAG = 5
DEFAULT_MIN_EVALUABLE_DATES = 30
DATE_BLOCK_COUNT = 3
BELOW_FALSIFICATION_SAMPLE_FLOOR = "BELOW_FALSIFICATION_SAMPLE_FLOOR"
NON_POSITIVE_IC_DECAY = "NON_POSITIVE_IC_DECAY"
INSUFFICIENT_POSITIVE_LAG_ICS = "INSUFFICIENT_POSITIVE_LAG_ICS"
WEAK_BLOCK_SIGN_CONSISTENCY = "WEAK_BLOCK_SIGN_CONSISTENCY"
_MINIMUM_LAG_IC_DATES = 2


@dataclass(frozen=True)
class FalsificationReport:
    """Advisory anti-overfit diagnostics for one factor score panel."""

    seed: int
    evaluable_dates: int
    placebo_permutations: int
    half_life_max_lag: int
    min_evaluable_dates: int
    real_rank_ic_mean: MetricValue
    placebo_percentile: MetricValue
    ic_half_life_days: MetricValue
    ic_decay_rate: MetricValue
    ic_lag_metrics: tuple[MetricValue, ...]
    block_metrics: tuple[MetricValue, ...]
    block_ic_spread: MetricValue
    block_sign_consistency: MetricValue
    warnings: tuple[str, ...] = field(default_factory=tuple)
    warning_codes: tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = FALSIFICATION_SCHEMA_VERSION
    sample_role: str = FALSIFICATION_SAMPLE_ROLE

    def __post_init__(self) -> None:
        if self.schema_version != FALSIFICATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported falsification schema version: {self.schema_version}")
        if self.evaluable_dates < 0:
            raise ValueError("evaluable_dates must be non-negative")
        if self.placebo_permutations < 1:
            raise ValueError("placebo_permutations must be positive")
        if self.half_life_max_lag < 1:
            raise ValueError("half_life_max_lag must be positive")
        if self.min_evaluable_dates < 1:
            raise ValueError("min_evaluable_dates must be positive")
        if len(self.ic_lag_metrics) != self.half_life_max_lag:
            raise ValueError("ic_lag_metrics must have one entry per lag")
        if len(self.block_metrics) != DATE_BLOCK_COUNT:
            raise ValueError(f"block_metrics must have {DATE_BLOCK_COUNT} entries")


def run_falsification(
    frame: pd.DataFrame,
    *,
    seed: int,
    placebo_permutations: int = DEFAULT_PLACEBO_PERMUTATIONS,
    half_life_max_lag: int = DEFAULT_HALF_LIFE_MAX_LAG,
    min_evaluable_dates: int = DEFAULT_MIN_EVALUABLE_DATES,
) -> FalsificationReport:
    """Run advisory falsification diagnostics on a joined score/label frame.

    ``frame`` must contain [trade_date, instrument, score, forward_return].
    ``seed`` is required so the placebo shuffle is reproducible; identical
    inputs and seed produce an identical report.
    """
    _validate_frame(frame)
    if placebo_permutations < 1:
        raise ValueError("placebo_permutations must be positive")
    if half_life_max_lag < 1:
        raise ValueError("half_life_max_lag must be positive")
    if min_evaluable_dates < 1:
        raise ValueError("min_evaluable_dates must be positive")

    working = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
    samples = _daily_rank_samples(working)
    evaluable_dates = len(samples)
    if evaluable_dates < min_evaluable_dates:
        return _insufficient_report(
            seed=seed,
            evaluable_dates=evaluable_dates,
            placebo_permutations=placebo_permutations,
            half_life_max_lag=half_life_max_lag,
            min_evaluable_dates=min_evaluable_dates,
        )

    warnings: list[str] = []
    real_mean = float(np.mean([sample.ic for sample in samples]))
    real_metric = MetricValue(
        value=real_mean,
        unit="correlation",
        status="available",
        observation_count=evaluable_dates,
        minimum_required=min_evaluable_dates,
        method="daily_rank_ic_mean",
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
    )

    percentile_metric = _placebo_percentile_metric(
        samples,
        real_mean=real_mean,
        seed=seed,
        permutations=placebo_permutations,
        min_evaluable_dates=min_evaluable_dates,
    )

    lag_metrics = _lag_ic_metrics(working, max_lag=half_life_max_lag)
    half_life_metric, decay_metric, decay_warnings = _half_life_metrics(
        lag_metrics,
        evaluable_dates=evaluable_dates,
        min_evaluable_dates=min_evaluable_dates,
    )
    warnings.extend(decay_warnings)

    block_metrics, spread_metric, sign_metric, block_warnings = _block_metrics(
        samples,
        min_evaluable_dates=min_evaluable_dates,
    )
    warnings.extend(block_warnings)

    return FalsificationReport(
        seed=seed,
        evaluable_dates=evaluable_dates,
        placebo_permutations=placebo_permutations,
        half_life_max_lag=half_life_max_lag,
        min_evaluable_dates=min_evaluable_dates,
        real_rank_ic_mean=real_metric,
        placebo_percentile=percentile_metric,
        ic_half_life_days=half_life_metric,
        ic_decay_rate=decay_metric,
        ic_lag_metrics=lag_metrics,
        block_metrics=block_metrics,
        block_ic_spread=spread_metric,
        block_sign_consistency=sign_metric,
        warnings=_warning_prose(warnings),
        warning_codes=tuple(dict.fromkeys(warnings)),
    )


_WARNING_PROSE = {
    "BELOW_FALSIFICATION_SAMPLE_FLOOR": "not enough evaluable dates for falsification diagnostics",
    "NON_POSITIVE_IC_DECAY": "lag IC decay is non-positive; the half-life is not identified",
    "WEAK_BLOCK_SIGN_CONSISTENCY": "rank IC sign is inconsistent across date blocks",
}


def _warning_prose(codes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_WARNING_PROSE.get(code, code) for code in codes))


@dataclass(frozen=True)
class _DailyRankSample:
    date: pd.Timestamp
    ic: float
    score_ranks: np.ndarray
    forward_return_ranks: np.ndarray


def _validate_frame(frame: pd.DataFrame) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"joined frame is missing required columns: {missing}")
    if frame.duplicated(subset=["trade_date", "instrument"]).any():
        raise ValueError("joined frame has duplicate (trade_date, instrument) rows")


def _daily_rank_samples(frame: pd.DataFrame) -> list[_DailyRankSample]:
    # Per-date Spearman rank IC replicated from evaluation/service.py::_ic_summary
    # (owner: quant_forge.evaluation.service). Reimplemented locally on purpose:
    # that helper is private and returns a full evaluation summary, so importing
    # it here would couple advisory diagnostics to its internals.
    usable = frame.dropna(subset=["score", "forward_return"])
    samples: list[_DailyRankSample] = []
    for date_value, group in usable.groupby("trade_date", sort=True):
        if group["score"].nunique() < 2 or group["forward_return"].nunique() < 2:
            continue
        score_ranks = group["score"].rank()
        forward_return_ranks = group["forward_return"].rank()
        ic = score_ranks.corr(forward_return_ranks)
        if pd.notna(ic):
            samples.append(
                _DailyRankSample(
                    date=pd.Timestamp(date_value),
                    ic=float(ic),
                    score_ranks=score_ranks.to_numpy(dtype=float),
                    forward_return_ranks=forward_return_ranks.to_numpy(dtype=float),
                )
            )
    return samples


def _pearson(left: np.ndarray, right: np.ndarray) -> float | None:
    left_centered = left - left.mean()
    right_centered = right - right.mean()
    denominator = math.sqrt(float(np.sum(left_centered**2)) * float(np.sum(right_centered**2)))
    if denominator <= 0.0:
        return None
    return float(np.sum(left_centered * right_centered) / denominator)


def _placebo_percentile_metric(
    samples: list[_DailyRankSample],
    *,
    real_mean: float,
    seed: int,
    permutations: int,
    min_evaluable_dates: int,
) -> MetricValue:
    # Permuting scores within a date permutes their ranks identically, so we
    # permute the precomputed score ranks and correlate against the fixed
    # forward-return ranks (Spearman == Pearson on ranks).
    rng = np.random.default_rng(seed)
    placebo_means: list[float] = []
    for _ in range(permutations):
        placebo_ics: list[float] = []
        for sample in samples:
            permuted = sample.score_ranks[rng.permutation(len(sample.score_ranks))]
            ic = _pearson(permuted, sample.forward_return_ranks)
            if ic is not None:
                placebo_ics.append(ic)
        if placebo_ics:
            placebo_means.append(float(np.mean(placebo_ics)))
    if not placebo_means:
        return _insufficient_metric(
            unit="fraction",
            observation_count=0,
            minimum_required=min_evaluable_dates,
            method="within_date_score_permutation",
            warning_codes=(BELOW_FALSIFICATION_SAMPLE_FLOOR,),
        )
    below = sum(1 for mean in placebo_means if mean < real_mean)
    ties = sum(1 for mean in placebo_means if mean == real_mean)
    percentile = (below + 0.5 * ties) / len(placebo_means)
    return MetricValue(
        value=float(percentile),
        unit="fraction",
        status="available",
        observation_count=len(samples),
        minimum_required=min_evaluable_dates,
        method="within_date_score_permutation",
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
    )


def _lag_ic_metrics(frame: pd.DataFrame, *, max_lag: int) -> tuple[MetricValue, ...]:
    ordered = frame.sort_values(["instrument", "trade_date"], kind="mergesort").reset_index(drop=True)
    metrics: list[MetricValue] = []
    for lag in range(1, max_lag + 1):
        lagged = ordered.copy()
        # Shift scores forward in time per instrument: the score formed at
        # t - lag is tested against the forward return labeled at t.
        lagged["score"] = lagged.groupby("instrument")["score"].shift(lag)
        lag_samples = _daily_rank_samples(lagged)
        count = len(lag_samples)
        if count >= _MINIMUM_LAG_IC_DATES:
            value: float | None = float(np.mean([sample.ic for sample in lag_samples]))
            status = "available"
        else:
            value = None
            status = "insufficient_sample"
        metrics.append(
            MetricValue(
                value=value,
                unit="correlation",
                status=status,
                observation_count=count,
                minimum_required=_MINIMUM_LAG_IC_DATES,
                method="lagged_daily_rank_ic_mean",
                source_series="daily_rank_ic",
                sample_role=FALSIFICATION_SAMPLE_ROLE,
                segment=f"lag_{lag}",
            )
        )
    return tuple(metrics)


def _half_life_metrics(
    lag_metrics: tuple[MetricValue, ...],
    *,
    evaluable_dates: int,
    min_evaluable_dates: int,
) -> tuple[MetricValue, MetricValue, tuple[str, ...]]:
    points = [
        (float(lag), math.log(metric.value))
        for lag, metric in enumerate(lag_metrics, start=1)
        if metric.status == "available" and metric.value is not None and metric.value > 0.0
    ]
    if len(points) < 2:
        warning_codes = (INSUFFICIENT_POSITIVE_LAG_ICS,)
        invalid = _invalid_metric(
            unit="days",
            observation_count=len(points),
            minimum_required=2,
            method="log_linear_ic_decay",
            warning_codes=warning_codes,
        )
        invalid_rate = _invalid_metric(
            unit="per_day",
            observation_count=len(points),
            minimum_required=2,
            method="log_linear_ic_decay",
            warning_codes=warning_codes,
        )
        return invalid, invalid_rate, warning_codes
    lags = np.array([point[0] for point in points])
    log_ics = np.array([point[1] for point in points])
    lag_deviation = lags - lags.mean()
    slope = float(np.sum(lag_deviation * (log_ics - log_ics.mean())) / np.sum(lag_deviation**2))
    if slope >= 0.0:
        warning_codes = (NON_POSITIVE_IC_DECAY,)
        invalid = _invalid_metric(
            unit="days",
            observation_count=len(points),
            minimum_required=2,
            method="log_linear_ic_decay",
            warning_codes=warning_codes,
        )
        invalid_rate = _invalid_metric(
            unit="per_day",
            observation_count=len(points),
            minimum_required=2,
            method="log_linear_ic_decay",
            warning_codes=warning_codes,
        )
        return invalid, invalid_rate, warning_codes
    decay_rate = -slope
    half_life = math.log(2.0) / decay_rate
    half_life_metric = MetricValue(
        value=float(half_life),
        unit="days",
        status="available",
        observation_count=len(points),
        minimum_required=2,
        method="log_linear_ic_decay",
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
    )
    decay_metric = MetricValue(
        value=float(decay_rate),
        unit="per_day",
        status="available",
        observation_count=len(points),
        minimum_required=2,
        method="log_linear_ic_decay",
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
    )
    return half_life_metric, decay_metric, ()


def _block_metrics(
    samples: list[_DailyRankSample],
    *,
    min_evaluable_dates: int,
) -> tuple[tuple[MetricValue, ...], MetricValue, MetricValue, tuple[str, ...]]:
    block_minimum = max(1, min_evaluable_dates // DATE_BLOCK_COUNT)
    index_blocks = np.array_split(np.arange(len(samples)), DATE_BLOCK_COUNT)
    metrics: list[MetricValue] = []
    block_means: list[float] = []
    for block_number, indices in enumerate(index_blocks, start=1):
        segment = f"block_{block_number}"
        if len(indices) == 0:
            metrics.append(
                _insufficient_metric(
                    unit="correlation",
                    observation_count=0,
                    minimum_required=block_minimum,
                    method="date_block_rank_ic_mean",
                    segment=segment,
                )
            )
            continue
        block_samples = [samples[index] for index in indices]
        mean_ic = float(np.mean([sample.ic for sample in block_samples]))
        block_means.append(mean_ic)
        metrics.append(
            MetricValue(
                value=mean_ic,
                unit="correlation",
                status="available",
                observation_count=len(block_samples),
                minimum_required=block_minimum,
                method="date_block_rank_ic_mean",
                source_series="daily_rank_ic",
                sample_role=FALSIFICATION_SAMPLE_ROLE,
                segment=segment,
                start_date=block_samples[0].date.date().isoformat(),
                end_date=block_samples[-1].date.date().isoformat(),
            )
        )
    if len(block_means) < DATE_BLOCK_COUNT:
        spread = _insufficient_metric(
            unit="correlation",
            observation_count=len(block_means),
            minimum_required=DATE_BLOCK_COUNT,
            method="block_rank_ic_spread",
        )
        consistency = _insufficient_metric(
            unit="ratio",
            observation_count=len(block_means),
            minimum_required=DATE_BLOCK_COUNT,
            method="block_rank_ic_sign_consistency",
        )
        return tuple(metrics), spread, consistency, ()
    spread_value = max(block_means) - min(block_means)
    consistency_value = float(abs(np.sum(np.sign(block_means))) / DATE_BLOCK_COUNT)
    warnings: tuple[str, ...] = ()
    consistency_warning_codes: tuple[str, ...] = ()
    if consistency_value < 1.0:
        warnings = (WEAK_BLOCK_SIGN_CONSISTENCY,)
        consistency_warning_codes = (WEAK_BLOCK_SIGN_CONSISTENCY,)
    spread = MetricValue(
        value=float(spread_value),
        unit="correlation",
        status="available",
        observation_count=DATE_BLOCK_COUNT,
        minimum_required=DATE_BLOCK_COUNT,
        method="block_rank_ic_spread",
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
    )
    consistency = MetricValue(
        value=consistency_value,
        unit="ratio",
        status="available",
        observation_count=DATE_BLOCK_COUNT,
        minimum_required=DATE_BLOCK_COUNT,
        method="block_rank_ic_sign_consistency",
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
        warning_codes=consistency_warning_codes,
    )
    return tuple(metrics), spread, consistency, warnings


def _insufficient_metric(
    *,
    unit: str,
    observation_count: int,
    minimum_required: int,
    method: str,
    segment: str = "",
    warning_codes: tuple[str, ...] = (),
) -> MetricValue:
    return MetricValue(
        value=None,
        unit=unit,
        status="insufficient_sample",
        observation_count=observation_count,
        minimum_required=minimum_required,
        method=method,
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
        segment=segment,
        warning_codes=warning_codes,
    )


def _invalid_metric(
    *,
    unit: str,
    observation_count: int,
    minimum_required: int,
    method: str,
    warning_codes: tuple[str, ...] = (),
) -> MetricValue:
    return MetricValue(
        value=None,
        unit=unit,
        status="invalid",
        observation_count=observation_count,
        minimum_required=minimum_required,
        method=method,
        source_series="daily_rank_ic",
        sample_role=FALSIFICATION_SAMPLE_ROLE,
        warning_codes=warning_codes,
    )


def _insufficient_report(
    *,
    seed: int,
    evaluable_dates: int,
    placebo_permutations: int,
    half_life_max_lag: int,
    min_evaluable_dates: int,
) -> FalsificationReport:
    floor_codes = (BELOW_FALSIFICATION_SAMPLE_FLOOR,)

    def floored(unit: str, method: str, segment: str = "") -> MetricValue:
        return _insufficient_metric(
            unit=unit,
            observation_count=evaluable_dates,
            minimum_required=min_evaluable_dates,
            method=method,
            segment=segment,
            warning_codes=floor_codes,
        )

    return FalsificationReport(
        seed=seed,
        evaluable_dates=evaluable_dates,
        placebo_permutations=placebo_permutations,
        half_life_max_lag=half_life_max_lag,
        min_evaluable_dates=min_evaluable_dates,
        real_rank_ic_mean=floored("correlation", "daily_rank_ic_mean"),
        placebo_percentile=floored("fraction", "within_date_score_permutation"),
        ic_half_life_days=floored("days", "log_linear_ic_decay"),
        ic_decay_rate=floored("per_day", "log_linear_ic_decay"),
        ic_lag_metrics=tuple(
            floored("correlation", "lagged_daily_rank_ic_mean", segment=f"lag_{lag}")
            for lag in range(1, half_life_max_lag + 1)
        ),
        block_metrics=tuple(
            floored("correlation", "date_block_rank_ic_mean", segment=f"block_{block}")
            for block in range(1, DATE_BLOCK_COUNT + 1)
        ),
        block_ic_spread=floored("correlation", "block_rank_ic_spread"),
        block_sign_consistency=floored("ratio", "block_rank_ic_sign_consistency"),
        warnings=_warning_prose(floor_codes),
        warning_codes=floor_codes,
    )
