"""Behavioral tests for the web Research History and Benchmark panels (CP4-2).

Covers ``GET /api/research/history`` and ``GET /api/bench``:

- empty-index degradation (HTTP 200, empty list, null latest);
- populated-index listing through the real lineage run index API, newest
  first, with the ``limit`` query parameter validated like the other
  ``_int_parameter`` helpers (default 50, max 200);
- path redaction: an absolute path fed into a run record never reaches the
  response verbatim (release-scan rule: no local absolute paths in any web
  payload);
- MetricValue status preservation: a null value with an explanatory status
  arrives as ``{"value": null, "status": ...}`` and is never coerced to 0 or
  a bare scalar;
- the index page exposes both panel sections and fetch endpoints;
- routing dispatches both GET payload builders late-bound through the server
  module namespace (the monkeypatch seam contract).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.cli.main as cli_main
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.lineage.store import RunIndex


JSON_CONTENT_TYPE = "application/json; charset=utf-8"

AVAILABLE_WINDOW = {"start_date": "2025-01-05", "end_date": "2025-06-30", "status": "available"}


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


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
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
    fingerprint: str = "a" * 64,
) -> None:
    RunIndex(config.paths.artifact_root).append_run(
        run_id=run_id,
        kind=kind,
        factor_ids=factor_ids,
        created_at=created_at,
        data_window=AVAILABLE_WINDOW,
        config_fingerprint=fingerprint,
        metric_highlights=metric_highlights or {},
        artifact_paths_rel=artifact_paths_rel,
        warnings_count=warnings_count,
    )


BENCH_RUN_ID = "bench-20260102T000000000000Z-bbbbbbbb"

NULL_VALUE_METRIC = {
    "value": None,
    "unit": "t_stat",
    "status": "insufficient_sample",
    "observation_count": 3,
}

AVAILABLE_METRIC = {
    "value": 0.0123,
    "unit": "",
    "status": "available",
    "observation_count": 100,
}


def _write_bench_run(config: QuantForgeConfig, *, run_id: str = BENCH_RUN_ID, write_artifact: bool = True) -> None:
    """Write a qf.bench.v1 artifact + run-index row in the documented format."""

    created_at = "2026-01-02T00:00:00+00:00"
    json_rel = f"bench/{run_id}.json"
    payload = {
        "schema_version": "qf.bench.v1",
        "run_id": run_id,
        "created_at": created_at,
        "config_fingerprint": "b" * 64,
        "shared_config": {"kind": "bench", "factor_ids": ["FTR_A"]},
        "factors": [
            {
                "factor_id": "FTR_A",
                "status": "evaluated",
                "metrics": {
                    "rank_ic_mean": dict(AVAILABLE_METRIC),
                    "rank_ic_t_stat": dict(NULL_VALUE_METRIC),
                },
                "warnings_count": 0,
                "artifact_path_rel": "evaluations/FTR_A.json",
            }
        ],
        "summary": {"evaluated_factor_count": 1, "error_factor_count": 0},
    }
    if write_artifact:
        artifact_path = config.paths.artifact_root / json_rel
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    _append_run(
        config,
        run_id=run_id,
        kind="bench",
        factor_ids=["FTR_A"],
        created_at=created_at,
        metric_highlights={"FTR_A:rank_ic_mean": dict(AVAILABLE_METRIC)},
        artifact_paths_rel=(json_rel, f"bench/{run_id}.md"),
        fingerprint="b" * 64,
    )


# ---------------------------------------------------------------------------
# GET /api/research/history
# ---------------------------------------------------------------------------


def test_research_history_empty_index_returns_empty_list(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/research/history")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload == {"runs": [], "count": 0, "limit": 50, "total": 0}


def test_research_history_lists_runs_newest_first(web_config, web_app) -> None:
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
        run_id="rd-20260103T000000000000Z-aaaaaaaa",
        kind="rd",
        factor_ids=["FTR_DEMO_SMALL_CAP", "FTR_RD_CHILD"],
        created_at="2026-01-03T00:00:00+00:00",
    )

    status, content_type, body = _get(f"{web_app}/api/research/history")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["count"] == 3
    assert payload["total"] == 3
    assert [row["kind"] for row in payload["runs"]] == ["rd", "backtest", "evaluate"]
    newest = payload["runs"][0]
    assert newest["run_id"] == "rd-20260103T000000000000Z-aaaaaaaa"
    assert newest["created_at"] == "2026-01-03T00:00:00+00:00"
    assert newest["factor_ids"] == ["FTR_DEMO_SMALL_CAP", "FTR_RD_CHILD"]
    assert newest["data_window"]["status"] == "available"
    assert newest["warnings_count"] == 0
    oldest = payload["runs"][2]
    assert oldest["warnings_count"] == 1
    # Metric highlights keep the {value, status} MetricValue convention.
    assert oldest["metric_highlights"]["rank_ic_t_stat"] == NULL_VALUE_METRIC
    assert oldest["metric_highlights"]["rank_ic_t_stat"]["value"] is None


def test_research_history_respects_and_validates_limit(web_config, web_app) -> None:
    for day in (1, 2, 3):
        _append_run(
            web_config,
            run_id=f"evaluate-2026010{day}T000000000000Z-aaaaaaaa",
            kind="evaluate",
            factor_ids=["FTR_DEMO_SMALL_CAP"],
            created_at=f"2026-01-0{day}T00:00:00+00:00",
        )

    status, _, body = _get(f"{web_app}/api/research/history?limit=2")
    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["count"] == 2
    assert payload["limit"] == 2
    assert payload["total"] == 3
    assert [row["run_id"] for row in payload["runs"]] == [
        "evaluate-20260103T000000000000Z-aaaaaaaa",
        "evaluate-20260102T000000000000Z-aaaaaaaa",
    ]

    status, _, body = _get(f"{web_app}/api/research/history?limit=0")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "limit must be positive"

    status, _, body = _get(f"{web_app}/api/research/history?limit=201")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "limit must be between 1 and 200"

    status, _, body = _get(f"{web_app}/api/research/history?limit=abc")
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "limit must be an integer"


def test_research_history_redacts_absolute_paths_from_run_records(web_config, web_app, tmp_path) -> None:
    absolute_factor_ref = str(tmp_path / "leaky" / "private_notes.json")
    _append_run(
        web_config,
        run_id="evaluate-20260101T000000000000Z-aaaaaaaa",
        kind="evaluate",
        factor_ids=["FTR_DEMO_SMALL_CAP", absolute_factor_ref],
        created_at="2026-01-01T00:00:00+00:00",
    )
    # Simulate a legacy/hand-edited index row carrying an absolute artifact
    # path (the store rejects such writes today; the read side must still
    # never republish them).
    index = RunIndex(web_config.paths.artifact_root)
    legacy_row = {
        "schema_version": "qf.run_index.v1",
        "run_id": "evaluate-20260102T000000000000Z-aaaaaaaa",
        "kind": "evaluate",
        "factor_ids": ["FTR_DEMO_SMALL_CAP"],
        "created_at": "2026-01-02T00:00:00+00:00",
        "data_window": dict(AVAILABLE_WINDOW),
        "config_fingerprint": "a" * 64,
        "metric_highlights": {},
        "artifact_paths_rel": [str(tmp_path / "legacy" / "abs_artifact.json")],
        "warnings_count": 0,
    }
    with index.index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy_row) + "\n")

    status, _, body = _get(f"{web_app}/api/research/history")

    assert status == 200
    text = body.decode("utf-8")
    assert str(tmp_path) not in text
    assert "private_notes.json" not in text
    assert "abs_artifact.json" not in text
    assert "<redacted-path>" in text
    payload = json.loads(text)
    assert payload["count"] == 2


# ---------------------------------------------------------------------------
# GET /api/bench
# ---------------------------------------------------------------------------


def test_bench_endpoint_degrades_gracefully_without_bench_runs(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/bench")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload == {"runs": [], "count": 0, "limit": 50, "total": 0, "latest": None}


def test_bench_endpoint_preserves_null_metric_values_with_status(web_config, web_app) -> None:
    _write_bench_run(web_config)

    status, content_type, body = _get(f"{web_app}/api/bench")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["count"] == 1
    assert payload["runs"][0]["kind"] == "bench"
    assert payload["runs"][0]["metric_highlights"]["FTR_A:rank_ic_mean"]["status"] == "available"
    latest = payload["latest"]
    assert latest["available"] is True
    assert latest["schema_version"] == "qf.bench.v1"
    assert latest["run_id"] == BENCH_RUN_ID
    assert latest["summary"]["evaluated_factor_count"] == 1
    metrics = latest["factors"][0]["metrics"]
    # Hard acceptance rule: the null-value metric arrives as {value: null,
    # status: ...} -- never as 0, never as a bare scalar.
    assert metrics["rank_ic_t_stat"] == NULL_VALUE_METRIC
    assert metrics["rank_ic_t_stat"]["value"] is None
    assert not isinstance(metrics["rank_ic_t_stat"], (int, float))
    assert metrics["rank_ic_mean"] == AVAILABLE_METRIC


def test_bench_endpoint_reports_missing_artifact_without_failing(web_config, web_app) -> None:
    _write_bench_run(web_config, write_artifact=False)

    status, _, body = _get(f"{web_app}/api/bench")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["count"] == 1
    assert payload["latest"]["available"] is False
    assert payload["latest"]["factors"] == []
    assert "not available" in payload["latest"]["reason"]


def test_bench_endpoint_rejects_non_bench_and_mismatched_artifacts(web_config, web_app) -> None:
    artifact_root = web_config.paths.artifact_root

    # A crafted bench row pointing at a valid-JSON NON-bench artifact inside
    # artifact_root must not surface that artifact through the bench panel.
    decoy_rel = "evaluations/decoy.json"
    decoy_path = artifact_root / decoy_rel
    decoy_path.parent.mkdir(parents=True, exist_ok=True)
    decoy_path.write_text(
        json.dumps(
            {
                "schema_version": "qf.evaluation.v2",
                "secret_marker": "EVAL-LEAK-MARKER",
                "factors": [{"factor_id": "FTR_LEAK", "metrics": {"x": {"value": 1, "status": "available"}}}],
            }
        ),
        encoding="utf-8",
    )
    _append_run(
        web_config,
        run_id="bench-20260103T000000000000Z-cccccccc",
        kind="bench",
        factor_ids=["FTR_A"],
        created_at="2026-01-03T00:00:00+00:00",
        artifact_paths_rel=(decoy_rel,),
        fingerprint="c" * 64,
    )

    status, _, body = _get(f"{web_app}/api/bench")
    assert status == 200
    text = body.decode("utf-8")
    payload = json.loads(text)
    assert payload["latest"]["available"] is False
    assert payload["latest"]["factors"] == []
    assert "not a matching qf.bench.v1 report" in payload["latest"]["reason"]
    assert "EVAL-LEAK-MARKER" not in text
    assert "secret_marker" not in text
    assert "FTR_LEAK" not in text

    # A real qf.bench.v1 artifact whose run_id does not match the referencing
    # run-index row is rejected the same way (report identity check).
    other_run_payload = {
        "schema_version": "qf.bench.v1",
        "run_id": "bench-20260101T000000000000Z-dddddddd",
        "created_at": "2026-01-01T00:00:00+00:00",
        "config_fingerprint": "d" * 64,
        "shared_config": {},
        "factors": [{"factor_id": "FTR_OTHER_RUN", "status": "evaluated", "metrics": {}}],
        "summary": {},
    }
    mismatched_rel = "bench/bench-20260101T000000000000Z-dddddddd.json"
    mismatched_path = artifact_root / mismatched_rel
    mismatched_path.parent.mkdir(parents=True, exist_ok=True)
    mismatched_path.write_text(json.dumps(other_run_payload), encoding="utf-8")
    _append_run(
        web_config,
        run_id="bench-20260104T000000000000Z-eeeeeeee",
        kind="bench",
        factor_ids=["FTR_A"],
        created_at="2026-01-04T00:00:00+00:00",
        artifact_paths_rel=(mismatched_rel,),
        fingerprint="e" * 64,
    )

    status, _, body = _get(f"{web_app}/api/bench")
    assert status == 200
    text = body.decode("utf-8")
    payload = json.loads(text)
    assert payload["latest"]["run_id"] == "bench-20260104T000000000000Z-eeeeeeee"
    assert payload["latest"]["available"] is False
    assert "FTR_OTHER_RUN" not in text


def test_bench_endpoint_rejects_symlinked_artifact_outside_root(web_config, web_app, tmp_path) -> None:
    # A matching bench payload placed OUTSIDE artifact_root and reached via a
    # symlink inside it must be rejected by the containment checks
    # (resolve()-based pre-check plus the O_NOFOLLOW open guard).
    run_id = "bench-20260105T000000000000Z-ffffffff"
    outside_path = tmp_path / "outside" / f"{run_id}.json"
    outside_path.parent.mkdir(parents=True, exist_ok=True)
    outside_path.write_text(
        json.dumps(
            {
                "schema_version": "qf.bench.v1",
                "run_id": run_id,
                "created_at": "2026-01-05T00:00:00+00:00",
                "config_fingerprint": "f" * 64,
                "shared_config": {},
                "factors": [{"factor_id": "FTR_OUTSIDE_MARKER", "status": "evaluated", "metrics": {}}],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )
    link_rel = f"bench/{run_id}.json"
    link_path = web_config.paths.artifact_root / link_rel
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside_path)
    _append_run(
        web_config,
        run_id=run_id,
        kind="bench",
        factor_ids=["FTR_A"],
        created_at="2026-01-05T00:00:00+00:00",
        artifact_paths_rel=(link_rel,),
        fingerprint="f" * 64,
    )

    status, _, body = _get(f"{web_app}/api/bench")

    assert status == 200
    text = body.decode("utf-8")
    payload = json.loads(text)
    assert payload["latest"]["available"] is False
    assert "FTR_OUTSIDE_MARKER" not in text
    assert str(tmp_path) not in text


def test_bench_endpoint_reads_real_qf_factor_bench_artifacts(web_config, web_app, capsys) -> None:
    exit_code = cli_main.main(
        [
            "factor",
            "bench",
            "--factor-ids",
            "FTR_DEMO_SMALL_CAP",
            "--factor-root",
            str(web_config.paths.factor_root),
            "--data-root",
            str(web_config.paths.data_root),
            "--artifact-root",
            str(web_config.paths.artifact_root),
        ]
    )
    capsys.readouterr()
    assert exit_code == 0

    status, _, body = _get(f"{web_app}/api/bench")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["count"] == 1
    latest = payload["latest"]
    assert latest["available"] is True
    assert latest["schema_version"] == "qf.bench.v1"
    factor_row = latest["factors"][0]
    assert factor_row["factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert factor_row["status"] == "evaluated"
    assert factor_row["metrics"]
    for entry in factor_row["metrics"].values():
        assert set(entry) == {"value", "unit", "status", "observation_count"}
        if entry["status"] == "available":
            assert isinstance(entry["value"], (int, float))
        else:
            assert entry["value"] is None


# ---------------------------------------------------------------------------
# Index page panels and routing seams
# ---------------------------------------------------------------------------


def test_index_html_contains_research_history_and_bench_panels(web_config) -> None:
    html = web_server._index_html(web_config)
    assert "研究历史" in html
    assert 'id="history-result"' in html
    assert "/api/research/history" in html
    assert "Benchmark" in html
    assert 'id="bench-result"' in html
    assert "/api/bench" in html
    assert "function renderHistory" in html
    assert "function renderBench" in html
    assert "function metricValueText" in html
    assert "不显示为 0" in html


def test_get_history_and_bench_routes_dispatch_via_server_namespace(monkeypatch, web_app) -> None:
    def fake_history_payload(config, *, limit=None):
        return {"echo": "history", "limit": limit}

    def fake_bench_payload(config, *, limit=None):
        return {"echo": "bench", "limit": limit}

    monkeypatch.setattr(web_server, "_research_history_payload", fake_history_payload)
    monkeypatch.setattr(web_server, "_bench_runs_payload", fake_bench_payload)

    status, content_type, body = _get(f"{web_app}/api/research/history?limit=7")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "history", "limit": "7"}

    status, content_type, body = _get(f"{web_app}/api/bench")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "bench", "limit": None}
