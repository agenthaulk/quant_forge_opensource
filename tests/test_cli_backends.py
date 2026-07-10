"""CLI + agent-facade wiring for external factor backends (CP3).

Pins the honest-degradation contract of `qf backends list` and
`qf factor submit --target`:

- every resolution degradation exits non-zero and prints its closed warning
  code verbatim plus a one-line hint (the exact opt-in env var for
  BACKEND_NOT_ENABLED);
- the default submit flow is DRY: translate + prescreen only, with no
  submit (and no simulate) call ever reaching the backend port;
- terminal translation refusals (NOT_TRANSLATABLE / MEMBER_FORMULA_DRIFT)
  stop the flow with a non-zero exit;
- COMPOSITE_* factors are loaded from the pinned synthesis report artifact
  (formulas, parameters, provenance), never re-resolved from the registry;
- --confirm-submit reaches the fake's submit with confirm=True and the
  receipt is reported verbatim, including SUBMIT_NOT_CONFIRMED refusals;
- the agent workspace facade exposes translate+prescreen read-only and has
  no submit surface at all (FP-D: outward submission is human-CLI-gated).

Every test monkeypatches a synthetic backend table plus a fake module in
sys.modules — the real adapter package must never be imported here.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import quant_forge.apps.cli.main as cli_main
from quant_forge.agent_workspace.tools import AgentWorkspaceTools
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.integrations import registry
from quant_forge.integrations.contracts import (
    BACKEND_NOT_CONFIGURED,
    NOT_TRANSLATABLE,
    PRESCREEN_LOCAL_PROXY_ONLY,
    REGION_MISMATCH,
    SUBMIT_NOT_CONFIRMED,
    TARGET_REGION_UNSUPPORTED,
    BackendDescriptor,
    CapabilityNotSupported,
    FactorBackendPort,
    PrescreenCheck,
    PrescreenReport,
    SimulationResult,
    SubmitReceipt,
    TranslationResult,
)
from quant_forge.lineage.store import RunIndex

FAKE_MODULE = "qf_cp3_fake_backend_mod"
FAKE_BACKEND_ID = "fakebackend"
FAKE_ENABLE_ENV = "QF_ENABLE_BACKEND_FAKEBACKEND"
COMPOSITE_ID = "COMPOSITE_9f3ac21b7e"


# ---------------------------------------------------------------------------
# Fixtures: fake backend module + tiny workspace
# ---------------------------------------------------------------------------


def _install_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capabilities: tuple[str, ...] = ("translate", "prescreen", "submit"),
    enabled: bool = True,
    translate_result: TranslationResult | None = None,
    prescreen_result: PrescreenReport | None = None,
    simulate_result: SimulationResult | None = None,
    submit_result: SubmitReceipt | None = None,
) -> list[tuple[str, Any]]:
    """Bind a synthetic table entry to a recording fake port module."""

    calls: list[tuple[str, Any]] = []
    descriptor = BackendDescriptor(
        backend_id=FAKE_BACKEND_ID,
        label="Fake Backend",
        regions=("REGION_A",),
        capabilities=frozenset(capabilities),
    )

    class _FakePort(FactorBackendPort):
        def describe(self) -> BackendDescriptor:
            return descriptor

        def translate(self, request):  # noqa: ANN001
            if not descriptor.supports("translate"):
                raise CapabilityNotSupported("translate")
            calls.append(("translate", request))
            return translate_result or TranslationResult(
                expression=f"translated({request.formula})",
                target_settings={"region": "REGION_A", "decay": 10},
            )

        def prescreen(self, request):  # noqa: ANN001
            if not descriptor.supports("prescreen"):
                raise CapabilityNotSupported("prescreen")
            calls.append(("prescreen", request))
            return prescreen_result or PrescreenReport(
                checks=(
                    PrescreenCheck(name="sharpe", value=1.5, threshold=1.25, status="passed"),
                    PrescreenCheck(name="fitness", value=None, threshold=None, status="not_configured"),
                    PrescreenCheck(
                        name="subwindow_sharpe", value=None, threshold=1.0, status="not_evaluable"
                    ),
                ),
                region_alignment="aligned",
                warning_codes=(PRESCREEN_LOCAL_PROXY_ONLY,),
            )

        def simulate(self, request):  # noqa: ANN001
            if not descriptor.supports("simulate"):
                raise CapabilityNotSupported("simulate")
            calls.append(("simulate", request))
            return simulate_result or SimulationResult(
                backend_ref="sim-ref-1", metrics={"sharpe": 1.2}
            )

        def submit(self, request):  # noqa: ANN001
            if not descriptor.supports("submit"):
                raise CapabilityNotSupported("submit")
            calls.append(("submit", request))
            return submit_result or SubmitReceipt(
                submission_ref="sub-1", status="submitted", provenance=request.provenance
            )

    module = types.ModuleType(FAKE_MODULE)
    module.create_backend = lambda: _FakePort()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, FAKE_MODULE, module)
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {FAKE_BACKEND_ID: FAKE_MODULE})
    if enabled:
        monkeypatch.setenv(FAKE_ENABLE_ENV, "1")
    else:
        monkeypatch.delenv(FAKE_ENABLE_ENV, raising=False)
    return calls


def _make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    factor_root = tmp_path / "factor_root"
    artifact_root = tmp_path / "artifacts"
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="F_PLAIN", name="Plain", formula="rank(close)", horizon_days=5
        )
    )
    return factor_root, artifact_root


def _write_synthesis_report(
    artifact_root: Path,
    factor_root: Path,
    *,
    member_formula: str = "rank(close)",
    pinned_formula: str = "rank(close)",
) -> dict[str, Any]:
    """Synthetic §8-shaped report artifact + run-index row for COMPOSITE_ID."""

    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="F_MEMBER", name="Member", formula=member_formula, horizon_days=5
        )
    )
    payload: dict[str, Any] = {
        "factor": {
            "factor_id": COMPOSITE_ID,
            "formula": f"precomputed:factor_id={COMPOSITE_ID}",
            "source": "synthesis",
            "horizon_days": 10,
        },
        "parameters": {"holding_days": 10, "decay_days": 10, "top_quantile": 0.3},
        "backtest": {
            "holding_days": 10,
            "metrics": {
                "net_long_short_sharpe": {"value": 1.4, "status": "available"},
                "net_annualized_return": {"value": 0.2, "status": "available"},
                "rebalance_turnover_mean": {"value": 0.5, "status": "available"},
            },
            "period_returns": [],
        },
        "synthesis_provenance": {
            "composite_id": COMPOSITE_ID,
            "method": "equal_weight",
            "is_fitted": False,
            "factors": [
                {
                    "factor_id": "F_MEMBER",
                    "direction": 1,
                    "source": "registry",
                    "formula": pinned_formula,
                }
            ],
            "weights_effective": {"F_MEMBER": 1.0},
        },
    }
    rel = "synthesis/COMPOSITE_report.json"
    report_path = artifact_root / rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    RunIndex(artifact_root).append_run(
        run_id="backtest-20260709T000000000000Z-abababab",
        kind="backtest",
        factor_ids=[COMPOSITE_ID],
        created_at="2026-07-09T00:00:00+00:00",
        data_window={"start_date": None, "end_date": None, "status": "unavailable"},
        config_fingerprint="ab" * 32,
        metric_highlights={},
        artifact_paths_rel=(rel,),
        warnings_count=0,
    )
    return payload


def _submit_argv(
    factor_id: str,
    factor_root: Path,
    artifact_root: Path,
    *extra: str,
) -> list[str]:
    return [
        "factor",
        "submit",
        factor_id,
        "--target",
        FAKE_BACKEND_ID,
        "--factor-root",
        str(factor_root),
        "--artifact-root",
        str(artifact_root),
        *extra,
    ]


class _ImportAttemptRecorder:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        self.requested.append(fullname)
        return None


# ---------------------------------------------------------------------------
# qf backends list
# ---------------------------------------------------------------------------


def test_backends_list_table_renders_codes_without_importing_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    enabled_module = "qf_cp3_list_enabled_mod"
    disabled_module = "qf_cp3_list_disabled_mod"
    monkeypatch.setattr(
        registry,
        "KNOWN_FACTOR_BACKENDS",
        {"zzfake": enabled_module, "aafake": disabled_module},
    )
    monkeypatch.setenv("QF_ENABLE_BACKEND_ZZFAKE", "1")
    monkeypatch.delenv("QF_ENABLE_BACKEND_AAFAKE", raising=False)
    descriptor = BackendDescriptor(
        backend_id="zzfake",
        label="ZZ Fake",
        regions=("REGION_A",),
        capabilities=frozenset(("translate", "prescreen")),
    )

    class _ListPort(FactorBackendPort):
        def describe(self) -> BackendDescriptor:
            return descriptor

    module = types.ModuleType(enabled_module)
    module.create_backend = lambda: _ListPort()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, enabled_module, module)
    monkeypatch.delitem(sys.modules, disabled_module, raising=False)
    recorder = _ImportAttemptRecorder()
    monkeypatch.setattr(sys, "meta_path", [recorder, *sys.meta_path])

    exit_code = cli_main.main(["backends", "list"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "aafake" in out and "zzfake" in out
    # Closed warning codes render verbatim; the exact opt-in env var shows.
    assert "BACKEND_NOT_ENABLED" in out
    assert "QF_ENABLE_BACKEND_AAFAKE" in out
    assert "not_enabled" in out and "available" in out
    assert "translate" in out and "prescreen" in out
    # Disabled backends are never imported by listing.
    assert disabled_module not in recorder.requested
    assert disabled_module not in sys.modules


def test_backends_list_json_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_backend(monkeypatch, enabled=False)

    exit_code = cli_main.main(["backends", "list", "--json"])

    assert exit_code == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["backend_id"] for row in rows] == [FAKE_BACKEND_ID]
    row = rows[0]
    assert row["status"] == "not_enabled"
    assert row["warning_code"] == "BACKEND_NOT_ENABLED"
    assert row["enable_env_var"] == FAKE_ENABLE_ENV
    assert row["module"] == FAKE_MODULE
    # FP-4: descriptor fields are never guessed for a backend that was not
    # imported.
    assert "label" not in row and "capabilities" not in row and "regions" not in row


# ---------------------------------------------------------------------------
# qf factor submit — resolution degradation paths
# ---------------------------------------------------------------------------


def test_submit_unknown_backend_exits_2_with_code_and_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {})
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_PLAIN", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "UNKNOWN_BACKEND" in out
    assert "qf backends list" in out


def test_submit_not_enabled_prints_exact_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch, enabled=False)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_PLAIN", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "BACKEND_NOT_ENABLED" in out
    assert f"set {FAKE_ENABLE_ENV}=1" in out
    assert calls == []


def test_submit_not_installed_names_the_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_module = "qf_cp3_missing_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {FAKE_BACKEND_ID: missing_module})
    monkeypatch.setenv(FAKE_ENABLE_ENV, "1")
    monkeypatch.delitem(sys.modules, missing_module, raising=False)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_PLAIN", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "BACKEND_NOT_INSTALLED" in out
    assert missing_module in out


def test_submit_backend_not_configured_warning_is_rendered_with_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The registry resolves fine, but the adapter reports it is missing its
    # runtime configuration on the results themselves: the code renders
    # verbatim with a hint, and the dry run stays an honest exit 0.
    _install_fake_backend(
        monkeypatch,
        translate_result=TranslationResult(
            expression="translated(rank(close))",
            warnings=(BACKEND_NOT_CONFIGURED,),
        ),
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_PLAIN", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "BACKEND_NOT_CONFIGURED" in out
    assert "runtime configuration" in out


def test_submit_missing_factor_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_ABSENT", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "factor not found" in out
    assert calls == []


# ---------------------------------------------------------------------------
# qf factor submit — dry-run default
# ---------------------------------------------------------------------------


def test_submit_dry_run_default_never_touches_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(
        monkeypatch, capabilities=("translate", "prescreen", "simulate", "submit")
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_PLAIN", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 0
    # Dry run: translate + prescreen only, even though the fake declares
    # simulate and submit.
    assert [name for name, _ in calls] == ["translate", "prescreen"]
    assert "translated(rank(close))" in out
    assert "PRESCREEN_LOCAL_PROXY_ONLY" in out
    # Honest statuses render in the check table.
    assert "not_configured" in out
    assert "not_evaluable" in out
    assert "dry run" in out
    translate_request = calls[0][1]
    assert translate_request.formula == "rank(close)"
    assert translate_request.horizon_days == 5
    assert translate_request.provenance is None
    prescreen_request = calls[1][1]
    # No local backtest report artifact exists: the report stays honestly
    # empty (the gate reports not_evaluable) and a note says so.
    assert prescreen_request.report == {}
    assert "no local backtest report artifact" in out


def test_submit_region_mismatch_rendered_prominently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_backend(
        monkeypatch,
        prescreen_result=PrescreenReport(
            checks=(
                PrescreenCheck(name="sharpe", value=None, threshold=1.25, status="not_evaluable"),
            ),
            region_alignment="mismatched",
            warning_codes=(PRESCREEN_LOCAL_PROXY_ONLY, REGION_MISMATCH),
        ),
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv(
            "F_PLAIN", factor_root, artifact_root, "--data-region", "REGION_B"
        )
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "REGION_MISMATCH" in out
    assert "PRESCREEN_LOCAL_PROXY_ONLY" in out
    assert "mismatched" in out
    assert "do not predict platform outcomes" in out


def test_submit_not_translatable_is_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(
        monkeypatch,
        translate_result=TranslationResult(
            expression="", warnings=(NOT_TRANSLATABLE,), notes=("fitted composites are refused",)
        ),
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv("F_PLAIN", factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "NOT_TRANSLATABLE" in out
    # Terminal: prescreen is never reached.
    assert [name for name, _ in calls] == ["translate"]


# ---------------------------------------------------------------------------
# qf factor submit — COMPOSITE_* pinned provenance (D-viii)
# ---------------------------------------------------------------------------


def test_submit_composite_uses_pinned_report_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)
    report = _write_synthesis_report(artifact_root, factor_root)

    exit_code = cli_main.main(_submit_argv(COMPOSITE_ID, factor_root, artifact_root))

    assert exit_code == 0
    translate_request = calls[0][1]
    assert translate_request.factor_id == COMPOSITE_ID
    assert translate_request.formula == f"precomputed:factor_id={COMPOSITE_ID}"
    assert translate_request.horizon_days == 10
    # Pinned run parameters and full synthesis provenance travel verbatim.
    assert translate_request.parameters == report["parameters"]
    assert translate_request.provenance == report["synthesis_provenance"]
    # Prescreen consumes the pinned local backtest block from the artifact.
    prescreen_request = calls[1][1]
    assert prescreen_request.report == report["backtest"]
    out = capsys.readouterr().out
    assert "composite" in out
    assert "synthesis/COMPOSITE_report.json" in out


def test_submit_composite_member_formula_drift_is_terminal_before_translate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)
    # The registry member formula drifted after synthesis pinned it.
    _write_synthesis_report(
        artifact_root,
        factor_root,
        member_formula="rank(volume)",
        pinned_formula="rank(close)",
    )

    exit_code = cli_main.main(_submit_argv(COMPOSITE_ID, factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "MEMBER_FORMULA_DRIFT" in out
    assert "re-run synthesis" in out
    # The refusal happens before any adapter call: nothing reached the port.
    assert calls == []


def test_submit_composite_without_report_artifact_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(_submit_argv(COMPOSITE_ID, factor_root, artifact_root))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "no synthesis report artifact with pinned provenance" in out
    assert calls == []


# ---------------------------------------------------------------------------
# qf factor submit — confirmed submission
# ---------------------------------------------------------------------------


def test_confirm_submit_reaches_fake_submit_with_confirm_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)
    report = _write_synthesis_report(artifact_root, factor_root)

    exit_code = cli_main.main(
        _submit_argv(COMPOSITE_ID, factor_root, artifact_root, "--confirm-submit")
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert [name for name, _ in calls] == ["translate", "prescreen", "submit"]
    submit_request = calls[-1][1]
    assert submit_request.confirm is True
    # Without a simulate capability the translated expression is the ref.
    assert submit_request.backend_ref == f"translated(precomputed:factor_id={COMPOSITE_ID})"
    # D-viii: provenance travels end-to-end into the submission request.
    assert submit_request.provenance == report["synthesis_provenance"]
    assert "submitted" in out
    assert "sub-1" in out


def test_confirm_submit_chains_simulate_backend_ref_when_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(
        monkeypatch, capabilities=("translate", "prescreen", "simulate", "submit")
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--confirm-submit")
    )

    assert exit_code == 0
    assert [name for name, _ in calls] == ["translate", "prescreen", "simulate", "submit"]
    simulate_request = calls[2][1]
    assert simulate_request.expression == "translated(rank(close))"
    submit_request = calls[3][1]
    assert submit_request.backend_ref == "sim-ref-1"
    assert submit_request.confirm is True


def test_confirm_submit_refused_receipt_surfaces_submit_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The adapter layer's own gate (for example its enable env) said no; the
    # receipt code surfaces verbatim and the exit is non-zero.
    _install_fake_backend(
        monkeypatch,
        submit_result=SubmitReceipt(
            submission_ref="",
            status="refused",
            warnings=(SUBMIT_NOT_CONFIRMED,),
        ),
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--confirm-submit")
    )

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "SUBMIT_NOT_CONFIRMED" in out
    assert "refused" in out


def test_confirm_submit_without_submit_capability_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _install_fake_backend(monkeypatch, capabilities=("translate", "prescreen"))
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--confirm-submit")
    )

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "does not declare the submit capability" in out
    assert [name for name, _ in calls] == ["translate", "prescreen"]


# ---------------------------------------------------------------------------
# qf factor submit — JSON shapes
# ---------------------------------------------------------------------------


def test_submit_json_dry_run_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--json")
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["ok"] is True
    assert payload["submission"] is None
    assert payload["resolution"]["status"] == "available"
    assert payload["resolution"]["descriptor"]["capabilities"] == [
        "prescreen",
        "submit",
        "translate",
    ]
    assert payload["factor"]["kind"] == "registry"
    assert payload["translation"]["expression"] == "translated(rank(close))"
    assert payload["prescreen"]["warning_codes"] == [PRESCREEN_LOCAL_PROXY_ONLY]
    statuses = {check["name"]: check["status"] for check in payload["prescreen"]["checks"]}
    assert statuses == {
        "sharpe": "passed",
        "fitness": "not_configured",
        "subwindow_sharpe": "not_evaluable",
    }


def test_submit_json_degradation_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_backend(monkeypatch, enabled=False)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--json")
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["resolution"]["warning_code"] == "BACKEND_NOT_ENABLED"
    assert FAKE_ENABLE_ENV in payload["resolution"]["hint"]
    assert payload["terminal_warning_codes"] == ["BACKEND_NOT_ENABLED"]
    assert payload["translation"] is None and payload["prescreen"] is None


def test_submit_json_confirmed_submission_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--confirm-submit", "--json")
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "submit"
    receipt = payload["submission"]["receipt"]
    assert receipt["status"] == "submitted"
    assert receipt["submission_ref"] == "sub-1"
    assert receipt["warnings"] == []
    assert payload["submission"]["simulation"] is None


# ---------------------------------------------------------------------------
# Agent facade (FP-D): read-only translate+prescreen, no submit surface
# ---------------------------------------------------------------------------


def test_agent_facade_surface_has_no_submit() -> None:
    public = [name for name in dir(AgentWorkspaceTools) if not name.startswith("_")]
    assert "backend_translate_prescreen" in public
    assert not any("submit" in name.lower() for name in public)


def test_agent_facade_dry_flow_never_calls_submit_even_when_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fake_backend(
        monkeypatch, capabilities=("translate", "prescreen", "simulate", "submit")
    )
    factor_root, artifact_root = _make_workspace(tmp_path)
    tools = AgentWorkspaceTools(
        factor_root=factor_root,
        data_root=tmp_path / "data",
        artifact_root=artifact_root,
    )

    payload = tools.backend_translate_prescreen(FAKE_BACKEND_ID, "F_PLAIN")

    # The behavioral half of the FP-D boundary: the fake declares submit,
    # and the agent flow still never reaches it (nor simulate).
    assert [name for name, _ in calls] == ["translate", "prescreen"]
    assert payload["ok"] is True
    assert payload["translation"]["expression"] == "translated(rank(close))"
    assert payload["prescreen"]["region_alignment"] == "aligned"
    # The facade payload has no submission stage at all, and it is JSON-safe.
    assert "submission" not in payload
    assert json.loads(json.dumps(payload)) == payload


def test_agent_facade_reports_degradation_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_backend(monkeypatch, enabled=False)
    factor_root, artifact_root = _make_workspace(tmp_path)
    tools = AgentWorkspaceTools(
        factor_root=factor_root,
        data_root=tmp_path / "data",
        artifact_root=artifact_root,
    )

    payload = tools.backend_translate_prescreen(FAKE_BACKEND_ID, "F_PLAIN")

    assert payload["ok"] is False
    assert payload["resolution"]["warning_code"] == "BACKEND_NOT_ENABLED"
    assert FAKE_ENABLE_ENV in payload["resolution"]["hint"]


# ---------------------------------------------------------------------------
# Post-review hardening: unserved target region + degraded-simulation block
# ---------------------------------------------------------------------------


def test_submit_refuses_unsupported_target_region_before_prescreen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--target-region outside descriptor.regions ends the flow honestly.

    Previously the request crossed the seam and surfaced as an uncontained
    adapter error; now the dry run refuses up front with the closed code and
    the adapter's prescreen is never invoked.
    """

    calls = _install_fake_backend(monkeypatch)
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv(
            "F_PLAIN", factor_root, artifact_root, "--target-region", "REGION_ELSEWHERE"
        )
    )

    out = capsys.readouterr().out
    assert exit_code == 2
    assert TARGET_REGION_UNSUPPORTED in out
    # Translation may run (it is region-independent); prescreen must not.
    assert "prescreen" not in [name for name, _ in calls]


def test_confirm_submit_blocked_on_degraded_simulation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A degraded simulation (warnings / empty backend_ref) blocks submission.

    Previously the confirmed flow chained into port.submit with an empty
    platform object id; now it exits 2 without attempting the submission.
    """

    calls = _install_fake_backend(
        monkeypatch,
        capabilities=("translate", "prescreen", "simulate", "submit"),
        simulate_result=SimulationResult(
            backend_ref="", warnings=(BACKEND_NOT_CONFIGURED,)
        ),
    )
    factor_root, artifact_root = _make_workspace(tmp_path)

    exit_code = cli_main.main(
        _submit_argv("F_PLAIN", factor_root, artifact_root, "--confirm-submit")
    )

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "submission not attempted" in out
    assert "submit" not in [name for name, _ in calls]
    assert "simulate" in [name for name, _ in calls]
