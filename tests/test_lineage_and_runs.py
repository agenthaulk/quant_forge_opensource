"""Lane C regression tests: artifact lineage index, run index, qf runs, factor bench."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import quant_forge.apps.cli.main as cli_main
from quant_forge.data.local import create_demo_workspace
from quant_forge.lineage.store import (
    LINEAGE_SCHEMA_VERSION,
    RUN_INDEX_SCHEMA_VERSION,
    LineageStore,
    RunIndex,
    artifact_id_for,
    canonical_fingerprint,
    redact_free_text,
)
from quant_forge.workbench.service import WorkbenchService

CREATED_AT = "2026-07-06T00:00:00+00:00"
METRIC_STATUSES = {"available", "insufficient_sample", "not_applicable", "unavailable_source_series", "invalid"}


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_no_absolute_paths(index_path: Path, forbidden_roots: list[Path]) -> None:
    text = index_path.read_text(encoding="utf-8")
    for root in forbidden_roots:
        assert str(root) not in text
    for row in _read_jsonl(index_path):
        for value in row.get("artifact_paths_rel", []):
            assert not value.startswith("/")
            assert ".." not in Path(value).parts
        if row.get("path_rel") is not None:
            assert not row["path_rel"].startswith("/")
            assert ".." not in Path(row["path_rel"]).parts


# ---------------------------------------------------------------------------
# Lineage store: append-only, relative paths, redaction
# ---------------------------------------------------------------------------


def test_lineage_rows_are_append_only_with_relative_paths(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "evaluations" / "demo.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"x": 1}\n', encoding="utf-8")

    store = LineageStore(artifact_root)
    first = store.record_artifact(artifact_type="evaluation", path=artifact, created_at=CREATED_AT, generated_by="test")

    assert first.artifact_id == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert first.path_rel == "evaluations/demo.json"
    content_after_first = store.index_path.read_text(encoding="utf-8")
    rows = _read_jsonl(store.index_path)
    assert len(rows) == 1
    assert rows[0]["schema_version"] == LINEAGE_SCHEMA_VERSION
    assert set(rows[0]) >= {"schema_version", "artifact_id", "artifact_type", "path_rel", "created_at", "generated_by", "parents"}

    second = store.record_artifact(
        artifact_type="bench_report",
        payload={"kind": "bench", "factors": ["A", "B"]},
        created_at=CREATED_AT,
        generated_by="test",
        parents=(first.artifact_id,),
    )
    # Append-only: the earlier content is a byte-for-byte prefix of the new file.
    content_after_second = store.index_path.read_text(encoding="utf-8")
    assert content_after_second.startswith(content_after_first)
    rows = _read_jsonl(store.index_path)
    assert len(rows) == 2
    assert rows[1]["parents"] == [first.artifact_id]
    # Payload-hashed id (no file) matches the canonical fingerprint.
    assert second.artifact_id == canonical_fingerprint({"kind": "bench", "factors": ["A", "B"]})
    assert rows[1]["path_rel"] is None

    # A path outside artifact_root must never be stored absolute: it becomes null.
    outside = tmp_path / "factor_root" / "factor.yaml"
    outside.parent.mkdir(parents=True)
    outside.write_text("factor_id: X\n", encoding="utf-8")
    third = store.record_artifact(artifact_type="factor_definition", path=outside, created_at=CREATED_AT, generated_by="test")
    assert third.path_rel is None
    _assert_no_absolute_paths(store.index_path, [tmp_path])


def test_lineage_metadata_redacts_home_paths_and_env_secrets(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    store = LineageStore(artifact_root)
    home = str(Path.home())
    leaky_note = f"panel loaded from {home}/research/data.parquet with QF_DEMO_API_KEY=abc123def456ghi789"
    store.record_artifact(
        artifact_type="evaluation",
        payload={"factor_id": "FTR_X"},
        created_at=CREATED_AT,
        generated_by="test",
        metadata={"note": leaky_note},
    )

    raw = store.index_path.read_text(encoding="utf-8")
    assert home not in raw
    assert "abc123def456ghi789" not in raw
    row = _read_jsonl(store.index_path)[0]
    assert "<redacted-path>" in row["metadata"]["note"]
    assert "QF_DEMO_API_KEY=<redacted>" in row["metadata"]["note"]


def test_redact_free_text_strips_paths_and_env_secrets() -> None:
    text = "seed at /" + "Users/someone/proj/x.yaml; MY_ACCESS_TOKEN=tok12345; kept plain words"
    redacted = redact_free_text(text)
    assert "someone" not in redacted
    assert "tok12345" not in redacted
    assert "kept plain words" in redacted


def test_lineage_deduplicates_identical_edges(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "artifacts")
    for _ in range(3):
        store.record_artifact(artifact_type="factor_definition", payload={"factor_id": "F"}, created_at=CREATED_AT, generated_by="factor_root")
    assert len(_read_jsonl(store.index_path)) == 1


# ---------------------------------------------------------------------------
# Run index axiom guards
# ---------------------------------------------------------------------------


def test_run_index_rejects_absolute_paths_and_fabricated_values(tmp_path: Path) -> None:
    index = RunIndex(tmp_path / "artifacts")
    fingerprint = canonical_fingerprint({"kind": "evaluate"})
    valid = dict(
        run_id="evaluate-x-1",
        kind="evaluate",
        factor_ids=("F",),
        created_at=CREATED_AT,
        data_window={"start_date": None, "end_date": None, "status": "unavailable"},
        config_fingerprint=fingerprint,
        metric_highlights={},
        artifact_paths_rel=("evaluations/F.json",),
        warnings_count=0,
    )
    index.append_run(**valid)

    with pytest.raises(ValueError):
        index.append_run(**{**valid, "artifact_paths_rel": ("/abs/evaluations/F.json",)})
    with pytest.raises(ValueError):
        index.append_run(**{**valid, "artifact_paths_rel": ("../outside.json",)})
    # Null-not-zero: a non-available metric must not carry a numeric value.
    with pytest.raises(ValueError):
        index.append_run(
            **{
                **valid,
                "metric_highlights": {
                    "rank_icir": {"value": 0.0, "unit": "ratio", "status": "insufficient_sample", "observation_count": 1}
                },
            }
        )
    # FP-2: an "available" window without dates is unrepresentable.
    with pytest.raises(ValueError):
        index.append_run(**{**valid, "data_window": {"start_date": None, "end_date": None, "status": "available"}})
    with pytest.raises(ValueError):
        index.append_run(**{**valid, "kind": "unknown_kind"})


# ---------------------------------------------------------------------------
# Workbench evaluate/backtest write run rows + lineage edges
# ---------------------------------------------------------------------------


def test_workbench_evaluate_and_backtest_append_run_rows_and_lineage(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    workbench = WorkbenchService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    evaluation = workbench.evaluate("FTR_DEMO_SMALL_CAP")
    backtest = workbench.run_backtest("FTR_DEMO_SMALL_CAP")
    assert evaluation.observations > 0
    assert backtest.periods > 0

    run_index_path = paths["artifact_root"] / "runs" / "index.jsonl"
    rows = _read_jsonl(run_index_path)
    assert [row["kind"] for row in rows] == ["evaluate", "backtest"]
    for row in rows:
        assert row["schema_version"] == RUN_INDEX_SCHEMA_VERSION
        assert row["factor_ids"] == ["FTR_DEMO_SMALL_CAP"]
        assert row["run_id"].startswith(row["kind"] + "-")
        assert len(row["config_fingerprint"]) == 64
        assert row["warnings_count"] >= 0
        assert row["data_window"]["status"] in {"available", "unavailable"}
        assert row["artifact_paths_rel"]
        for entry in row["metric_highlights"].values():
            assert entry["status"] in METRIC_STATUSES
            # null-not-zero, both directions
            assert (entry["value"] is None) == (entry["status"] != "available")
    _assert_no_absolute_paths(run_index_path, [tmp_path, Path.home()])

    lineage_path = paths["artifact_root"] / "lineage" / "artifact_index.jsonl"
    lineage_rows = _read_jsonl(lineage_path)
    by_type = {row["artifact_type"]: row for row in lineage_rows}
    assert set(by_type) >= {"factor_definition", "evaluation", "backtest"}
    definition_id = by_type["factor_definition"]["artifact_id"]
    # The factor definition file exists on disk, so its id is the file-bytes hash.
    definition_rows = [row for row in lineage_rows if row["artifact_type"] == "factor_definition"]
    assert len(definition_rows) == 1  # deduplicated across evaluate + backtest
    assert by_type["evaluation"]["parents"] == [definition_id]
    assert by_type["backtest"]["parents"] == [definition_id]
    assert by_type["evaluation"]["path_rel"] == "evaluations/FTR_DEMO_SMALL_CAP.json"
    assert by_type["evaluation"]["artifact_id"] == artifact_id_for(path=evaluation.artifact_path)
    _assert_no_absolute_paths(lineage_path, [tmp_path, Path.home()])


# ---------------------------------------------------------------------------
# qf runs list / show / search
# ---------------------------------------------------------------------------


@pytest.fixture()
def recorded_workspace(tmp_path: Path) -> dict[str, Path]:
    paths = create_demo_workspace(tmp_path / "demo")
    workbench = WorkbenchService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    workbench.evaluate("FTR_DEMO_SMALL_CAP")
    return paths


def test_cli_runs_list_show_search(recorded_workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    artifact_root = str(recorded_workspace["artifact_root"])
    run_id = _read_jsonl(recorded_workspace["artifact_root"] / "runs" / "index.jsonl")[0]["run_id"]

    assert cli_main.main(["runs", "list", "--artifact-root", artifact_root]) == 0
    listed = capsys.readouterr().out
    assert run_id in listed
    assert "(available)" in listed or "(insufficient_sample)" in listed

    assert cli_main.main(["runs", "show", run_id, "--artifact-root", artifact_root]) == 0
    shown = capsys.readouterr().out
    assert run_id in shown
    assert "config_fingerprint:" in shown
    assert "rank_icir" in shown
    assert "evaluations/FTR_DEMO_SMALL_CAP.json" in shown
    for entry_status in METRIC_STATUSES:
        # statuses are spelled out, never rendered as bare numbers
        assert f"0.0000 ({entry_status})" not in shown or entry_status == "available"

    assert cli_main.main(["runs", "show", "missing-run-id", "--artifact-root", artifact_root]) == 2

    assert cli_main.main(["runs", "search", "--factor", "FTR_DEMO_SMALL_CAP", "--kind", "evaluate", "--artifact-root", artifact_root]) == 0
    found = capsys.readouterr().out
    assert run_id in found

    assert cli_main.main(["runs", "search", "--factor", "FTR_ABSENT", "--artifact-root", artifact_root]) == 0
    assert "no runs matched" in capsys.readouterr().out


def test_cli_runs_list_empty_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main.main(["runs", "list", "--artifact-root", str(tmp_path / "artifacts")]) == 0
    assert "no runs recorded" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# qf factor bench
# ---------------------------------------------------------------------------


def test_cli_factor_bench_two_demo_factors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    exit_code = cli_main.main(
        [
            "factor",
            "bench",
            "--factor-ids",
            "FTR_DEMO_SMALL_CAP,FTR_DEMO_MOMENTUM",
            "--factor-root",
            str(paths["factor_root"]),
            "--data-root",
            str(paths["data_root"]),
            "--artifact-root",
            str(paths["artifact_root"]),
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0

    bench_rows = [
        row
        for row in _read_jsonl(paths["artifact_root"] / "runs" / "index.jsonl")
        if row["kind"] == "bench"
    ]
    assert len(bench_rows) == 1
    bench_row = bench_rows[0]
    assert bench_row["factor_ids"] == ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"]
    assert bench_row["run_id"] in output

    json_rel, markdown_rel = bench_row["artifact_paths_rel"]
    json_path = paths["artifact_root"] / json_rel
    markdown_path = paths["artifact_root"] / markdown_rel
    assert json_path.exists()
    assert markdown_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "qf.bench.v1"
    assert [row["factor_id"] for row in payload["factors"]] == ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"]
    metric_entries = [entry for row in payload["factors"] for entry in row["metrics"].values()]
    assert metric_entries
    for entry in metric_entries:
        assert entry["status"] in METRIC_STATUSES
        assert (entry["value"] is None) == (entry["status"] != "available")
    summary = payload["summary"]
    assert summary["evaluated_factor_count"] == 2
    assert summary["error_factor_count"] == 0
    assert (
        summary["available_metric_count"] + summary["insufficient_metric_count"] + summary["other_status_metric_count"]
        == len(metric_entries)
    )

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "FTR_DEMO_SMALL_CAP" in markdown
    assert "FTR_DEMO_MOMENTUM" in markdown
    assert "(available)" in markdown or "(insufficient_sample)" in markdown

    # Statuses are spelled out on stdout too.
    assert "rank_ic_mean" in output
    assert "(available)" in output or "(insufficient_sample)" in output

    # Lineage: bench report descends from both evaluation artifacts.
    lineage_rows = _read_jsonl(paths["artifact_root"] / "lineage" / "artifact_index.jsonl")
    bench_lineage = [row for row in lineage_rows if row["artifact_type"] == "bench_report"]
    assert len(bench_lineage) == 1
    evaluation_ids = {row["artifact_id"] for row in lineage_rows if row["artifact_type"] == "evaluation"}
    assert set(bench_lineage[0]["parents"]) == evaluation_ids
    assert len(evaluation_ids) == 2

    for index_name in ("runs/index.jsonl", "lineage/artifact_index.jsonl"):
        _assert_no_absolute_paths(paths["artifact_root"] / index_name, [tmp_path, Path.home()])


def test_cli_factor_bench_records_error_rows_without_fabricating_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    exit_code = cli_main.main(
        [
            "factor",
            "bench",
            "--factor-ids",
            "FTR_DEMO_SMALL_CAP,FTR_DOES_NOT_EXIST",
            "--factor-root",
            str(paths["factor_root"]),
            "--data-root",
            str(paths["data_root"]),
            "--artifact-root",
            str(paths["artifact_root"]),
        ]
    )
    capsys.readouterr()
    assert exit_code == 2

    bench_row = [row for row in _read_jsonl(paths["artifact_root"] / "runs" / "index.jsonl") if row["kind"] == "bench"][0]
    payload = json.loads((paths["artifact_root"] / bench_row["artifact_paths_rel"][0]).read_text(encoding="utf-8"))
    by_id = {row["factor_id"]: row for row in payload["factors"]}
    assert by_id["FTR_DOES_NOT_EXIST"]["status"] == "error"
    assert by_id["FTR_DOES_NOT_EXIST"]["metrics"] == {}
    assert by_id["FTR_DEMO_SMALL_CAP"]["status"] == "evaluated"
    assert payload["summary"]["error_factor_count"] == 1


def test_cli_factor_bench_requires_a_selection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    exit_code = cli_main.main(
        [
            "factor",
            "bench",
            "--factor-root",
            str(paths["factor_root"]),
            "--data-root",
            str(paths["data_root"]),
            "--artifact-root",
            str(paths["artifact_root"]),
        ]
    )
    assert exit_code == 2
    assert "no factors selected" in capsys.readouterr().out
