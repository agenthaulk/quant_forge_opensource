"""Workflow entrypoints, payload builders, and validators for the local web adapter.

Monkeypatch seams (``evaluate_factor``, ``run_factor_backtest``,
``parse_factor_idea``, ``_run_research_once``, ``DEFAULT_RD_CONFIG_PATH``) are
resolved through :mod:`quant_forge.apps.web.server` at call time so patches on
the server module namespace keep taking effect.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
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
from quant_forge.config import QuantForgeConfig, simulation_profile_from_mapping, validate_llm_runtime
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.data.local import catalog_field_availability, data_field_catalog, validate_data_root
from quant_forge.extensions.registry import contribution_points_payload, scan_extensions
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.lineage.store import RUN_KINDS, RunIndex, redact_free_text
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
    return _validation_payload(
        factor,
        parser=parser,
        evaluation=evaluation,
        in_sample_backtest=in_sample_backtest,
        backtest=backtest,
        parameters=settings.parameters,
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
    return result


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


def _registry_factor_row(factor: FactorDefinition, tags: dict[str, Any] | None) -> dict[str, Any]:
    """Project one FactorDefinition into the Registry list/detail row shape.

    Extends the pinned ``read_models.list_factors`` projection (which stays
    unchanged for LLM/CLI surfaces) with ``description``, ``source``, and the
    joined research tags. ``tags`` is ``None`` when unobservable (FP-4);
    precomputed formulas already render as ``precomputed:<key>`` — no paths.
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
    rows = [_registry_factor_row(factor, tags_by_id.get(factor.factor_id)) for factor in factors]
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
        "factor": _registry_factor_row(factor, tags_by_id.get(factor.factor_id)),
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
