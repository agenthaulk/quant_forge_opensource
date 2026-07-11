"""The single-build reuse seams are numerically identical to the pure builders.

The web workflow standardizes the member panel ONCE, computes the per-period
rank IC sweep ONCE, and threads both into the fitted combine and the advisory
redundancy matrix — instead of ``build_fitted_composite`` /
``build_apriori_composite`` and ``member_rank_ic_redundancy`` each rebuilding
the identical wide-matrix pipeline and forward-return sweep from the same
inputs. This file pins that the seams are a pure structural refactor:

- ``build_directed_matrix`` + ``combine_fitted(..., period_ics=sweep)`` /
  ``combine_apriori`` + ``redundancy_from_period_ics(sweep)`` reproduce the
  pure builders byte-for-byte (composite frame, weights path, coverage,
  warning codes, degenerate metadata, redundancy dict) across ``zscore``/
  ``rank`` and every runnable method, where ``sweep`` is a
  :class:`~quant_forge.synthesis.service.PeriodICSweep` built by
  :func:`~quant_forge.synthesis.service.compute_period_ic_sweep`;
- the ``period_ics`` seam accepts ONLY a ``PeriodICSweep`` and validates its
  provenance hard — a raw mapping is a ``TypeError`` naming
  ``compute_period_ic_sweep``, and a tampered sweep field (grid, dates,
  columns, matrix row count, matrix/close content hash, ...) is a
  ``ValueError`` naming that field, never a silent point-in-time leak. The
  content hashes specifically close the gap structural checks alone leave
  open: a foreign matrix or close frame with the SAME shape and edge keys
  but REVISED interior values is caught too, not just a resized one;
- the web workflow runs the ``_period_rank_ic_by_signal_index`` sweep exactly
  ONCE per fitted run and ONCE per a-priori run (it used to run it twice on the
  fitted branch), via exactly one ``compute_period_ic_sweep`` call.

The fixture builds an exact close chain (each instrument's close steps only at
each closed grid period's exit bar), so the forward returns the engine
primitive realizes are deterministic; one member is made degenerate on a single
date so the ``degenerate_dates_by_factor`` provenance is exercised, not just
asserted empty.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import quant_forge.apps.web.server as web_server
import quant_forge.synthesis.service as synthesis_service
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.research_loop.config import load_research_loop_config
from quant_forge.synthesis.service import (
    PeriodICSweep,
    build_apriori_composite,
    build_directed_matrix,
    build_fitted_composite,
    combine_apriori,
    combine_fitted,
    compute_period_ic_sweep,
    member_rank_ic_redundancy,
    redundancy_from_period_ics,
)

INSTRUMENTS = [f"STK{index:03d}" for index in range(6)]
# Three members with distinct orderings so the directed matrix, the per-period
# ICs, and the redundancy matrix are all non-trivial.
SCORES = {
    "F_ALPHA": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
    "F_BRAVO": [5.0, 3.0, 4.0, 1.0, 2.0, 0.0],
    "F_CHARLIE": [1.0, 4.0, 0.0, 5.0, 3.0, 2.0],
}
DIRECTIONS = {"F_ALPHA": 1, "F_BRAVO": -1, "F_CHARLIE": 1}
DELAY = 1
HOLDING = 2
IC_MIN_PERIODS = 3
N_DATES = 30
# A single date on which F_CHARLIE has a no-dispersion cross-section, so the
# §4.2 degenerate mark for that factor is non-empty (a non-grid calendar date,
# so it does not perturb any signal-date IC).
DEGENERATE_DATE_INDEX = 9


def _build_fixture() -> tuple[list[pd.Timestamp], pd.DataFrame, dict[str, pd.DataFrame]]:
    dates = list(pd.bdate_range("2026-01-05", periods=N_DATES))
    grid = list(range(0, N_DATES - DELAY - 1, HOLDING))
    closed = [s for s in grid if s + DELAY + HOLDING < N_DATES]
    # Each closed period's returns follow one of the member orderings, cycled so
    # the per-period IC series carry real cross-period variation (mean != 0 for
    # ic_weighted, std != 0 for icir_weighted), not a single constant pattern.
    targets = list(SCORES.values())
    step_by_exit_index: dict[int, dict[int, float]] = {}
    for ordinal, signal_index in enumerate(closed):
        target = targets[ordinal % len(targets)]
        step_by_exit_index[signal_index + DELAY + HOLDING] = {
            index: 1.0 + 0.01 * (target[index] + 1.0) for index in range(len(INSTRUMENTS))
        }
    close_rows: list[dict[str, object]] = []
    for index, instrument in enumerate(INSTRUMENTS):
        level = 100.0
        for date_index, date in enumerate(dates):
            if date_index in step_by_exit_index:
                level *= step_by_exit_index[date_index][index]
            close_rows.append({"trade_date": date, "instrument": instrument, "close": level})

    members: dict[str, pd.DataFrame] = {}
    for factor_id, scores in SCORES.items():
        rows: list[dict[str, object]] = []
        for date_index, date in enumerate(dates):
            vector = scores
            if factor_id == "F_CHARLIE" and date_index == DEGENERATE_DATE_INDEX:
                vector = [2.0] * len(INSTRUMENTS)
            for index, instrument in enumerate(INSTRUMENTS):
                rows.append(
                    {"trade_date": date, "instrument": instrument, "score": vector[index]}
                )
        members[factor_id] = pd.DataFrame(rows)
    return dates, pd.DataFrame(close_rows), members


def _seam_sweep(
    members: dict[str, pd.DataFrame],
    dates: list[pd.Timestamp],
    close: pd.DataFrame,
    standardization: str,
):
    """Reproduce the web workflow's single build: directed matrix + one sweep."""

    directed, outcome = build_directed_matrix(
        members, directions=DIRECTIONS, standardization=standardization
    )
    working = directed.sort_index()
    sweep = compute_period_ic_sweep(
        directed,
        close=close,
        dates=dates,
        delay=DELAY,
        holding=HOLDING,
    )
    assert isinstance(sweep, PeriodICSweep)
    return outcome, working, sweep


# ---------------------------------------------------------------------------
# Numerics identity: seam path == pure builder path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("standardization", ["zscore", "rank"])
@pytest.mark.parametrize("method", ["ic_weighted", "icir_weighted"])
def test_fitted_seam_matches_build_fitted_composite(standardization: str, method: str) -> None:
    dates, close, members = _build_fixture()
    pure = build_fitted_composite(
        members,
        directions=DIRECTIONS,
        standardization=standardization,
        method=method,
        close=close,
        dates=dates,
        delay=DELAY,
        holding=HOLDING,
        ic_min_periods=IC_MIN_PERIODS,
    )
    outcome, working, sweep = _seam_sweep(members, dates, close, standardization)
    seam = replace(
        combine_fitted(
            working,
            method=method,
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=sweep,
        ),
        standardization=standardization,
        degenerate_dates_by_factor=outcome.degenerate_dates_by_factor,
    )

    # The fixture must actually fit at least one rebalance and observe real
    # closed periods, so identity is not asserted over a degenerate no-op run.
    assert pure.fitted_period_count >= 1
    assert pure.composite["score"].notna().any()

    assert seam.composite.equals(pure.composite)
    assert seam.weights_path == pure.weights_path
    assert seam.coverage == pure.coverage
    assert seam.warning_codes == pure.warning_codes
    assert seam.is_fitted == pure.is_fitted
    assert seam.fitted_weights_latest == pure.fitted_weights_latest
    assert seam.fitted_period_count == pure.fitted_period_count
    assert seam.warmup_period_count == pure.warmup_period_count
    assert seam.degenerate_weight_period_count == pure.degenerate_weight_period_count
    assert seam.fitted_period_fraction == pure.fitted_period_fraction
    assert seam.ic_min_periods == pure.ic_min_periods
    assert seam.method == pure.method
    assert seam.standardization == pure.standardization
    assert seam.degenerate_dates == pure.degenerate_dates
    assert seam.degenerate_dates_by_factor == pure.degenerate_dates_by_factor
    # Provenance actually exercised: F_CHARLIE degenerates on the injected date.
    assert seam.degenerate_dates_by_factor["F_CHARLIE"]

    pure_redundancy = member_rank_ic_redundancy(
        members,
        directions=DIRECTIONS,
        standardization=standardization,
        close=close,
        dates=dates,
        delay=DELAY,
        holding=HOLDING,
    )
    seam_redundancy = redundancy_from_period_ics(sweep)
    assert seam_redundancy == pure_redundancy


@pytest.mark.parametrize("standardization", ["zscore", "rank"])
@pytest.mark.parametrize(
    ("method", "weights"),
    [
        ("equal_weight", None),
        ("weighted", {"F_ALPHA": 0.5, "F_BRAVO": 0.3, "F_CHARLIE": 0.2}),
    ],
)
def test_apriori_seam_matches_build_apriori_composite(
    standardization: str, method: str, weights: dict[str, float] | None
) -> None:
    dates, close, members = _build_fixture()
    pure = build_apriori_composite(
        members,
        directions=DIRECTIONS,
        standardization=standardization,
        method=method,
        weights=weights,
    )
    outcome, working, sweep = _seam_sweep(members, dates, close, standardization)
    seam = replace(
        combine_apriori(working, method=method, weights=weights),
        standardization=standardization,
        degenerate_dates_by_factor=outcome.degenerate_dates_by_factor,
    )

    assert pure.composite["score"].notna().any()
    assert seam.composite.equals(pure.composite)
    assert seam.weights_effective == pure.weights_effective
    assert seam.coverage == pure.coverage
    assert seam.method == pure.method
    assert seam.warning_codes == pure.warning_codes
    assert seam.degenerate_dates == pure.degenerate_dates
    assert seam.degenerate_dates_by_factor == pure.degenerate_dates_by_factor
    assert seam.standardization == pure.standardization
    assert seam.degenerate_dates_by_factor["F_CHARLIE"]

    pure_redundancy = member_rank_ic_redundancy(
        members,
        directions=DIRECTIONS,
        standardization=standardization,
        close=close,
        dates=dates,
        delay=DELAY,
        holding=HOLDING,
    )
    seam_redundancy = redundancy_from_period_ics(sweep)
    assert seam_redundancy == pure_redundancy


# ---------------------------------------------------------------------------
# The period_ics seam accepts ONLY a genuine PeriodICSweep and validates its
# provenance hard against this call's own computed values.
# ---------------------------------------------------------------------------


def test_period_ics_raw_mapping_raises_type_error() -> None:
    """A bare dict is a caller programming error, not a foreign-data case."""

    dates, close, members = _build_fixture()
    _outcome, working, sweep = _seam_sweep(members, dates, close, "zscore")
    with pytest.raises(TypeError, match="compute_period_ic_sweep"):
        combine_fitted(
            working,
            method="ic_weighted",
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=dict(sweep.ics),
        )


@pytest.mark.parametrize(
    ("field", "tampered_value", "match"),
    [
        ("grid", (0, 999), "grid"),
        ("dates", (pd.Timestamp("2020-01-01"),), "dates"),
        ("columns", ("F_ALPHA", "F_BRAVO"), "columns"),
        ("matrix_row_count", 1, "matrix_row_count"),
    ],
)
def test_period_ics_tampered_sweep_field_raises_value_error(
    field: str, tampered_value: object, match: str
) -> None:
    """A ``PeriodICSweep`` whose recorded provenance disagrees with this call
    is rejected naming the mismatched field — proven via ``dataclasses.replace``
    on a GENUINE sweep (never a hand-built one), so only ``field`` differs from
    a sweep that would otherwise validate cleanly."""

    dates, close, members = _build_fixture()
    _outcome, working, sweep = _seam_sweep(members, dates, close, "zscore")
    tampered = replace(sweep, **{field: tampered_value})
    with pytest.raises(ValueError, match=match):
        combine_fitted(
            working,
            method="ic_weighted",
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=tampered,
        )


@pytest.mark.parametrize("field", ["matrix_content_hash", "close_content_hash"])
def test_period_ics_tampered_content_hash_raises_value_error(field: str) -> None:
    """The content-hash fields participate in the same field-named rejection
    as the structural fields. Tampering is ``genuine + 1`` (never a fixed
    constant), so the tampered value is guaranteed to differ from the value
    this call recomputes."""

    dates, close, members = _build_fixture()
    _outcome, working, sweep = _seam_sweep(members, dates, close, "zscore")
    tampered = replace(sweep, **{field: getattr(sweep, field) + 1})
    with pytest.raises(ValueError, match=field):
        combine_fitted(
            working,
            method="ic_weighted",
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=tampered,
        )


def _interior_close_row(close: pd.DataFrame, dates: list[pd.Timestamp]) -> pd.Index:
    """One close row that is interior under any stable ordering: a middle
    instrument on a middle date, so the first/last edge fingerprints the
    validator also checks stay untouched and only the CONTENT hash can catch
    the revision."""

    mask = (close["instrument"] == INSTRUMENTS[2]) & (close["trade_date"] == dates[15])
    index = close.index[mask]
    assert len(index) == 1
    return index


def test_period_ics_rejects_revised_close_interior_value() -> None:
    """The poisoning scenario, close side: a close series with the SAME shape
    and SAME first/last rows but ONE revised interior value must not drive
    weights fitted from the original series — every structural field still
    matches, and ``close_content_hash`` is what refuses."""

    dates, close, members = _build_fixture()
    _outcome, working, sweep = _seam_sweep(members, dates, close, "zscore")
    revised_close = close.copy()
    row = _interior_close_row(revised_close, dates)
    revised_close.loc[row, "close"] = float(revised_close.loc[row, "close"].iloc[0]) + 1.0
    with pytest.raises(ValueError, match="close_content_hash"):
        combine_fitted(
            working,
            method="ic_weighted",
            close=revised_close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=sweep,
        )


def test_period_ics_rejects_revised_matrix_interior_value() -> None:
    """The poisoning scenario, matrix side: a directed matrix with the SAME
    index (row count and edge keys unchanged) but ONE revised interior score
    must not consume a sweep computed from the original values —
    ``matrix_content_hash`` is what refuses."""

    dates, close, members = _build_fixture()
    _outcome, working, sweep = _seam_sweep(members, dates, close, "zscore")
    revised = working.copy()
    key = (dates[15], INSTRUMENTS[2])
    revised.loc[key, "F_ALPHA"] = float(revised.loc[key, "F_ALPHA"]) + 1.0
    assert tuple(revised.index[0]) == tuple(working.index[0])
    assert tuple(revised.index[-1]) == tuple(working.index[-1])
    with pytest.raises(ValueError, match="matrix_content_hash"):
        combine_fitted(
            revised,
            method="ic_weighted",
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=sweep,
        )


# ---------------------------------------------------------------------------
# The web workflow runs the forward-return IC sweep exactly once per run
# ---------------------------------------------------------------------------


def _run_workflow_counting_sweeps(tmp_path, synthesis: dict, monkeypatch) -> int:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = load_research_loop_config(
        web_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation
    )
    calls = {"count": 0}
    real = synthesis_service._period_rank_ic_by_signal_index

    def counting(*args, **kwargs):
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(synthesis_service, "_period_rank_ic_by_signal_index", counting)
    web_server.run_multi_factor_backtest_workflow(
        config,
        factor_refs=[
            {"factor_id": "FTR_DEMO_SMALL_CAP", "direction": 1},
            {"factor_id": "FTR_DEMO_MOMENTUM", "direction": -1},
        ],
        synthesis=synthesis,
        standardization={"method": "zscore", "params": {}},
        parameters={"holding_days": 5},
        rd_config=rd_config,
    )
    return calls["count"]


def test_fitted_workflow_sweeps_period_ic_exactly_once(tmp_path, monkeypatch) -> None:
    count = _run_workflow_counting_sweeps(
        tmp_path, {"method": "ic_weighted", "params": {}}, monkeypatch
    )
    assert count == 1


def test_apriori_workflow_sweeps_period_ic_exactly_once(tmp_path, monkeypatch) -> None:
    count = _run_workflow_counting_sweeps(
        tmp_path, {"method": "equal_weight", "params": {}}, monkeypatch
    )
    assert count == 1
