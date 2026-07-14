"""Characterization tests for the CP6-3 Data console + Registry endpoints.

Covers ``GET /api/data/catalog``, ``GET /api/data/status``,
``GET /api/registry/factors``, and ``GET /api/registry/factors/{factor_id}``,
pinning each payload shape the way ``tests/test_data_catalog_port.py`` pins
``GET /catalog``:

- happy-path payload shapes over the live HTTP adapter;
- token gate: every new endpoint returns 401 without the control token when
  the server binding requires one;
- input validation: bad ``limit`` / ``kind`` values are reflected as 400 and
  an unknown or malformed factor id maps to 404;
- path absence for ``/api/data/status``: ``data_root`` / ``panel_path`` keys
  never appear anywhere in the payload and no string value is shaped like a
  local absolute path;
- FP-4 None-vs-empty preservation for research tags (``columns_required`` is
  ``null`` for precomputed factors, ``[]`` for known-empty field tags) and
  MetricValue null-not-zero preservation in evidence-chain run rows;
- catalog read failure degrades the Registry list to an empty payload;
- routing dispatches all four builders late-bound through the server module
  namespace (the monkeypatch seam contract).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

import quant_forge.apps.web.api as web_api
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig, WebSettings
from quant_forge.core.contracts import FactorDefinition
from quant_forge.data.local import PANEL_FILE, create_demo_workspace, data_field_catalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.lineage.store import RunIndex


JSON_CONTENT_TYPE = "application/json; charset=utf-8"

AVAILABLE_WINDOW = {"start_date": "2025-01-05", "end_date": "2025-06-30", "status": "available"}

NULL_VALUE_METRIC = {
    "value": None,
    "unit": "t_stat",
    "status": "insufficient_sample",
    "observation_count": 3,
}


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


@pytest.fixture()
def web_app(web_config):
    server = create_local_web_server(host="127.0.0.1", port=0, config=web_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _append_run(
    config: QuantForgeConfig,
    *,
    run_id: str,
    kind: str,
    factor_ids: list[str],
    created_at: str,
    metric_highlights: dict | None = None,
    artifact_paths_rel: tuple[str, ...] = (),
    warnings_count: int = 0,
) -> None:
    RunIndex(config.paths.artifact_root).append_run(
        run_id=run_id,
        kind=kind,
        factor_ids=factor_ids,
        created_at=created_at,
        data_window=AVAILABLE_WINDOW,
        config_fingerprint="a" * 64,
        metric_highlights=metric_highlights or {},
        artifact_paths_rel=artifact_paths_rel,
        warnings_count=warnings_count,
    )


def _walk_payload(node, keys: set, strings: set) -> None:
    if isinstance(node, dict):
        for key, item in node.items():
            keys.add(key)
            _walk_payload(item, keys, strings)
    elif isinstance(node, list):
        for item in node:
            _walk_payload(item, keys, strings)
    elif isinstance(node, str):
        strings.add(node)


# ---------------------------------------------------------------------------
# GET /api/data/catalog (G1)
# ---------------------------------------------------------------------------


def test_data_catalog_lists_declared_fields_with_role_and_tags(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/data/catalog")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"fields", "count"}
    assert payload["count"] == len(payload["fields"]) == len(data_field_catalog())
    by_name = {entry["name"]: entry for entry in payload["fields"]}
    assert set(by_name) == {item.name for item in data_field_catalog()}
    # Unlike the pinned /catalog projection, key columns and roles are shown.
    assert by_name["trade_date"]["role"] == "key"
    assert by_name["market_cap"]["role"] == "required"
    assert by_name["return_5d"]["role"] == "optional"
    for entry in payload["fields"]:
        assert set(entry) == {"name", "description", "role", "tags"}
        assert entry["role"] in {"key", "required", "optional"}
        tags = entry["tags"]
        assert tags["schema_version"] == "qf.research_tags.v1"
        assert tags["subject_kind"] == "field"
        assert tags["subject_id"] == entry["name"]


def test_data_catalog_preserves_none_vs_empty_tag_values(web_app) -> None:
    """FP-4: null stays null and known-empty collections stay [] in G1 tags."""

    _, _, body = _get(f"{web_app}/api/data/catalog")
    by_name = {entry["name"]: entry["tags"] for entry in json.loads(body.decode("utf-8"))["fields"]}

    # close observably requires no other panel columns: [] (known-empty),
    # and carries no notes: null, never "" or a fabricated value.
    assert by_name["close"]["columns_required"] == []
    assert by_name["close"]["notes"] is None
    # Derived fields keep their loader facts verbatim.
    assert by_name["return_5d"]["columns_required"] == ["close"]
    assert by_name["return_5d"]["min_warmup_bars"] == 6
    assert isinstance(by_name["is_st"]["notes"], str) and by_name["is_st"]["notes"]


# ---------------------------------------------------------------------------
# GET /api/data/status (G2)
# ---------------------------------------------------------------------------


def test_data_status_reports_coverage_quality_and_availability(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/data/status")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"ok", "coverage", "quality", "fields"}
    assert payload["ok"] is True
    assert set(payload["coverage"]) == {"rows", "instruments", "date_count", "start_date", "end_date"}
    assert payload["coverage"]["rows"] > 0
    assert payload["coverage"]["instruments"] == 12
    assert payload["coverage"]["date_count"] == 160
    assert payload["coverage"]["start_date"] == "2024-01-02"
    assert set(payload["quality"]) == {"missing_columns", "problems", "synthesized_columns", "optional_columns"}
    assert payload["quality"]["missing_columns"] == []
    assert payload["quality"]["problems"] == []
    assert payload["quality"]["synthesized_columns"] == []
    assert sorted(payload["quality"]["optional_columns"]) == [
        "return_1d",
        "return_5d",
        "volatility_5d",
        "volume",
    ]
    # Availability rows are labels only (FP-4), one per declared non-key field.
    declared_non_key = [item.name for item in data_field_catalog() if item.role != "key"]
    assert [row["name"] for row in payload["fields"]] == declared_non_key
    for row in payload["fields"]:
        assert set(row) == {"name", "role", "status"}
        assert row["status"] == "available"


def test_data_status_payload_never_contains_local_paths(web_config, web_app, tmp_path) -> None:
    """data_root/panel_path are dropped entirely; no path-shaped strings."""

    status, _, body = _get(f"{web_app}/api/data/status")

    assert status == 200
    text = body.decode("utf-8")
    assert str(tmp_path) not in text
    assert str(web_config.paths.data_root) not in text
    assert PANEL_FILE not in text
    keys: set = set()
    strings: set = set()
    _walk_payload(json.loads(text), keys, strings)
    assert "data_root" not in keys
    assert "panel_path" not in keys
    for value in strings:
        assert not value.startswith("/"), value
        assert not value.startswith("~"), value
        assert "\\" not in value, value


def test_data_status_splits_schema_names_from_quality_problem_tokens(web_config, web_app) -> None:
    panel_path = web_config.paths.data_root / PANEL_FILE
    panel = pd.read_parquet(panel_path)
    pd.concat([panel, panel.iloc[[0]]], ignore_index=True).to_parquet(panel_path, index=False)

    status, _, body = _get(f"{web_app}/api/data/status")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is False
    # Quality tokens land in problems, never in the schema missing list.
    assert payload["quality"]["problems"] == ["duplicate_keys"]
    assert payload["quality"]["missing_columns"] == []


def test_data_status_reports_missing_required_columns_for_empty_root(tmp_path) -> None:
    config = QuantForgeConfig().resolve(tmp_path / "empty")

    payload = web_server._data_status_payload(config)

    assert payload["ok"] is False
    assert payload["coverage"]["rows"] == 0
    required = [item.name for item in data_field_catalog() if item.role in ("key", "required")]
    assert payload["quality"]["missing_columns"] == required
    assert payload["quality"]["problems"] == []
    assert all(row["status"] == "missing" for row in payload["fields"])
    keys: set = set()
    strings: set = set()
    _walk_payload(payload, keys, strings)
    assert "data_root" not in keys
    assert "panel_path" not in keys
    assert all(not value.startswith("/") for value in strings)


# ---------------------------------------------------------------------------
# GET /api/registry/factors (G3)
# ---------------------------------------------------------------------------


def test_registry_factors_lists_catalog_with_description_source_and_tags(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/registry/factors")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"factors", "count"}
    assert payload["count"] == len(payload["factors"]) == 2
    by_id = {row["factor_id"]: row for row in payload["factors"]}
    row = by_id["FTR_DEMO_SMALL_CAP"]
    assert set(row) == {
        "factor_id",
        "name",
        "formula",
        "status",
        "horizon_days",
        "universe_filters",
        "description",
        "source",
        "tags",
        "precomputed_values_present",
    }
    assert row["name"] == "demo_small_cap"
    assert row["formula"] == "-rank(market_cap)"
    assert row["status"] == "candidate"
    assert row["horizon_days"] == 5
    assert row["universe_filters"] == ["is_st == false"]
    assert row["description"] == "Small market-cap stocks receive higher scores."
    assert row["source"] == "demo"
    # A formula-backed factor computes scores on demand: "are values present"
    # is not a meaningful question, so the key stays null (FP-4).
    assert row["precomputed_values_present"] is None
    tags = row["tags"]
    assert tags["schema_version"] == "qf.research_tags.v1"
    assert tags["subject_kind"] == "factor"
    assert tags["subject_id"] == "FTR_DEMO_SMALL_CAP"
    assert tags["columns_required"] == ["market_cap"]
    assert tags["decay_horizon_days"] == 5


def test_registry_factors_preserve_none_vs_empty_tag_inputs(web_config, web_app) -> None:
    """FP-4: precomputed inputs are unobservable -> null, never []."""

    FactorRepository(web_config.paths.factor_root).save(
        FactorDefinition(
            factor_id="FTR_PRE_MARKER",
            name="pre_marker",
            formula="precomputed:FTR_PRE_MARKER",
            status="candidate",
            source="precomputed",
        )
    )

    _, _, body = _get(f"{web_app}/api/registry/factors")
    by_id = {row["factor_id"]: row for row in json.loads(body.decode("utf-8"))["factors"]}

    precomputed = by_id["FTR_PRE_MARKER"]
    assert precomputed["formula"].startswith("precomputed:")
    assert "/" not in precomputed["formula"]
    assert precomputed["tags"]["columns_required"] is None
    # A formula-backed factor keeps its observed inputs as a list.
    assert by_id["FTR_DEMO_MOMENTUM"]["tags"]["columns_required"] == ["return_5d"]


def test_registry_factors_degrade_to_empty_list_on_catalog_failure(monkeypatch, web_app) -> None:
    class _UnreadableCatalog:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list(self):
            raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(web_api, "FactorCatalog", _UnreadableCatalog)

    status, content_type, body = _get(f"{web_app}/api/registry/factors")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"factors": [], "count": 0}


# ---------------------------------------------------------------------------
# GET /api/registry/factors/{factor_id} (G4)
# ---------------------------------------------------------------------------


def test_registry_factor_detail_returns_factor_and_runs_newest_first(web_config, web_app) -> None:
    _append_run(
        web_config,
        run_id="evaluate-20260101T000000000000Z-aaaaaaaa",
        kind="evaluate",
        factor_ids=["FTR_DEMO_SMALL_CAP"],
        created_at="2026-01-01T00:00:00+00:00",
        metric_highlights={"rank_ic_t_stat": dict(NULL_VALUE_METRIC)},
        artifact_paths_rel=("evaluations/FTR_DEMO_SMALL_CAP.json",),
        warnings_count=1,
    )
    _append_run(
        web_config,
        run_id="backtest-20260102T000000000000Z-aaaaaaaa",
        kind="backtest",
        factor_ids=["FTR_DEMO_SMALL_CAP"],
        created_at="2026-01-02T00:00:00+00:00",
    )
    _append_run(
        web_config,
        run_id="evaluate-20260103T000000000000Z-aaaaaaaa",
        kind="evaluate",
        factor_ids=["FTR_DEMO_MOMENTUM"],
        created_at="2026-01-03T00:00:00+00:00",
    )

    status, content_type, body = _get(f"{web_app}/api/registry/factors/FTR_DEMO_SMALL_CAP")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"factor", "runs", "count", "limit", "total"}
    assert payload["factor"]["factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert payload["factor"]["tags"]["columns_required"] == ["market_cap"]
    assert payload["count"] == 2
    assert payload["limit"] == 50
    assert payload["total"] == 2
    assert [row["run_id"] for row in payload["runs"]] == [
        "backtest-20260102T000000000000Z-aaaaaaaa",
        "evaluate-20260101T000000000000Z-aaaaaaaa",
    ]
    oldest = payload["runs"][1]
    assert set(oldest) == {
        "run_id",
        "kind",
        "created_at",
        "factor_ids",
        "data_window",
        "config_fingerprint",
        "metric_highlights",
        "artifact_paths_rel",
        "warnings_count",
    }
    assert oldest["data_window"]["status"] == "available"
    assert oldest["warnings_count"] == 1
    # MetricValue convention: a null value keeps its explanatory status and
    # is never coerced to 0 or a bare scalar (FP-4).
    assert oldest["metric_highlights"]["rank_ic_t_stat"] == NULL_VALUE_METRIC
    assert oldest["metric_highlights"]["rank_ic_t_stat"]["value"] is None


def test_registry_factor_detail_validates_limit_and_kind(web_config, web_app) -> None:
    for day, kind in ((1, "evaluate"), (2, "backtest")):
        _append_run(
            web_config,
            run_id=f"{kind}-2026010{day}T000000000000Z-aaaaaaaa",
            kind=kind,
            factor_ids=["FTR_DEMO_SMALL_CAP"],
            created_at=f"2026-01-0{day}T00:00:00+00:00",
        )
    base = f"{web_app}/api/registry/factors/FTR_DEMO_SMALL_CAP"

    status, _, body = _get(f"{base}?kind=backtest")
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert [row["kind"] for row in payload["runs"]] == ["backtest"]
    assert payload["count"] == payload["total"] == 1

    status, _, body = _get(f"{base}?limit=1")
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["count"] == 1
    assert payload["limit"] == 1
    assert payload["total"] == 2
    assert payload["runs"][0]["run_id"] == "backtest-20260102T000000000000Z-aaaaaaaa"

    status, _, body = _get(f"{base}?limit=0")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "limit must be positive"

    status, _, body = _get(f"{base}?limit=201")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "limit must be between 1 and 200"

    status, _, body = _get(f"{base}?limit=abc")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "limit must be an integer"

    status, _, body = _get(f"{base}?kind=unknown_kind")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"].startswith("kind must be one of")


def test_registry_factor_detail_unknown_or_malformed_id_returns_404(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/registry/factors/FTR_DOES_NOT_EXIST")
    assert status == 404
    assert content_type == JSON_CONTENT_TYPE
    assert "unknown factor" in json.loads(body.decode("utf-8"))["error"]

    # Ids that do not match the FactorDefinition id rule are unknown too.
    status, _, body = _get(f"{web_app}/api/registry/factors/1FTR_STARTS_WITH_DIGIT")
    assert status == 404
    assert "unknown factor" in json.loads(body.decode("utf-8"))["error"]

    # Extra path segments are not a factor id.
    status, _, body = _get(f"{web_app}/api/registry/factors/FTR_DEMO_SMALL_CAP/extra")
    assert status == 404
    assert "unknown registry path" in json.loads(body.decode("utf-8"))["error"]


def test_registry_factor_detail_resolves_percent_encoded_id(web_config, web_app) -> None:
    """An id containing '=' (legal per the id rule) survives the encoded URL.

    The frontend requests detail with ``encodeURIComponent``, so '=' arrives
    as '%3D' in the path segment; the server decodes it once and then applies
    the id rule.
    """

    FactorRepository(web_config.paths.factor_root).save(
        FactorDefinition(
            factor_id="FTR_EQ=OK",
            name="eq_marker",
            formula="rank(return_5d)",
            status="candidate",
        )
    )

    status, content_type, body = _get(f"{web_app}/api/registry/factors/FTR_EQ%3DOK")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["factor"]["factor_id"] == "FTR_EQ=OK"
    assert payload["factor"]["name"] == "eq_marker"
    assert payload["runs"] == []
    assert payload["count"] == payload["total"] == 0


def test_registry_factor_detail_decodes_id_segment_exactly_once(web_config, web_app) -> None:
    """Only one percent-encoding layer is removed before id validation."""

    FactorRepository(web_config.paths.factor_root).save(
        FactorDefinition(
            factor_id="FTR_EQ=OK",
            name="eq_marker",
            formula="rank(return_5d)",
            status="candidate",
        )
    )

    # A doubly-encoded id decodes to 'FTR_EQ%3DOK', which still carries an
    # encoding layer, fails the id rule, and is treated as unknown — even
    # though the singly-decoded factor exists.
    status, _, body = _get(f"{web_app}/api/registry/factors/FTR_EQ%253DOK")
    assert status == 404
    assert "unknown factor" in json.loads(body.decode("utf-8"))["error"]

    # An encoded '/' decodes to a character outside the id rule -> 404 via
    # regex rejection, never a path lookup.
    status, _, body = _get(f"{web_app}/api/registry/factors/FTR%2FDEMO")
    assert status == 404
    assert "unknown factor" in json.loads(body.decode("utf-8"))["error"]


def test_registry_factor_detail_redacts_absolute_paths_from_legacy_rows(
    web_config, web_app, tmp_path
) -> None:
    """Hand-edited index rows with absolute paths are never republished."""

    index = RunIndex(web_config.paths.artifact_root)
    index.index_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_row = {
        "schema_version": "qf.run_index.v1",
        "run_id": "evaluate-20260102T000000000000Z-aaaaaaaa",
        "kind": "evaluate",
        "factor_ids": ["FTR_DEMO_SMALL_CAP"],
        "created_at": "2026-01-02T00:00:00+00:00",
        "data_window": dict(AVAILABLE_WINDOW),
        "config_fingerprint": "a" * 64,
        "metric_highlights": {},
        "artifact_paths_rel": [str(tmp_path / "legacy" / "marker_artifact.json")],
        "warnings_count": 0,
    }
    with index.index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy_row) + "\n")

    status, _, body = _get(f"{web_app}/api/registry/factors/FTR_DEMO_SMALL_CAP")

    assert status == 200
    text = body.decode("utf-8")
    assert str(tmp_path) not in text
    assert "marker_artifact.json" not in text
    assert "<redacted-path>" in text
    payload = json.loads(text)
    assert payload["count"] == 1


# ---------------------------------------------------------------------------
# Token gate + routing seams
# ---------------------------------------------------------------------------


def test_new_endpoints_require_control_token_when_gated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_WEB_TOKEN", "token-for-tests")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_WEB_TOKEN")
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    endpoints = (
        "/api/data/catalog",
        "/api/data/status",
        "/api/registry/factors",
        "/api/registry/factors/FTR_DEMO_SMALL_CAP",
    )

    try:
        for endpoint in endpoints:
            status, content_type, body = _get(f"{base_url}{endpoint}")
            assert status == 401, endpoint
            assert content_type == JSON_CONTENT_TYPE
            assert json.loads(body.decode("utf-8")) == {"error": "unauthorized"}

            status, _, _ = _get(
                f"{base_url}{endpoint}",
                headers={"Authorization": "Bearer token-for-tests"},
            )
            assert status == 200, endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_routing_dispatches_builders_via_server_namespace(monkeypatch, web_app) -> None:
    seen: dict[str, object] = {}

    def fake_data_catalog(config):
        seen["data_catalog"] = True
        return {"echo": "data-catalog"}

    def fake_data_status(config):
        seen["data_status"] = True
        return {"echo": "data-status"}

    def fake_registry_factors(config):
        seen["registry_factors"] = True
        return {"echo": "registry-factors"}

    def fake_registry_detail(config, factor_id, *, limit, kind):
        seen["registry_detail"] = (factor_id, limit, kind)
        return {"echo": "registry-detail"}

    monkeypatch.setattr(web_server, "_data_catalog_payload", fake_data_catalog)
    monkeypatch.setattr(web_server, "_data_status_payload", fake_data_status)
    monkeypatch.setattr(web_server, "_registry_factors_payload", fake_registry_factors)
    monkeypatch.setattr(web_server, "_registry_factor_detail_payload", fake_registry_detail)

    status, _, body = _get(f"{web_app}/api/data/catalog")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "data-catalog"}

    status, _, body = _get(f"{web_app}/api/data/status")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "data-status"}

    status, _, body = _get(f"{web_app}/api/registry/factors")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "registry-factors"}

    status, _, body = _get(f"{web_app}/api/registry/factors/FTR_SEAM?limit=7&kind=bench")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "registry-detail"}
    assert seen["registry_detail"] == ("FTR_SEAM", "7", "bench")
    assert seen.keys() >= {"data_catalog", "data_status", "registry_factors"}
