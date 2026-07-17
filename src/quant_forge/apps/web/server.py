"""Minimal local-only web/API adapter.

Composition root for the web adapter. The implementation lives in the
sibling modules :mod:`quant_forge.apps.web.jobs` (background job manager),
:mod:`quant_forge.apps.web.api` (workflow entrypoints, payload builders,
validators), :mod:`quant_forge.apps.web.html` (index page template), and
:mod:`quant_forge.apps.web.routing` (HTTP server and dispatch).

This module re-exports every name that was module-level before the split --
public and underscore-prefixed alike -- so existing imports keep working, and
the sibling modules look monkeypatch seams (workflow callables,
``evaluate_factor``, ``run_factor_backtest``, ``parse_factor_idea``,
``_run_research_once``, ``_web_public_json``, ``DEFAULT_RD_CONFIG_PATH``) up
through this namespace at call time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime
import gc
import hmac
from html import escape
import json
import logging
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from quant_forge.backtesting.service import run_factor_backtest, run_staggered_entry_backtest
from quant_forge.config import QuantForgeConfig, simulation_profile_from_mapping, validate_llm_runtime
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.llm_factor_parser import ParsedFactor, parse_factor_idea
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.scheduler import (
    ResearchLoopScheduler,
    ResearchScheduleRequest,
)
from quant_forge.research_loop.config import (
    DEFAULT_RD_CONFIG_PATH,
    ResearchLoopConfig,
    load_research_loop_config,
    weights_for_objective,
)
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.service import ResearchLoopResult, ResearchLoopService

from quant_forge.apps.web.jobs import (
    LOGGER as LOGGER,
    LONG_RUNNING_JOB_SECONDS as LONG_RUNNING_JOB_SECONDS,
    RequestBodyTooLarge as RequestBodyTooLarge,
    _IdeaValidationSettings as _IdeaValidationSettings,
    _TERMINAL_JOB_STATUSES as _TERMINAL_JOB_STATUSES,
    _WebJob as _WebJob,
    _WebJobCancelled as _WebJobCancelled,
    _WebJobManager as _WebJobManager,
    _client_error_message as _client_error_message,
    _utc_now as _utc_now,
)

from quant_forge.apps.web.api import (
    DOCS_ROOT as DOCS_ROOT,
    EXTENSIONS_ROOT as EXTENSIONS_ROOT,
    MAX_RD_ITERATIONS as MAX_RD_ITERATIONS,
    RESEARCH_HISTORY_DEFAULT_LIMIT as RESEARCH_HISTORY_DEFAULT_LIMIT,
    RESEARCH_HISTORY_MAX_LIMIT as RESEARCH_HISTORY_MAX_LIMIT,
    WebControlTokenError as WebControlTokenError,
    apply_llm_settings_update as apply_llm_settings_update,
    _EVALUATION_DISPLAY_METRIC_KEYS as _EVALUATION_DISPLAY_METRIC_KEYS,
    _WEB_PATH_KEYS as _WEB_PATH_KEYS,
    _active_llm as _active_llm,
    _aggregate_research_accepted_ids as _aggregate_research_accepted_ids,
    _aggregate_research_comparison_rows as _aggregate_research_comparison_rows,
    _apply_metric_display as _apply_metric_display,
    _backtest_payload as _backtest_payload,
    _bench_report_payload as _bench_report_payload,
    _bench_runs_payload as _bench_runs_payload,
    _bool_parameter as _bool_parameter,
    _catalog_factor_ids as _catalog_factor_ids,
    _control_token_for_bind as _control_token_for_bind,
    _cost_parameters as _cost_parameters,
    _data_catalog_payload as _data_catalog_payload,
    _data_status_payload as _data_status_payload,
    _default_seed_factor_id as _default_seed_factor_id,
    _default_validation_parameters as _default_validation_parameters,
    _docs_document_payload as _docs_document_payload,
    _docs_list_payload as _docs_list_payload,
    _docs_relpath_from_path as _docs_relpath_from_path,
    _evaluation_payload as _evaluation_payload,
    _existing_factor as _existing_factor,
    _extensions_payload as _extensions_payload,
    _factor_from_request as _factor_from_request,
    _factor_from_validation_payload as _factor_from_validation_payload,
    _factor_research_tags_by_id as _factor_research_tags_by_id,
    _flat_backtest_profile_overrides as _flat_backtest_profile_overrides,
    _float_parameter as _float_parameter,
    _horizon_metric_payload as _horizon_metric_payload,
    _idea_validation_settings as _idea_validation_settings,
    _int_parameter as _int_parameter,
    _job_id_from_cancel_path as _job_id_from_cancel_path,
    _job_id_from_path as _job_id_from_path,
    _json_safe as _json_safe,
    _last_accepted_research_factor_id as _last_accepted_research_factor_id,
    _last_explored_research_factor_id as _last_explored_research_factor_id,
    _llm_provider_options as _llm_provider_options,
    _llm_runtime_status as _llm_runtime_status,
    _next_research_seed as _next_research_seed,
    _nonnegative_float_parameter as _nonnegative_float_parameter,
    _nonnegative_int_parameter as _nonnegative_int_parameter,
    _optional_date_parameter as _optional_date_parameter,
    _optional_int as _optional_int,
    _optional_parameters_payload as _optional_parameters_payload,
    _optional_parser_payload as _optional_parser_payload,
    _optional_standardization as _optional_standardization,
    _optional_str as _optional_str,
    _parse_idea as _parse_idea,
    _parse_payload as _parse_payload,
    _parser_payload as _parser_payload,
    _parser_payload_from_request as _parser_payload_from_request,
    _path_label as _path_label,
    _paths_payload as _paths_payload,
    _positive_int_parameter as _positive_int_parameter,
    _query_parameter as _query_parameter,
    _raise_if_cancelled as _raise_if_cancelled,
    _rd_generation_mode as _rd_generation_mode,
    _rd_iterations_parameter as _rd_iterations_parameter,
    _rd_llm_settings as _rd_llm_settings,
    _rd_optimizer_label as _rd_optimizer_label,
    _rd_status_payload as _rd_status_payload,
    _read_bench_artifact as _read_bench_artifact,
    _redact_web_text as _redact_web_text,
    _registry_factor_detail_payload as _registry_factor_detail_payload,
    _registry_factor_id_from_path as _registry_factor_id_from_path,
    _registry_factor_row as _registry_factor_row,
    _registry_factors_payload as _registry_factors_payload,
    _research_candidate_payload as _research_candidate_payload,
    _research_exploration_seed_summary as _research_exploration_seed_summary,
    _research_history_payload as _research_history_payload,
    _research_iteration_summary as _research_iteration_summary,
    _research_optimization_summary as _research_optimization_summary,
    _research_result_payload as _research_result_payload,
    _restore_factor_after_failed_validation as _restore_factor_after_failed_validation,
    _role_profile_overrides as _role_profile_overrides,
    _role_test_period_override as _role_test_period_override,
    _run_history_limit as _run_history_limit,
    _run_kind_parameter as _run_kind_parameter,
    _run_record_payload as _run_record_payload,
    _run_research_iterations as _run_research_iterations,
    _run_research_once as _run_research_once,
    _simulation_parameter_overrides as _simulation_parameter_overrides,
    _simulation_profile_payload as _simulation_profile_payload,
    _simulation_profile_period_text as _simulation_profile_period_text,
    _synthesis_block as _synthesis_block,
    _synthesis_factor_refs as _synthesis_factor_refs,
    _synthesis_methods_payload as _synthesis_methods_payload,
    _test_period_override as _test_period_override,
    _transaction_costs_payload as _transaction_costs_payload,
    _validate_factor_workflow as _validate_factor_workflow,
    _validation_payload as _validation_payload,
    _web_public_json as _web_public_json,
    _web_public_path_value as _web_public_path_value,
    preflight_multi_factor_backtest as preflight_multi_factor_backtest,
    run_idea_parse_workflow as run_idea_parse_workflow,
    run_idea_validation_workflow as run_idea_validation_workflow,
    run_idea_workflow as run_idea_workflow,
    run_multi_factor_backtest_workflow as run_multi_factor_backtest_workflow,
    run_research_once_workflow as run_research_once_workflow,
    run_staggered_entry_workflow as run_staggered_entry_workflow,
)

from quant_forge.apps.web.markdown import (
    extract_markdown_title as extract_markdown_title,
    render_markdown_html as render_markdown_html,
)

from quant_forge.apps.web.html import (
    _index_html as _index_html,
    _page_config_json as _page_config_json,
    _provider_options_script_payload as _provider_options_script_payload,
    _provider_readiness_label as _provider_readiness_label,
    _script_json as _script_json,
    _selected_attr as _selected_attr,
)

from quant_forge.apps.web.routing import (
    MAX_REQUEST_BODY_BYTES as MAX_REQUEST_BODY_BYTES,
    STATIC_CONTENT_TYPES as STATIC_CONTENT_TYPES,
    STATIC_ROOT as STATIC_ROOT,
    STATIC_URL_PREFIX as STATIC_URL_PREFIX,
    _static_asset as _static_asset,
    create_local_web_server as create_local_web_server,
    run_local_web as run_local_web,
)
