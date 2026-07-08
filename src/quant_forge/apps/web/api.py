"""Workflow entrypoints, payload builders, and validators for the local web adapter.

Monkeypatch seams (``evaluate_factor``, ``run_factor_backtest``,
``parse_factor_idea``, ``_run_research_once``, ``DEFAULT_RD_CONFIG_PATH``) are
resolved through :mod:`quant_forge.apps.web.server` at call time so patches on
the server module namespace keep taking effect.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import math
import os
from pathlib import Path
import re
import threading
from typing import Any

from quant_forge.apps.web.jobs import _IdeaValidationSettings, _WebJobCancelled, _client_error_message
from quant_forge.backtesting.service import run_staggered_entry_backtest
from quant_forge.config import QuantForgeConfig, simulation_profile_from_mapping, validate_llm_runtime
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.llm_factor_parser import ParsedFactor
from quant_forge.research_loop.config import (
    ResearchLoopConfig,
    load_research_loop_config,
    weights_for_objective,
)
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.service import ResearchLoopResult, ResearchLoopService


MAX_RD_ITERATIONS = 5


_WEB_PATH_KEYS = {
    "artifact_path",
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
    result = run_staggered_entry_backtest(
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
    return FactorDefinition(
        factor_id=str(raw["factor_id"]),
        name=str(raw.get("name", raw["factor_id"])),
        formula=str(raw["formula"]),
        status=str(raw.get("status", "draft")),  # type: ignore[arg-type]
        description=str(raw.get("description", "")),
        horizon_days=_positive_int_parameter(raw.get("horizon_days", 5), "factor.horizon_days"),
        universe_filters=tuple(str(item) for item in filters_raw),
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


def _control_token_for_bind(host: str, config: QuantForgeConfig) -> str:
    if host != "0.0.0.0":
        return ""
    token_env = config.web.control_token_env.strip()
    if not token_env:
        raise ValueError("web.control_token_env is required when binding the web adapter to 0.0.0.0")
    control_value = os.environ.get(token_env, "")
    if not control_value:
        raise ValueError(f"web control token environment variable is not set: {token_env}")
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
