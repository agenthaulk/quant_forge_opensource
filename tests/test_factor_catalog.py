from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_engine.signal_processing import prepare_factor_scores_result
from quant_forge.factor_library.catalog import (
    FactorCatalog,
    discover_factor_value_roots,
    import_precomputed_factors,
    normalize_precomputed_factor_store,
)
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
    assert by_id["WQ_ALPHA_003"].formula == "precomputed:factor_id=WQ_ALPHA_003"
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


def test_precomputed_scores_do_not_route_gtja_ids_to_worldquant_alias(tmp_path: Path) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-02"]),
            "instrument": ["AAA", "BBB"],
            "close": [10.0, 11.0],
            "is_st": [False, False],
        }
    )
    factor_values_root = tmp_path / "factor_values"
    _write_precomputed_factor(
        factor_values_root / "worldquant_alpha_099",
        factor_id="WQ_ALPHA_099",
        factor_name="worldquant_alpha_099",
        factor_store_key="worldquant_alpha_099",
        value=0.99,
    )
    gtja_dir = factor_values_root / "factor_id=GTJA_ALPHA_099"
    _write_precomputed_factor(
        gtja_dir,
        factor_id="GTJA_ALPHA_099",
        factor_name="alpha_099",
        factor_store_key="factor_id=GTJA_ALPHA_099",
        value=0.42,
    )

    result = prepare_factor_scores_result(
        panel,
        "precomputed:factor_id=GTJA_ALPHA_099",
        factor_id="GTJA_ALPHA_099",
        factor_name="alpha_099",
        factor_values_root=factor_values_root,
    )

    assert result.factor_values_path == gtja_dir
    assert result.source == "factor_values_cached_partial"
    assert list(result.scores["score"]) == [0.42]


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
    assert registered.formula == "precomputed:factor_id=WQ_ALPHA_003"
    assert registered.source == "precomputed"
    assert str(factor_values_root) not in payload


def test_catalog_canonicalizes_legacy_local_precomputed_registration(tmp_path: Path) -> None:
    factor_root = tmp_path / "factor_root"
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="WQ_ALPHA_003",
            name="worldquant_alpha_003",
            formula="precomputed:worldquant_alpha_003",
            status="candidate",
            source="precomputed",
        )
    )
    factor_values_root = _mounted_wq_factor_values(tmp_path)

    factor = FactorCatalog(factor_root, factor_values_root=factor_values_root).get("WQ_ALPHA_003")

    assert factor.formula == "precomputed:factor_id=WQ_ALPHA_003"


def test_normalize_precomputed_store_creates_canonical_factor_id_partition(tmp_path: Path) -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-02"]),
            "instrument": ["AAA", "BBB"],
            "close": [10.0, 11.0],
            "is_st": [False, False],
        }
    )
    factor_values_root = _mounted_wq_factor_values(tmp_path)
    manifest_root = tmp_path / "manifests"

    result = normalize_precomputed_factor_store(factor_values_root, manifest_root=manifest_root)

    canonical_dir = factor_values_root / "factor_id=WQ_ALPHA_003"
    metadata = json.loads((canonical_dir / "WQ_ALPHA_003.metadata.json").read_text(encoding="utf-8"))
    manifest = json.loads((manifest_root / "WQ_ALPHA_003.json").read_text(encoding="utf-8"))
    reread_factor = FactorCatalog(
        Path("unused"),
        factor_values_root=factor_values_root,
        factor_values_manifest_root=manifest_root,
    ).get("alpha_003")
    assert result.discovered_count == 1
    assert result.legacy_count == 1
    assert result.created_count == 1
    assert (canonical_dir / "2025.parquet").exists()
    assert not (canonical_dir / "metadata.json").exists()
    assert not (canonical_dir / "2025.metadata.json").exists()
    assert metadata["factor_store_key"] == "factor_id=WQ_ALPHA_003"
    assert metadata["legacy_store_key"] == "worldquant_alpha_003"
    assert metadata["factor_values_relative_path"] == "factor_id=WQ_ALPHA_003"
    assert "machine_path_note" not in metadata
    assert metadata["labels"] == ["portable"]
    assert manifest["canonical_store_key"] == "factor_id=WQ_ALPHA_003"
    assert "metadata_path" not in manifest
    assert "source_root" not in manifest
    assert "metadata_path" not in reread_factor.description
    assert "source_root" not in reread_factor.description

    listed = reread_factor
    assert listed.formula == "precomputed:factor_id=WQ_ALPHA_003"

    score_result = prepare_factor_scores_result(
        panel,
        listed.formula,
        factor_id=listed.factor_id,
        factor_name=listed.name,
        factor_values_root=factor_values_root,
    )
    assert score_result.factor_values_path == canonical_dir
    assert list(score_result.scores["score"]) == [0.3]


def test_normalize_precomputed_store_merges_extra_source_roots(tmp_path: Path) -> None:
    target_root = tmp_path / "canonical" / "factor=cn_a"
    source_root = tmp_path / "facotrs" / "wq77" / "factor_values"
    _write_precomputed_factor(
        source_root / "factor_id=WQ_ALPHA_004",
        factor_id="WQ_ALPHA_004",
        factor_name="worldquant_alpha_004",
        factor_store_key="factor_id=WQ_ALPHA_004",
        value=0.4,
    )
    manifest_root = tmp_path / "catalog" / "manifests" / "market=cn_a" / "dataset=factor_values"

    result = normalize_precomputed_factor_store(
        target_root,
        manifest_root=manifest_root,
        source_roots=(source_root,),
        link_files=True,
    )

    target_dir = target_root / "factor_id=WQ_ALPHA_004"
    assert result.discovered_count == 1
    assert result.source_roots == (source_root,)
    assert result.created_count == 1
    assert result.items[0].action == "create"
    assert (target_dir / "2025.parquet").exists()
    assert (source_root / "factor_id=WQ_ALPHA_004" / "2025.parquet").exists()
    assert json.loads((manifest_root / "WQ_ALPHA_004.json").read_text(encoding="utf-8"))[
        "factor_values_relative_path"
    ] == "factor_id=WQ_ALPHA_004"

    listed = FactorCatalog(Path("unused"), factor_values_root=target_root, factor_values_manifest_root=manifest_root).get(
        "alpha_004"
    )
    assert listed.formula == "precomputed:factor_id=WQ_ALPHA_004"


def test_normalize_precomputed_store_preserves_conflicting_source_files(tmp_path: Path) -> None:
    target_root = tmp_path / "canonical" / "factor=cn_a"
    source_root = tmp_path / "old" / "factor_values"
    _write_precomputed_factor(
        target_root / "factor_id=WQ_ALPHA_006",
        factor_id="WQ_ALPHA_006",
        factor_name="worldquant_alpha_006",
        factor_store_key="factor_id=WQ_ALPHA_006",
        value=0.6,
    )
    _write_precomputed_factor(
        source_root / "factor_id=WQ_ALPHA_006",
        factor_id="WQ_ALPHA_006",
        factor_name="worldquant_alpha_006",
        factor_store_key="factor_id=WQ_ALPHA_006",
        value=0.7,
    )

    result = normalize_precomputed_factor_store(target_root, source_roots=(source_root,), link_files=True)

    source_item = next(item for item in result.items if item.source_dir.parent == source_root)
    target_values = pd.read_parquet(target_root / "factor_id=WQ_ALPHA_006" / "2025.parquet")
    conflict_values = pd.read_parquet(
        target_root / "factor_id=WQ_ALPHA_006" / "conflicts" / "factor_id=WQ_ALPHA_006" / "2025.parquet"
    )
    assert source_item.action == "merge"
    assert source_item.files_conflicted == 1
    assert list(target_values["factor_value"]) == [0.6]
    assert list(conflict_values["factor_value"]) == [0.7]


def test_discover_factor_value_roots_finds_mounted_sources_without_factor_dirs(tmp_path: Path) -> None:
    canonical_root = tmp_path / "canonical" / "factor=cn_a"
    source_root = tmp_path / "facotrs" / "wq77" / "factor_values"
    _write_precomputed_factor(
        canonical_root / "factor_id=FTR_LOCAL",
        factor_id="FTR_LOCAL",
        factor_name="local",
        factor_store_key="factor_id=FTR_LOCAL",
        value=1.0,
    )
    _write_precomputed_factor(
        source_root / "factor_id=WQ_ALPHA_005",
        factor_id="WQ_ALPHA_005",
        factor_name="worldquant_alpha_005",
        factor_store_key="factor_id=WQ_ALPHA_005",
        value=0.5,
    )

    roots = discover_factor_value_roots(tmp_path)

    assert canonical_root in roots
    assert source_root in roots
    assert canonical_root / "factor_id=FTR_LOCAL" not in roots


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
                "labels": ["C:\\local\\source", "portable"],
                "machine_path_note": "C:\\local\\source\\2025.parquet",
                "metadata_path": "machine-local-source",
                "schema_version": "qf.canonical_factor_values.v1",
                "source_root": "machine-local-source",
                "universe": "cn_a_full_market",
            }
        ),
        encoding="utf-8",
    )
    (factor_dir / "metadata.json").write_text(
        json.dumps({"path": "C:\\local\\source\\metadata.json", "rows": 1}),
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


def _write_precomputed_factor(
    factor_dir: Path,
    *,
    factor_id: str,
    factor_name: str,
    factor_store_key: str,
    value: float,
) -> None:
    factor_dir.mkdir(parents=True)
    (factor_dir / "2025.metadata.json").write_text(
        json.dumps(
            {
                "factor_id": factor_id,
                "factor_name": factor_name,
                "factor_store_key": factor_store_key,
                "schema_version": "qf.canonical_factor_values.v1",
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02"],
            "instrument": ["AAA"],
            "factor_id": [factor_id],
            "factor_value": [value],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)
