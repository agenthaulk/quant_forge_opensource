"""Workflow entrypoints, payload builders, and validators for the local web adapter.

Monkeypatch seams (``evaluate_factor``, ``run_factor_backtest``,
``parse_factor_idea``, ``_run_research_once``, ``DEFAULT_RD_CONFIG_PATH``) are
resolved through :mod:`quant_forge.apps.web.server` at call time so patches on
the server module namespace keep taking effect.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime
import gc
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
from typing import Any
from urllib.parse import parse_qs, unquote

from quant_forge.apps.web.jobs import _IdeaValidationSettings, _WebJobCancelled, _client_error_message
from quant_forge.apps.web.markdown import extract_markdown_title, render_markdown_html
from quant_forge.backtesting.service import EXTERNAL_OOS_ROLE
from quant_forge.config import QuantForgeConfig, simulation_profile_from_mapping, validate_llm_runtime
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.data.local import (
    LocalPanelDataProvider,
    catalog_field_availability,
    data_field_catalog,
    validate_data_root,
)
from quant_forge.extensions.registry import contribution_points_payload, scan_extensions
from quant_forge.factor_engine.signal_processing import (
    MIN_DISPLAY_TRADING_DAYS,
    apply_test_period,
    prepare_factor_scores_result,
)
from quant_forge.factor_engine.value_store import FactorValueStore
from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.lineage.recording import record_run
from quant_forge.lineage.store import RUN_KINDS, RunIndex, metric_highlight, redact_free_text
from quant_forge.mcp.read_models import list_factor_research_tags
from quant_forge.llm_factor_parser import (
    FACTOR_DESCRIPTION_MAX_CHARS,
    UNIVERSE_FILTER_MAX_CHARS,
    ParsedFactor,
    sanitize_factor_text,
    slugify_factor_name,
)
from quant_forge.research_loop.config import (
    ResearchLoopConfig,
    load_research_loop_config,
    weights_for_objective,
)
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.service import ResearchLoopResult, ResearchLoopService
from quant_forge.synthesis.methods import (
    STANDARDIZATIONS,
    SYNTHESIS_METHODS,
    MethodSpec,
    apply_param_defaults,
    method_catalog_payload,
    validate_params_against_schema,
)
from quant_forge.synthesis.service import (
    COVERAGE_RULE_ALL_FACTORS,
    DEFAULT_IC_MIN_PERIODS,
    DEFAULT_PINNED_UNIVERSE,
    EVALUATION_WINDOW_TOO_SHORT,
    NON_OVERLAPPING_COHORTS,
    PHASE_SENSITIVE_SMALL_SAMPLE,
    CompositeBacktestRun,
    CompositeResult,
    CoverageAccounting,
    FittedCompositeResult,
    MemberFetchSpec,
    PeriodICSweep,
    RebalancePrescan,
    build_directed_matrix,
    build_member_fetch_plan,
    cleanup_composite_artifacts,
    combine_apriori,
    combine_fitted,
    compute_period_ic_sweep,
    derive_composite_id,
    prescan_rebalance_coverage,
    redundancy_from_period_ics,
    require_backtest_window,
    resolve_pinned_universe,
    run_composite_backtest,
)
from quant_forge.workbench.service import (
    BACKTEST_HIGHLIGHT_METRICS,
    EVALUATION_HIGHLIGHT_METRICS,
    backtest_data_window,
    evaluation_data_window,
    result_warnings_count,
)


MAX_RD_ITERATIONS = 5

RESEARCH_HISTORY_DEFAULT_LIMIT = 50
RESEARCH_HISTORY_MAX_LIMIT = 200

# Repo-checkout layout: parents[4] of this file is the repository root.
# Both roots may be absent (e.g. an installed package); the payload
# builders degrade to available=false instead of erroring.
DOCS_ROOT = Path(__file__).resolve().parents[4] / "docs"
EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"


_WEB_PATH_KEYS = {
    "artifact_path",
    "artifact_path_rel",
    "artifact_paths_rel",
    "evaluation_artifact_path",
    "selection_backtest_artifact_path",
    "external_oos_artifact_path",
    "factor_values_path",
    "factor_values_write_path",
    "trace_root",
    "report_path",
    "round_report_paths",
}


def run_idea_workflow(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str = "llm",
    llm_provider: str | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Compatibility shim: parse an idea, then validate it with default parameters."""

    from quant_forge.apps.web import server as _server

    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    parsed = _parse_idea(
        config,
        text,
        parser_mode=parser_mode,
        llm_provider=llm_provider,
        cancel_event=cancel_event,
    )
    return _validate_factor_workflow(
        config,
        parsed.factor,
        parser=_parser_payload(parsed),
        parameters=None,
        rd_config=research_config,
        cancel_event=cancel_event,
    )


def run_idea_parse_workflow(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str = "llm",
    llm_provider: str | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Parse natural language into a draft factor and editable validation defaults."""

    from quant_forge.apps.web import server as _server

    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    parsed = _parse_idea(
        config,
        text,
        parser_mode=parser_mode,
        llm_provider=llm_provider,
        cancel_event=cancel_event,
    )
    return _parse_payload(parsed, research_config)


def run_idea_validation_workflow(
    config: QuantForgeConfig,
    factor: dict[str, Any] | FactorDefinition,
    *,
    parser: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Persist an edited draft factor, then evaluate and backtest it."""

    from quant_forge.apps.web import server as _server

    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    return _validate_factor_workflow(
        config,
        _factor_from_request(factor),
        parser=parser,
        parameters=parameters,
        rd_config=research_config,
        cancel_event=cancel_event,
    )


def _parse_idea(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str,
    llm_provider: str | None,
    cancel_event: threading.Event | None,
) -> ParsedFactor:
    from quant_forge.apps.web import server as _server

    if not text.strip():
        raise ValueError("idea text is required")
    _raise_if_cancelled(cancel_event)
    llm_settings = config.llm.select_provider(llm_provider) if parser_mode == "llm" else config.llm
    if parser_mode == "llm":
        validate_llm_runtime(config.llm, llm_provider)
    _raise_if_cancelled(cancel_event)
    parsed = _server.parse_factor_idea(text, llm_settings, mode=parser_mode)
    _raise_if_cancelled(cancel_event)
    return parsed


def _validate_factor_workflow(
    config: QuantForgeConfig,
    factor: FactorDefinition,
    *,
    parser: dict[str, Any] | None,
    parameters: dict[str, Any] | None,
    rd_config: ResearchLoopConfig,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    from quant_forge.apps.web import server as _server

    settings = _idea_validation_settings(factor, parameters, rd_config)
    repo = FactorRepository(config.paths.factor_root)
    previous_factor = _existing_factor(repo, factor.factor_id)
    try:
        repo.save(factor)
        _raise_if_cancelled(cancel_event)
        evaluation = _server.evaluate_factor(
            factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            horizon_days=settings.holding_days,
            horizon_days_matrix=rd_config.horizon_days_matrix,
            sample_splits=rd_config.sample_splits,
            simulation_profile=settings.evaluation_profile,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        )
        _raise_if_cancelled(cancel_event)
        in_sample_backtest = _server.run_factor_backtest(
            factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            simulation_profile=settings.evaluation_profile,
            holding_days=settings.holding_days,
            transaction_costs=settings.transaction_costs,
            sample_splits=rd_config.sample_splits,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
            sample_role="in_sample_backtest",
            include_partial_final_period=settings.include_partial_final_period,
        )
        _raise_if_cancelled(cancel_event)
        backtest = _server.run_factor_backtest(
            factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            simulation_profile=settings.backtest_profile,
            holding_days=settings.holding_days,
            transaction_costs=settings.transaction_costs,
            sample_splits=rd_config.sample_splits,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
            sample_role="external_oos_backtest",
            include_partial_final_period=settings.include_partial_final_period,
        )
        _raise_if_cancelled(cancel_event)
    except Exception:
        _restore_factor_after_failed_validation(repo, factor.factor_id, previous_factor)
        raise
    # BUG #007: only successful runs are recorded (mirrors WorkbenchService,
    # which never wraps _record_run in a try/except either), and this sits
    # OUTSIDE the try/except above so a recording failure propagates as its
    # own error instead of triggering the compute-failure factor-restore path.
    # PF-F4 residual: uniform last-look invariant — cancellation is observed
    # immediately before the first record of every workflow.
    _raise_if_cancelled(cancel_event)
    _record_validate_factor_runs(
        config,
        factor,
        settings,
        rd_config,
        evaluation=evaluation,
        in_sample_backtest=in_sample_backtest,
        backtest=backtest,
    )
    return _validation_payload(
        factor,
        parser=parser,
        evaluation=evaluation,
        in_sample_backtest=in_sample_backtest,
        backtest=backtest,
        parameters=settings.parameters,
    )


def _record_validate_factor_runs(
    config: QuantForgeConfig,
    factor: FactorDefinition,
    settings: _IdeaValidationSettings,
    rd_config: ResearchLoopConfig,
    *,
    evaluation: EvaluationResult,
    in_sample_backtest: BacktestResult,
    backtest: BacktestResult,
) -> None:
    """Give a web-originated validate run the same lineage/run-index trail a
    CLI/workbench run leaves (BUG #007: web runs never reached RunIndex, so
    the registry evidence chain and 研究历史/research-history panel stayed
    empty for web-only users).

    Mirrors ``WorkbenchService.evaluate`` / ``run_backtest`` exactly: the same
    highlight metric sets (``EVALUATION_HIGHLIGHT_METRICS`` /
    ``BACKTEST_HIGHLIGHT_METRICS``), the same ``data_window`` / warnings-count
    builders, and the same shared ``record_run`` helper the workbench service
    now calls too, so a factor validated through the web leaves the same kind
    ("evaluate" / "backtest") and highlight shape a CLI run of the same
    artifact type would.
    """

    sample_splits_payload = [asdict(split) for split in rd_config.sample_splits] if rd_config.sample_splits else None
    record_run(
        factor_root=config.paths.factor_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        kind="evaluate",
        factor_id=factor.factor_id,
        artifact_type="evaluation",
        artifact_path=evaluation.artifact_path,
        generated_by="web.validate_factor.evaluate",
        request={
            "kind": "evaluate",
            "factor_id": factor.factor_id,
            "horizon_days": settings.holding_days,
            "horizon_days_matrix": list(rd_config.horizon_days_matrix) if rd_config.horizon_days_matrix else None,
            "sample_splits": sample_splits_payload,
            "simulation_profile": asdict(settings.evaluation_profile),
        },
        metric_highlights={
            name: metric_highlight(evaluation.metrics[name])
            for name in EVALUATION_HIGHLIGHT_METRICS
            if name in evaluation.metrics
        },
        data_window=evaluation_data_window(evaluation),
        warnings_count=result_warnings_count(evaluation),
    )
    for sample_role, profile, result in (
        ("in_sample_backtest", settings.evaluation_profile, in_sample_backtest),
        ("external_oos_backtest", settings.backtest_profile, backtest),
    ):
        record_run(
            factor_root=config.paths.factor_root,
            artifact_root=config.paths.artifact_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
            kind="backtest",
            factor_id=factor.factor_id,
            artifact_type="backtest",
            artifact_path=result.artifact_path,
            generated_by=f"web.validate_factor.{sample_role}",
            request={
                "kind": "backtest",
                "sample_role": sample_role,
                "factor_id": factor.factor_id,
                "holding_days": settings.holding_days,
                "include_partial_final_period": settings.include_partial_final_period,
                "sample_splits": sample_splits_payload,
                "simulation_profile": asdict(profile),
                "transaction_costs": asdict(settings.transaction_costs),
            },
            metric_highlights={
                name: metric_highlight(result.metrics[name])
                for name in BACKTEST_HIGHLIGHT_METRICS
                if name in result.metrics
            },
            data_window=backtest_data_window(result),
            warnings_count=result_warnings_count(result),
        )


def run_staggered_entry_workflow(
    config: QuantForgeConfig,
    factor_id: str,
    *,
    parameters: dict[str, Any] | None = None,
    formation_trading_days: int | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run first-month staggered-entry robustness for a persisted factor."""

    from quant_forge.apps.web import server as _server

    if not factor_id.strip():
        raise ValueError("factor_id is required")
    _raise_if_cancelled(cancel_event)
    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    factor = FactorRepository(config.paths.factor_root).get(factor_id)
    settings = _idea_validation_settings(factor, parameters, research_config)
    result = _server.run_staggered_entry_backtest(
        factor.factor_id,
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        holding_days=settings.holding_days,
        simulation_profile=settings.backtest_profile,
        transaction_costs=settings.transaction_costs,
        formation_trading_days=formation_trading_days,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
    )
    _raise_if_cancelled(cancel_event)
    # BUG #007: record only after a successful (non-cancelled) run, same
    # posture as _validate_factor_workflow. run_staggered_entry_backtest
    # returns a plain JSON-shaped dict (there is no CLI/workbench analog for
    # this web-only surface), so there is no MetricValue-carrying metrics map
    # to highlight from - metric_highlights is honestly empty rather than
    # fabricated from plain floats.
    record_run(
        factor_root=config.paths.factor_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        kind="backtest",
        factor_id=factor.factor_id,
        artifact_type="staggered_backtest",
        artifact_path=Path(str(result["artifact_path"])),
        generated_by="web.staggered_entry_backtest",
        request={
            "kind": "staggered_backtest",
            "factor_id": factor.factor_id,
            "holding_days": settings.holding_days,
            "formation_trading_days": formation_trading_days,
            "simulation_profile": asdict(settings.backtest_profile),
            "transaction_costs": asdict(settings.transaction_costs),
        },
        metric_highlights={},
        data_window=_staggered_data_window(result),
        warnings_count=_staggered_warnings_count(result),
    )
    return result


def _staggered_data_window(result: dict[str, Any]) -> dict[str, str | None]:
    """Observed staggered-aggregate NAV window; unavailable when there is no NAV."""

    nav_rows = result.get("daily_nav") or []
    dates = [str(row["date"]) for row in nav_rows if isinstance(row, dict) and row.get("date")]
    if dates:
        return {"start_date": min(dates), "end_date": max(dates), "status": "available"}
    return {"start_date": None, "end_date": None, "status": "unavailable"}


def _staggered_warnings_count(result: dict[str, Any]) -> int:
    return len({str(code) for code in (result.get("warning_codes") or [])})


def run_research_once_workflow(
    config: QuantForgeConfig,
    seed_factor_id: str,
    *,
    objective: str | None = None,
    max_candidates: int | None = None,
    iterations: int | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one or more local RD iterations and return JSON-safe data."""

    from quant_forge.apps.web import server as _server

    _raise_if_cancelled(cancel_event)
    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    return _run_research_iterations(
        config,
        research_config,
        seed_factor_id,
        objective=objective or research_config.objective,
        max_candidates=max_candidates if max_candidates is not None else research_config.default_max_candidates,
        iterations=iterations if iterations is not None else 1,
        cancel_event=cancel_event,
    )


# ---------------------------------------------------------------------------
# Multi-factor composite backtest (design §8/§10, P5)
# ---------------------------------------------------------------------------
#
# `run_multi_factor_backtest_workflow` mirrors `_validate_factor_workflow`:
# validate -> run -> assemble payload -> clean up on failure. The composite
# core (standardize/direction/combine/coverage/pre-scan, materialization,
# engine drive with decay pinned to 0) lives in `quant_forge.synthesis.
# service`; this layer owns request re-validation, settings mapping, the
# same-window evaluation slot, and the exact §8 JSON contract the shipped
# frontend renderers consume. Placement note: design §10 sketches the
# orchestrator inside `synthesis/service.py`, but the `evaluate_factor`
# monkeypatch seam and the web-config/rd-config types live in this layer, and
# a core module importing `apps.web.server` would invert the adapter
# boundary — so the orchestrator lands here, next to its template.


@dataclass(frozen=True)
class _MultiFactorBacktestSettings:
    """Composite analog of ``_IdeaValidationSettings`` (design §10 step 2).

    One profile only — the module is backtest-only (owner directive), so
    there is no evaluation interval; the same-window diagnostics reuse the
    backtest profile. ``parameters`` is the §8 response echo (flat keys plus
    the nested ``backtest`` / ``transaction_costs`` blocks; deliberately NO
    ``evaluation_start`` / ``evaluation_end`` / nested ``evaluation``).
    """

    holding_days: int
    profile: SimulationProfile
    transaction_costs: TransactionCostModel
    include_partial_final_period: bool
    parameters: dict[str, Any]


@dataclass(frozen=True)
class _MultiFactorBacktestPlan:
    """Fully validated run plan produced by ``_prepare_multi_factor_backtest``.

    Everything data-independent AND data-dependent has been re-validated by
    the time this exists: the routing layer runs the same preparation
    synchronously (``preflight_multi_factor_backtest``) so every rejection in
    design §13's request matrix is a clean 4xx, and the job re-runs it so the
    workflow stays safe when invoked directly.
    """

    factor_refs: tuple[tuple[str, int], ...]
    directions: dict[str, int]
    method: MethodSpec
    method_params: dict[str, Any]
    weights: dict[str, Any] | None
    standardization: str
    standardization_pinned: bool
    settings: _MultiFactorBacktestSettings
    member_plan: tuple[MemberFetchSpec, ...]
    universe_filters: tuple[str, ...]
    composite_id: str
    period_count: int
    in_window_date_count: int
    panel: Any
    dates: tuple[Any, ...]
    previous_definition: FactorDefinition | None


def _synthesis_factor_refs(value: Any) -> list[dict[str, Any]]:
    """Validate the request ``factor_refs`` shape (client guard re-assertion).

    Mirrors ``buildRunRequest`` (synthesis.js:495-501): at least 2 refs, each
    with a non-empty ``factor_id`` and a strict integer direction of +1 or -1
    (never a float, never a bool), and no duplicate factor ids.
    """

    if not isinstance(value, list) or not value:
        raise ValueError("factor_refs must be a non-empty JSON array")
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("factor_refs entries must be JSON objects with factor_id and direction")
        factor_id = str(item.get("factor_id", "")).strip()
        if not factor_id:
            raise ValueError("factor_refs[].factor_id is required")
        direction = item.get("direction")
        if isinstance(direction, bool) or not isinstance(direction, int) or direction not in (1, -1):
            raise ValueError(f"direction must be the integer +1 or -1 for factor: {factor_id}")
        refs.append({"factor_id": factor_id, "direction": direction})
    if len(refs) < 2:
        raise ValueError("a multi-factor backtest requires at least 2 factor_refs")
    factor_ids = [ref["factor_id"] for ref in refs]
    if len(set(factor_ids)) != len(factor_ids):
        raise ValueError("factor_refs must not repeat a factor_id")
    return refs


def _synthesis_block(value: Any) -> dict[str, Any]:
    """Validate the request ``synthesis`` block shape: {method, params}."""

    if not isinstance(value, dict):
        raise ValueError("synthesis must be a JSON object with method and params")
    method = str(value.get("method", "")).strip()
    if not method:
        raise ValueError("synthesis.method is required")
    params = value.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("synthesis.params must be a JSON object")
    return {"method": method, "params": params}


def _optional_standardization(value: Any) -> dict[str, Any] | None:
    """Validate the optional ``standardization`` block shape.

    The frontend omits the block entirely when the chosen method pins its own
    standardization (B3 deviation #6), so absence is legal here; whether it is
    REQUIRED for the chosen method is decided against the catalog in
    ``_resolve_synthesis_standardization``.
    """

    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("standardization must be a JSON object with method and params")
    method = str(value.get("method", "")).strip()
    if not method:
        raise ValueError("standardization.method is required")
    params = value.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("standardization.params must be a JSON object")
    return {"method": method, "params": params}


def _synthesis_method_spec(name: str) -> MethodSpec:
    """Resolve a catalog method; unknown AND reserved names are rejected.

    Reserved (``available: false``) methods are advertised in the catalog as
    预留 options but are not runnable; the frontend never submits them
    (buildRunRequest checks ``method.available === true``), and the server
    re-asserts that guard here.
    """

    by_name = {spec.name: spec for spec in SYNTHESIS_METHODS}
    spec = by_name.get(name)
    if spec is None:
        raise ValueError(f"unknown synthesis method: {name}; expected one of {sorted(by_name)}")
    if not spec.available:
        raise ValueError(f"synthesis method is reserved and not runnable yet: {name}")
    return spec


def _synthesis_weights_for_members(
    method: MethodSpec, method_params: dict[str, Any], factor_ids: list[str]
) -> dict[str, Any] | None:
    """Cross-field weights guard: keys must cover EXACTLY the selected set.

    Schema-driven like the frontend form — any ``weights``-type ParamSpec is
    checked, with zero per-method hardcoding. ``validate_params_against_
    schema`` already enforced the value types/finiteness; this layer owns the
    request-context rule (key set == checked factor set) plus the all-zero
    rejection the combine step would otherwise only raise inside the job.
    """

    weights_value: dict[str, Any] | None = None
    for spec in method.params:
        if spec.type != "weights" or spec.name not in method_params:
            continue
        declared = {str(key): value for key, value in method_params[spec.name].items()}
        missing = sorted(set(factor_ids) - set(declared))
        extra = sorted(set(declared) - set(factor_ids))
        if missing or extra:
            raise ValueError(
                f"synthesis.params.{spec.name} must provide exactly one weight per selected "
                f"factor; missing: {missing}; unknown: {extra}"
            )
        if all(float(value) == 0.0 for value in declared.values()):
            raise ValueError(f"synthesis.params.{spec.name} must not be all zero")
        weights_value = declared
    return weights_value


def _resolve_synthesis_standardization(
    method: MethodSpec, block: dict[str, Any] | None
) -> tuple[str, bool]:
    """Resolve the effective standardization name + pinned flag.

    A method that pins its own standardization wins (a conflicting explicit
    block is rejected, never silently overridden); otherwise the block is
    REQUIRED and must name a catalog standardization, with its params
    validated against the declared (currently empty) schema.
    """

    pinned = method.required_standardization
    if pinned:
        if block is not None and block["method"] != pinned:
            raise ValueError(
                f"method {method.name} pins standardization {pinned}; got: {block['method']}"
            )
        return str(pinned), True
    if block is None:
        raise ValueError("standardization is required: send {\"method\": ..., \"params\": {}}")
    by_name = {spec.name: spec for spec in STANDARDIZATIONS}
    spec = by_name.get(block["method"])
    if spec is None:
        raise ValueError(
            f"unknown standardization: {block['method']}; expected one of {sorted(by_name)}"
        )
    validate_params_against_schema(spec.params, block["params"], owner="standardization.params")
    return spec.name, False


def _multi_factor_backtest_settings(
    raw_parameters: dict[str, Any] | None,
    rd_config: ResearchLoopConfig,
) -> _MultiFactorBacktestSettings:
    """Map the flat §8.2 request parameters onto profile + cost settings.

    Composite analog of ``_idea_validation_settings`` with one deliberate
    difference (RF-5): ``holding_days`` is REQUIRED and this function RAISES
    when it is absent — a composite has no single-factor ``horizon_days`` to
    fall back to, and a silent default would misstate both cadence and
    lifetime. Accepted keys are exactly what ``buildRunRequest`` sends:
    ``holding_days`` plus optional ``decay_days`` / ``top_quantile`` /
    ``execution_delay_days`` / ``backtest_start`` / ``backtest_end`` / cost
    fields / ``include_partial_final_period``; omitted values fall back to
    the RD backtest profile, never to frontend-invented numbers.
    """

    raw = raw_parameters or {}
    if not isinstance(raw, dict):
        raise ValueError("parameters must be a JSON object")
    holding_value = raw.get("holding_days")
    if holding_value is None or holding_value == "":
        raise ValueError(
            "parameters.holding_days is required for a multi-factor backtest; a composite "
            "has no single-factor horizon_days fallback (RF-5)"
        )
    holding_days = _positive_int_parameter(holding_value, "holding_days")
    include_partial_final_period = _bool_parameter(
        raw.get("include_partial_final_period", False), "include_partial_final_period"
    )
    overrides = _flat_backtest_profile_overrides(raw)
    overrides.update(_test_period_override("backtest", raw))
    profile = simulation_profile_from_mapping(overrides, rd_config.backtest_profile)
    cost_payload = _cost_parameters(raw, _transaction_costs_payload(rd_config.transaction_costs))
    transaction_costs = TransactionCostModel(
        commission_bps=_nonnegative_float_parameter(cost_payload["commission_bps"], "commission_bps"),
        slippage_bps=_nonnegative_float_parameter(cost_payload["slippage_bps"], "slippage_bps"),
        short_borrow_bps_annual=_nonnegative_float_parameter(
            cost_payload["short_borrow_bps_annual"], "short_borrow_bps_annual"
        ),
    )
    parameters = {
        "holding_days": holding_days,
        "backtest_start": profile.test_period_start,
        "backtest_end": profile.test_period_end,
        "top_quantile": profile.top_quantile,
        "decay_days": profile.decay_days,
        "execution_delay_days": profile.execution_delay_days,
        "commission_bps": transaction_costs.commission_bps,
        "slippage_bps": transaction_costs.slippage_bps,
        "short_borrow_bps_annual": transaction_costs.short_borrow_bps_annual,
        "include_partial_final_period": include_partial_final_period,
        "backtest": _simulation_profile_payload(profile),
        "transaction_costs": _transaction_costs_payload(transaction_costs),
    }
    return _MultiFactorBacktestSettings(
        holding_days=holding_days,
        profile=profile,
        transaction_costs=transaction_costs,
        include_partial_final_period=include_partial_final_period,
        parameters=parameters,
    )


def _precomputed_values_present(config: QuantForgeConfig, factor: FactorDefinition) -> bool | None:
    """Probe whether a precomputed factor's VALUES exist under configured roots.

    ``None`` for a non-precomputed formula: scores are computed from the
    formula on demand, so "are values present" is not a meaningful question.
    ``None`` also on any probe failure, including no ``factor_values_root``
    or ``factor_values_overlay_root`` configured at all (FP-4: unobservable
    is null, never guessed as True/False).

    Otherwise mirrors EXACTLY how ``prepare_factor_scores_result`` builds its
    ``FactorValueStore`` (read root = ``factor_values_root or
    factor_values_overlay_root``, write root = ``factor_values_overlay_root``)
    so the probe answers with the SAME roots the scoring path would actually
    read from — a False here means selecting this factor as a synthesis
    member would find no stored rows, not that the registry merely does not
    know.
    """

    if not is_precomputed_formula(factor.formula):
        return None
    try:
        read_root = config.paths.factor_values_root or config.paths.factor_values_overlay_root
        store = FactorValueStore(read_root, write_root=config.paths.factor_values_overlay_root)
        return store.has_stored_values(
            factor_id=factor.factor_id,
            factor_name=factor.name,
            formula=factor.formula,
        )
    except Exception:
        return None


def _prepare_multi_factor_backtest(
    config: QuantForgeConfig,
    *,
    factor_refs: Any,
    synthesis: Any,
    standardization: Any,
    parameters: Any,
    rd_config: ResearchLoopConfig,
) -> _MultiFactorBacktestPlan:
    """Re-validate EVERY client guard server-side and resolve the run plan.

    Design §10 step 1-2 in one pass: request shape (>=2 refs, ±1 directions,
    known+available method, schema-validated params, weights covering exactly
    the checked set, required standardization, REQUIRED holding_days),
    member resolution (unknown factor -> clean error), the ONE pinned
    universe (RB-6, ``UNIVERSE_MISMATCH``), and the RB-2 window precondition
    (``WINDOW_TOO_SHORT``). Every failure raises ``ValueError`` (or a typed
    subclass), which both the synchronous preflight route and the job error
    mapping surface as a client error, never a 500.
    """

    refs = _synthesis_factor_refs(factor_refs)
    synthesis_request = _synthesis_block(synthesis)
    standardization_request = _optional_standardization(standardization)
    method = _synthesis_method_spec(synthesis_request["method"])
    method_params = validate_params_against_schema(
        method.params, synthesis_request["params"], owner="synthesis.params"
    )
    # Schema-declared defaults resolve here so provenance echoes and the
    # composite-id digest carry the values the run ACTUALLY used (a fitted
    # request without ic_min_periods truthfully reports the catalog default).
    method_params = apply_param_defaults(method.params, method_params)
    factor_ids = [ref["factor_id"] for ref in refs]
    weights = _synthesis_weights_for_members(method, method_params, factor_ids)
    standardization_name, standardization_pinned = _resolve_synthesis_standardization(
        method, standardization_request
    )
    settings = _multi_factor_backtest_settings(parameters, rd_config)

    repository = FactorRepository(config.paths.factor_root)
    members: list[FactorDefinition] = []
    for factor_id in factor_ids:
        try:
            members.append(repository.get(factor_id))
        except FileNotFoundError:
            raise ValueError(f"unknown factor: {factor_id}") from None
    # A precomputed member's DEFINITION can persist in factor_root while its
    # VALUES were only ever written to a past run's overlay directory that
    # this run does not read. Refuse here — before any panel load or engine
    # work — instead of letting the composite drive fail deep inside
    # materialization with an opaque "no rows" error. Only a CONFIRMED
    # absence (False) refuses; an unobservable probe (None) never guesses.
    for member in members:
        if is_precomputed_formula(member.formula) and _precomputed_values_present(config, member) is False:
            raise ValueError(
                f"factor {member.factor_id} is precomputed but has no stored values under "
                "the configured factor_values_root/factor_values_overlay_root: its values "
                "were materialized for a past run only and are not present for this run"
            )
    # RB-6 + Codex A-1: when no member declares a universe, the pin falls back
    # to the cn_a formation default instead of an empty (unfiltered) set — an
    # empty pin would let ST names scored by every member enter the book.
    universe_filters = resolve_pinned_universe(
        members, default=DEFAULT_PINNED_UNIVERSE
    )
    directions = {ref["factor_id"]: ref["direction"] for ref in refs}
    member_plan = build_member_fetch_plan(
        members, directions=directions, universe_filters=universe_filters
    )

    panel = LocalPanelDataProvider(config.paths.data_root).load_panel()
    working_panel = apply_test_period(panel, settings.profile)
    dates = tuple(sorted(working_panel["trade_date"].drop_duplicates()))
    period_count = require_backtest_window(
        len(dates),
        delay=settings.profile.execution_delay_days,
        holding=settings.holding_days,
    )

    composite_id = derive_composite_id(
        factor_refs=[(ref["factor_id"], ref["direction"]) for ref in refs],
        method=method.name,
        method_params=method_params,
        standardization=standardization_name,
        backtest_start=settings.profile.test_period_start,
        backtest_end=settings.profile.test_period_end,
        decay_days=settings.profile.decay_days,
        execution_delay_days=settings.profile.execution_delay_days,
        top_quantile=settings.profile.top_quantile,
        coverage_rule=COVERAGE_RULE_ALL_FACTORS,
        min_factor_coverage=None,
        universe_filters=universe_filters,
        holding_days=settings.holding_days,
    )
    return _MultiFactorBacktestPlan(
        factor_refs=tuple((ref["factor_id"], ref["direction"]) for ref in refs),
        directions=directions,
        method=method,
        method_params=method_params,
        weights=weights,
        standardization=standardization_name,
        standardization_pinned=standardization_pinned,
        settings=settings,
        member_plan=member_plan,
        universe_filters=universe_filters,
        composite_id=composite_id,
        period_count=period_count,
        in_window_date_count=len(dates),
        panel=panel,
        dates=dates,
        previous_definition=_existing_factor(repository, composite_id),
    )


def preflight_multi_factor_backtest(
    config: QuantForgeConfig,
    *,
    factor_refs: Any,
    synthesis: Any,
    standardization: Any = None,
    parameters: Any = None,
    rd_config: ResearchLoopConfig,
) -> None:
    """Synchronous request validation for the POST route (clean 4xx contract).

    Jobs run asynchronously, so a guard that only fired inside the job body
    would surface as a failed job instead of a request rejection. The route
    calls this BEFORE ``job_manager.start``: every §13 rejection — shape
    guards, unknown/reserved method, missing ``holding_days``, weights
    coverage, unknown factors, ``UNIVERSE_MISMATCH``, ``WINDOW_TOO_SHORT`` —
    raises ``ValueError`` here and maps to HTTP 400. The window check needs
    the real trade calendar, so this loads the panel once per request; the
    job reloads it, an accepted cost for a local-first tool in exchange for
    honest request semantics.
    """

    _prepare_multi_factor_backtest(
        config,
        factor_refs=factor_refs,
        synthesis=synthesis,
        standardization=standardization,
        parameters=parameters,
        rd_config=rd_config,
    )


def run_multi_factor_backtest_workflow(
    config: QuantForgeConfig,
    *,
    factor_refs: Any,
    synthesis: Any,
    standardization: Any = None,
    parameters: Any = None,
    rd_config: ResearchLoopConfig,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the multi-factor composite backtest end-to-end (design §10 steps 1-6).

    Mirrors ``_validate_factor_workflow``: validate, fetch member scores with
    the ONE pinned universe, standardize/direction/combine — a-priori, or
    the §4.4 point-in-time IC/ICIR fit on the shared ``rebalance_indices``
    grid with ``_with_period_return`` forward returns (P6), with the
    time-varying weights folded into the composite BEFORE materialization —
    pre-scan the shared rebalance grid, materialize + drive the engine
    (decay pinned to 0 on the engine profile — LA-1), fill the same-window
    evaluation slot (FP-2), and assemble the §8 payload. Any failure after
    materialization cleans up the synthetic definition and the per-run
    overlay (the engine-drive step handles its own failures the same way).
    """

    _raise_if_cancelled(cancel_event)
    plan = _prepare_multi_factor_backtest(
        config,
        factor_refs=factor_refs,
        synthesis=synthesis,
        standardization=standardization,
        parameters=parameters,
        rd_config=rd_config,
    )
    _raise_if_cancelled(cancel_event)
    member_scores: dict[str, Any] = {}
    for spec in plan.member_plan:
        member_result_scores = prepare_factor_scores_result(
            plan.panel,
            spec.formula,
            spec.universe_filters,
            profile=plan.settings.profile,
            factor_id=spec.factor_id,
            factor_name=spec.factor_name,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
        ).scores
        # Backstop distinct from the earlier presence refusal (which already
        # confirmed a stored value file exists somewhere for this factor):
        # zero rows survived the formula-signature + panel-key read filter,
        # so the stored values do not cover THIS request's universe/dates —
        # a different failure than "no file at all", named as such instead of
        # surfacing as an opaque empty composite deep inside materialization.
        if is_precomputed_formula(spec.formula) and member_result_scores.empty:
            raise ValueError(
                f"factor {spec.factor_id} has stored precomputed values, but none are "
                "readable for this request's universe/date signature; rerun the synthesis "
                "that produced it, or check factor_values_root/factor_values_overlay_root"
            )
        member_scores[spec.factor_id] = member_result_scores
        _raise_if_cancelled(cancel_event)
    # The loop local still names the LAST member's full tidy frame after the
    # loop exits; release it here so it does not outlive `member_scores.clear()`
    # below and survive through the IC sweep and engine drive (members are
    # validated >= 2, so the name is always bound at this point).
    del member_result_scores
    # The working-panel close frame over the SAME in-window dates the engine
    # trades: the RB-5 forward-return source for the fitted IC estimate and
    # the advisory redundancy diagnostic.
    working_close = apply_test_period(plan.panel, plan.settings.profile)[
        ["trade_date", "instrument", "close"]
    ].copy()
    delay = plan.settings.profile.execution_delay_days
    # Standardize + direction the full member panel ONCE, then release the
    # per-member tidy frames right away: they are the largest live objects here
    # and nothing downstream needs them once the wide matrix exists. Jobs run
    # with gc disabled (jobs.py), so freeing is refcount-driven — clearing the
    # mapping drops the last references and the explicit collect reclaims them.
    directed, standardization_outcome = build_directed_matrix(
        member_scores,
        directions=plan.directions,
        standardization=plan.standardization,
    )
    member_scores.clear()
    gc.collect()

    # Compute the per-period rank IC sweep ONCE over the sorted directed matrix
    # and share it between the fitted weights and the advisory redundancy matrix.
    # Both service functions would otherwise rebuild this identical sweep from
    # the same inputs — a second full-panel forward-return pass — so threading it
    # through is a structural single-sweep, numerics unchanged; the returned
    # sweep also binds the ICs to a recorded fingerprint of this matrix/close/
    # grid so combine_fitted/redundancy_from_period_ics can verify provenance
    # instead of trusting a bare mapping (see PeriodICSweep).
    working = directed.sort_index()
    # Restore the pre-expensive-pass checkpoint: both method families (a-priori
    # and fitted) pass through here BEFORE the full-panel forward-return sweep,
    # matching where this check sat prior to the shared-sweep refactor.
    _raise_if_cancelled(cancel_event)
    sweep: PeriodICSweep = compute_period_ic_sweep(
        directed,
        close=working_close,
        dates=plan.dates,
        delay=delay,
        holding=plan.settings.holding_days,
    )

    composite: CompositeResult | FittedCompositeResult
    if plan.method.is_fitted:
        composite = combine_fitted(
            working,
            method=plan.method.name,
            close=working_close,
            dates=plan.dates,
            delay=delay,
            holding=plan.settings.holding_days,
            # apply_param_defaults guarantees the key; the catalog default is
            # the single source of truth (a service-side fallback here would
            # read as if a second constant could govern).
            ic_min_periods=int(plan.method_params["ic_min_periods"]),
            period_ics=sweep,
        )
    else:
        composite = combine_apriori(
            working,
            method=plan.method.name,
            weights=plan.weights,
        )
    # Enrich with the §4.2 standardization provenance exactly as the pure
    # build_*_composite entry points do, so every payload field is byte-identical.
    composite = replace(
        composite,
        standardization=plan.standardization,
        degenerate_dates_by_factor=standardization_outcome.degenerate_dates_by_factor,
    )
    _raise_if_cancelled(cancel_event)
    redundancy = redundancy_from_period_ics(sweep)
    prescan = prescan_rebalance_coverage(
        composite.composite,
        plan.dates,
        delay=plan.settings.profile.execution_delay_days,
        holding=plan.settings.holding_days,
        include_partial_final_period=plan.settings.include_partial_final_period,
    )
    _raise_if_cancelled(cancel_event)
    # Release the wide-matrix working set before the engine drive — the largest
    # allocator ahead. ``composite`` already holds the degenerate-date mapping it
    # needs, so dropping ``standardization_outcome`` frees its standardized matrix
    # without losing provenance; ``plan.panel`` is kept for the engine. ``sweep``
    # replaces the old ``ic_by_period`` local here; its ``ics`` mapping is small,
    # but it is released in the same batch anyway for a single clean cut.
    del working_close, directed, working, sweep, standardization_outcome
    gc.collect()
    run = run_composite_backtest(
        composite.composite,
        composite_id=plan.composite_id,
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        holding_days=plan.settings.holding_days,
        profile=plan.settings.profile,
        universe_filters=plan.universe_filters,
        transaction_costs=plan.settings.transaction_costs,
        factor_values_root=config.paths.factor_values_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        include_partial_final_period=plan.settings.include_partial_final_period,
        panel=plan.panel,
    )
    try:
        _raise_if_cancelled(cancel_event)
        evaluation_payload = _same_window_evaluation(config, plan, run, rd_config)
        _raise_if_cancelled(cancel_event)
        # BUG #007: record the composite backtest run under the COMPOSITE_
        # factor id so it reaches the registry evidence chain the same way a
        # CLI/workbench backtest does (mirrors WorkbenchService.run_backtest's
        # kind/highlight semantics for a BacktestResult). PF-F3: recording sits
        # LAST, after payload construction succeeds, so a payload-build failure
        # never leaves a success-shaped run row behind; the except-clause below
        # still cleans up the composite definition and overlay either way.
        payload = _multi_factor_backtest_payload(
            plan, composite, prescan, run, evaluation_payload, redundancy
        )
        # PF-F4 residual: last look BEFORE recording begins — a cancel that
        # arrives during payload assembly is still pre-recording, so it ends
        # as a cooperative cancel (cleanup below, zero run rows), never a
        # recorded-and-completed run.
        _raise_if_cancelled(cancel_event)
        _record_multi_factor_backtest_run(config, plan, run)
        return payload
    except Exception:
        cleanup_composite_artifacts(
            config.paths.factor_root,
            composite_id=run.composite_id,
            previous_definition=plan.previous_definition,
            overlay_root=run.overlay_root,
        )
        raise


def _record_multi_factor_backtest_run(
    config: QuantForgeConfig, plan: _MultiFactorBacktestPlan, run: CompositeBacktestRun
) -> None:
    """Record the composite backtest under ``run.composite_id`` (BUG #007).

    Mirrors ``WorkbenchService.run_backtest``'s kind/highlight semantics for a
    ``BacktestResult`` (``kind="backtest"``, ``BACKTEST_HIGHLIGHT_METRICS``,
    ``backtest_data_window``) so a synthesized ``COMPOSITE_*`` factor gets the
    same registry evidence chain a single-factor backtest gets, using the
    shared ``record_run`` helper.
    """

    record_run(
        factor_root=config.paths.factor_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        kind="backtest",
        factor_id=run.composite_id,
        artifact_type="backtest",
        artifact_path=run.result.artifact_path,
        generated_by="web.multi_factor_backtest",
        request={
            "kind": "multi_factor_backtest",
            "composite_id": run.composite_id,
            "factor_refs": [{"factor_id": factor_id, "direction": direction} for factor_id, direction in plan.factor_refs],
            "method": plan.method.name,
            "method_params": plan.method_params,
            "weights": plan.weights,
            "standardization": plan.standardization,
            "holding_days": plan.settings.holding_days,
            "include_partial_final_period": plan.settings.include_partial_final_period,
            "simulation_profile": asdict(plan.settings.profile),
            "transaction_costs": asdict(plan.settings.transaction_costs),
            "universe_filters": list(plan.universe_filters),
        },
        metric_highlights={
            name: metric_highlight(run.result.metrics[name])
            for name in BACKTEST_HIGHLIGHT_METRICS
            if name in run.result.metrics
        },
        data_window=backtest_data_window(run.result),
        warnings_count=result_warnings_count(run.result),
    )


def _same_window_evaluation_meta(run: CompositeBacktestRun, *, status: str) -> dict[str, Any]:
    """FP-2 basis marker: the evaluation slot is same-window diagnostics.

    The frontend's evaluation section title is hard-coded, so the honesty
    signal lives here and in ``validity.caveats``; renderers ignore the extra
    key (additive change).
    """

    return {
        "basis": "same_window_diagnostics",
        "status": status,
        "test_period": {
            "start": run.engine_profile.test_period_start,
            "end": run.engine_profile.test_period_end,
        },
    }


def _degraded_same_window_evaluation(
    run: CompositeBacktestRun, plan: _MultiFactorBacktestPlan
) -> dict[str, Any]:
    """Honest degraded evaluation slot when the window is below the 126 floor.

    Decision record (design task FP-2 note): a ``null`` slot renders without
    crashing (`synthesis.js:448` maps it to ``{}``), but `factor.js:122`
    interpolates ``esc(evaluation.ic_days)`` — a literal ``undefined`` tile —
    and the warning surface disappears with the object. This degraded payload
    instead carries FP-4-typed statuses (the tiles render
    ``insufficient_sample`` labels), a genuine observed ``ic_days`` of 0, the
    ``EVALUATION_WINDOW_TOO_SHORT`` code, and a plain-language warning. The
    backtest itself is unaffected: its real gate is max(2, holding+delay+1)
    (RF-4); the 126-day floor belongs to the evaluation layer only.
    """

    status = "insufficient_sample"
    return {
        "factor_id": run.composite_id,
        "sample_role": "research_evaluation",
        "observations": 0,
        "coverage": None,
        "rank_ic_mean": None,
        "rank_ic_mean_status": status,
        "rank_ic_std": None,
        "rank_icir": None,
        "rank_icir_status": status,
        "rank_ic_t_stat": None,
        "rank_ic_t_stat_status": status,
        "ic_days": 0,
        "split_metrics": [],
        "horizon_metrics": [],
        "metrics": {},
        "warning_codes": [EVALUATION_WINDOW_TOO_SHORT],
        "warnings": [
            "同窗评价诊断不可用：回测窗口仅 "
            f"{plan.in_window_date_count} 个交易日，少于评价所需的 "
            f"{MIN_DISPLAY_TRADING_DAYS} 个交易日；组合回测本身不受此下限影响"
        ],
        "simulation_profile": _json_safe(run.engine_profile),
        "coverage_lineage": {},
        "artifact_path": None,
        "meta": _same_window_evaluation_meta(run, status="unavailable"),
    }


def _same_window_evaluation(
    config: QuantForgeConfig,
    plan: _MultiFactorBacktestPlan,
    run: CompositeBacktestRun,
    rd_config: ResearchLoopConfig,
) -> dict[str, Any]:
    """Fill the ``evaluation`` slot from the SAME backtest window (FP-2).

    Runs ``evaluate_factor`` over the materialized composite with the engine
    profile (same test period, decay already 0), reading values from the
    per-run overlay. ``evaluate_factor`` enforces the 126-trading-day display
    floor; instead of string-matching its raise, the window length is checked
    against the same constant up front and the slot degrades honestly.
    """

    from quant_forge.apps.web import server as _server

    if plan.in_window_date_count < MIN_DISPLAY_TRADING_DAYS:
        return _degraded_same_window_evaluation(run, plan)
    evaluation = _server.evaluate_factor(
        run.composite_id,
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        horizon_days=plan.settings.holding_days,
        horizon_days_matrix=rd_config.horizon_days_matrix,
        sample_splits=rd_config.sample_splits,
        simulation_profile=run.engine_profile,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=run.overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
    )
    payload = _apply_metric_display(_json_safe(evaluation))
    for nested_metric in [
        *(payload.get("split_metrics") or []),
        *(payload.get("horizon_metrics") or []),
    ]:
        if isinstance(nested_metric, dict):
            _apply_metric_display(nested_metric)
    payload["meta"] = _same_window_evaluation_meta(run, status="available")
    return payload


def _synthesis_validity_payload(period_count: int) -> dict[str, Any]:
    """§8 validity block with the literal caveat list.

    The RB-1 phase caveat carries the REALIZED non-overlapping period count
    (§8 writes the placeholder ``N``; RB-1 requires the realized count in
    plain language, so the number is substituted here).
    """

    return {
        "message": "研究口径合成回测（非生产交易口径）",
        "basis": EXTERNAL_OOS_ROLE,
        "caveats": [
            "先验/拟合已如实标注",
            "调仓周期与持有期为同一参数（holding_days）：K=1 非重叠，指标基于约 "
            f"{period_count} 个独立区间，对起始相位敏感",
            "样本内评价为同窗诊断，非独立研究样本",
            "成本以目标簿 L1 换手计，漂移回补交易未计成本（换手/成本偏低估）",
            "is_st/上市过滤仅在建仓时点应用；持有期内转 ST/退市按最后成交价了结",
        ],
    }


def _synthesis_provenance_payload(
    *,
    member_plan: tuple[MemberFetchSpec, ...],
    method_name: str,
    method_params: dict[str, Any],
    standardization: str,
    standardization_pinned: bool,
    composite_id: str,
    is_fitted: bool,
    coverage: CoverageAccounting,
    universe_filters: tuple[str, ...],
    period_count: int,
    skipped_rebalances: int,
    degenerate_cross_sections: int,
    weights_effective: dict[str, float] | None,
    fitted: FittedCompositeResult | None = None,
    rank_ic_redundancy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§8 ``synthesis_provenance`` block (both FP-1 branches).

    ``factors[]`` carries each member's formula PINNED at plan-build time
    (CP0 amendment) so downstream consumers never depend on the live
    registry. ``coverage_by_role`` carries the single backtest-only role;
    ``coverage_ratio`` is a real ``null`` when unobservable (FP-4) — the
    frontend renders it n/a, never 0 (synthesis.js:349).

    FP-1 branch selection keys off the REQUESTED method family (``fitted``):
    a-priori runs carry ``weights_effective`` RAW (equal_weight echoes its
    uniform 1.0 claim) and NO fitted fields; fitted runs OMIT
    ``weights_effective`` entirely — the frontend captions that field
    unconditionally as an a-priori raw-declared claim (synthesis.js:386-388)
    — and instead carry ``fitted_weights_latest`` (last GENUINELY fitted
    vector, or ``null``), the per-signal-date ``fitted_weights_path``
    diagnostic, ``fitted_period_fraction`` and ``warmup_period_count``.
    ``is_fitted`` is the run-level REALIZED truth: a fitted request that
    downgraded (``NO_FITTED_PERIODS``, §3 RB-8) reports ``false`` while
    keeping the fitted diagnostic fields so the downgrade is auditable.
    ``rank_ic_redundancy`` is the §4.5 advisory crowding matrix — attached
    for both branches, never a gate.
    """

    directions = {spec.factor_id: spec.direction for spec in member_plan}
    sources = {spec.factor_id: spec.source for spec in member_plan}
    coverage_rows = [
        {
            "factor_id": row.factor_id,
            "direction": directions.get(row.factor_id),
            "source": sources.get(row.factor_id),
            "rows_scored": row.rows_scored,
            "rows_in_composite": row.rows_in_composite,
            "coverage_ratio": row.coverage_ratio,
        }
        for row in coverage.per_factor
    ]
    payload: dict[str, Any] = {
        "factors": [spec.provenance_entry() for spec in member_plan],
        "directions": directions,
        "method": method_name,
        "method_params": dict(method_params),
        "standardization": standardization,
        "standardization_pinned_by_method": standardization_pinned,
        "composite_id": composite_id,
        "is_fitted": is_fitted,
        "coverage_rule": coverage.coverage_rule,
        "min_factor_coverage": coverage.min_factor_coverage,
        "universe_filters": [str(item) for item in universe_filters],
        "period_count": period_count,
        "non_overlapping": True,
        "rows_required": coverage.rows_required,
        "rows_full_coverage": coverage.rows_full_coverage,
        "skipped_rebalances": skipped_rebalances,
        "degenerate_cross_sections": degenerate_cross_sections,
        "coverage_by_role": {
            EXTERNAL_OOS_ROLE: {
                "coverage": coverage_rows,
                "rows_required": coverage.rows_required,
                "rows_full_coverage": coverage.rows_full_coverage,
            }
        },
    }
    if fitted is None and weights_effective is not None:
        # A-priori branch only: the raw declared claim (FP-1). A fitted run
        # never routes any vector through this field — not even a downgraded
        # equal-weight fallback, which the user never declared.
        payload["weights_effective"] = dict(weights_effective)
    if fitted is not None:
        payload["fitted_period_fraction"] = fitted.fitted_period_fraction
        payload["warmup_period_count"] = fitted.warmup_period_count
        payload["fitted_weights_latest"] = (
            dict(fitted.fitted_weights_latest)
            if fitted.fitted_weights_latest is not None
            else None
        )
        payload["fitted_weights_path"] = [
            {
                "signal_date": entry.signal_date.date().isoformat(),
                "weights": dict(entry.weights),
                "eligible_period_count": entry.eligible_period_count,
                "flag": entry.flag,
            }
            for entry in fitted.weights_path
        ]
    if rank_ic_redundancy is not None:
        payload["rank_ic_redundancy"] = dict(rank_ic_redundancy)
    return payload


def _multi_factor_backtest_payload(
    plan: _MultiFactorBacktestPlan,
    composite: CompositeResult | FittedCompositeResult,
    prescan: RebalancePrescan,
    run: CompositeBacktestRun,
    evaluation_payload: dict[str, Any],
    redundancy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the exact §8 response the shipped renderers consume.

    ``backtest`` is ``_backtest_payload`` output VERBATIM (FP-3 — every
    top-level scalar tile factor.js:136-176 reads), extended additively with
    the synthesis disclosure codes: ``NON_OVERLAPPING_COHORTS`` +
    ``PHASE_SENSITIVE_SMALL_SAMPLE`` (RB-1), the pre-scan skip codes (RB-7),
    ``DEGENERATE_CROSS_SECTION`` (RB-9) and — on the fitted branch — the
    §4.4 fit codes (``WARM_UP_IC_UNFITTED`` / ``IC_DEGENERATE_EQUAL_WEIGHT``
    / ``NO_FITTED_PERIODS``) carried by ``composite.warning_codes``; the
    engine's own codes — including the inherited
    ``FINAL_PARTIAL_PERIOD_EXCLUDED`` handling — stay first and unchanged.
    ``in_sample_backtest`` is ``null``: the module is backtest-only over ONE
    window, a second same-window engine pass would duplicate ``backtest``
    under a misleading in-sample label, and the renderer is null-safe
    (synthesis.js:449 ``|| null``; factor.js:131 returns ``''``).
    """

    backtest_payload = _backtest_payload(run.result)
    merged_codes = list(backtest_payload["warning_codes"])
    for code in (
        NON_OVERLAPPING_COHORTS,
        PHASE_SENSITIVE_SMALL_SAMPLE,
        *prescan.warning_codes,
        *composite.warning_codes,
    ):
        if code not in merged_codes:
            merged_codes.append(code)
    backtest_payload["warning_codes"] = merged_codes
    warnings = list(backtest_payload["warnings"])
    warnings.append(
        "非重叠持有期口径：holding_days 同时是调仓周期与持有期（K=1），窗口约含 "
        f"{plan.period_count} 个独立持有期，指标对起始相位敏感"
    )
    backtest_payload["warnings"] = warnings

    fitted_result = composite if isinstance(composite, FittedCompositeResult) else None
    provenance = _synthesis_provenance_payload(
        member_plan=plan.member_plan,
        method_name=plan.method.name,
        method_params=plan.method_params,
        standardization=plan.standardization,
        standardization_pinned=plan.standardization_pinned,
        composite_id=run.composite_id,
        # Run-level realized truth: a fitted request that produced zero
        # genuinely fitted rebalances reports false (RB-8 downgrade);
        # a-priori methods are false by nature.
        is_fitted=(
            fitted_result.is_fitted if fitted_result is not None else plan.method.is_fitted
        ),
        coverage=composite.coverage,
        universe_filters=plan.universe_filters,
        period_count=plan.period_count,
        # Realized ledger count (the engine keeps skip stubs visible, RB-7);
        # the pre-scan is a score-side lower bound and feeds warning codes.
        skipped_rebalances=int(run.result.skipped_rebalances),
        degenerate_cross_sections=len(composite.degenerate_dates),
        weights_effective=(
            None if fitted_result is not None else composite.weights_effective
        ),
        fitted=fitted_result,
        rank_ic_redundancy=redundancy,
    )
    return {
        "factor": _json_safe(run.materialized.definition),
        "parameters": _json_safe(plan.settings.parameters),
        "evaluation": evaluation_payload,
        "in_sample_backtest": None,
        "backtest": backtest_payload,
        "validity": _synthesis_validity_payload(plan.period_count),
        "synthesis_provenance": provenance,
    }


def _parse_payload(parsed: ParsedFactor, rd_config: ResearchLoopConfig) -> dict[str, Any]:
    return {
        "parser": _parser_payload(parsed),
        "factor": _json_safe(parsed.factor),
        "parameters": _default_validation_parameters(parsed.factor, rd_config),
        # No-silent-fallback: always present so the frontend can rely on the
        # field; non-empty exactly when the parse landed on the generic
        # fallback formula (see llm_factor_parser.generic_fallback_warnings).
        "warnings": list(parsed.warnings),
    }


def _validation_payload(
    factor: FactorDefinition,
    *,
    parser: dict[str, Any] | None,
    evaluation: EvaluationResult,
    in_sample_backtest: BacktestResult,
    backtest: BacktestResult,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    evaluation_payload = _apply_metric_display(_json_safe(evaluation))
    for nested_metric in [
        *(evaluation_payload.get("split_metrics") or []),
        *(evaluation_payload.get("horizon_metrics") or []),
    ]:
        if isinstance(nested_metric, dict):
            _apply_metric_display(nested_metric)
    return {
        "parser": _parser_payload_from_request(parser, factor),
        "factor": _json_safe(factor),
        "parameters": _json_safe(parameters),
        "evaluation": evaluation_payload,
        "in_sample_backtest": _json_safe(in_sample_backtest),
        "backtest": _json_safe(backtest),
    }


def _parser_payload(parsed: ParsedFactor) -> dict[str, str]:
    return {
        "source": parsed.source,
        "provider": parsed.provider,
        "model": parsed.model,
    }


def _parser_payload_from_request(parser: dict[str, Any] | None, factor: FactorDefinition) -> dict[str, str]:
    raw = parser if isinstance(parser, dict) else {}
    source = str(raw.get("source") or factor.source or "user")
    provider = str(raw.get("provider") or source)
    model = str(raw.get("model") or "")
    return {
        "source": source,
        "provider": provider,
        "model": model,
    }


def _default_validation_parameters(factor: FactorDefinition, rd_config: ResearchLoopConfig) -> dict[str, Any]:
    backtest_profile = rd_config.backtest_profile
    evaluation_profile = rd_config.evaluation_profile
    costs = rd_config.transaction_costs
    return {
        "holding_days": factor.horizon_days,
        "execution_delay_days": backtest_profile.execution_delay_days,
        "decay_days": backtest_profile.decay_days,
        "top_quantile": backtest_profile.top_quantile,
        "evaluation_start": evaluation_profile.test_period_start,
        "evaluation_end": evaluation_profile.test_period_end,
        "backtest_start": backtest_profile.test_period_start,
        "backtest_end": backtest_profile.test_period_end,
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "short_borrow_bps_annual": costs.short_borrow_bps_annual,
        "include_partial_final_period": False,
        "evaluation": _simulation_profile_payload(evaluation_profile),
        "backtest": _simulation_profile_payload(backtest_profile),
        "transaction_costs": _transaction_costs_payload(costs),
    }


def _idea_validation_settings(
    factor: FactorDefinition,
    raw_parameters: dict[str, Any] | None,
    rd_config: ResearchLoopConfig,
) -> _IdeaValidationSettings:
    defaults = _default_validation_parameters(factor, rd_config)
    raw = raw_parameters or {}
    if not isinstance(raw, dict):
        raise ValueError("validation parameters must be a JSON object")
    holding_days = _positive_int_parameter(raw.get("holding_days", defaults["holding_days"]), "holding_days")
    include_partial_final_period = _bool_parameter(
        raw.get("include_partial_final_period", defaults["include_partial_final_period"]),
        "include_partial_final_period",
    )
    evaluation_overrides = _test_period_override("evaluation", raw)
    evaluation_overrides.update(_role_profile_overrides("evaluation", raw))
    backtest_overrides = _flat_backtest_profile_overrides(raw)
    backtest_overrides.update(_test_period_override("backtest", raw))
    backtest_overrides.update(_role_profile_overrides("backtest", raw))
    cost_payload = _cost_parameters(raw, defaults)
    transaction_costs = TransactionCostModel(
        commission_bps=_nonnegative_float_parameter(cost_payload["commission_bps"], "commission_bps"),
        slippage_bps=_nonnegative_float_parameter(cost_payload["slippage_bps"], "slippage_bps"),
        short_borrow_bps_annual=_nonnegative_float_parameter(
            cost_payload["short_borrow_bps_annual"],
            "short_borrow_bps_annual",
        ),
    )
    evaluation_profile = simulation_profile_from_mapping(evaluation_overrides, rd_config.evaluation_profile)
    backtest_profile = simulation_profile_from_mapping(backtest_overrides, rd_config.backtest_profile)
    return _IdeaValidationSettings(
        holding_days=holding_days,
        evaluation_profile=evaluation_profile,
        backtest_profile=backtest_profile,
        transaction_costs=transaction_costs,
        include_partial_final_period=include_partial_final_period,
        parameters={
            "holding_days": holding_days,
            "execution_delay_days": backtest_profile.execution_delay_days,
            "decay_days": backtest_profile.decay_days,
            "top_quantile": backtest_profile.top_quantile,
            "evaluation_start": evaluation_profile.test_period_start,
            "evaluation_end": evaluation_profile.test_period_end,
            "backtest_start": backtest_profile.test_period_start,
            "backtest_end": backtest_profile.test_period_end,
            "commission_bps": transaction_costs.commission_bps,
            "slippage_bps": transaction_costs.slippage_bps,
            "short_borrow_bps_annual": transaction_costs.short_borrow_bps_annual,
            "include_partial_final_period": include_partial_final_period,
            "evaluation": _simulation_profile_payload(evaluation_profile),
            "backtest": _simulation_profile_payload(backtest_profile),
            "transaction_costs": _transaction_costs_payload(transaction_costs),
        },
    )


def _simulation_profile_payload(profile: SimulationProfile) -> dict[str, Any]:
    return {
        "simulation": {
            "execution_delay_days": profile.execution_delay_days,
            "decay_days": profile.decay_days,
            "top_quantile": profile.top_quantile,
        },
        "test_period": {
            "start": profile.test_period_start,
            "end": profile.test_period_end,
        },
    }


def _transaction_costs_payload(costs: TransactionCostModel) -> dict[str, float]:
    return {
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "short_borrow_bps_annual": costs.short_borrow_bps_annual,
    }


def _role_profile_overrides(role: str, raw: dict[str, Any]) -> dict[str, Any]:
    role_payload = raw.get(role)
    if role_payload is None:
        return {}
    if not isinstance(role_payload, dict):
        raise ValueError(f"{role} parameters must be a JSON object")
    simulation_payload = role_payload.get("simulation", {})
    if simulation_payload is None:
        simulation_payload = {}
    if not isinstance(simulation_payload, dict):
        raise ValueError(f"{role}.simulation must be a JSON object")
    overrides = _simulation_parameter_overrides(simulation_payload, f"{role}.simulation")
    overrides.update(_role_test_period_override(role, role_payload))
    return overrides


def _flat_backtest_profile_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    return _simulation_parameter_overrides(raw, "")


def _simulation_parameter_overrides(raw: dict[str, Any], prefix: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    label = f"{prefix}." if prefix else ""
    if "execution_delay_days" in raw:
        overrides["execution_delay_days"] = _positive_int_parameter(
            raw["execution_delay_days"],
            f"{label}execution_delay_days",
        )
    if "decay_days" in raw:
        overrides["decay_days"] = _nonnegative_int_parameter(raw["decay_days"], f"{label}decay_days")
    if "top_quantile" in raw:
        overrides["top_quantile"] = _float_parameter(raw["top_quantile"], f"{label}top_quantile")
    return overrides


def _role_test_period_override(role: str, role_payload: dict[str, Any]) -> dict[str, Any]:
    test_period = role_payload.get("test_period", {})
    if test_period is None:
        test_period = {}
    if not isinstance(test_period, dict):
        raise ValueError(f"{role}.test_period must be a JSON object")
    overrides: dict[str, dict[str, str | None]] = {}
    if "start" in test_period:
        overrides.setdefault("test_period", {})["start"] = _optional_date_parameter(
            test_period["start"],
            f"{role}.test_period.start",
        )
    if "end" in test_period:
        overrides.setdefault("test_period", {})["end"] = _optional_date_parameter(
            test_period["end"],
            f"{role}.test_period.end",
        )
    return overrides


def _cost_parameters(raw: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    costs = raw.get("transaction_costs")
    if costs is None:
        costs = {}
    if not isinstance(costs, dict):
        raise ValueError("transaction_costs must be a JSON object")
    return {
        "commission_bps": costs.get("commission_bps", raw.get("commission_bps", defaults["commission_bps"])),
        "slippage_bps": costs.get("slippage_bps", raw.get("slippage_bps", defaults["slippage_bps"])),
        "short_borrow_bps_annual": costs.get(
            "short_borrow_bps_annual",
            raw.get("short_borrow_bps_annual", defaults["short_borrow_bps_annual"]),
        ),
    }


def _test_period_override(prefix: str, raw: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    start_key = f"{prefix}_start"
    end_key = f"{prefix}_end"
    test_period: dict[str, str | None] = {}
    if start_key in raw:
        test_period["start"] = _optional_date_parameter(raw[start_key], start_key)
    if end_key in raw:
        test_period["end"] = _optional_date_parameter(raw[end_key], end_key)
    if not test_period:
        return {}
    return {"test_period": test_period}


def _optional_date_parameter(value: Any, name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    return text


def _bool_parameter(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def _positive_int_parameter(value: Any, name: str) -> int:
    parsed = _int_parameter(value, name)
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_int_parameter(value: Any, name: str) -> int:
    parsed = _int_parameter(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _int_parameter(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_parameter(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _nonnegative_float_parameter(value: Any, name: str) -> float:
    parsed = _float_parameter(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _factor_from_request(raw: dict[str, Any] | FactorDefinition) -> FactorDefinition:
    if isinstance(raw, FactorDefinition):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("factor must be a JSON object")
    filters_raw = raw.get("universe_filters", ())
    if not isinstance(filters_raw, list | tuple):
        raise ValueError("factor.universe_filters must be a list")
    # P4: the idea-validation endpoint persists request-supplied factor JSON,
    # so it accepts draft rows only (promotion has its own audited path in
    # factor_library.repository) and applies the same name slug and free-text
    # shape limits as the LLM parser path before anything reaches factor_root.
    status = str(raw.get("status", "draft"))
    if status != "draft":
        raise ValueError("idea validation accepts factor.status 'draft' only")
    return FactorDefinition(
        factor_id=str(raw["factor_id"]),
        name=slugify_factor_name(str(raw.get("name", raw["factor_id"]))),
        formula=str(raw["formula"]),
        status=status,  # type: ignore[arg-type]
        description=sanitize_factor_text(str(raw.get("description", "")), FACTOR_DESCRIPTION_MAX_CHARS),
        horizon_days=_positive_int_parameter(raw.get("horizon_days", 5), "factor.horizon_days"),
        universe_filters=tuple(sanitize_factor_text(str(item), UNIVERSE_FILTER_MAX_CHARS) for item in filters_raw),
        source=str(raw.get("source", "user")),
    )


def _factor_from_validation_payload(payload: dict[str, Any], config: QuantForgeConfig) -> FactorDefinition:
    if "factor" in payload:
        return _factor_from_request(payload["factor"])
    factor_id = str(payload.get("factor_id", "")).strip()
    if not factor_id:
        raise ValueError("factor is required")
    return FactorRepository(config.paths.factor_root).get(factor_id)


def _optional_parser_payload(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("parser must be a JSON object")
    return value


def _optional_parameters_payload(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("parameters must be a JSON object")
    return value


def _run_research_once(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    seed_factor_id: str,
    *,
    objective: str,
    max_candidates: int,
    cancel_event: threading.Event | None = None,
) -> ResearchLoopResult:
    if not seed_factor_id.strip():
        raise ValueError("seed_factor_id is required")
    _raise_if_cancelled(cancel_event)
    hypothesis_generator = None
    review_generator = None
    if _rd_generation_mode(rd_config.llm.hypothesis_mode) == "llm":
        hypothesis_generator = LLMHypothesisGenerator(_rd_llm_settings(config, feature="RD hypothesis generation"))
    if _rd_generation_mode(rd_config.llm.review_mode) == "llm":
        review_generator = LLMResearchReviewGenerator(_rd_llm_settings(config, feature="RD self-review"))
    service = ResearchLoopService(
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        simulation_profile=rd_config.simulation_profile,
        trial_simulation_overlays=rd_config.trial_overlays,
        evaluation_simulation_profile=rd_config.evaluation_profile,
        backtest_simulation_profile=rd_config.backtest_profile,
        parameter_search_enabled=rd_config.parameter_search.enabled,
        parameter_search_method=rd_config.parameter_search.method,
        parameter_search_keep_ratio=rd_config.parameter_search.keep_ratio,
        parameter_search_min_survivors=rd_config.parameter_search.min_survivors,
        quick_horizon_days_matrix=rd_config.parameter_search.quick_horizon_days_matrix,
        quick_sample_splits=rd_config.parameter_search.quick_sample_splits,
        horizon_days_matrix=rd_config.horizon_days_matrix,
        sample_splits=rd_config.sample_splits,
        transaction_costs=rd_config.transaction_costs,
        deduplication=rd_config.deduplication,
        llm_formula_repair_attempts=rd_config.llm.max_formula_repair_attempts,
        strategy_selector_enabled=rd_config.strategy_selector_enabled,
        research_memory_enabled=rd_config.research_memory_enabled,
        hypothesis_generator=hypothesis_generator,
        review_generator=review_generator,
        cancel_event=cancel_event,
    )
    weights = weights_for_objective(rd_config, objective)
    _raise_if_cancelled(cancel_event)
    return service.run_once(
        seed_factor_id,
        objective=objective,
        max_candidates=max_candidates,
        weights=weights,
        gate=rd_config.gate,
    )


def _run_research_iterations(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    seed_factor_id: str,
    *,
    objective: str,
    max_candidates: int,
    iterations: int,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    from quant_forge.apps.web import server as _server

    requested_iterations = _rd_iterations_parameter(iterations)
    original_seed = seed_factor_id
    current_seed = seed_factor_id
    rounds: list[dict[str, Any]] = []
    last_result: ResearchLoopResult | None = None
    stopped_reason = "completed"
    failed_round_index = 0
    chain_error = ""

    for round_index in range(1, requested_iterations + 1):
        _raise_if_cancelled(cancel_event)
        try:
            result = _server._run_research_once(
                config,
                rd_config,
                current_seed,
                objective=objective,
                max_candidates=max_candidates,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise _WebJobCancelled("run cancelled by user") from exc
            if last_result is None:
                raise
            failed_round_index = round_index
            chain_error = _client_error_message(exc, fallback="research iteration failed")
            stopped_reason = "iteration_failed"
            break
        last_result = result
        next_seed, selection_reason = _next_research_seed(result, current_seed)
        rounds.append(_research_iteration_summary(result, round_index, next_seed, selection_reason))
        if round_index >= requested_iterations:
            break
        if next_seed is None:
            stopped_reason = selection_reason
            break
        if next_seed == current_seed:
            stopped_reason = "no_new_seed"
            rounds[-1]["selection_reason"] = stopped_reason
            break
        current_seed = next_seed

    if last_result is None:
        raise ValueError("research iterations require at least one round")
    payload = _research_result_payload(last_result)
    optimization_summary = _research_optimization_summary(rounds)
    accepted_factor_id = _last_accepted_research_factor_id(rounds)
    last_explored_factor_id = _last_explored_research_factor_id(last_result, original_seed)
    recommended_factor_id = accepted_factor_id or original_seed
    recommendation_basis = "accepted_candidate" if accepted_factor_id else "original_seed_retained"
    exploration_summary = _research_exploration_seed_summary(rounds)
    payload["requested_iterations"] = requested_iterations
    payload["iteration_count"] = len(rounds)
    payload["failed_round_index"] = failed_round_index
    payload["chain_error"] = chain_error
    payload["partial_result"] = bool(chain_error)
    payload["seed_factor_id"] = original_seed
    payload["original_seed_factor_id"] = original_seed
    payload["last_round_seed_factor_id"] = last_result.seed_factor_id
    payload["last_accepted_factor_id"] = accepted_factor_id
    payload["last_explored_factor_id"] = last_explored_factor_id
    payload["recommended_factor_id"] = recommended_factor_id
    payload["recommendation_basis"] = recommendation_basis
    payload["final_factor_id"] = recommended_factor_id
    payload["stopped_reason"] = stopped_reason
    payload.update(optimization_summary)
    payload["optimization_performed"] = optimization_summary["chain_optimization_performed"]
    payload["no_optimization_performed"] = optimization_summary["chain_no_optimization_performed"]
    payload.update(exploration_summary)
    payload["accepted_candidate_ids"] = _aggregate_research_accepted_ids(rounds)
    payload["round_report_paths"] = [str(item["report_path"]) for item in rounds if item.get("report_path")]
    payload["comparison_rows"] = _aggregate_research_comparison_rows(rounds)
    payload["iteration_chain"] = {
        "requested_iterations": requested_iterations,
        "completed_iterations": len(rounds),
        "failed_round_index": failed_round_index,
        "chain_error": chain_error,
        "partial_result": bool(chain_error),
        "original_seed_factor_id": original_seed,
        "last_round_seed_factor_id": last_result.seed_factor_id,
        "final_seed_factor_id": last_result.seed_factor_id,
        "last_accepted_factor_id": accepted_factor_id,
        "last_explored_factor_id": last_explored_factor_id,
        "recommended_factor_id": recommended_factor_id,
        "recommendation_basis": recommendation_basis,
        "final_factor_id": payload["final_factor_id"],
        "stopped_reason": stopped_reason,
        **optimization_summary,
        **exploration_summary,
        "round_report_paths": payload["round_report_paths"],
        "comparison_rows": payload["comparison_rows"],
        "rounds": rounds,
    }
    return payload


def _rd_iterations_parameter(value: Any) -> int:
    iterations = _positive_int_parameter(value, "iterations")
    if iterations > MAX_RD_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_RD_ITERATIONS}")
    return iterations


def _next_research_seed(result: ResearchLoopResult, current_seed: str) -> tuple[str | None, str]:
    for factor_id in result.accepted_candidate_ids:
        if factor_id and factor_id != current_seed:
            return factor_id, "accepted_candidate"
    if result.candidates:
        best = result.candidates[0].factor.factor_id
        if best and best != current_seed:
            return best, "fallback_best_score"
        return best, "best_candidate_is_seed"
    return None, "no_candidates"


def _last_explored_research_factor_id(result: ResearchLoopResult, original_seed: str) -> str:
    next_seed, reason = _next_research_seed(result, result.seed_factor_id)
    if next_seed is not None and reason != "best_candidate_is_seed":
        return next_seed
    if result.candidates:
        return result.candidates[0].factor.factor_id
    return result.seed_factor_id or original_seed


def _last_accepted_research_factor_id(rounds: list[dict[str, Any]]) -> str:
    for item in reversed(rounds):
        accepted = [str(factor_id) for factor_id in item.get("accepted_candidate_ids") or () if factor_id]
        if accepted:
            return accepted[-1]
    return ""


def _research_exploration_seed_summary(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    if not rounds:
        return {
            "next_exploration_seed_factor_id": "",
            "next_exploration_seed_reason": "no_rounds",
            "next_exploration_seed_gate_passed": None,
        }
    last_round = rounds[-1]
    selected_seed = str(last_round.get("selected_next_seed_factor_id") or "")
    reason = str(last_round.get("selection_reason") or "")
    if not selected_seed or reason in {"no_candidates", "no_new_seed", "best_candidate_is_seed"}:
        return {
            "next_exploration_seed_factor_id": "",
            "next_exploration_seed_reason": reason or "none",
            "next_exploration_seed_gate_passed": None,
        }
    return {
        "next_exploration_seed_factor_id": selected_seed,
        "next_exploration_seed_reason": reason,
        "next_exploration_seed_gate_passed": reason == "accepted_candidate",
    }


def _research_iteration_summary(
    result: ResearchLoopResult,
    round_index: int,
    selected_next_seed: str | None,
    selection_reason: str,
) -> dict[str, Any]:
    top_candidate = result.candidates[0] if result.candidates else None
    return {
        "round": round_index,
        "seed_factor_id": result.seed_factor_id,
        "candidate_count": len(result.candidates),
        "accepted_candidate_ids": list(result.accepted_candidate_ids),
        "top_candidate_id": top_candidate.factor.factor_id if top_candidate is not None else "",
        "top_score": top_candidate.score if top_candidate is not None else None,
        "selected_next_seed_factor_id": selected_next_seed,
        "selection_reason": selection_reason,
        "optimization_performed": result.optimization_performed,
        "no_optimization_performed": result.no_optimization_performed,
        "report_path": str(result.report_path) if result.report_path is not None else "",
        "comparison_rows": _json_safe(result.comparison_rows),
    }


def _research_result_payload(result: ResearchLoopResult) -> dict[str, Any]:
    """Return the compact RD shape used by the Web UI.

    ResearchLoopResult carries full evaluation/backtest objects, including
    per-day ledgers and schedules. Those belong in artifacts, not in the
    browser job payload.
    """

    return {
        "rd_stage": result.rd_stage,
        "seed_factor_id": result.seed_factor_id,
        "objective": result.objective,
        "objective_weights": _json_safe(result.objective_weights),
        "gate": _json_safe(result.gate),
        "candidates": [_research_candidate_payload(candidate) for candidate in result.candidates],
        "accepted_candidate_ids": list(result.accepted_candidate_ids),
        "comparison_rows": _json_safe(result.comparison_rows),
        "generation": _json_safe(result.generation),
        "search_trace": _json_safe(result.search_trace),
        "blocked_plans": _json_safe(result.blocked_plans),
        "trace_root": result.trace_root,
        "report_path": result.report_path,
        "workflow_type": result.workflow_type,
        "deduplication": _json_safe(result.deduplication),
        "optimization_performed": result.optimization_performed,
        "no_optimization_performed": result.no_optimization_performed,
    }


def _research_candidate_payload(candidate: Any) -> dict[str, Any]:
    selection_backtest = candidate.selection_backtest or candidate.backtest
    external_oos_backtest = candidate.external_oos_backtest or candidate.backtest
    return {
        "hypothesis": _json_safe(candidate.hypothesis),
        "factor": _json_safe(candidate.factor),
        "evaluation": _evaluation_payload(candidate.evaluation),
        "backtest": _backtest_payload(candidate.backtest),
        "selection_backtest": _backtest_payload(selection_backtest),
        "external_oos_backtest": _backtest_payload(external_oos_backtest),
        "split_weighted_icir": candidate.split_weighted_icir,
        "score": candidate.score,
        "gate_passed": candidate.gate_passed,
        "gate_reasons": list(candidate.gate_reasons),
        "self_review": _json_safe(candidate.self_review),
        "transitioned_to_candidate": candidate.transitioned_to_candidate,
        "formula_fingerprint": candidate.formula_fingerprint,
        "result_signature": candidate.result_signature,
        "candidate_shape_fingerprint": candidate.candidate_shape_fingerprint,
    }


_EVALUATION_DISPLAY_METRIC_KEYS = ("rank_ic_mean", "rank_icir", "rank_ic_t_stat")


def _apply_metric_display(
    payload: dict[str, Any], keys: tuple[str, ...] = _EVALUATION_DISPLAY_METRIC_KEYS
) -> dict[str, Any]:
    """Make legacy scalar metric keys honest against the qf.metrics.v2 map.

    For each key that has a MetricValue entry in ``payload["metrics"]``, a
    non-"available" status replaces the legacy placeholder scalar (for example
    0.0) with None and records the status under ``<key>_status``; an
    "available" status keeps the typed value. Payloads without the map (old
    artifacts) keep the legacy scalar and are marked with status "legacy".
    """

    metrics = payload.get("metrics")
    for key in keys:
        metric = metrics.get(key) if isinstance(metrics, dict) else None
        if isinstance(metric, dict) and metric.get("status"):
            status = str(metric["status"])
            payload[key] = metric.get("value") if status == "available" else None
            payload[f"{key}_status"] = status
        else:
            payload[f"{key}_status"] = "legacy"
    return payload


def _evaluation_payload(evaluation: EvaluationResult) -> dict[str, Any]:
    return _apply_metric_display({
        "factor_id": evaluation.factor_id,
        "observations": evaluation.observations,
        "coverage": evaluation.coverage,
        "rank_ic_mean": evaluation.rank_ic_mean,
        "rank_ic_std": evaluation.rank_ic_std,
        "rank_icir": evaluation.rank_icir,
        "ic_days": evaluation.ic_days,
        "artifact_path": evaluation.artifact_path,
        "rank_ic_t_stat": evaluation.rank_ic_t_stat,
        "split_metrics": _json_safe(evaluation.split_metrics),
        "horizon_metrics": [_horizon_metric_payload(metric) for metric in evaluation.horizon_metrics],
        "simulation_profile": _json_safe(evaluation.simulation_profile),
        "score_source": evaluation.score_source,
        "score_cached_rows": evaluation.score_cached_rows,
        "score_computed_rows": evaluation.score_computed_rows,
        "factor_values_path": evaluation.factor_values_path,
        "factor_values_write_path": evaluation.factor_values_write_path,
        "score_compute_mode": evaluation.score_compute_mode,
        "score_compute_reason": evaluation.score_compute_reason,
        "score_missing_rows": evaluation.score_missing_rows,
        "score_required_rows": evaluation.score_required_rows,
        "score_missing_ratio": evaluation.score_missing_ratio,
        "score_lookback_rows": evaluation.score_lookback_rows,
        "score_context_rows": evaluation.score_context_rows,
        "warnings": list(evaluation.warnings),
        "schema_version": evaluation.schema_version,
        "sample_role": evaluation.sample_role,
        "rank_ic_t_stat_naive": evaluation.rank_ic_t_stat_naive,
        "rank_ic_t_stat_hac": evaluation.rank_ic_t_stat_hac,
        "rank_ic_hac_standard_error": evaluation.rank_ic_hac_standard_error,
        "rank_ic_hac_lag": evaluation.rank_ic_hac_lag,
        "rank_ic_p_value_hac": evaluation.rank_ic_p_value_hac,
        "coverage_lineage": _json_safe(evaluation.coverage_lineage),
        "boundary_diagnostics": _json_safe(evaluation.boundary_diagnostics),
        "metric_provenance": _json_safe(evaluation.metric_provenance),
        "warning_codes": list(evaluation.warning_codes),
        "metrics": _json_safe(evaluation.metrics),
    })


def _horizon_metric_payload(metric: Any) -> dict[str, Any]:
    return _apply_metric_display({
        "horizon_days": metric.horizon_days,
        "observations": metric.observations,
        "coverage": metric.coverage,
        "rank_ic_mean": metric.rank_ic_mean,
        "rank_ic_std": metric.rank_ic_std,
        "rank_icir": metric.rank_icir,
        "ic_days": metric.ic_days,
        "rank_ic_t_stat": metric.rank_ic_t_stat,
        "sample_role": metric.sample_role,
        "rank_ic_t_stat_naive": metric.rank_ic_t_stat_naive,
        "rank_ic_t_stat_hac": metric.rank_ic_t_stat_hac,
        "rank_ic_hac_standard_error": metric.rank_ic_hac_standard_error,
        "rank_ic_hac_lag": metric.rank_ic_hac_lag,
        "rank_ic_p_value_hac": metric.rank_ic_p_value_hac,
        "coverage_lineage": _json_safe(metric.coverage_lineage),
        "boundary_diagnostics": _json_safe(metric.boundary_diagnostics),
        "metric_provenance": _json_safe(metric.metric_provenance),
        "warning_codes": list(metric.warning_codes),
        "metrics": _json_safe(metric.metrics),
    })


def _backtest_payload(backtest: BacktestResult) -> dict[str, Any]:
    return {
        "factor_id": backtest.factor_id,
        "periods": backtest.periods,
        "holding_days": backtest.holding_days,
        "cumulative_return": backtest.cumulative_return,
        "annualized_return": backtest.annualized_return,
        "annualized_volatility": backtest.annualized_volatility,
        "max_drawdown": backtest.max_drawdown,
        "artifact_path": backtest.artifact_path,
        "long_short_sharpe": backtest.long_short_sharpe,
        "gross_cumulative_return": backtest.gross_cumulative_return,
        "gross_annualized_return": backtest.gross_annualized_return,
        "gross_annualized_volatility": backtest.gross_annualized_volatility,
        "gross_long_short_sharpe": backtest.gross_long_short_sharpe,
        "gross_max_drawdown": backtest.gross_max_drawdown,
        "rebalance_rate": backtest.rebalance_rate,
        "turnover_rate": backtest.turnover_rate,
        "net_cumulative_return": backtest.net_cumulative_return,
        "net_annualized_return": backtest.net_annualized_return,
        "net_annualized_volatility": backtest.net_annualized_volatility,
        "net_long_short_sharpe": backtest.net_long_short_sharpe,
        "net_max_drawdown": backtest.net_max_drawdown,
        "top_quantile": backtest.top_quantile,
        "transaction_costs": _json_safe(backtest.transaction_costs),
        "simulation_profile": _json_safe(backtest.simulation_profile),
        "group_returns": _json_safe(backtest.group_returns),
        "segment_metrics": _json_safe(backtest.segment_metrics),
        "warnings": list(backtest.warnings),
        "assumptions": list(backtest.assumptions),
        "score_source": backtest.score_source,
        "score_cached_rows": backtest.score_cached_rows,
        "score_computed_rows": backtest.score_computed_rows,
        "factor_values_path": backtest.factor_values_path,
        "factor_values_write_path": backtest.factor_values_write_path,
        "score_compute_mode": backtest.score_compute_mode,
        "score_compute_reason": backtest.score_compute_reason,
        "score_missing_rows": backtest.score_missing_rows,
        "score_required_rows": backtest.score_required_rows,
        "score_missing_ratio": backtest.score_missing_ratio,
        "score_lookback_rows": backtest.score_lookback_rows,
        "score_context_rows": backtest.score_context_rows,
        "schema_version": backtest.schema_version,
        "sample_role": backtest.sample_role,
        "return_series_kind": backtest.return_series_kind,
        "completed_periods": backtest.completed_periods,
        "partial_periods": backtest.partial_periods,
        "skipped_rebalances": backtest.skipped_rebalances,
        "lost_positions": backtest.lost_positions,
        "exposure_days": backtest.exposure_days,
        "calendar_days": backtest.calendar_days,
        "reportable_annualization": _json_safe(backtest.reportable_annualization),
        "extrapolated_annualization": _json_safe(backtest.extrapolated_annualization),
        "initial_build_turnover": backtest.initial_build_turnover,
        "rebalance_turnover_mean": backtest.rebalance_turnover_mean,
        "rebalance_turnover_observation_count": backtest.rebalance_turnover_observation_count,
        "replacement_rate_mean": backtest.replacement_rate_mean,
        "replacement_rate_observation_count": backtest.replacement_rate_observation_count,
        "cost_reconciliation": _json_safe(backtest.cost_reconciliation),
        "metric_provenance": _json_safe(backtest.metric_provenance),
        "warning_codes": list(backtest.warning_codes),
        "metrics": _json_safe(backtest.metrics),
        "benchmark": _json_safe(backtest.benchmark),
        "benchmark_cumulative_return": backtest.benchmark_cumulative_return,
        "arithmetic_excess_return": backtest.arithmetic_excess_return,
        "relative_wealth_excess_return": backtest.relative_wealth_excess_return,
        "tracking_error": backtest.tracking_error,
        "information_ratio": backtest.information_ratio,
    }


def _research_optimization_summary(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate RD progress across chained rounds for user-facing status."""

    generated = any(
        int(item.get("candidate_count") or 0) > 0
        and item.get("selection_reason") not in {"best_candidate_is_seed", "no_new_seed"}
        for item in rounds
    )
    accepted = any(item.get("accepted_candidate_ids") for item in rounds)
    performed = any(bool(item.get("optimization_performed")) for item in rounds) or accepted
    if performed:
        status = "performed"
    elif generated:
        status = "attempted_no_acceptance"
    else:
        status = "no_optimization_performed"
    return {
        "optimization_status": status,
        "chain_optimization_status": status,
        "chain_optimization_performed": performed,
        "chain_candidate_generation_performed": generated,
        "chain_no_optimization_performed": status == "no_optimization_performed",
    }


def _aggregate_research_accepted_ids(rounds: list[dict[str, Any]]) -> list[str]:
    accepted: list[str] = []
    seen: set[str] = set()
    for item in rounds:
        for factor_id in item.get("accepted_candidate_ids") or ():
            if factor_id and factor_id not in seen:
                accepted.append(str(factor_id))
                seen.add(str(factor_id))
    return accepted


def _aggregate_research_comparison_rows(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in rounds:
        round_index = int(item.get("round") or 0)
        for raw_row in item.get("comparison_rows") or ():
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            row["round"] = round_index
            rows.append(row)
    return rows


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _WebJobCancelled("run cancelled by user")


def _existing_factor(repo: FactorRepository, factor_id: str) -> FactorDefinition | None:
    try:
        return repo.get(factor_id)
    except FileNotFoundError:
        return None


def _restore_factor_after_failed_validation(
    repo: FactorRepository,
    factor_id: str,
    previous_factor: FactorDefinition | None,
) -> None:
    if previous_factor is None:
        repo.delete(factor_id)
    else:
        repo.save(previous_factor)


def _job_id_from_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[:2] != ["api", "jobs"] or not parts[2]:
        raise KeyError(f"unknown job path: {path}")
    return parts[2]


def _job_id_from_cancel_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["api", "jobs"] or parts[3] != "cancel" or not parts[2]:
        raise KeyError(f"unknown job path: {path}")
    return parts[2]


def _rd_llm_settings(config: QuantForgeConfig, *, feature: str) -> Any:
    selected = config.llm.select_provider()
    if selected.provider.lower() in {"rule", "deterministic"}:
        raise RuntimeError(f"{feature} requires a configured LLM provider; selected provider is local rule.")
    validate_llm_runtime(selected)
    return selected


def _rd_generation_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in {"deterministic", "rule", "local_rule"}:
        return "local"
    return normalized


class WebControlTokenError(ValueError):
    """Predictable control-token startup misconfiguration for a 0.0.0.0 bind.

    Subclasses ``ValueError`` so existing callers that catch or pin
    ``ValueError`` keep working; the CLI boundary catches this specific
    type to print one actionable line instead of a traceback. The refusal
    to start without a token is deliberate and must stay.
    """


def _control_token_for_bind(host: str, config: QuantForgeConfig) -> str:
    if host != "0.0.0.0":
        return ""
    token_env = config.web.control_token_env.strip()
    if not token_env:
        raise WebControlTokenError("web.control_token_env is required when binding the web adapter to 0.0.0.0")
    control_value = os.environ.get(token_env, "")
    if not control_value:
        raise WebControlTokenError(
            f"web control token environment variable is not set: {token_env}; "
            f"set {token_env} to a non-empty secret value before binding to 0.0.0.0"
        )
    return control_value


def _web_public_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _web_public_json(asdict(value))
    if isinstance(value, Path):
        return _path_label(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_web_public_json(item) for item in value]
    if isinstance(value, set):
        return [_web_public_json(item) for item in sorted(value, key=str)]
    if isinstance(value, list):
        return [_web_public_json(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name == "raw_response":
                continue
            if name in _WEB_PATH_KEYS and item:
                result[name] = _web_public_path_value(item)
            else:
                result[name] = _web_public_json(item)
        return result
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _path_label(path: Path) -> str:
    return path.name or "path"


def _web_public_path_value(value: Any) -> Any:
    if isinstance(value, str | os.PathLike):
        return _path_label(Path(value))
    if isinstance(value, tuple):
        return [_web_public_path_value(item) for item in value]
    if isinstance(value, set):
        return [_web_public_path_value(item) for item in sorted(value, key=str)]
    if isinstance(value, list):
        return [_web_public_path_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _web_public_path_value(item) for key, item in value.items()}
    return _web_public_json(value)


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if str(key) != "raw_response"}
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, bool | int | str):
        return value
    return str(value)


def _optional_int(value: Any, name: str = "value") -> int | None:
    if value in {None, ""}:
        return None
    return _int_parameter(value, name)


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _paths_payload(config: QuantForgeConfig) -> dict[str, str]:
    return {
        "data_root": str(config.paths.data_root),
        "factor_root": str(config.paths.factor_root),
        "factor_values_root": str(config.paths.factor_values_root or ""),
        "factor_values_overlay_root": str(config.paths.factor_values_overlay_root or ""),
        "factor_values_manifest_root": str(config.paths.factor_values_manifest_root or ""),
        "artifact_root": str(config.paths.artifact_root),
    }


def _llm_provider_options(config: QuantForgeConfig) -> tuple[dict[str, str], ...]:
    options: list[dict[str, str]] = []
    for option in config.llm.public_provider_options():
        runtime_ready, runtime_error = _llm_runtime_status(config, option["provider"])
        enriched = dict(option)
        enriched["runtime_ready"] = "true" if runtime_ready else "false"
        enriched["runtime_error"] = runtime_error
        options.append(enriched)
    return tuple(options)


def _llm_runtime_status(config: QuantForgeConfig, provider: str) -> tuple[bool, str]:
    try:
        validate_llm_runtime(config.llm, provider)
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


def _active_llm(config: QuantForgeConfig) -> Any:
    return config.llm.select_provider()


def _rd_status_payload(config: QuantForgeConfig, rd_config: ResearchLoopConfig) -> dict[str, str]:
    active_llm = _active_llm(config)
    return {
        "research_stage": "research",
        "hypothesis_mode": _rd_generation_mode(rd_config.llm.hypothesis_mode),
        "review_mode": _rd_generation_mode(rd_config.llm.review_mode),
        "provider": active_llm.provider,
        "model": active_llm.model,
    }


def _rd_optimizer_label(config: QuantForgeConfig, rd_config: ResearchLoopConfig) -> str:
    active_llm = _active_llm(config)
    modes = (
        _rd_generation_mode(rd_config.llm.hypothesis_mode),
        _rd_generation_mode(rd_config.llm.review_mode),
    )
    if "llm" not in modes:
        return "research local deterministic"
    if active_llm.provider in {"rule", "deterministic"}:
        return "research LLM required / provider not configured"
    return f"research LLM / {active_llm.provider} / {active_llm.model}"


def _default_seed_factor_id(config: QuantForgeConfig) -> str:
    factor_ids = _catalog_factor_ids(config)
    if "FTR_DEMO_SMALL_CAP" in factor_ids:
        return "FTR_DEMO_SMALL_CAP"
    return factor_ids[0] if factor_ids else ""


def _simulation_profile_period_text(profile: Any) -> str:
    start = getattr(profile, "test_period_start", None) or "full available data"
    end = getattr(profile, "test_period_end", None) or "latest available data"
    return f"{start} -> {end}"


def _catalog_factor_ids(config: QuantForgeConfig) -> list[str]:
    try:
        factors = FactorCatalog(
            config.paths.factor_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        ).list()
    except Exception:
        return []
    return [factor.factor_id for factor in factors]


def _query_parameter(query: str, name: str) -> str | None:
    values = parse_qs(query).get(name)
    if not values:
        return None
    return values[-1]


def _run_history_limit(value: Any) -> int:
    if value in {None, ""}:
        return RESEARCH_HISTORY_DEFAULT_LIMIT
    limit = _positive_int_parameter(value, "limit")
    if limit > RESEARCH_HISTORY_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {RESEARCH_HISTORY_MAX_LIMIT}")
    return limit


def _redact_web_text(value: Any) -> Any:
    """Recursively redact absolute-path/secret-like fragments in free text.

    The run index validates its own writes, but the web adapter must not trust
    historical rows (legacy or hand-edited indexes may carry absolute paths in
    free-text fields). Every string that reaches a web payload goes through the
    lineage redactor first -- one definition per quantity:
    :func:`quant_forge.lineage.store.redact_free_text`.
    """

    if isinstance(value, str):
        return redact_free_text(value)
    if isinstance(value, dict):
        return {redact_free_text(str(key)): _redact_web_text(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_web_text(item) for item in value]
    return value


def _run_record_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Project one run-index row into the web history record shape.

    Metric highlights keep the qf.metrics.v2 convention: ``value`` may be null
    with an explanatory ``status``; null is never coerced to a number.
    """

    highlights_raw = row.get("metric_highlights")
    highlights: dict[str, dict[str, Any]] = {}
    if isinstance(highlights_raw, dict):
        for name, entry in highlights_raw.items():
            source = entry if isinstance(entry, dict) else {}
            highlights[str(name)] = {
                "value": source.get("value"),
                "unit": source.get("unit"),
                "status": source.get("status"),
                "observation_count": source.get("observation_count"),
            }
    window_raw = row.get("data_window")
    window = window_raw if isinstance(window_raw, dict) else {}
    return {
        "run_id": row.get("run_id"),
        "kind": row.get("kind"),
        "created_at": row.get("created_at"),
        "factor_ids": [str(item) for item in (row.get("factor_ids") or [])],
        "data_window": {
            "start_date": window.get("start_date"),
            "end_date": window.get("end_date"),
            "status": window.get("status"),
        },
        "config_fingerprint": row.get("config_fingerprint"),
        "metric_highlights": highlights,
        "artifact_paths_rel": [str(item) for item in (row.get("artifact_paths_rel") or [])],
        "warnings_count": row.get("warnings_count"),
    }


def _research_history_payload(config: QuantForgeConfig, *, limit: Any = None) -> dict[str, Any]:
    """Read-only run history for the web UI, most recent first.

    Reuses the lineage run index read API (the same one ``qf runs`` uses);
    newest-first is defined as reversed append order, matching
    ``qf runs list``. Path-like values pass through ``_web_public_json`` /
    ``_WEB_PATH_KEYS`` and free text through the lineage redactor.
    """

    from quant_forge.apps.web import server as _server

    parsed_limit = _run_history_limit(limit)
    rows = RunIndex(config.paths.artifact_root).read_rows()
    ordered = list(reversed(rows))[:parsed_limit]
    payload = {
        "runs": [_run_record_payload(row) for row in ordered],
        "count": len(ordered),
        "limit": parsed_limit,
        "total": len(rows),
    }
    return _server._web_public_json(_redact_web_text(payload))


def _bench_runs_payload(config: QuantForgeConfig, *, limit: Any = None) -> dict[str, Any]:
    """Bench run history plus the latest qf.bench.v1 report, most recent first.

    Uses the run index (kind="bench") written by ``qf factor bench`` and loads
    the latest bench JSON artifact from ``artifact_root``. Degrades to an empty
    list with ``latest: null`` when no bench run exists.
    """

    from quant_forge.apps.web import server as _server

    parsed_limit = _run_history_limit(limit)
    rows = RunIndex(config.paths.artifact_root).search(kind="bench")
    ordered = list(reversed(rows))[:parsed_limit]
    payload = {
        "runs": [_run_record_payload(row) for row in ordered],
        "count": len(ordered),
        "limit": parsed_limit,
        "total": len(rows),
        "latest": _bench_report_payload(config, ordered[0]) if ordered else None,
    }
    return _server._web_public_json(_redact_web_text(payload))


def _bench_report_payload(config: QuantForgeConfig, row: dict[str, Any]) -> dict[str, Any]:
    """Project one qf.bench.v1 artifact referenced by a bench run-index row.

    Metric cells keep the full MetricValue convention ``{value, unit, status,
    observation_count}``: a null value with an explanatory status must reach
    the browser as ``{"value": null, "status": ...}``, never as 0 and never as
    a bare scalar.
    """

    base: dict[str, Any] = {
        "run_id": row.get("run_id"),
        "created_at": row.get("created_at"),
        "available": False,
        "factors": [],
        "summary": {},
    }
    report = _read_bench_artifact(config, row)
    if report is None:
        base["reason"] = "bench artifact not available under artifact_root or not a matching qf.bench.v1 report"
        return base
    factors: list[dict[str, Any]] = []
    for factor_row in report.get("factors") or []:
        if not isinstance(factor_row, dict):
            continue
        metrics_raw = factor_row.get("metrics")
        metrics: dict[str, dict[str, Any]] = {}
        if isinstance(metrics_raw, dict):
            for name, entry in metrics_raw.items():
                source = entry if isinstance(entry, dict) else {}
                metrics[str(name)] = {
                    "value": source.get("value"),
                    "unit": source.get("unit"),
                    "status": source.get("status"),
                    "observation_count": source.get("observation_count"),
                }
        factors.append(
            {
                "factor_id": factor_row.get("factor_id"),
                "status": factor_row.get("status"),
                "error": factor_row.get("error"),
                "metrics": metrics,
                "warnings_count": factor_row.get("warnings_count"),
                "artifact_path_rel": factor_row.get("artifact_path_rel"),
            }
        )
    base.update(
        {
            "available": True,
            "schema_version": report.get("schema_version"),
            "factors": factors,
            "summary": report.get("summary") if isinstance(report.get("summary"), dict) else {},
        }
    )
    return base


def _read_bench_artifact(config: QuantForgeConfig, row: dict[str, Any]) -> dict[str, Any] | None:
    """Load the JSON bench artifact for one run-index row, containment-checked.

    Only relative references that resolve inside ``artifact_root`` are
    followed (FP-4: an unknown location yields None, never a guess), and only
    payloads that identify themselves as the referenced bench report are
    accepted: ``schema_version`` must be ``qf.bench.v1`` and the artifact's
    ``run_id`` (which the writer ``cli.factor_bench`` always emits) must match
    the run-index row, so a crafted row cannot surface unrelated artifact JSON
    through the bench panel.
    """

    artifact_root = Path(config.paths.artifact_root).expanduser().resolve(strict=False)
    for path_rel in row.get("artifact_paths_rel") or []:
        text = str(path_rel)
        if not text.endswith(".json"):
            continue
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        resolved = (artifact_root / candidate).resolve(strict=False)
        if not resolved.is_relative_to(artifact_root):
            continue
        if not resolved.is_file():
            continue
        # Authorize-then-open guard: O_NOFOLLOW makes the open fail (ELOOP ->
        # OSError) if the final component was swapped for a symlink after the
        # resolve()-based containment check above. The residual race on
        # intermediate directories is accepted under the local-only
        # single-user threat model, consistent with the CLI evidence-path
        # containment precedent.
        try:
            fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(loaded, dict):
            return None
        if loaded.get("schema_version") != "qf.bench.v1":
            return None
        if loaded.get("run_id") != row.get("run_id"):
            return None
        return loaded
    return None


# ---------------------------------------------------------------------------
# CP6-3 read-only Data console + Registry payload builders (GET-only).
#
# Endpoint discipline copies CP4-2: builders live here, are re-exported
# through the quant_forge.apps.web.server facade, and routing invokes them as
# _server.<fn> so monkeypatches on the server namespace keep taking effect.
# Every payload passes through _web_public_json(_redact_web_text(...)).
# ---------------------------------------------------------------------------


# Mirrors the FactorDefinition factor_id rule (core/contracts.py); the web
# route validates path-supplied ids against it before any catalog lookup.
_REGISTRY_FACTOR_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_=-]*")


def _registry_factor_id_from_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:3] != ["api", "registry", "factors"] or not parts[3]:
        raise KeyError(f"unknown registry path: {path}")
    # Decode exactly one percent-encoding layer before validation, mirroring
    # the static-asset handler (routing.py): the client sends the id segment
    # through encodeURIComponent, so a contract-legal id containing '='
    # arrives as '%3D'. The id regex stays the single validation gate after
    # decoding; decoded values outside the id rule (a '/', a control
    # character, a second encoding layer) fail it and map to 404 as before.
    return unquote(parts[3])


def _run_kind_parameter(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    kind = str(value).strip()
    if kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(RUN_KINDS)}")
    return kind


def _data_catalog_payload(config: QuantForgeConfig) -> dict[str, Any]:
    """Declared panel fields with role and research tags (Data console).

    Wraps the one authoritative catalog accessor ``data_field_catalog()``
    (FP-5) instead of extending ``GET /catalog``, whose payload shape is
    pinned and shared with LLM/CLI surfaces. Tag values keep the
    qf.research_tags.v1 None-vs-empty convention verbatim (FP-4): ``None``
    stays ``null`` and known-empty collections stay ``[]``.

    ``config`` is unused today; the uniform builder signature keeps the
    routing dispatch and monkeypatch seam identical across GET builders.
    """

    del config
    from quant_forge.apps.web import server as _server

    fields = [
        {
            "name": item.name,
            "description": item.description,
            "role": item.role,
            "tags": item.tags.to_dict(),
        }
        for item in data_field_catalog()
    ]
    payload = {"fields": fields, "count": len(fields)}
    return _server._web_public_json(_redact_web_text(payload))


def _data_status_payload(config: QuantForgeConfig) -> dict[str, Any]:
    """Coverage, quality gate result, and field availability in one payload.

    All three views derive from a single ``validate()`` pass over the data
    root. ``DataValidationResult.data_root`` / ``panel_path`` are absolute
    local paths: the payload is built field-by-field so they are dropped
    entirely and never serialized (no reliance on generic Path coercion).
    ``missing_columns`` mixes schema names with quality tokens
    (``duplicate_keys`` / ``null:*`` / ``dtype:*``); they are split
    server-side so the frontend renders labels, never derived scalars (FP-4).
    """

    from quant_forge.apps.web import server as _server

    validation = validate_data_root(config.paths.data_root)
    declared = {item.name for item in data_field_catalog()}
    missing_schema_columns = [name for name in validation.missing_columns if name in declared]
    quality_problems = [token for token in validation.missing_columns if token not in declared]
    payload = {
        "ok": bool(validation.ok),
        "coverage": {
            "rows": validation.rows,
            "instruments": validation.instruments,
            "date_count": validation.date_count,
            "start_date": validation.start_date,
            "end_date": validation.end_date,
        },
        "quality": {
            "missing_columns": missing_schema_columns,
            "problems": quality_problems,
            "synthesized_columns": list(validation.synthesized_columns),
            "optional_columns": list(validation.optional_columns),
        },
        "fields": [
            {"name": item.name, "role": item.role, "status": item.status}
            for item in catalog_field_availability(validation)
        ],
    }
    return _server._web_public_json(_redact_web_text(payload))


def _synthesis_methods_payload(config: QuantForgeConfig) -> dict[str, Any]:
    """Method + standardization catalog for the synthesis workbench (design §9).

    Wraps the one authoritative catalog in
    :mod:`quant_forge.synthesis.methods` (the same constants that drive
    server-side parameter re-validation) instead of duplicating the JSON
    here, so the advertised form schema and the enforced schema cannot
    drift. Fitted methods ship ``available: false`` (reserved 预留) until
    the fitted implementation phase lands; the frontend renders reserved
    methods as disabled options generically, keeping the capability surface
    honest without special-casing any method name.

    ``config`` is unused today; the uniform builder signature keeps the
    routing dispatch and monkeypatch seam identical across GET builders.
    """

    del config
    from quant_forge.apps.web import server as _server

    return _server._web_public_json(_redact_web_text(method_catalog_payload()))


def _factor_research_tags_by_id(config: QuantForgeConfig) -> dict[str, dict[str, Any]]:
    """qf.research_tags.v1 dicts keyed by factor id; empty when unreadable.

    A tags read failure degrades to ``{}`` so factor rows carry
    ``tags: null`` (unobserved, FP-4) rather than turning the whole listing
    into a 500.
    """

    try:
        tags = list_factor_research_tags(
            config.paths.factor_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        )
    except Exception:
        return {}
    return {str(tag.get("subject_id")): tag for tag in tags}


def _registry_factor_row(
    config: QuantForgeConfig, factor: FactorDefinition, tags: dict[str, Any] | None
) -> dict[str, Any]:
    """Project one FactorDefinition into the Registry list/detail row shape.

    Extends the pinned ``read_models.list_factors`` projection (which stays
    unchanged for LLM/CLI surfaces) with ``description``, ``source``, the
    joined research tags, and ``precomputed_values_present``. ``tags`` is
    ``None`` when unobservable (FP-4); precomputed formulas already render
    as ``precomputed:<key>`` — no paths.

    ``precomputed_values_present`` is ``null`` for a non-precomputed formula
    (values are computed from the formula on demand — presence is not a
    meaningful question) and ``null`` on any probe failure (FP-4: unobservable
    is null, never a guess). Otherwise it reports whether the value store —
    probed with the SAME roots the scoring path uses — holds at least one
    stored value file for this factor, so a dangling composite (definition
    saved, values only ever written to a past run's overlay) is marked
    ``false`` here instead of failing deep inside a synthesis run.
    """

    return {
        "factor_id": factor.factor_id,
        "name": factor.name,
        "formula": factor.formula,
        "status": factor.status,
        "horizon_days": factor.horizon_days,
        "universe_filters": list(factor.universe_filters),
        "description": factor.description,
        "source": factor.source,
        "tags": tags,
        "precomputed_values_present": _precomputed_values_present(config, factor),
    }


def _registry_factors_payload(config: QuantForgeConfig) -> dict[str, Any]:
    """Registry list view: the full factor catalog with research tags.

    A catalog read failure degrades to an empty list exactly like
    ``_catalog_factor_ids`` (never a 500); the catalog is small, so there is
    no pagination.
    """

    from quant_forge.apps.web import server as _server

    try:
        factors = FactorCatalog(
            config.paths.factor_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        ).list()
    except Exception:
        factors = []
    tags_by_id = _factor_research_tags_by_id(config) if factors else {}
    rows = [_registry_factor_row(config, factor, tags_by_id.get(factor.factor_id)) for factor in factors]
    payload = {"factors": rows, "count": len(rows)}
    return _server._web_public_json(_redact_web_text(payload))


def _registry_factor_detail_payload(
    config: QuantForgeConfig,
    factor_id: str,
    *,
    limit: Any = None,
    kind: Any = None,
) -> dict[str, Any]:
    """Registry detail view: one factor plus its evidence chain of runs.

    The path-supplied id must match the FactorDefinition id rule before any
    lookup; ids that do not match are treated as unknown (KeyError -> 404).
    ``FileNotFoundError`` from the catalog also maps to KeyError -> 404, while
    an ambiguous precomputed id keeps its ValueError -> 400 (reflected
    per-route like the limit/kind validation). Runs come from the lineage run
    index, newest first, projected through ``_run_record_payload`` so metric
    highlights keep the MetricValue null-not-zero convention (FP-4).
    """

    from quant_forge.apps.web import server as _server

    if not _REGISTRY_FACTOR_ID_RE.fullmatch(factor_id):
        raise KeyError("unknown factor")
    parsed_limit = _run_history_limit(limit)
    parsed_kind = _run_kind_parameter(kind)
    try:
        factor = FactorCatalog(
            config.paths.factor_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        ).get(factor_id)
    except FileNotFoundError:
        raise KeyError(f"unknown factor: {factor_id}") from None
    tags_by_id = _factor_research_tags_by_id(config)
    rows = RunIndex(config.paths.artifact_root).search(factor_id=factor.factor_id, kind=parsed_kind)
    ordered = list(reversed(rows))[:parsed_limit]
    payload = {
        "factor": _registry_factor_row(config, factor, tags_by_id.get(factor.factor_id)),
        "runs": [_run_record_payload(row) for row in ordered],
        "count": len(ordered),
        "limit": parsed_limit,
        "total": len(rows),
    }
    return _server._web_public_json(_redact_web_text(payload))


# ---------------------------------------------------------------------------
# CP6-4 read-only Docs view + Extensions registry payload builders (GET-only).
#
# Same endpoint discipline as CP6-3: builders live here, are re-exported
# through the quant_forge.apps.web.server facade, and routing invokes them as
# _server.<fn> so monkeypatches keep taking effect. DOCS_ROOT/EXTENSIONS_ROOT
# are read through the server namespace at call time for the same reason.
# ---------------------------------------------------------------------------

# Single definition of the doc-name rule: every '/'-separated relpath segment
# must match this pattern (no leading dot; conservative charset). The frontend
# hash router (views/lab.js, views/docs.js #docs-doc-<relpath>) mirrors this
# charset, so any name the server can serve deep-links cleanly. Conservative
# by design: names outside [A-Za-z0-9_.-] (spaces, '=', '&', ...) never enter
# payloads, keeping filename-derived identifiers redaction-neutral.
_DOCS_RELPATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _is_public_doc_relpath(relpath: str) -> bool:
    """True when every segment of ``relpath`` matches the doc-name rule.

    Shared by list (skip non-matching files) and detail (404 non-matching
    requests) so the index and the document endpoint can never disagree.
    """

    return all(
        _DOCS_RELPATH_SEGMENT_RE.fullmatch(segment) for segment in relpath.split("/")
    )


def _docs_relpath_from_path(path: str) -> str:
    # Decode exactly one percent-encoding layer, then validate (static-asset
    # discipline): a double-encoded traversal stays literal and fails below.
    relpath = unquote(path[len("/api/docs/"):])
    if not relpath or "\x00" in relpath or "\\" in relpath:
        raise KeyError(f"unknown doc: {path}")
    if relpath.startswith("/"):
        raise KeyError(f"unknown doc: {path}")
    if not _is_public_doc_relpath(relpath):
        # Blocks "..", ".", ".hidden", "//", trailing "/", and every name
        # outside the conservative charset above.
        raise KeyError(f"unknown doc: {path}")
    if not relpath.endswith(".md"):  # case-sensitive by contract
        raise KeyError(f"unknown doc: {path}")
    return relpath


def _docs_section_label(relpath: str) -> str:
    return relpath.split("/", 1)[0] if "/" in relpath else "root"


def _docs_list_payload(config: QuantForgeConfig) -> dict[str, Any]:
    """Document index over DOCS_ROOT, grouped by first path segment.

    ``config`` is unused; the uniform builder signature keeps the routing
    dispatch and monkeypatch seam identical across GET builders.
    """

    del config  # uniform builder signature
    from quant_forge.apps.web import server as _server

    root = Path(_server.DOCS_ROOT)
    if not root.is_dir():
        payload: dict[str, Any] = {"available": False, "count": 0, "sections": []}
        return _server._web_public_json(_redact_web_text(payload))
    resolved_root = root.resolve()
    by_section: dict[str, list[dict[str, str]]] = {}
    count = 0
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        relpath = relative.as_posix()
        # Same doc-name rule as the detail endpoint (dot segments, spaces,
        # '=', '&', ... are skipped) so list and detail can never disagree.
        if not _is_public_doc_relpath(relpath):
            continue
        if not path.resolve(strict=False).is_relative_to(resolved_root):
            continue
        section = _docs_section_label(relpath)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            title = path.stem
        else:
            title = extract_markdown_title(text) or path.stem
        by_section.setdefault(section, []).append(
            {"relpath": relpath, "section": section, "title": title}
        )
        count += 1
    ordered_labels = sorted(
        (label for label in by_section if label != "root"),
        key=lambda label: (label.casefold(), label),
    )
    if "root" in by_section:
        ordered_labels.insert(0, "root")
    sections = [
        {
            "section": label,
            "docs": sorted(
                by_section[label],
                key=lambda doc: (doc["relpath"].casefold(), doc["relpath"]),
            ),
        }
        for label in ordered_labels
    ]
    payload = {"available": True, "count": count, "sections": sections}
    return _server._web_public_json(_redact_web_text(payload))


def _docs_document_payload(config: QuantForgeConfig, relpath: str) -> dict[str, Any]:
    """One repo document rendered to whitelisted HTML (contained read)."""

    del config  # uniform builder signature
    from quant_forge.apps.web import server as _server

    root = Path(_server.DOCS_ROOT).resolve()
    candidate = (root / relpath).resolve(strict=False)
    if (
        candidate == root
        or not candidate.is_relative_to(root)
        or candidate.suffix != ".md"
        or not candidate.is_file()
    ):
        raise KeyError(f"unknown doc: {relpath}")
    # Authorize-then-open guard (same pattern as _read_bench_artifact):
    # O_NOFOLLOW fails the open if the final component was swapped for a
    # symlink after the containment check above; any OSError maps to 404.
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise KeyError(f"unknown doc: {relpath}") from None
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError:
        raise KeyError(f"unknown doc: {relpath}") from None
    # Redaction ordering (binding): redact the RAW source BEFORE rendering,
    # then render, then wrap in _web_public_json only -- never pass the
    # finished payload through _redact_web_text. The "html" field is
    # post-escape; a second redaction pass could rewrite an escaped fragment
    # into text containing raw "<"/">" (e.g. "KEY=&lt;redacted&gt;"
    # re-matches the env-secret rule and would inject literal <redacted>
    # markup). Redacting the source keeps one definition of the quantity
    # (lineage.store.redact_free_text) and the escape step neutralizes the
    # redaction tokens.
    redacted = redact_free_text(source)
    title = extract_markdown_title(redacted) or PurePosixPath(relpath).stem
    payload = {
        "relpath": relpath,
        "section": _docs_section_label(relpath),
        "title": title,
        "html": render_markdown_html(redacted, current_relpath=relpath),
    }
    return _server._web_public_json(payload)


def _extensions_payload(config: QuantForgeConfig) -> dict[str, Any]:
    """Declarative extensions registry listing (D7/D7a; read-only)."""

    del config  # uniform builder signature
    from quant_forge.apps.web import server as _server

    points = contribution_points_payload()
    root = Path(_server.EXTENSIONS_ROOT)
    if not root.is_dir():
        payload: dict[str, Any] = {
            "available": False,
            "points": points,
            "extensions": [],
            "count": 0,
            "valid_count": 0,
            "rejected_count": 0,
        }
        return _server._web_public_json(_redact_web_text(payload))
    rows = scan_extensions(root)
    valid_count = sum(1 for row in rows if row["status"] == "valid")
    payload = {
        "available": True,
        "points": points,
        "extensions": rows,
        "count": len(rows),
        "valid_count": valid_count,
        "rejected_count": len(rows) - valid_count,
    }
    return _server._web_public_json(_redact_web_text(payload))
