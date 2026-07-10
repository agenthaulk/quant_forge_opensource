"""Tests for the provider-neutral factor-backend seam (CP1, D-ii/D-iv/CP0).

Pins the public pluggability contract of quant_forge.integrations:

- closed sets are closed: warning codes, capabilities, resolution statuses,
  and the reviewed static import table are pinned constants;
- the port defaults: every undeclared capability raises
  CapabilityNotSupported, never a silent no-op;
- the D-iv resolution state machine, including the CP0 amendment-4 opt-in
  precedence: a disabled backend is never imported, so "not enabled" beats
  "not installed";
- adapter contract violations (missing factory, wrong return type,
  descriptor identity mismatch) raise the typed BackendContractViolation.

All resolution tests monkeypatch a synthetic table plus fake modules in
sys.modules: they must never depend on any real adapter package being
present or absent in the environment.
"""

from __future__ import annotations

import sys
import types

import pytest

from quant_forge.integrations import contracts, registry
from quant_forge.integrations.contracts import (
    BACKEND_NOT_CONFIGURED,
    BACKEND_NOT_ENABLED,
    BACKEND_NOT_INSTALLED,
    CAPABILITIES,
    MEMBER_FORMULA_DRIFT,
    NOT_TRANSLATABLE,
    PRESCREEN_CHECK_STATUSES,
    PRESCREEN_LOCAL_PROXY_ONLY,
    REGION_ALIGNMENTS,
    REGION_MISMATCH,
    SUBMIT_NOT_CONFIRMED,
    UNKNOWN_BACKEND,
    WARNING_CODES,
    BackendContractViolation,
    BackendDescriptor,
    CapabilityNotSupported,
    FactorBackendPort,
    IntegrationContractError,
    PrescreenCheck,
    PrescreenReport,
    PrescreenRequest,
    SimulationRequest,
    SimulationResult,
    SubmitReceipt,
    SubmitRequest,
    TranslationRequest,
    TranslationResult,
)
from quant_forge.integrations.registry import (
    BACKEND_STATUSES,
    KNOWN_FACTOR_BACKENDS,
    enable_env_var,
    is_known_backend,
    list_backends,
    resolve_backend,
)


# ---------------------------------------------------------------------------
# Closed sets and reviewed constants
# ---------------------------------------------------------------------------


def test_warning_codes_closed_set_is_pinned() -> None:
    # TARGET_REGION_UNSUPPORTED and BACKEND_ERROR joined the closed set in the
    # post-review hardening pass: an unserved target region is refused up
    # front instead of crossing the seam as a raw adapter error, and
    # platform-side failures ride typed results instead of escaping mid-flow.
    assert WARNING_CODES == frozenset(
        (
            "BACKEND_NOT_INSTALLED",
            "BACKEND_NOT_ENABLED",
            "BACKEND_NOT_CONFIGURED",
            "REGION_MISMATCH",
            "NOT_TRANSLATABLE",
            "MEMBER_FORMULA_DRIFT",
            "SUBMIT_NOT_CONFIRMED",
            "PRESCREEN_LOCAL_PROXY_ONLY",
            "UNKNOWN_BACKEND",
            "TARGET_REGION_UNSUPPORTED",
            "BACKEND_ERROR",
        )
    )
    # Constant values equal the constant names (decision-register spelling).
    for name in sorted(WARNING_CODES):
        assert getattr(contracts, name) == name
    assert BACKEND_NOT_INSTALLED == "BACKEND_NOT_INSTALLED"
    assert BACKEND_NOT_ENABLED == "BACKEND_NOT_ENABLED"
    assert BACKEND_NOT_CONFIGURED == "BACKEND_NOT_CONFIGURED"
    assert REGION_MISMATCH == "REGION_MISMATCH"
    assert NOT_TRANSLATABLE == "NOT_TRANSLATABLE"
    assert MEMBER_FORMULA_DRIFT == "MEMBER_FORMULA_DRIFT"
    assert SUBMIT_NOT_CONFIRMED == "SUBMIT_NOT_CONFIRMED"
    assert PRESCREEN_LOCAL_PROXY_ONLY == "PRESCREEN_LOCAL_PROXY_ONLY"
    assert UNKNOWN_BACKEND == "UNKNOWN_BACKEND"


def test_capability_and_status_closed_sets_are_pinned() -> None:
    assert CAPABILITIES == frozenset(("translate", "prescreen", "simulate", "submit"))
    assert BACKEND_STATUSES == ("available", "not_enabled", "not_installed", "unknown")
    # "not_configured" joined the closed set in CP2 (D-iii optional
    # thresholds): a spec-skipped check is reported explicitly, never
    # silently omitted and never coerced into a verdict.
    assert PRESCREEN_CHECK_STATUSES == (
        "passed",
        "failed",
        "not_evaluable",
        "not_configured",
    )
    assert REGION_ALIGNMENTS == ("aligned", "mismatched", "unknown")


def test_known_factor_backends_table_is_the_reviewed_constant() -> None:
    # D-iv: one reviewed line per backend; the table, not any manifest, is
    # the availability authority. This test only compares constants and must
    # never trigger resolution of the real id.
    assert KNOWN_FACTOR_BACKENDS == {"worldquant": "quant_forge_worldquant"}
    assert is_known_backend("worldquant")
    assert not is_known_backend("quant_forge_worldquant")
    assert not is_known_backend("")


def test_enable_env_var_derivation() -> None:
    assert enable_env_var("worldquant") == "QF_ENABLE_BACKEND_WORLDQUANT"
    assert enable_env_var("some-backend.x") == "QF_ENABLE_BACKEND_SOME_BACKEND_X"


# ---------------------------------------------------------------------------
# BackendDescriptor
# ---------------------------------------------------------------------------


def test_descriptor_normalizes_and_supports() -> None:
    descriptor = BackendDescriptor(
        backend_id="fakebackend",
        label="Fake Backend",
        regions=["REGION_A", "REGION_B"],  # type: ignore[arg-type]
        capabilities={"translate", "prescreen"},  # type: ignore[arg-type]
    )
    assert descriptor.regions == ("REGION_A", "REGION_B")
    assert descriptor.capabilities == frozenset(("translate", "prescreen"))
    assert descriptor.supports("translate")
    assert not descriptor.supports("submit")


def test_descriptor_rejects_unknown_capability_and_empty_identity() -> None:
    with pytest.raises(ValueError, match="unknown capabilities"):
        BackendDescriptor(
            backend_id="fakebackend",
            label="Fake Backend",
            regions=(),
            capabilities=frozenset(("translate", "optimize")),
        )
    with pytest.raises(ValueError, match="backend_id"):
        BackendDescriptor(
            backend_id="", label="Fake", regions=(), capabilities=frozenset()
        )
    with pytest.raises(ValueError, match="label"):
        BackendDescriptor(
            backend_id="fakebackend", label=" ", regions=(), capabilities=frozenset()
        )
    with pytest.raises(ValueError, match="regions"):
        BackendDescriptor(
            backend_id="fakebackend",
            label="Fake",
            regions=("REGION_A", ""),
            capabilities=frozenset(),
        )


# ---------------------------------------------------------------------------
# Port defaults: undeclared capability raises, never a silent no-op
# ---------------------------------------------------------------------------


class _DescribeOnlyPort(FactorBackendPort):
    def describe(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_id="fakebackend",
            label="Fake Backend",
            regions=("REGION_A",),
            capabilities=frozenset(),
        )


def test_default_port_methods_raise_capability_not_supported() -> None:
    port = _DescribeOnlyPort()
    request_by_capability = {
        "translate": TranslationRequest(factor_id="F_ONE", formula="rank(close)", horizon_days=5),
        "prescreen": PrescreenRequest(
            factor_id="F_ONE", data_region="REGION_A", target_region="REGION_A", report={}
        ),
        "simulate": SimulationRequest(factor_id="F_ONE", expression="rank(close)"),
        "submit": SubmitRequest(factor_id="F_ONE", backend_ref="ref-1", confirm=True),
    }
    for capability in sorted(CAPABILITIES):
        method = getattr(port, capability)
        with pytest.raises(CapabilityNotSupported) as excinfo:
            method(request_by_capability[capability])
        assert excinfo.value.capability == capability
        assert isinstance(excinfo.value, IntegrationContractError)


# ---------------------------------------------------------------------------
# Request/result contract details
# ---------------------------------------------------------------------------


def test_submit_request_requires_explicit_confirm_flag() -> None:
    with pytest.raises(TypeError):
        SubmitRequest(factor_id="F_ONE", backend_ref="ref-1")  # type: ignore[call-arg]
    request = SubmitRequest(factor_id="F_ONE", backend_ref="ref-1", confirm=False)
    assert request.confirm is False


def test_translation_request_validates_horizon() -> None:
    with pytest.raises(ValueError):
        TranslationRequest(factor_id="F_ONE", formula="rank(close)", horizon_days=0)
    with pytest.raises(ValueError):
        TranslationRequest(factor_id="F_ONE", formula="rank(close)", horizon_days=True)  # type: ignore[arg-type]


def test_result_warning_codes_are_closed_set_validated() -> None:
    result = TranslationResult(expression="rank(close)", warnings=[NOT_TRANSLATABLE])  # type: ignore[arg-type]
    assert result.warnings == (NOT_TRANSLATABLE,)
    with pytest.raises(ValueError, match="closed warning-code set"):
        TranslationResult(expression="rank(close)", warnings=("made_up_code",))
    with pytest.raises(ValueError, match="closed warning-code set"):
        SimulationResult(backend_ref="ref-1", warnings=("made_up_code",))
    with pytest.raises(ValueError, match="closed warning-code set"):
        SubmitReceipt(submission_ref="sub-1", status="refused", warnings=("made_up_code",))
    receipt = SubmitReceipt(
        submission_ref="sub-1", status="refused", warnings=(SUBMIT_NOT_CONFIRMED,)
    )
    assert receipt.warnings == (SUBMIT_NOT_CONFIRMED,)


def test_prescreen_check_status_and_passed_stay_coherent() -> None:
    derived = PrescreenCheck(name="sharpe", value=1.4, threshold=1.25, status="passed")
    assert derived.passed is True
    derived_failed = PrescreenCheck(name="sharpe", value=0.4, threshold=1.25, status="failed")
    assert derived_failed.passed is False
    not_evaluable = PrescreenCheck(name="sharpe", value=None, threshold=1.25, status="not_evaluable")
    assert not_evaluable.passed is None
    with pytest.raises(ValueError, match="disagrees"):
        PrescreenCheck(name="sharpe", value=1.4, threshold=1.25, status="failed", passed=True)
    with pytest.raises(ValueError, match="closed set"):
        PrescreenCheck(name="sharpe", value=1.4, threshold=1.25, status="maybe")


def test_prescreen_report_validates_alignment_and_codes() -> None:
    check = PrescreenCheck(name="sharpe", value=1.4, threshold=1.25, status="passed")
    report = PrescreenReport(
        checks=[check],  # type: ignore[arg-type]
        region_alignment="mismatched",
        warning_codes=(REGION_MISMATCH, PRESCREEN_LOCAL_PROXY_ONLY),
    )
    assert report.checks == (check,)
    with pytest.raises(ValueError, match="region_alignment"):
        PrescreenReport(checks=(), region_alignment="close_enough")
    with pytest.raises(ValueError, match="closed warning-code set"):
        PrescreenReport(checks=(), region_alignment="aligned", warning_codes=("made_up_code",))
    with pytest.raises(ValueError, match="PrescreenCheck"):
        PrescreenReport(checks=({"name": "sharpe"},), region_alignment="aligned")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_backend state machine (synthetic table + fake modules only)
# ---------------------------------------------------------------------------


class _ImportAttemptRecorder:
    """Meta-path hook proving which module names import ever asked for."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        self.requested.append(fullname)
        return None


def _recording_meta_path(monkeypatch) -> _ImportAttemptRecorder:
    recorder = _ImportAttemptRecorder()
    monkeypatch.setattr(sys, "meta_path", [recorder, *sys.meta_path])
    return recorder


def _install_fake_backend_module(
    monkeypatch,
    module_name: str,
    backend_id: str,
    capabilities: tuple[str, ...] = ("translate", "prescreen"),
) -> types.ModuleType:
    descriptor = BackendDescriptor(
        backend_id=backend_id,
        label="Fake Backend",
        regions=("REGION_A",),
        capabilities=frozenset(capabilities),
    )

    class _FakePort(FactorBackendPort):
        def describe(self) -> BackendDescriptor:
            return descriptor

        def translate(self, request: TranslationRequest) -> TranslationResult:
            return TranslationResult(expression=f"translated({request.formula})")

    module = types.ModuleType(module_name)
    module.create_backend = lambda: _FakePort()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


def test_resolve_unknown_backend_id(monkeypatch) -> None:
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": "qf_cp1_fake_mod"})
    resolution = resolve_backend("never.heard.of.it")
    assert resolution.status == "unknown"
    assert resolution.warning_code == UNKNOWN_BACKEND
    assert resolution.port is None
    assert resolution.module is None
    assert resolution.enable_env_var is None


def test_resolve_not_enabled_never_imports(monkeypatch) -> None:
    # CP0 amendment 4 precedence: the module here does NOT exist anywhere,
    # yet with the gate unset the answer is not_enabled — proving the gate is
    # checked before any import and disabled backends are import-free.
    module_name = "qf_cp1_missing_disabled_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    monkeypatch.delenv("QF_ENABLE_BACKEND_FAKEBACKEND", raising=False)
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    recorder = _recording_meta_path(monkeypatch)

    resolution = resolve_backend("fakebackend")

    assert resolution.status == "not_enabled"
    assert resolution.warning_code == BACKEND_NOT_ENABLED
    assert resolution.port is None
    assert resolution.module == module_name
    assert resolution.enable_env_var == "QF_ENABLE_BACKEND_FAKEBACKEND"
    assert module_name not in recorder.requested
    assert module_name not in sys.modules


def test_enable_gate_requires_exactly_one(monkeypatch) -> None:
    module_name = "qf_cp1_gate_value_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    _install_fake_backend_module(monkeypatch, module_name, "fakebackend")
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", value)
        assert resolve_backend("fakebackend").status == "not_enabled", value
    monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", "1")
    assert resolve_backend("fakebackend").status == "available"


def test_resolve_enabled_but_not_installed(monkeypatch) -> None:
    module_name = "qf_cp1_missing_enabled_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", "1")
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    recorder = _recording_meta_path(monkeypatch)

    resolution = resolve_backend("fakebackend")

    assert resolution.status == "not_installed"
    assert resolution.warning_code == BACKEND_NOT_INSTALLED
    assert resolution.port is None
    # Sanity: the import really was attempted for the enabled backend.
    assert module_name in recorder.requested


def test_resolve_enabled_and_installed_returns_working_port(monkeypatch) -> None:
    module_name = "qf_cp1_installed_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", "1")
    _install_fake_backend_module(monkeypatch, module_name, "fakebackend")

    resolution = resolve_backend("fakebackend")

    assert resolution.status == "available"
    assert resolution.warning_code is None
    assert resolution.port is not None
    descriptor = resolution.port.describe()
    assert descriptor.backend_id == "fakebackend"
    result = resolution.port.translate(
        TranslationRequest(factor_id="F_ONE", formula="rank(close)", horizon_days=5)
    )
    assert result.expression == "translated(rank(close))"
    # Undeclared capabilities still raise on a resolved port.
    with pytest.raises(CapabilityNotSupported):
        resolution.port.submit(
            SubmitRequest(factor_id="F_ONE", backend_ref="ref-1", confirm=True)
        )


def test_resolve_descriptor_identity_mismatch_raises_typed_error(monkeypatch) -> None:
    module_name = "qf_cp1_mismatched_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", "1")
    _install_fake_backend_module(monkeypatch, module_name, "some.other.identity")

    with pytest.raises(BackendContractViolation, match="identity mismatch"):
        resolve_backend("fakebackend")


def test_resolve_module_without_factory_raises_typed_error(monkeypatch) -> None:
    module_name = "qf_cp1_no_factory_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", "1")
    monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))

    with pytest.raises(BackendContractViolation, match="create_backend"):
        resolve_backend("fakebackend")


def test_resolve_factory_returning_non_port_raises_typed_error(monkeypatch) -> None:
    module_name = "qf_cp1_wrong_type_backend_mod"
    monkeypatch.setattr(registry, "KNOWN_FACTOR_BACKENDS", {"fakebackend": module_name})
    monkeypatch.setenv("QF_ENABLE_BACKEND_FAKEBACKEND", "1")
    module = types.ModuleType(module_name)
    module.create_backend = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(BackendContractViolation, match="FactorBackendPort"):
        resolve_backend("fakebackend")


# ---------------------------------------------------------------------------
# list_backends
# ---------------------------------------------------------------------------


def test_list_backends_reports_states_without_importing_disabled(monkeypatch) -> None:
    enabled_module = "qf_cp1_list_enabled_backend_mod"
    disabled_module = "qf_cp1_list_disabled_backend_mod"
    monkeypatch.setattr(
        registry,
        "KNOWN_FACTOR_BACKENDS",
        {"zzfake": enabled_module, "aafake": disabled_module},
    )
    monkeypatch.setenv("QF_ENABLE_BACKEND_ZZFAKE", "1")
    monkeypatch.delenv("QF_ENABLE_BACKEND_AAFAKE", raising=False)
    _install_fake_backend_module(monkeypatch, enabled_module, "zzfake")
    monkeypatch.delitem(sys.modules, disabled_module, raising=False)
    recorder = _recording_meta_path(monkeypatch)

    rows = list_backends()

    assert [row["backend_id"] for row in rows] == ["aafake", "zzfake"]
    disabled_row, enabled_row = rows
    assert disabled_row["status"] == "not_enabled"
    assert disabled_row["warning_code"] == BACKEND_NOT_ENABLED
    assert disabled_row["module"] == disabled_module
    assert disabled_row["enable_env_var"] == "QF_ENABLE_BACKEND_AAFAKE"
    assert "label" not in disabled_row
    assert "capabilities" not in disabled_row
    assert enabled_row["status"] == "available"
    assert enabled_row["warning_code"] is None
    assert enabled_row["label"] == "Fake Backend"
    assert enabled_row["regions"] == ["REGION_A"]
    assert enabled_row["capabilities"] == ["prescreen", "translate"]
    # Listing never imported the disabled backend.
    assert disabled_module not in recorder.requested
    assert disabled_module not in sys.modules


# ---------------------------------------------------------------------------
# Package seam re-exports
# ---------------------------------------------------------------------------


def test_package_reexports_the_public_seam() -> None:
    import quant_forge.integrations as integrations

    for name in (
        "FactorBackendPort",
        "BackendDescriptor",
        "CapabilityNotSupported",
        "BackendContractViolation",
        "WARNING_CODES",
        "CAPABILITIES",
        "KNOWN_FACTOR_BACKENDS",
        "resolve_backend",
        "list_backends",
        "enable_env_var",
        "is_known_backend",
        # Post-review closed-code additions ride the same seam re-export
        # (Codex B-4): a code in WARNING_CODES that the package does not
        # re-export is an inconsistent public API.
        "TARGET_REGION_UNSUPPORTED",
        "BACKEND_ERROR",
    ):
        assert hasattr(integrations, name), name
        assert name in integrations.__all__, name


# ---------------------------------------------------------------------------
# Post-review hardening: gate-var uniqueness + per-row violation containment
# ---------------------------------------------------------------------------


def test_enable_env_vars_are_unique_across_the_table() -> None:
    # Distinct ids like "a.b"/"a-b"/"a_b" all map onto QF_ENABLE_BACKEND_A_B;
    # this pins the invariant that the reviewed table never ships two ids
    # sharing one opt-in gate variable.
    gates = [enable_env_var(backend_id) for backend_id in KNOWN_FACTOR_BACKENDS]
    assert len(set(gates)) == len(gates)


def test_list_backends_isolates_a_contract_violation_to_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One misdeclared adapter must not hide every other backend's row."""

    healthy_module = types.ModuleType("qf_row_ok_mod")

    class _HealthyPort(contracts.FactorBackendPort):
        def describe(self) -> contracts.BackendDescriptor:
            return contracts.BackendDescriptor(
                backend_id="rowok",
                label="Row OK",
                regions=("REGION_A",),
                capabilities=frozenset({"translate"}),
            )

    healthy_module.create_backend = _HealthyPort  # type: ignore[attr-defined]

    misdeclared_module = types.ModuleType("qf_row_bad_mod")

    class _MisdeclaredPort(contracts.FactorBackendPort):
        def describe(self) -> contracts.BackendDescriptor:
            return contracts.BackendDescriptor(
                backend_id="someotherid",
                label="Misdeclared",
                regions=("REGION_A",),
                capabilities=frozenset({"translate"}),
            )

    misdeclared_module.create_backend = _MisdeclaredPort  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "qf_row_ok_mod", healthy_module)
    monkeypatch.setitem(sys.modules, "qf_row_bad_mod", misdeclared_module)
    monkeypatch.setattr(
        registry,
        "KNOWN_FACTOR_BACKENDS",
        {"rowok": "qf_row_ok_mod", "rowbad": "qf_row_bad_mod"},
    )
    monkeypatch.setenv("QF_ENABLE_BACKEND_ROWOK", "1")
    monkeypatch.setenv("QF_ENABLE_BACKEND_ROWBAD", "1")

    rows = {row["backend_id"]: row for row in list_backends()}

    # The violation is loud in its own row...
    assert rows["rowbad"]["status"] == "contract_violation"
    assert "identity mismatch" in rows["rowbad"]["violation"]
    assert rows["rowbad"]["warning_code"] is None
    # ...while the healthy backend still reports normally.
    assert rows["rowok"]["status"] == "available"
    assert rows["rowok"]["label"] == "Row OK"
    # Direct single-backend resolution keeps the loud raise.
    with pytest.raises(contracts.BackendContractViolation):
        resolve_backend("rowbad")


def test_nested_import_failure_is_a_contract_violation_not_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Codex B-5: an INSTALLED adapter whose import dies on a missing
    # dependency must not masquerade as "not installed" — the user would be
    # told to install a package that is already present.
    pkg = tmp_path / "qf_nested_fail_mod.py"
    pkg.write_text("import qf_dep_that_does_not_exist_zz\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        registry, "KNOWN_FACTOR_BACKENDS", {"nestedfail": "qf_nested_fail_mod"}
    )
    monkeypatch.setenv("QF_ENABLE_BACKEND_NESTEDFAIL", "1")
    sys.modules.pop("qf_nested_fail_mod", None)

    with pytest.raises(contracts.BackendContractViolation, match="dependency"):
        resolve_backend("nestedfail")
    # The listing contains it loudly per-row instead of hiding other rows.
    rows = {row["backend_id"]: row for row in list_backends()}
    assert rows["nestedfail"]["status"] == "contract_violation"
    assert "dependency" in rows["nestedfail"]["violation"]
    # A genuinely absent top-level module still reads as not_installed.
    monkeypatch.setattr(
        registry, "KNOWN_FACTOR_BACKENDS", {"absent": "qf_truly_absent_mod_zz"}
    )
    monkeypatch.setenv("QF_ENABLE_BACKEND_ABSENT", "1")
    assert resolve_backend("absent").status == "not_installed"
