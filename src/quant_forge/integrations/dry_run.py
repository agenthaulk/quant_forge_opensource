"""Translate + prescreen dry-run flow shared by the CLI and the agent facade.

This module is deliberately free of any submission code path (FP-D: outward
submission is irreversible, so it stays explicit and human-gated). The CLI
composes an actual submission stage on top of this flow behind its own
``--confirm-submit`` flag; the agent workspace facade builds on this module
alone, so the agent-facing surface structurally cannot reach a backend
submit call.

Honesty contract carried through the flow (FP-4 / D-viii):

- Backend degradation states are reported with their closed warning codes
  plus a one-line human hint (:func:`warning_hint`) — never a silent skip.
- ``COMPOSITE_*`` factors are loaded exclusively from the run's report
  artifact under ``artifact_root`` so translation consumes the **pinned**
  member formulas and parameters recorded at backtest time (CP0 amendment 2
  to D-viii), never a live-registry re-resolution. When a pinned member
  formula no longer matches the live registry the flow refuses with
  :data:`~quant_forge.integrations.contracts.MEMBER_FORMULA_DRIFT` before
  calling the adapter: the expression that would be produced was never
  backtested in that registry state.
- Plain factors read their formula from the local factor registry; their
  newest local backtest report artifact (when one exists) feeds prescreen,
  and its absence is reported as a note with checks left honestly
  ``not_evaluable`` by the gate — never fabricated inputs.
- ``NOT_TRANSLATABLE`` and ``MEMBER_FORMULA_DRIFT`` on the translation are
  terminal: the flow stops and reports instead of prescreening an
  expression that misrepresents the factor.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from quant_forge.factor_library.repository import FactorRepository
from quant_forge.integrations.contracts import (
    BACKEND_NOT_CONFIGURED,
    BACKEND_NOT_ENABLED,
    BACKEND_NOT_INSTALLED,
    MEMBER_FORMULA_DRIFT,
    NOT_TRANSLATABLE,
    PRESCREEN_LOCAL_PROXY_ONLY,
    REGION_MISMATCH,
    SUBMIT_NOT_CONFIRMED,
    TARGET_REGION_UNSUPPORTED,
    UNKNOWN_BACKEND,
    CapabilityNotSupported,
    PrescreenReport,
    PrescreenRequest,
    TranslationRequest,
    TranslationResult,
)
from quant_forge.integrations.registry import BackendResolution, resolve_backend
from quant_forge.lineage.store import RunIndex

__all__ = [
    "COMPOSITE_ID_PREFIX",
    "TERMINAL_TRANSLATION_CODES",
    "DryRunOutcome",
    "FactorLoadError",
    "TargetFactor",
    "load_target_factor",
    "member_formula_drift",
    "run_translate_prescreen",
    "warning_hint",
]

# Materialized composite factors (design §8 / D-viii) carry this id prefix;
# they are translated from pinned report provenance, never from the registry.
COMPOSITE_ID_PREFIX = "COMPOSITE_"

# Translation warnings that end the flow (D-viii honesty boundary): both mean
# the produced expression would not represent what was actually backtested.
TERMINAL_TRANSLATION_CODES: frozenset[str] = frozenset(
    (NOT_TRANSLATABLE, MEMBER_FORMULA_DRIFT)
)

_STATIC_HINTS: dict[str, str] = {
    UNKNOWN_BACKEND: (
        "backend id is not in the reviewed backend table; run `qf backends list` for known ids"
    ),
    BACKEND_NOT_ENABLED: (
        "the backend is declared but not enabled; set its QF_ENABLE_BACKEND_* opt-in variable to 1"
    ),
    BACKEND_NOT_INSTALLED: (
        "the adapter package for this backend is not importable; install it to use the backend"
    ),
    BACKEND_NOT_CONFIGURED: (
        "the backend adapter is missing runtime configuration (for example its credential "
        "environment variables); configure the adapter per its documentation"
    ),
    REGION_MISMATCH: (
        "local data region differs from the target platform region; these local numbers "
        "do not predict platform outcomes"
    ),
    PRESCREEN_LOCAL_PROXY_ONLY: (
        "prescreen is local arithmetic over a local backtest report, never the target "
        "platform's own evaluation"
    ),
    NOT_TRANSLATABLE: (
        "this factor cannot be honestly expressed on the target platform; translation refused"
    ),
    MEMBER_FORMULA_DRIFT: (
        "a member formula changed in the local registry after synthesis; re-run synthesis "
        "so the target expression matches what was actually backtested"
    ),
    SUBMIT_NOT_CONFIRMED: (
        "the backend layer refused: its own confirmation gate did not allow the submission"
    ),
}


def warning_hint(
    code: str, *, enable_env_var: str | None = None, module: str | None = None
) -> str:
    """One-line human hint for a closed warning code.

    ``enable_env_var`` and ``module`` specialize the hint with the exact
    remediation (the precise opt-in variable / module name) when known.
    """

    if code == BACKEND_NOT_ENABLED and enable_env_var:
        return f"the backend is declared but not enabled; set {enable_env_var}=1 to opt in"
    if code == BACKEND_NOT_INSTALLED and module:
        return f"the adapter module '{module}' is not importable; install the backend package to use it"
    return _STATIC_HINTS.get(code, "")


class FactorLoadError(Exception):
    """The target factor could not be loaded honestly (missing registry entry
    or missing/malformed pinned synthesis report artifact)."""


@dataclass(frozen=True)
class TargetFactor:
    """One factor prepared for a backend flow, with its honest inputs.

    ``kind`` is ``"registry"`` (formula read from the local factor registry)
    or ``"composite"`` (everything pinned from the synthesis report
    artifact). ``prescreen_report`` is the local backtest mapping handed to
    the backend's prescreen — possibly empty, in which case the gate reports
    ``not_evaluable`` rows rather than this loader inventing numbers.
    """

    factor_id: str
    kind: str
    formula: str
    horizon_days: int
    parameters: Mapping[str, Any]
    provenance: Mapping[str, Any] | None
    prescreen_report: Mapping[str, Any]
    report_artifact_rel: str | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DryRunOutcome:
    """Result of one translate+prescreen flow.

    ``payload`` is JSON-safe and self-contained (codes, hints, stages);
    the typed fields carry live objects so the CLI can compose further
    stages without re-resolving the backend.
    """

    payload: dict[str, Any]
    ok: bool
    resolution: BackendResolution | None
    factor: TargetFactor | None
    translation: TranslationResult | None
    prescreen: PrescreenReport | None


# ---------------------------------------------------------------------------
# Factor loading
# ---------------------------------------------------------------------------


def load_target_factor(
    factor_id: str, *, factor_root: Path, artifact_root: Path
) -> TargetFactor:
    """Load one factor for a backend flow (D-viii source rules).

    ``COMPOSITE_*`` ids load from the pinned synthesis report artifact;
    every other id loads its formula from the local factor registry.
    Raises :class:`FactorLoadError` with an actionable message when the
    honest source is absent.
    """

    if factor_id.startswith(COMPOSITE_ID_PREFIX):
        return _load_composite_factor(factor_id, artifact_root=artifact_root)
    return _load_registry_factor(
        factor_id, factor_root=factor_root, artifact_root=artifact_root
    )


def _load_registry_factor(
    factor_id: str, *, factor_root: Path, artifact_root: Path
) -> TargetFactor:
    try:
        definition = FactorRepository(factor_root).get(factor_id)
    except FileNotFoundError as exc:
        raise FactorLoadError(str(exc)) from exc
    except ValueError as exc:
        raise FactorLoadError(f"invalid factor id {factor_id!r}: {exc}") from exc
    report: Mapping[str, Any] = {}
    report_rel: str | None = None
    notes: tuple[str, ...] = ()
    for rel, payload in _run_artifact_payloads(artifact_root, factor_id):
        if isinstance(payload.get("metrics"), Mapping):
            report = payload
            report_rel = rel
            break
    if report_rel is None:
        notes = (
            "no local backtest report artifact found for this factor; "
            "prescreen checks will be not_evaluable",
        )
    return TargetFactor(
        factor_id=factor_id,
        kind="registry",
        formula=definition.formula,
        horizon_days=definition.horizon_days,
        parameters={},
        provenance=None,
        prescreen_report=report,
        report_artifact_rel=report_rel,
        notes=notes,
    )


def _load_composite_factor(factor_id: str, *, artifact_root: Path) -> TargetFactor:
    for rel, payload in _run_artifact_payloads(artifact_root, factor_id):
        provenance = payload.get("synthesis_provenance")
        if not isinstance(provenance, Mapping):
            continue
        factor_block = payload.get("factor")
        factor_block = factor_block if isinstance(factor_block, Mapping) else {}
        identities = {
            str(provenance.get("composite_id") or ""),
            str(factor_block.get("factor_id") or ""),
        }
        if factor_id not in identities:
            continue
        formula = factor_block.get("formula")
        if not isinstance(formula, str) or not formula.strip():
            raise FactorLoadError(
                f"synthesis report artifact '{rel}' carries no factor.formula for {factor_id}"
            )
        horizon = factor_block.get("horizon_days")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise FactorLoadError(
                f"synthesis report artifact '{rel}' carries no positive integer "
                f"factor.horizon_days for {factor_id}"
            )
        parameters = payload.get("parameters")
        parameters = parameters if isinstance(parameters, Mapping) else {}
        backtest = payload.get("backtest")
        backtest = backtest if isinstance(backtest, Mapping) else {}
        notes: tuple[str, ...] = ()
        if not backtest:
            notes = (
                "synthesis report artifact carries no 'backtest' block; "
                "prescreen checks will be not_evaluable",
            )
        return TargetFactor(
            factor_id=factor_id,
            kind="composite",
            formula=formula,
            horizon_days=horizon,
            parameters=parameters,
            provenance=provenance,
            prescreen_report=backtest,
            report_artifact_rel=rel,
            notes=notes,
        )
    raise FactorLoadError(
        f"no synthesis report artifact with pinned provenance found for {factor_id} "
        "under artifact_root; run its synthesis backtest first (translation reads the "
        "pinned report provenance, never the live registry)"
    )


def _run_artifact_payloads(
    artifact_root: Path, factor_id: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    """JSON artifact payloads for a factor's recorded runs, newest first.

    Paths are contained to ``artifact_root`` (the run index validates them as
    relative on write; this reader re-checks before opening anything).
    Unreadable or malformed artifacts are skipped: an honest locator keeps
    scanning older runs rather than failing the flow on one bad file.
    """

    root = artifact_root.expanduser()
    try:
        resolved_root = root.resolve()
    except OSError:
        return
    rows = RunIndex(root).search(factor_id=factor_id)
    for row in reversed(rows):
        for rel in reversed(list(row.get("artifact_paths_rel") or [])):
            rel_text = str(rel)
            if not rel_text.endswith(".json"):
                continue
            candidate = root / rel_text
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved != resolved_root and resolved_root not in resolved.parents:
                continue
            if not resolved.is_file():
                continue
            try:
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                yield rel_text, payload


def member_formula_drift(
    provenance: Mapping[str, Any], factor_root: Path
) -> tuple[dict[str, str], ...]:
    """Members whose live registry formula no longer matches the pinned one.

    Per the CP0 amendment to D-viii, ``synthesis_provenance.factors[]``
    pins each member's formula at run time. A member that still exists in
    the registry with a *different* formula is drift: translating from the
    pinned formulas would target an expression the current registry no
    longer describes, and translating from the live ones would target an
    expression that was never backtested. Members absent from the registry
    or without a pinned formula are not comparable and are not reported
    here (the adapter still decides translatability from the pinned data).
    """

    members = provenance.get("factors")
    if not isinstance(members, (list, tuple)):
        return ()
    repository = FactorRepository(factor_root)
    drifted: list[dict[str, str]] = []
    for member in members:
        if not isinstance(member, Mapping):
            continue
        member_id = str(member.get("factor_id") or "")
        pinned = member.get("formula")
        if not member_id or not isinstance(pinned, str) or not pinned:
            continue
        try:
            current = repository.get(member_id).formula
        except (FileNotFoundError, ValueError):
            continue
        if current != pinned:
            drifted.append(
                {
                    "factor_id": member_id,
                    "pinned_formula": pinned,
                    "registry_formula": current,
                }
            )
    return tuple(drifted)


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def run_translate_prescreen(
    backend_id: str,
    factor_id: str,
    *,
    factor_root: Path,
    artifact_root: Path,
    data_region: str | None = None,
    target_region: str | None = None,
) -> DryRunOutcome:
    """Resolve a backend, load a factor, translate, and prescreen.

    Every stage lands in ``payload`` with closed warning codes and hints;
    a degradation ends the flow with ``ok=False`` and the stages that did
    not run left ``None``. This function performs no submission and has no
    parameter that could request one.
    """

    payload: dict[str, Any] = {
        "backend_id": str(backend_id),
        "factor_id": str(factor_id),
        "resolution": None,
        "factor": None,
        "translation": None,
        "prescreen": None,
        "terminal_warning_codes": [],
        "notes": [],
        "ok": False,
    }

    resolution = resolve_backend(backend_id)
    resolution_payload: dict[str, Any] = {
        "status": resolution.status,
        "warning_code": resolution.warning_code,
        "module": resolution.module,
        "enable_env_var": resolution.enable_env_var,
        "descriptor": None,
        "hint": None,
    }
    if resolution.warning_code is not None:
        resolution_payload["hint"] = warning_hint(
            resolution.warning_code,
            enable_env_var=resolution.enable_env_var,
            module=resolution.module,
        )
    payload["resolution"] = resolution_payload
    if resolution.status != "available" or resolution.port is None:
        payload["terminal_warning_codes"] = [resolution.warning_code]
        return DryRunOutcome(payload, False, resolution, None, None, None)

    descriptor = resolution.port.describe()
    resolution_payload["descriptor"] = {
        "backend_id": descriptor.backend_id,
        "label": descriptor.label,
        "regions": list(descriptor.regions),
        "capabilities": sorted(descriptor.capabilities),
    }

    try:
        factor = load_target_factor(
            factor_id, factor_root=factor_root, artifact_root=artifact_root
        )
    except FactorLoadError as exc:
        payload["factor"] = {"error": str(exc)}
        return DryRunOutcome(payload, False, resolution, None, None, None)
    payload["factor"] = {
        "factor_id": factor.factor_id,
        "kind": factor.kind,
        "formula": factor.formula,
        "horizon_days": factor.horizon_days,
        "report_artifact_rel": factor.report_artifact_rel,
        "notes": list(factor.notes),
    }

    if factor.provenance is not None:
        drifted = member_formula_drift(factor.provenance, factor_root)
        if drifted:
            payload["translation"] = {
                "expression": None,
                "target_settings": {},
                "warnings": [MEMBER_FORMULA_DRIFT],
                "notes": [
                    f"member '{row['factor_id']}' registry formula no longer matches "
                    "the formula pinned in the synthesis report"
                    for row in drifted
                ],
                "drift": list(drifted),
            }
            payload["terminal_warning_codes"] = [MEMBER_FORMULA_DRIFT]
            return DryRunOutcome(payload, False, resolution, factor, None, None)

    request = TranslationRequest(
        factor_id=factor.factor_id,
        formula=factor.formula,
        horizon_days=factor.horizon_days,
        parameters=factor.parameters,
        provenance=factor.provenance,
    )
    try:
        translation = resolution.port.translate(request)
    except CapabilityNotSupported:
        payload["translation"] = {"supported": False}
        payload["notes"].append(
            f"backend '{descriptor.backend_id}' does not declare the translate "
            "capability; nothing further can honestly run"
        )
        return DryRunOutcome(payload, False, resolution, factor, None, None)
    payload["translation"] = {
        "expression": translation.expression,
        "target_settings": dict(translation.target_settings),
        "warnings": list(translation.warnings),
        "notes": list(translation.notes),
    }
    terminal = sorted(TERMINAL_TRANSLATION_CODES.intersection(translation.warnings))
    if terminal:
        payload["terminal_warning_codes"] = terminal
        return DryRunOutcome(payload, False, resolution, factor, translation, None)

    resolved_data_region = data_region or "unknown"
    resolved_target_region = target_region or (
        descriptor.regions[0] if descriptor.regions else "unknown"
    )
    if (
        target_region is not None
        and descriptor.regions
        and resolved_target_region not in descriptor.regions
    ):
        # A region the backend does not serve is refused up front (FP-4/FP-G):
        # forwarding it would force the adapter to either fabricate a report
        # or escape the contract with a raw error.
        payload["prescreen"] = {
            "data_region": resolved_data_region,
            "target_region": resolved_target_region,
            "region_alignment": "unknown",
            "warning_codes": [TARGET_REGION_UNSUPPORTED],
            "checks": [],
        }
        payload["notes"].append(
            f"backend '{descriptor.backend_id}' serves region(s) "
            f"{', '.join(descriptor.regions)}; it cannot honestly prescreen for "
            f"'{resolved_target_region}'"
        )
        payload["terminal_warning_codes"] = [TARGET_REGION_UNSUPPORTED]
        return DryRunOutcome(payload, False, resolution, factor, translation, None)
    prescreen_request = PrescreenRequest(
        factor_id=factor.factor_id,
        data_region=resolved_data_region,
        target_region=resolved_target_region,
        report=factor.prescreen_report,
    )
    try:
        prescreen = resolution.port.prescreen(prescreen_request)
    except CapabilityNotSupported:
        payload["prescreen"] = {"supported": False}
        payload["notes"].append(
            f"backend '{descriptor.backend_id}' does not declare the prescreen "
            "capability; no local gate table is available"
        )
        payload["ok"] = True
        return DryRunOutcome(payload, True, resolution, factor, translation, None)
    payload["prescreen"] = {
        "data_region": resolved_data_region,
        "target_region": resolved_target_region,
        "region_alignment": prescreen.region_alignment,
        "warning_codes": list(prescreen.warning_codes),
        "checks": [asdict(check) for check in prescreen.checks],
    }
    payload["ok"] = True
    return DryRunOutcome(payload, True, resolution, factor, translation, prescreen)
