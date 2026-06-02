from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_engine.signal_processing import prepare_factor_scores_result
from quant_forge.factor_library.catalog import FactorCatalog, import_precomputed_factors
from quant_forge.factor_library.repository import FactorRepository


def test_catalog_lists_registered_and_mounted_precomputed_factors(tmp_path: Path) -> None:
    factor_root = tmp_path / "factor_root"
    repo = FactorRepository(factor_root)
    repo.save(
        FactorDefinition(
            factor_id="FTR_LOCAL",
            name="local_factor",
            formula="rank(close)",
            status="candidate",
        )
    )
    factor_values_root = _mounted_wq_factor_values(tmp_path)

    factors = FactorCatalog(factor_root, factor_values_root=factor_values_root).list()

    by_id = {factor.factor_id: factor for factor in factors}
    assert set(by_id) == {"FTR_LOCAL", "WQ_ALPHA_003"}
    assert by_id["WQ_ALPHA_003"].name == "worldquant_alpha_003"
    assert by_id["WQ_ALPHA_003"].formula == "precomputed:worldquant_alpha_003"
    assert by_id["WQ_ALPHA_003"].source == "precomputed"
    assert FactorCatalog(factor_root, factor_values_root=factor_values_root).get("alpha_003").factor_id == "WQ_ALPHA_003"


def test_precomputed_scores_use_partial_cache_without_recomputing(tmp_path: Path) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-02"]),
            "instrument": ["AAA", "BBB"],
            "close": [10.0, 11.0],
            "market_cap": [100.0, 200.0],
            "is_st": [False, False],
        }
    )
    factor_values_root = _mounted_wq_factor_values(tmp_path)

    result = prepare_factor_scores_result(
        panel,
        "precomputed:worldquant_alpha_003",
        factor_id="WQ_ALPHA_003",
        factor_name="worldquant_alpha_003",
        factor_values_root=factor_values_root,
    )

    assert result.source == "factor_values_cached_partial"
    assert result.cached_rows == 1
    assert result.computed_rows == 0
    assert list(result.scores["instrument"]) == ["AAA"]
    assert list(result.scores["score"]) == [0.3]


def test_import_precomputed_registers_selected_factors_without_mount_paths(tmp_path: Path) -> None:
    factor_root = tmp_path / "factor_root"
    factor_values_root = _mounted_wq_factor_values(tmp_path)

    imported = import_precomputed_factors(
        factor_root,
        factor_values_root=factor_values_root,
        factor_ids=("alpha_003",),
    )
    registered = FactorRepository(factor_root).get("WQ_ALPHA_003")
    payload = (factor_root / "inactive_factors" / "WQ_ALPHA_003" / "factor.yaml").read_text(encoding="utf-8")

    assert [factor.factor_id for factor in imported] == ["WQ_ALPHA_003"]
    assert registered.formula == "precomputed:worldquant_alpha_003"
    assert registered.source == "precomputed"
    assert str(factor_values_root) not in payload


def _mounted_wq_factor_values(tmp_path: Path) -> Path:
    factor_values_root = tmp_path / "factor_values"
    factor_dir = factor_values_root / "worldquant_alpha_003"
    factor_dir.mkdir(parents=True)
    (factor_dir / "2025.metadata.json").write_text(
        json.dumps(
            {
                "factor_id": "WQ_ALPHA_003",
                "factor_name": "worldquant_alpha_003",
                "factor_store_key": "worldquant_alpha_003",
                "schema_version": "qf.canonical_factor_values.v1",
                "universe": "cn_a_full_market",
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02"],
            "instrument_id": ["AAA"],
            "factor_id": ["WQ_ALPHA_003"],
            "factor_value": [0.3],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)
    return factor_values_root
