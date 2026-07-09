"""RB-5 grid fidelity: the engine's realized schedule equals the shared grid.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.4 RB-5,
§10 step 5, §13 test_grid_fidelity): synthesis and the engine must not derive
schedules independently — ``rebalance_indices`` is the single source of
truth, and after every composite run the workflow asserts
``resolved_schedule.signal_dates == dates[rebalance_indices(...)]`` (filtered
only by the engine's own disclosed D3 final-partial exclusion). The
expectation here is recomputed from first principles in the test — not via
the shipped checker — so a broken checker cannot vouch for itself; a
divergence surfaces as a hard ``GridFidelityError``, never silently.
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
    GridFidelityError,
    assert_engine_schedule_matches_grid,
    build_apriori_composite,
    derive_composite_id,
    expected_engine_signal_dates,
    run_composite_backtest,
)

UNIVERSE = ("is_st == false",)


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
def test_realized_engine_schedule_equals_shared_grid(
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
    composite = build_apriori_composite(
        members,
        directions={"F_MEM_ALPHA": 1, "F_MEM_BETA": -1},
        standardization="zscore",
        method="equal_weight",
    ).composite
    composite_id = derive_composite_id(
        factor_refs=(("F_MEM_ALPHA", 1), ("F_MEM_BETA", -1)),
        method="equal_weight",
        method_params=None,
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
        composite,
        composite_id=composite_id,
        factor_root=tmp_path / "factor_root",
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        holding_days=holding,
        profile=profile,
        universe_filters=UNIVERSE,
        panel=panel,
    )

    # A-priori expectation rebuilt from first principles: the shared grid,
    # minus only the trailing signals whose scheduled exit falls beyond the
    # window (engine default D3 exclusion — disclosed, and mirrored here
    # WITHOUT calling the shipped checker).
    dates = sorted(panel["trade_date"].drop_duplicates())
    expected: list[str] = []
    for signal_index in rebalance_indices(
        dates, delay=delay, holding=holding, start_signal_index=0
    ):
        if signal_index + delay + holding >= len(dates):
            break
        expected.append(dates[signal_index].date().isoformat())

    realized = [str(row["signal_date"]) for row in run.result.resolved_schedule]
    assert realized == expected
    assert run.expected_signal_dates == tuple(expected)
    assert len(expected) >= 2
    # Every realized signal is exactly `delay` bars before its entry.
    index_of = {date.date().isoformat(): position for position, date in enumerate(dates)}
    for row in run.result.resolved_schedule:
        assert index_of[str(row["entry_date"])] - index_of[str(row["signal_date"])] == delay


def test_grid_divergence_is_a_hard_error_never_silent() -> None:
    dates = list(pd.bdate_range("2026-01-05", periods=15))
    expected = expected_engine_signal_dates(dates, delay=1, holding=3)
    schedule = [{"signal_date": item.date().isoformat()} for item in expected]

    # Matching schedule passes and returns the expected labels for provenance.
    labels = assert_engine_schedule_matches_grid(schedule, dates, delay=1, holding=3)
    assert labels == tuple(item.date().isoformat() for item in expected)

    with pytest.raises(GridFidelityError):
        assert_engine_schedule_matches_grid(schedule[:-1], dates, delay=1, holding=3)

    tampered = [dict(row) for row in schedule]
    tampered[0]["signal_date"] = "2025-12-31"
    with pytest.raises(GridFidelityError):
        assert_engine_schedule_matches_grid(tampered, dates, delay=1, holding=3)

    extra = [*schedule, {"signal_date": dates[-1].date().isoformat()}]
    with pytest.raises(GridFidelityError):
        assert_engine_schedule_matches_grid(extra, dates, delay=1, holding=3)
