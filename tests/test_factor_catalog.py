from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_engine.signal_processing import prepare_factor_scores_result
from quant_forge.factor_library.catalog import (
    FactorCatalog,
    _manifest_candidates,
    discover_factor_value_roots,
    discover_precomputed_factors,
    import_precomputed_factors,
    normalize_precomputed_factor_store,
)
from quant_forge.factor_library.repository import FactorRepository, normalize_factor_root_layout


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
    payload = (factor_root / "原始因子" / "inactive_factors" / "WQ_ALPHA_003" / "factor.yaml").read_text(encoding="utf-8")

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


def test_factor_repository_writes_synthetic_factors_under_synthetic_category(tmp_path: Path) -> None:
    factor_root = tmp_path / "factor_root"
    factor = FactorDefinition(
        factor_id="RD_SYN_TEST",
        name="synthetic_test",
        formula="precomputed:factor_id=RD_SYN_TEST",
        status="candidate",
        source="rd",
    )

    path = FactorRepository(factor_root).save(factor)
    loaded = FactorRepository(factor_root).get("RD_SYN_TEST")

    assert path == factor_root / "合成因子" / "inactive_factors" / "RD_SYN_TEST" / "factor.yaml"
    assert loaded.factor_id == "RD_SYN_TEST"


def test_normalize_factor_root_layout_copies_legacy_definitions_and_dedupes_list(tmp_path: Path) -> None:
    factor_root = tmp_path / "factor_root"
    legacy_dir = factor_root / "inactive_factors" / "FTR_LEGACY"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "factor.yaml").write_text(
        "\n".join(
            [
                "factor_id: FTR_LEGACY",
                "name: legacy_factor",
                "formula: rank(close)",
                "status: candidate",
                "description: Legacy definition.",
                "horizon_days: 5",
                "universe_filters: []",
                "source: user",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_factor_root_layout(factor_root)
    categorized_path = factor_root / "原始因子" / "inactive_factors" / "FTR_LEGACY" / "factor.yaml"
    listed = FactorRepository(factor_root).list()

    assert result.created_count == 1
    assert categorized_path.exists()
    assert legacy_dir.exists()
    assert [factor.factor_id for factor in listed] == ["FTR_LEGACY"]


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

    canonical_dir = factor_values_root / "原始因子" / "factor_id=WQ_ALPHA_003"
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
    assert metadata["factor_category"] == "original"
    assert metadata["factor_values_relative_path"] == "原始因子/factor_id=WQ_ALPHA_003"
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


def test_manifest_supplements_sidecar_metadata_without_factor_id(tmp_path: Path) -> None:
    factor_values_root = tmp_path / "factor_values"
    factor_dir = factor_values_root / "opaque_vendor_store"
    factor_dir.mkdir(parents=True)
    (factor_dir / "2025.metadata.json").write_text(
        json.dumps({"schema_version": "qf.vendor_sidecar.v1", "universe": "cn_a"}),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02"],
            "instrument": ["AAA"],
            "factor_value": [0.8],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir()
    (manifest_root / "opaque_vendor_store.json").write_text(
        json.dumps(
            {
                "factor_id": "FTR_OPAQUE_VENDOR",
                "factor_name": "opaque_vendor",
                "factor_store_key": "opaque_vendor_store",
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_precomputed_factors(factor_values_root, manifest_root=manifest_root)
    listed = FactorCatalog(
        Path("unused"),
        factor_values_root=factor_values_root,
        factor_values_manifest_root=manifest_root,
    ).get("FTR_OPAQUE_VENDOR")

    assert [factor.factor_id for factor in discovered] == ["FTR_OPAQUE_VENDOR"]
    assert listed.name == "opaque_vendor"
    assert listed.description == (
        "Precomputed factor values loaded from factor_values_root. "
        "schema=qf.vendor_sidecar.v1. universe=cn_a."
    )


def test_manifest_candidates_prefer_canonical_factor_id_over_alias_manifest(tmp_path: Path) -> None:
    factor_values_root = tmp_path / "factor_values"
    factor_dir = factor_values_root / "worldquant_alpha_003"
    factor_dir.mkdir(parents=True)
    (factor_dir / "2025.metadata.json").write_text(
        json.dumps(
            {
                "factor_id": "WQ_ALPHA_003",
                "schema_version": "qf.canonical_factor_values.v1",
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02"],
            "instrument": ["AAA"],
            "factor_id": ["WQ_ALPHA_003"],
            "factor_value": [0.3],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir()
    (manifest_root / "WQ_ALPHA_003.json").write_text(
        json.dumps(
            {
                "factor_name": "canonical_manifest_name",
                "universe": "canonical_universe",
            }
        ),
        encoding="utf-8",
    )
    (manifest_root / "worldquant_alpha_003.json").write_text(
        json.dumps(
            {
                "factor_name": "alias_manifest_name",
                "universe": "alias_universe",
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_precomputed_factors(factor_values_root, manifest_root=manifest_root)
    listed = FactorCatalog(
        Path("unused"),
        factor_values_root=factor_values_root,
        factor_values_manifest_root=manifest_root,
    ).get("alpha_003")

    assert [factor.factor_id for factor in discovered] == ["WQ_ALPHA_003"]
    assert discovered[0].name == "canonical_manifest_name"
    assert discovered[0].description == (
        "Precomputed factor values loaded from factor_values_root. "
        "schema=qf.canonical_factor_values.v1. universe=canonical_universe."
    )
    assert listed.name == "canonical_manifest_name"
    assert listed.description == (
        "Precomputed factor values loaded from factor_values_root. "
        "schema=qf.canonical_factor_values.v1. universe=canonical_universe."
    )


def test_manifest_candidates_fill_missing_fields_by_priority(tmp_path: Path) -> None:
    factor_values_root = tmp_path / "factor_values"
    factor_dir = factor_values_root / "factor_id=wq-alpha-008"
    factor_dir.mkdir(parents=True)
    (factor_dir / "2025.metadata.json").write_text(
        json.dumps({"schema_version": "qf.canonical_factor_values.v1"}),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": ["2025-01-02"],
            "instrument": ["AAA"],
            "factor_id": ["WQ_ALPHA_008"],
            "factor_value": [0.8],
        }
    ).to_parquet(factor_dir / "2025.parquet", index=False)
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir()
    (manifest_root / "WQ_ALPHA_008.json").write_text(
        json.dumps({"factor_id": "WQ_ALPHA_008", "universe": "canonical_universe"}),
        encoding="utf-8",
    )
    (manifest_root / "factor_id=WQ_ALPHA_008.json").write_text(
        json.dumps({"factor_name": "store_manifest_name", "universe": "store_universe"}),
        encoding="utf-8",
    )
    (manifest_root / "factor_id=wq-alpha-008.json").write_text(
        json.dumps({"factor_name": "directory_manifest_name", "universe": "directory_universe"}),
        encoding="utf-8",
    )

    discovered = discover_precomputed_factors(factor_values_root, manifest_root=manifest_root)

    assert [factor.factor_id for factor in discovered] == ["WQ_ALPHA_008"]
    assert discovered[0].name == "store_manifest_name"
    assert discovered[0].description == (
        "Precomputed factor values loaded from factor_values_root. "
        "schema=qf.canonical_factor_values.v1. universe=canonical_universe."
    )


def test_catalog_skips_macos_appledouble_entries_before_stat(tmp_path: Path, monkeypatch) -> None:
    factor_values_root = tmp_path / "factor_values"
    _write_precomputed_factor(
        factor_values_root / "原始因子" / "factor_id=WQ_ALPHA_003",
        factor_id="WQ_ALPHA_003",
        factor_name="worldquant_alpha_003",
        factor_store_key="factor_id=WQ_ALPHA_003",
        value=0.3,
    )
    (factor_values_root / "._原始因子").write_bytes(b"appledouble")

    original_is_dir = Path.is_dir

    def guarded_is_dir(path: Path) -> bool:
        if path.name.startswith("._"):
            raise PermissionError(f"operation not permitted: {path}")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

    discovered = discover_precomputed_factors(factor_values_root)

    assert [factor.factor_id for factor in discovered] == ["WQ_ALPHA_003"]


def test_manifest_candidates_are_ordered_and_deduped(tmp_path: Path) -> None:
    manifest_root = tmp_path / "manifests"

    assert _manifest_candidates(tmp_path / "factor_id=WQ_ALPHA_007", manifest_root) == (
        manifest_root / "WQ_ALPHA_007.json",
        manifest_root / "factor_id=WQ_ALPHA_007.json",
    )
    assert _manifest_candidates(tmp_path / "factor_id=wq-alpha-007", manifest_root) == (
        manifest_root / "WQ_ALPHA_007.json",
        manifest_root / "factor_id=WQ_ALPHA_007.json",
        manifest_root / "factor_id=wq-alpha-007.json",
    )


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

    target_dir = target_root / "原始因子" / "factor_id=WQ_ALPHA_004"
    assert result.discovered_count == 1
    assert result.source_roots == (source_root,)
    assert result.created_count == 1
    assert result.items[0].action == "create"
    assert (target_dir / "2025.parquet").exists()
    assert (source_root / "factor_id=WQ_ALPHA_004" / "2025.parquet").exists()
    assert json.loads((manifest_root / "WQ_ALPHA_004.json").read_text(encoding="utf-8"))[
        "factor_values_relative_path"
    ] == "原始因子/factor_id=WQ_ALPHA_004"

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
    target_dir = target_root / "原始因子" / "factor_id=WQ_ALPHA_006"
    target_values = pd.read_parquet(target_dir / "2025.parquet")
    conflict_values = pd.read_parquet(
        target_dir / "conflicts" / "factor_id=WQ_ALPHA_006" / "2025.parquet"
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


def test_catalog_get_resolves_canonical_factor_not_first_walked(tmp_path: Path) -> None:
    """CAT-1: get() resolves on the strict canonical key, not fuzzy intersection."""

    factor_values_root = tmp_path / "factor_values"
    # A distinct factor whose fuzzy alias set overlapped WQ_ALPHA_001 under the
    # old logic (name canonicalizes toward alpha_001) but has a distinct key.
    _write_precomputed_factor(
        factor_values_root / "factor_id=GTJA_ALPHA_001",
        factor_id="GTJA_ALPHA_001",
        factor_name="alpha_001",
        factor_store_key="factor_id=GTJA_ALPHA_001",
        value=0.11,
    )
    _write_precomputed_factor(
        factor_values_root / "factor_id=WQ_ALPHA_001",
        factor_id="WQ_ALPHA_001",
        factor_name="worldquant_alpha_001",
        factor_store_key="factor_id=WQ_ALPHA_001",
        value=0.99,
    )

    catalog = FactorCatalog(tmp_path / "factor_root", factor_values_root=factor_values_root)

    # Strict, unambiguous ids resolve to exactly the requested factor rather than
    # the first walked dir.
    assert catalog.get("wq_alpha_001").factor_id == "WQ_ALPHA_001"
    assert catalog.get("GTJA_ALPHA_001").factor_id == "GTJA_ALPHA_001"
    # A genuinely ambiguous alias (GTJA's own name is "alpha_001", which also
    # canonicalizes toward WQ_ALPHA_001) must raise rather than silently return
    # the first walked factor.
    with pytest.raises(ValueError):
        catalog.get("alpha_001")


def test_catalog_get_missing_id_raises_file_not_found(tmp_path: Path) -> None:
    factor_values_root = _mounted_wq_factor_values(tmp_path)
    catalog = FactorCatalog(tmp_path / "factor_root", factor_values_root=factor_values_root)
    with pytest.raises(FileNotFoundError):
        catalog.get("WQ_ALPHA_999")


def test_catalog_reads_horizon_days_from_manifest_metadata(tmp_path: Path) -> None:
    """CAT-3: mounted factor horizon comes from metadata, not hardcoded 5."""

    factor_values_root = tmp_path / "factor_values"
    _write_precomputed_factor_with_metadata(
        factor_values_root / "factor_id=WQ_ALPHA_010",
        factor_id="WQ_ALPHA_010",
        factor_name="worldquant_alpha_010",
        factor_store_key="factor_id=WQ_ALPHA_010",
        value=0.1,
        extra_metadata={"horizon_days": 10},
    )

    factor = FactorCatalog(
        tmp_path / "factor_root", factor_values_root=factor_values_root
    ).get("WQ_ALPHA_010")

    assert factor.horizon_days == 10


def test_catalog_horizon_falls_back_to_5_without_metadata(tmp_path: Path) -> None:
    factor_values_root = _mounted_wq_factor_values(tmp_path)

    factor = FactorCatalog(
        tmp_path / "factor_root", factor_values_root=factor_values_root
    ).get("WQ_ALPHA_003")

    assert factor.horizon_days == 5


def test_catalog_horizon_distrusts_metadata_when_factor_id_mismatches_dir(tmp_path: Path) -> None:
    """CAT-3: metadata horizon is not trusted when its factor_id disagrees with the dir."""

    factor_values_root = tmp_path / "factor_values"
    _write_precomputed_factor_with_metadata(
        factor_values_root / "factor_id=WQ_ALPHA_011",
        factor_id="WQ_ALPHA_777",  # disagrees with the dir/id resolved from name
        factor_name="worldquant_alpha_011",
        factor_store_key="factor_id=WQ_ALPHA_011",
        value=0.1,
        extra_metadata={"horizon_days": 20},
    )

    # Metadata factor_id (WQ_ALPHA_777) disagrees with the directory-derived id
    # (WQ_ALPHA_011); the factor resolves under the metadata id but its horizon
    # metadata is distrusted and falls back to 5.
    factor = FactorCatalog(
        tmp_path / "factor_root", factor_values_root=factor_values_root
    ).get("WQ_ALPHA_777")

    assert factor.horizon_days == 5


def test_catalog_horizon_coerces_and_rejects_invalid_metadata(tmp_path: Path) -> None:
    factor_values_root = tmp_path / "factor_values"
    _write_precomputed_factor_with_metadata(
        factor_values_root / "factor_id=WQ_ALPHA_012",
        factor_id="WQ_ALPHA_012",
        factor_name="worldquant_alpha_012",
        factor_store_key="factor_id=WQ_ALPHA_012",
        value=0.1,
        extra_metadata={"horizon_days": "8"},
    )
    _write_precomputed_factor_with_metadata(
        factor_values_root / "factor_id=WQ_ALPHA_013",
        factor_id="WQ_ALPHA_013",
        factor_name="worldquant_alpha_013",
        factor_store_key="factor_id=WQ_ALPHA_013",
        value=0.1,
        extra_metadata={"horizon_days": "not-a-number"},
    )
    catalog = FactorCatalog(tmp_path / "factor_root", factor_values_root=factor_values_root)

    assert catalog.get("WQ_ALPHA_012").horizon_days == 8
    assert catalog.get("WQ_ALPHA_013").horizon_days == 5


def test_dedupe_raises_on_cross_category_duplicate_with_distinct_content(tmp_path: Path) -> None:
    """CAT-2: same canonical id under both categories with differing content raises."""

    factor_values_root = tmp_path / "factor_values"
    _write_precomputed_factor(
        factor_values_root / "原始因子" / "factor_id=WQ_ALPHA_020",
        factor_id="WQ_ALPHA_020",
        factor_name="worldquant_alpha_020",
        factor_store_key="factor_id=WQ_ALPHA_020",
        value=0.2,
    )
    _write_precomputed_factor(
        factor_values_root / "合成因子" / "factor_id=WQ_ALPHA_020",
        factor_id="WQ_ALPHA_020",
        factor_name="synthetic_alpha_020",
        factor_store_key="factor_id=WQ_ALPHA_020",
        value=0.2,
    )

    with pytest.raises(ValueError):
        discover_precomputed_factors(factor_values_root)


def _write_precomputed_factor_with_metadata(
    factor_dir: Path,
    *,
    factor_id: str,
    factor_name: str,
    factor_store_key: str,
    value: float,
    extra_metadata: dict,
) -> None:
    factor_dir.mkdir(parents=True)
    metadata = {
        "factor_id": factor_id,
        "factor_name": factor_name,
        "factor_store_key": factor_store_key,
        "schema_version": "qf.canonical_factor_values.v1",
    }
    metadata.update(extra_metadata)
    (factor_dir / "2025.metadata.json").write_text(
        json.dumps(metadata),
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
