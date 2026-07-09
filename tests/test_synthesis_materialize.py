"""RF-2 / RF-3 materialization: store-resolved write, engine read-back, cleanup.

Design contract (docs/design/multi_factor_portfolio_backtest.md §11, §13
test_materialize, CP0 RF-3 amendment): the composite is written through the
value store's OWN path resolution (``_resolve_factor_paths`` +
``write_incremental_values``) — never a hand-built ``overlay_root/<id>``
directory — and the ENGINE must actually read the rows back through the
precomputed/cache path with ``factor_values_overlay_root`` set. The
definition is registered via ``FactorRepository(factor_root).save`` (there is
no ``register_factor`` symbol) and must be discoverable through
``FactorCatalog``. On any failure after materialization begins, both
artifacts — the registered definition and the per-run overlay — are removed
(a pre-existing definition is restored, mirroring the single-factor
validation restore pattern).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from quant_forge.core.contracts import FactorDefinition, SimulationProfile
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import prepare_factor_scores_result
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.synthesis.service import (
    build_apriori_composite,
    derive_composite_id,
    materialize_composite,
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


def _member_scores(panel: pd.DataFrame, profile: SimulationProfile) -> dict[str, pd.DataFrame]:
    alpha = prepare_factor_scores_result(panel, "rank(return_5d)", UNIVERSE, profile=profile).scores
    beta = prepare_factor_scores_result(panel, "rank(market_cap)", UNIVERSE, profile=profile).scores
    return {"F_MEM_ALPHA": alpha, "F_MEM_BETA": beta}


def _composite_id(**overrides: object) -> str:
    base: dict[str, object] = {
        "factor_refs": (("F_MEM_ALPHA", 1), ("F_MEM_BETA", -1)),
        "method": "equal_weight",
        "method_params": None,
        "standardization": "zscore",
        "backtest_start": None,
        "backtest_end": None,
        "decay_days": 0,
        "execution_delay_days": 1,
        "top_quantile": 0.3,
        "coverage_rule": "all_factors",
        "min_factor_coverage": None,
        "universe_filters": UNIVERSE,
    }
    return derive_composite_id(**{**base, **overrides})  # type: ignore[arg-type]


def _build_composite(panel: pd.DataFrame, profile: SimulationProfile) -> pd.DataFrame:
    result = build_apriori_composite(
        _member_scores(panel, profile),
        directions={"F_MEM_ALPHA": 1, "F_MEM_BETA": -1},
        standardization="zscore",
        method="equal_weight",
    )
    return result.composite


def test_engine_reads_materialized_rows_back_through_the_cache_path(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile()
    composite = _build_composite(panel, profile)
    composite_id = _composite_id()

    run = run_composite_backtest(
        composite,
        composite_id=composite_id,
        factor_root=tmp_path / "factor_root",
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        holding_days=5,
        profile=profile,
        universe_filters=UNIVERSE,
        composite_name="composite_equal_weight",
    )

    # RF-3 evidence: the engine resolved the precomputed formula and read the
    # cached rows from the per-run overlay — a hand-built directory (or a
    # signature mismatch) would read zero rows and yield an empty schedule.
    assert run.result.periods > 0
    assert run.result.score_compute_mode == "cache_only"
    assert run.result.score_cached_rows > 0
    assert run.result.score_source in {"factor_values_cached", "factor_values_cached_partial"}
    # Store-resolved layout under the per-run overlay, not a hand-built dir.
    assert run.materialized.values_dir.is_relative_to(run.overlay_root)
    assert run.materialized.values_dir.name == f"factor_id={composite_id}"
    assert run.overlay_root.is_relative_to(tmp_path / "artifacts")

    # Value-level round-trip through the FULL shipped read path with the
    # engine-driving profile: what was written is exactly what is read.
    readback = prepare_factor_scores_result(
        panel,
        run.materialized.formula,
        UNIVERSE,
        profile=run.engine_profile,
        factor_id=composite_id,
        factor_name=composite_id,
        factor_values_overlay_root=run.overlay_root,
    ).scores
    expected = (
        composite.sort_values(["trade_date", "instrument"]).reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(
        readback.sort_values(["trade_date", "instrument"]).reset_index(drop=True),
        expected,
    )


def test_definition_is_discoverable_via_the_catalog(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile()
    composite_id = _composite_id()

    run = run_composite_backtest(
        _build_composite(panel, profile),
        composite_id=composite_id,
        factor_root=tmp_path / "factor_root",
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        holding_days=5,
        profile=profile,
        universe_filters=UNIVERSE,
        panel=panel,
    )

    # RF-2: the definition lives in factor_root and resolves through the same
    # FactorCatalog.get the engine uses — with the identical formula/filters,
    # so the read-time value-store signature equals the written one.
    resolved = FactorCatalog(tmp_path / "factor_root").get(composite_id)
    assert resolved.factor_id == composite_id
    assert resolved.formula == f"precomputed:factor_id={composite_id}"
    assert resolved.source == "synthesis"
    assert resolved.status == "candidate"
    assert resolved.horizon_days == 5
    assert tuple(resolved.universe_filters) == UNIVERSE
    # On SUCCESS the definition stays (first-class factor; retention policy is
    # a recorded deferred question) and the overlay remains readable.
    assert run.materialized.definition_path.exists()
    assert run.overlay_root.exists()


def test_failure_after_materialization_removes_definition_and_overlay(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile()
    composite_id = _composite_id()
    overlay_root = tmp_path / "artifacts" / "overlay_run_fail"

    # holding_days far beyond the window: the ENGINE raises after the
    # composite was already materialized and registered.
    with pytest.raises(ValueError):
        run_composite_backtest(
            _build_composite(panel, profile),
            composite_id=composite_id,
            factor_root=tmp_path / "factor_root",
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            holding_days=200,
            profile=profile,
            universe_filters=UNIVERSE,
            overlay_root=overlay_root,
            panel=panel,
        )

    assert not overlay_root.exists()
    with pytest.raises(FileNotFoundError):
        FactorRepository(tmp_path / "factor_root").get(composite_id)
    with pytest.raises(FileNotFoundError):
        FactorCatalog(tmp_path / "factor_root").get(composite_id)


def test_failure_restores_a_pre_existing_definition(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile()
    composite_id = _composite_id()
    repository = FactorRepository(tmp_path / "factor_root")
    # A prior successful run of the identical config registered the same id
    # (horizon 7 as a sentinel); a later failing run must restore it, not
    # delete it (mirror of _restore_factor_after_failed_validation).
    repository.save(
        FactorDefinition(
            factor_id=composite_id,
            name=composite_id,
            formula=f"precomputed:factor_id={composite_id}",
            status="candidate",
            horizon_days=7,
            universe_filters=UNIVERSE,
            source="synthesis",
        )
    )

    with pytest.raises(ValueError):
        run_composite_backtest(
            _build_composite(panel, profile),
            composite_id=composite_id,
            factor_root=tmp_path / "factor_root",
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            holding_days=200,
            profile=profile,
            universe_filters=UNIVERSE,
            panel=panel,
        )

    assert repository.get(composite_id).horizon_days == 7


def test_failure_before_any_write_still_cleans_the_per_run_overlay(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile()
    overlay_root = tmp_path / "artifacts" / "overlay_run_invalid"
    invalid = _build_composite(panel, profile).drop(columns=["score"])

    with pytest.raises(ValueError):
        run_composite_backtest(
            invalid,
            composite_id=_composite_id(),
            factor_root=tmp_path / "factor_root",
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            holding_days=5,
            profile=profile,
            universe_filters=UNIVERSE,
            overlay_root=overlay_root,
            panel=panel,
        )

    assert not overlay_root.exists()


def test_materialize_rejects_non_canonical_ids(tmp_path: Path) -> None:
    # Lowercase hex would be rewritten by the catalog's canonicalization, so
    # the write-time formula (and value-store signature) would differ from the
    # read-time one and the engine would read zero rows. Rejected up front.
    frame = pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2026-01-05")],
            "instrument": ["STK000"],
            "score": [1.0],
        }
    )
    with pytest.raises(ValueError):
        materialize_composite(
            frame,
            factor_root=tmp_path / "factor_root",
            overlay_root=tmp_path / "overlay",
            composite_id="COMPOSITE_abc123def456",
            holding_days=5,
            universe_filters=(),
        )
    with pytest.raises(ValueError):
        materialize_composite(
            frame,
            factor_root=tmp_path / "factor_root",
            overlay_root=tmp_path / "overlay",
            composite_id="composite:legacy",
            holding_days=5,
            universe_filters=(),
        )


def test_holding_days_is_required_with_no_horizon_fallback(tmp_path: Path) -> None:
    # RF-5: the composite path never defaults holding_days from horizon_days.
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile()
    for bad_holding in (None, 0, True):
        with pytest.raises(ValueError):
            run_composite_backtest(
                _build_composite(panel, profile),
                composite_id=_composite_id(),
                factor_root=tmp_path / "factor_root",
                data_root=tmp_path / "data",
                artifact_root=tmp_path / "artifacts",
                holding_days=bad_holding,  # type: ignore[arg-type]
                profile=profile,
                universe_filters=UNIVERSE,
                panel=panel,
            )
