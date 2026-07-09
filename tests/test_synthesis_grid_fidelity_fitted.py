"""RB-5 grid fidelity for the FITTED branch: fit grid == engine realized grid.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.4 RB-5,
§13 test_grid_fidelity): the fitted embargo ``idx(s)+delay+holding <= idx(d)``
is only sound if it is measured on the SAME index grid the engine actually
trades, so the §4.4 fit computes a weight vector for EVERY index the shared
``rebalance_indices`` helper yields, and after the engine run the realized
``resolved_schedule`` signal dates must equal that grid (filtered only by
the engine's own disclosed D3 final-partial exclusion). As in the a-priori
twin (tests/test_synthesis_grid_fidelity_apriori.py), the expectation is
recomputed from first principles here — never via the shipped checker — so
a broken checker cannot vouch for itself.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from quant_forge.backtesting.service import rebalance_indices
from quant_forge.core.contracts import SimulationProfile
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import prepare_factor_scores_result
from quant_forge.synthesis.service import (
    build_fitted_composite,
    derive_composite_id,
    run_composite_backtest,
)

UNIVERSE = ("is_st == false",)
IC_MIN_PERIODS = 3


def _write_panel(data_root: Path, *, periods: int = 40, instruments: int = 8) -> pd.DataFrame:
    data_root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2026-01-05", periods=periods)
    rows: list[dict[str, object]] = []
    for instrument_index in range(instruments):
        instrument = f"STK{instrument_index:03d}"
        for day_index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": 10.0 + instrument_index + day_index * (0.03 + instrument_index * 0.002),
                    "market_cap": 1_000_000_000.0 + instrument_index * 150_000_000.0,
                    "is_st": False,
                    "volume": 1_000.0 + instrument_index * 25.0 + day_index * 5.0,
                    "return_5d": 0.01 * ((day_index + 2 * instrument_index) % 7) - 0.02,
                    "volatility_5d": 0.02 + 0.001 * ((day_index + instrument_index) % 5),
                }
            )
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    return LocalPanelDataProvider(data_root).load_panel()


@pytest.mark.parametrize(("delay", "holding"), [(1, 3), (1, 5), (2, 4)])
def test_fitted_run_realizes_exactly_the_shared_grid(
    tmp_path: Path, delay: int, holding: int
) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile(execution_delay_days=delay)
    members = {
        "F_MEM_ALPHA": prepare_factor_scores_result(
            panel, "rank(return_5d)", UNIVERSE, profile=profile
        ).scores,
        "F_MEM_BETA": prepare_factor_scores_result(
            panel, "rank(market_cap)", UNIVERSE, profile=profile
        ).scores,
    }
    dates = sorted(panel["trade_date"].drop_duplicates())
    close = panel[["trade_date", "instrument", "close"]].copy()
    fitted = build_fitted_composite(
        members,
        directions={"F_MEM_ALPHA": 1, "F_MEM_BETA": -1},
        standardization="zscore",
        method="ic_weighted",
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        ic_min_periods=IC_MIN_PERIODS,
    )
    # The window must genuinely exercise the fitted branch, or this test
    # would only re-prove the a-priori grid result under another name.
    assert fitted.is_fitted is True
    assert fitted.fitted_period_count >= 1
    assert fitted.warmup_period_count >= 1

    # First principles, part 1: the fit produced a weight vector for EVERY
    # index the shared helper yields — including the final-partial tail the
    # engine will exclude, because the grid (not the engine's realization)
    # is the §4.4 embargo's measuring stick.
    grid = rebalance_indices(dates, delay=delay, holding=holding, start_signal_index=0)
    assert [entry.signal_index for entry in fitted.weights_path] == grid

    composite_id = derive_composite_id(
        factor_refs=(("F_MEM_ALPHA", 1), ("F_MEM_BETA", -1)),
        method="ic_weighted",
        method_params={"ic_min_periods": IC_MIN_PERIODS},
        standardization="zscore",
        backtest_start=None,
        backtest_end=None,
        decay_days=0,
        execution_delay_days=delay,
        top_quantile=0.3,
        coverage_rule="all_factors",
        min_factor_coverage=None,
        universe_filters=UNIVERSE,
    )
    run = run_composite_backtest(
        fitted.composite,
        composite_id=composite_id,
        factor_root=tmp_path / "factor_root",
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        holding_days=holding,
        profile=profile,
        universe_filters=UNIVERSE,
        panel=panel,
    )

    # First principles, part 2: the engine's realized schedule equals the
    # shared grid minus ONLY the trailing signals whose scheduled exit falls
    # beyond the window (engine default D3 exclusion — disclosed, mirrored
    # here WITHOUT calling the shipped checker).
    expected: list[str] = []
    for signal_index in grid:
        if signal_index + delay + holding >= len(dates):
            break
        expected.append(dates[signal_index].date().isoformat())
    realized = [str(row["signal_date"]) for row in run.result.resolved_schedule]
    assert realized == expected
    assert run.expected_signal_dates == tuple(expected)
    assert len(expected) >= 2

    # Every traded signal date has a fitted weight vector from the SAME grid
    # slot — the engine can never trade a date the fit did not cover.
    weights_by_date = {
        entry.signal_date.date().isoformat(): entry for entry in fitted.weights_path
    }
    for row in run.result.resolved_schedule:
        assert str(row["signal_date"]) in weights_by_date

    # Causality spot check inherited from the a-priori twin: every realized
    # signal is exactly `delay` bars before its entry.
    index_of = {date.date().isoformat(): position for position, date in enumerate(dates)}
    for row in run.result.resolved_schedule:
        assert index_of[str(row["entry_date"])] - index_of[str(row["signal_date"])] == delay
