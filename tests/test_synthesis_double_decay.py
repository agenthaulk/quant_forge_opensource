"""LA-1: decay is a member-level transform, applied exactly once.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.1, §12,
§13 test_double_decay): members fetched under the shared profile are already
EWMA-decayed INSIDE ``prepare_factor_scores_result`` (the decay branch is
unconditional on formula type), so the profile handed to the engine over the
materialized composite must pin ``decay_days=0``. A two-member run with
``decay_days=10`` must store composite values that are NOT EWMA'd a second
time, and the engine-driving profile must carry ``decay_days=0``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from quant_forge.core.contracts import SimulationProfile
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import (
    _apply_ewma_decay,
    prepare_factor_scores_result,
)
from quant_forge.synthesis.service import (
    build_apriori_composite,
    derive_composite_id,
    run_composite_backtest,
)

UNIVERSE = ("is_st == false",)
SHARED_DECAY_DAYS = 10


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


def test_composite_values_are_not_decayed_a_second_time(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    shared_profile = SimulationProfile(decay_days=SHARED_DECAY_DAYS)

    # Members are EWMA-decayed INSIDE this fetch (decay_days=10 on the shared
    # profile) — the one and only decay application (LA-1).
    members = {
        "F_MEM_ALPHA": prepare_factor_scores_result(
            panel, "rank(return_5d)", UNIVERSE, profile=shared_profile
        ).scores,
        "F_MEM_BETA": prepare_factor_scores_result(
            panel, "rank(market_cap)", UNIVERSE, profile=shared_profile
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
        decay_days=SHARED_DECAY_DAYS,
        execution_delay_days=1,
        top_quantile=0.3,
        coverage_rule="all_factors",
        min_factor_coverage=None,
        universe_filters=UNIVERSE,
        holding_days=5,
    )

    run = run_composite_backtest(
        composite,
        composite_id=composite_id,
        factor_root=tmp_path / "factor_root",
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        holding_days=5,
        profile=shared_profile,
        universe_filters=UNIVERSE,
        panel=panel,
    )

    # The engine-driving profile pins decay to 0 (LA-1) — structurally, not by
    # caller discipline — and that is what the engine actually ran with.
    assert shared_profile.decay_days == SHARED_DECAY_DAYS
    assert run.engine_profile.decay_days == 0
    assert run.result.simulation_profile.decay_days == 0
    artifact = json.loads(run.result.artifact_path.read_text(encoding="utf-8"))
    assert artifact["simulation_profile"]["decay_days"] == 0

    # Stored values read back EXACTLY equal to the member-decayed composite:
    # the precomputed read path applied no second EWMA.
    readback = prepare_factor_scores_result(
        panel,
        run.materialized.formula,
        UNIVERSE,
        profile=run.engine_profile,
        factor_id=composite_id,
        factor_name=composite_id,
        factor_values_overlay_root=run.overlay_root,
    ).scores
    expected = composite.sort_values(["trade_date", "instrument"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        readback.sort_values(["trade_date", "instrument"]).reset_index(drop=True),
        expected,
    )

    # Teeth: a second EWMA pass is NOT a no-op on this fixture, so the
    # equality above genuinely rules out double decay.
    double_decayed = _apply_ewma_decay(expected, SHARED_DECAY_DAYS)
    merged = expected.merge(
        double_decayed,
        on=["trade_date", "instrument"],
        suffixes=("_once", "_twice"),
    )
    divergence = (merged["score_once"] - merged["score_twice"]).abs()
    assert float(divergence.max()) > 1e-9
