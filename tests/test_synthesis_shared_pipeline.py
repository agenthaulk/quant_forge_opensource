"""The single-build reuse seams are numerically identical to the pure builders.

The web workflow standardizes the member panel ONCE, computes the per-period
rank IC sweep ONCE, and threads both into the fitted combine and the advisory
redundancy matrix — instead of ``build_fitted_composite`` /
``build_apriori_composite`` and ``member_rank_ic_redundancy`` each rebuilding
the identical wide-matrix pipeline and forward-return sweep from the same
inputs. This file pins that the seams are a pure structural refactor:

- ``build_directed_matrix`` + ``combine_fitted(..., period_ics=...)`` /
  ``combine_apriori`` + ``redundancy_from_period_ics`` reproduce the pure
  builders byte-for-byte (composite frame, weights path, coverage, warning
  codes, degenerate metadata, redundancy dict) across ``zscore``/``rank`` and
  every runnable method;
- the ``period_ics`` seam validates its input hard — a foreign signal index or
  a wrong factor set is a ``ValueError``, never a silent point-in-time leak;
- the web workflow runs the ``_period_rank_ic_by_signal_index`` sweep exactly
  ONCE per fitted run and ONCE per a-priori run (it used to run it twice on the
  fitted branch).

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
from quant_forge.backtesting.service import rebalance_indices
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.research_loop.config import load_research_loop_config
from quant_forge.synthesis.service import (
    build_apriori_composite,
    build_directed_matrix,
    build_fitted_composite,
    combine_apriori,
    combine_fitted,
    member_rank_ic_redundancy,
    period_rank_ic_by_signal_index,
    redundancy_from_period_ics,
    require_matrix_dates_on_calendar,
    validated_calendar,
    validated_close_frame,
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


def _seam_ic_by_period(
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
    calendar = validated_calendar(dates)
    require_matrix_dates_on_calendar(working, calendar)
    grid = rebalance_indices(calendar, delay=DELAY, holding=HOLDING, start_signal_index=0)
    ic_by_period = period_rank_ic_by_signal_index(
        working,
        close=validated_close_frame(close),
        calendar=calendar,
        grid=grid,
        delay=DELAY,
        holding=HOLDING,
    )
    return outcome, working, ic_by_period


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
    outcome, working, ic_by_period = _seam_ic_by_period(members, dates, close, standardization)
    seam = replace(
        combine_fitted(
            working,
            method=method,
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=ic_by_period,
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
    seam_redundancy = redundancy_from_period_ics(
        ic_by_period, [str(column) for column in working.columns]
    )
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
    outcome, working, ic_by_period = _seam_ic_by_period(members, dates, close, standardization)
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
    seam_redundancy = redundancy_from_period_ics(
        ic_by_period, [str(column) for column in working.columns]
    )
    assert seam_redundancy == pure_redundancy


# ---------------------------------------------------------------------------
# The period_ics seam is validated hard against a foreign IC set
# ---------------------------------------------------------------------------


def test_period_ics_foreign_signal_index_raises() -> None:
    dates, close, members = _build_fixture()
    _outcome, working, ic_by_period = _seam_ic_by_period(members, dates, close, "zscore")
    corrupted = dict(ic_by_period)
    template = dict(next(iter(ic_by_period.values())))
    corrupted[max(ic_by_period) + 500] = template  # a signal index off the grid
    with pytest.raises(ValueError, match="not on the rebalance grid"):
        combine_fitted(
            working,
            method="ic_weighted",
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=corrupted,
        )


def test_period_ics_wrong_factor_set_raises() -> None:
    dates, close, members = _build_fixture()
    _outcome, working, ic_by_period = _seam_ic_by_period(members, dates, close, "zscore")
    corrupted = {index: dict(ics) for index, ics in ic_by_period.items()}
    victim = next(iter(corrupted))
    corrupted[victim].pop(next(iter(corrupted[victim])))  # drop one factor key
    with pytest.raises(ValueError, match="factor keys must equal"):
        combine_fitted(
            working,
            method="ic_weighted",
            close=close,
            dates=dates,
            delay=DELAY,
            holding=HOLDING,
            ic_min_periods=IC_MIN_PERIODS,
            period_ics=corrupted,
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
