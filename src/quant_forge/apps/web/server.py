"""Minimal local-only web/API adapter."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from html import escape
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from typing import Any

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.config import QuantForgeConfig, validate_llm_runtime
from quant_forge.core.contracts import BacktestResult, EvaluationResult
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
from quant_forge.research_loop.campaign import (
    DEFAULT_CAMPAIGN_ROUNDS,
    MAX_CAMPAIGN_SEEDS,
    ResearchCampaignResult,
    ResearchCampaignService,
)
from quant_forge.research_loop.service import ResearchLoopResult, ResearchLoopService

DEFAULT_CAMPAIGN_SEED_AUDIT_DIR = "ralph_2025_factor_audit_20260605T035747Z"
DEFAULT_CAMPAIGN_SEED_AUDIT_FILE = "top20_stable_decorrelated.json"


class _CampaignSeedSelection(tuple):
    __slots__ = ()

    @property
    def seed_factor_ids(self) -> tuple[str, ...]:
        return self[0]

    @property
    def source_path(self) -> str | None:
        return self[1]

    @property
    def source_label(self) -> str:
        return self[2]


def run_idea_workflow(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str = "llm",
    llm_provider: str | None = None,
    rd_config: ResearchLoopConfig | None = None,
) -> dict[str, Any]:
    """Parse an idea, persist the draft, evaluate it, and run a backtest."""

    if not text.strip():
        raise ValueError("idea text is required")
    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    llm_settings = config.llm.select_provider(llm_provider) if parser_mode == "llm" else config.llm
    if parser_mode == "llm":
        validate_llm_runtime(llm_settings)
    parsed = parse_factor_idea(text, llm_settings, mode=parser_mode)
    FactorRepository(config.paths.factor_root).save(parsed.factor)
    evaluation = evaluate_factor(
        parsed.factor.factor_id,
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        horizon_days=parsed.factor.horizon_days,
        horizon_days_matrix=research_config.horizon_days_matrix,
        sample_splits=research_config.sample_splits,
        simulation_profile=research_config.simulation_profile,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
    )
    backtest = run_factor_backtest(
        parsed.factor.factor_id,
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        simulation_profile=research_config.simulation_profile,
        holding_days=parsed.factor.horizon_days,
        transaction_costs=research_config.transaction_costs,
        sample_splits=research_config.sample_splits,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
    )
    return _workflow_payload(parsed, evaluation, backtest)


def run_research_once_workflow(
    config: QuantForgeConfig,
    seed_factor_id: str,
    *,
    objective: str | None = None,
    max_candidates: int | None = None,
    rd_config: ResearchLoopConfig | None = None,
) -> dict[str, Any]:
    """Run one local research-development iteration and return JSON-safe data."""

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    result = _run_research_once(
        config,
        research_config,
        seed_factor_id,
        objective=objective or research_config.objective,
        max_candidates=max_candidates if max_candidates is not None else research_config.default_max_candidates,
    )
    return _json_safe(result)


def run_research_campaign_workflow(
    config: QuantForgeConfig,
    seed_factor_ids: list[str] | tuple[str, ...],
    *,
    seed_source_path: str | None = None,
    objective: str | None = None,
    rounds: int = DEFAULT_CAMPAIGN_ROUNDS,
    rd_config: ResearchLoopConfig | None = None,
) -> dict[str, Any]:
    """Run a bounded multi-round RD campaign and return JSON-safe data."""

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    seed_selection = _campaign_seed_selection(
        config,
        seed_factor_ids=tuple(seed_factor_ids),
        seed_source_path=seed_source_path,
    )
    result = _run_research_campaign(
        config,
        research_config,
        seed_selection.seed_factor_ids,
        objective=objective or research_config.objective,
        rounds=rounds,
    )
    payload = _json_safe(result)
    payload["seed_source_path"] = seed_selection.source_path
    payload["seed_source_label"] = seed_selection.source_label
    payload["simulation_profile"] = _json_safe(research_config.simulation_profile)
    payload["status"] = _campaign_status(result)
    payload["status_label"] = _campaign_status_label(result)
    return payload


def create_local_web_server(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("OpenSource web adapter is local-only; use 127.0.0.1 or localhost")

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    scheduler = ResearchLoopScheduler(
        lambda seed_factor_id, objective, max_candidates: _run_research_once(
            config,
            research_config,
            seed_factor_id,
            objective=objective,
            max_candidates=max_candidates,
        ),
        allowed_interval_days=research_config.allowed_interval_days,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._json({"ok": True})
            elif self.path == "/catalog":
                self._json({"fields": list_available_fields(), "operators": list_available_operators()})
            elif self.path == "/api/status":
                active_llm = _active_llm(config)
                self._json(
                    {
                        "name": "Quant Forge",
                        "paths": _paths_payload(config),
                        "llm": {
                            "provider": active_llm.provider,
                            "model": active_llm.model,
                            "api_key_env": active_llm.api_key_env,
                            "providers": _llm_provider_options(config),
                        },
                    }
                )
            elif self.path == "/api/research/status":
                self._json(_json_safe(scheduler.status()))
            else:
                self._html(_index_html(config, research_config))

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
                if self.path == "/api/run-idea":
                    result = run_idea_workflow(
                        config,
                        str(payload.get("text", "")),
                        parser_mode=str(payload.get("parser_mode", "llm")),
                        llm_provider=_optional_str(payload.get("llm_provider")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if self.path == "/api/research/run-once":
                    result = run_research_once_workflow(
                        config,
                        str(payload.get("seed_factor_id", "")),
                        objective=str(payload.get("objective", research_config.objective)),
                        max_candidates=_optional_int(payload.get("max_candidates")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if self.path == "/api/research/campaign":
                    result = run_research_campaign_workflow(
                        config,
                        _seed_factor_ids(payload.get("seed_factor_ids")),
                        seed_source_path=_optional_str(payload.get("seed_source_path")),
                        objective=str(payload.get("objective", research_config.objective)),
                        rounds=int(payload.get("rounds", DEFAULT_CAMPAIGN_ROUNDS)),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if self.path == "/api/research/schedule":
                    action = str(payload.get("action", "")).strip().lower()
                    if action == "start":
                        request = ResearchScheduleRequest(
                            seed_factor_id=str(payload.get("seed_factor_id", "")),
                            objective=str(payload.get("objective", research_config.objective)),
                            interval_days=int(payload.get("interval_days", research_config.default_interval_days)),
                            max_candidates=int(payload.get("max_candidates", research_config.default_max_candidates)),
                        )
                        self._json(_json_safe(scheduler.start(request)))
                        return
                    if action == "stop":
                        self._json(_json_safe(scheduler.stop()))
                        return
                    self._json({"error": "action must be start or stop"}, status=400)
                    return
                self._json({"error": f"unknown endpoint: {self.path}"}, status=404)
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), Handler)


def run_local_web(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> None:
    server = create_local_web_server(host=host, port=port, config=config, rd_config=rd_config)
    actual_host, actual_port = server.server_address
    print(f"Quant Forge local web listening on http://{actual_host}:{actual_port}")
    server.serve_forever()


def _workflow_payload(parsed: ParsedFactor, evaluation: EvaluationResult, backtest: BacktestResult) -> dict[str, Any]:
    return {
        "parser": {
            "source": parsed.source,
            "provider": parsed.provider,
            "model": parsed.model,
        },
        "factor": _json_safe(parsed.factor),
        "evaluation": _json_safe(evaluation),
        "backtest": _json_safe(backtest),
    }


def _run_research_once(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    seed_factor_id: str,
    *,
    objective: str,
    max_candidates: int,
) -> ResearchLoopResult:
    if not seed_factor_id.strip():
        raise ValueError("seed_factor_id is required")
    service = ResearchLoopService(
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        simulation_profile=rd_config.simulation_profile,
        simulation_profiles=rd_config.simulation_profiles,
        parameter_search_enabled=rd_config.parameter_search.enabled,
        parameter_search_method=rd_config.parameter_search.method,
        parameter_search_keep_ratio=rd_config.parameter_search.keep_ratio,
        parameter_search_min_survivors=rd_config.parameter_search.min_survivors,
        quick_horizon_days_matrix=rd_config.parameter_search.quick_horizon_days_matrix,
        quick_sample_splits=rd_config.parameter_search.quick_sample_splits,
        horizon_days_matrix=rd_config.horizon_days_matrix,
        sample_splits=rd_config.sample_splits,
        transaction_costs=rd_config.transaction_costs,
    )
    weights = weights_for_objective(rd_config, objective)
    return service.run_once(
        seed_factor_id,
        objective=objective,
        max_candidates=max_candidates,
        weights=weights,
        gate=rd_config.gate,
    )


def _run_research_campaign(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    seed_factor_ids: list[str] | tuple[str, ...],
    *,
    objective: str,
    rounds: int,
) -> ResearchCampaignResult:
    service = ResearchCampaignService(
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        simulation_profile=rd_config.simulation_profile,
        horizon_days_matrix=rd_config.horizon_days_matrix,
        sample_splits=rd_config.sample_splits,
        transaction_costs=rd_config.transaction_costs,
    )
    weights = weights_for_objective(rd_config, objective)
    return service.run(
        tuple(seed_factor_ids),
        objective=objective,
        rounds=rounds,
        weights=weights,
        gate=rd_config.gate,
    )


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _seed_factor_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = re.split(r"[\s,]+", value)
    elif isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raise ValueError("seed_factor_ids must be a list or string")
    normalized: list[str] = []
    for item in raw_items:
        factor_id = str(item).strip()
        if factor_id and factor_id not in normalized:
            normalized.append(factor_id)
    return tuple(normalized[:MAX_CAMPAIGN_SEEDS])


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


def _default_seed_factor_id(config: QuantForgeConfig) -> str:
    factor_ids = _catalog_factor_ids(config)
    if "FTR_DEMO_SMALL_CAP" in factor_ids:
        return "FTR_DEMO_SMALL_CAP"
    return factor_ids[0] if factor_ids else ""


def _default_campaign_seed_factor_ids(config: QuantForgeConfig) -> tuple[str, ...]:
    return _default_campaign_seed_selection(config).seed_factor_ids


def _default_campaign_seed_selection(config: QuantForgeConfig) -> _CampaignSeedSelection:
    default_audit_path = _default_campaign_seed_source_path(config)
    if default_audit_path.exists():
        try:
            seed_factor_ids = _load_campaign_seed_factor_ids(default_audit_path)
        except Exception:
            pass
        else:
            return _CampaignSeedSelection(
                (
                    seed_factor_ids,
                    str(default_audit_path),
                    "Top20 audit JSON (rank order)",
                )
            )
    factor_ids = _catalog_factor_ids(config)
    prioritized = [factor_id for factor_id in factor_ids if factor_id != "FTR_DEMO_SMALL_CAP"]
    if "FTR_DEMO_SMALL_CAP" in factor_ids:
        prioritized.insert(0, "FTR_DEMO_SMALL_CAP")
    return _CampaignSeedSelection((tuple(prioritized[:MAX_CAMPAIGN_SEEDS]), None, "Catalog fallback"))


def _campaign_seed_selection(
    config: QuantForgeConfig,
    *,
    seed_factor_ids: tuple[str, ...],
    seed_source_path: str | None,
) -> _CampaignSeedSelection:
    if seed_source_path:
        path = _normalize_allowed_campaign_seed_source_path(config, seed_source_path)
        return _CampaignSeedSelection(
            (
                _load_campaign_seed_factor_ids(path),
                str(path),
                "Audit JSON seed source",
            )
        )
    return _CampaignSeedSelection((tuple(seed_factor_ids), None, "Manual seed list"))


def _default_campaign_seed_source_path(config: QuantForgeConfig) -> Path:
    repo_level_path, *fallback_paths = _allowed_campaign_seed_source_paths(config)
    if repo_level_path.exists():
        return repo_level_path
    return fallback_paths[0] if fallback_paths else repo_level_path


def _normalize_allowed_campaign_seed_source_path(config: QuantForgeConfig, path: str | Path) -> Path:
    normalized_path = _normalize_campaign_seed_source_path(path)
    if normalized_path not in _allowed_campaign_seed_source_paths(config):
        raise ValueError(f"campaign seed source path is not allowed: {normalized_path}")
    return normalized_path


def _allowed_campaign_seed_source_paths(config: QuantForgeConfig) -> tuple[Path, ...]:
    repo_level_path = _normalize_campaign_seed_source_path(
        Path.cwd() / "artifacts" / DEFAULT_CAMPAIGN_SEED_AUDIT_DIR / DEFAULT_CAMPAIGN_SEED_AUDIT_FILE
    )
    artifact_root_path = _normalize_campaign_seed_source_path(
        config.paths.artifact_root / DEFAULT_CAMPAIGN_SEED_AUDIT_DIR / DEFAULT_CAMPAIGN_SEED_AUDIT_FILE
    )
    if artifact_root_path == repo_level_path:
        return (repo_level_path,)
    return (repo_level_path, artifact_root_path)


def _normalize_campaign_seed_source_path(path: str | Path) -> Path:
    normalized_path = Path(path).expanduser()
    if not normalized_path.is_absolute():
        normalized_path = Path.cwd() / normalized_path
    return normalized_path.resolve(strict=False)


def _load_campaign_seed_factor_ids(path: Path) -> tuple[str, ...]:
    if not path.exists():
        raise FileNotFoundError(f"campaign seed source does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("campaign seed source must be a JSON array")

    ranked_seed_ids: list[tuple[int, int, str]] = []
    for index, item in enumerate(payload):
        factor_id = ""
        rank = index + 1
        if isinstance(item, str):
            factor_id = item.strip()
        elif isinstance(item, dict):
            factor_id = str(item.get("factor_id", "")).strip()
            raw_rank = item.get("rank", rank)
            try:
                rank = int(raw_rank)
            except (TypeError, ValueError) as exc:
                raise ValueError("campaign seed source rank must be an integer") from exc
        else:
            raise ValueError("campaign seed source items must be strings or objects")
        if not factor_id:
            raise ValueError("campaign seed source items must include factor_id")
        ranked_seed_ids.append((rank, index, factor_id))

    ranked_seed_ids.sort(key=lambda item: (item[0], item[1]))
    normalized: list[str] = []
    for _, _, factor_id in ranked_seed_ids:
        if factor_id not in normalized:
            normalized.append(factor_id)
    return tuple(normalized[:MAX_CAMPAIGN_SEEDS])


def _campaign_status(result: ResearchCampaignResult) -> str:
    has_errors = bool(result.errors or any(round_result.errors for round_result in result.round_results))
    if result.final_factor_id is None:
        return "fail"
    if has_errors:
        return "warning"
    return "ok"


def _campaign_status_label(result: ResearchCampaignResult) -> str:
    status = _campaign_status(result)
    if status == "warning":
        return "Campaign 完成，但存在 partial errors"
    if status == "fail":
        return "Campaign 失败"
    return "Campaign 完成"


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


def _selected_attr(selected: bool) -> str:
    return " selected" if selected else ""


def _provider_readiness_label(option: dict[str, str]) -> str:
    if option.get("runtime_ready") == "true":
        return " · env " + option["api_key_env"] if option["api_key_env"] else " · no auth"
    api_key_env = option.get("api_key_env", "")
    if api_key_env:
        return " · missing env " + api_key_env
    return " · not ready"


def _index_html(config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None) -> str:
    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    paths = _paths_payload(config)
    provider_options = _llm_provider_options(config)
    active_llm = _active_llm(config)
    active_provider = active_llm.provider if active_llm.provider not in {"rule", "deterministic"} else ""
    provider = escape(active_llm.provider)
    model = escape(active_llm.model)
    parser_label = escape(active_provider or "未配置 LLM provider")
    seed_factor_id = escape(_default_seed_factor_id(config))
    campaign_seed_selection = _default_campaign_seed_selection(config)
    campaign_seed_ids = campaign_seed_selection.seed_factor_ids
    campaign_seed_text = escape("\n".join(campaign_seed_ids))
    campaign_seed_source_path = escape(campaign_seed_selection.source_path or "")
    campaign_seed_source_label = escape(campaign_seed_selection.source_label)
    campaign_test_period = escape(_simulation_profile_period_text(research_config.simulation_profile))
    data_root = escape(paths["data_root"])
    factor_root = escape(paths["factor_root"])
    factor_values_root = escape(paths["factor_values_root"])
    factor_values_overlay_root = escape(paths["factor_values_overlay_root"])
    artifact_root = escape(paths["artifact_root"])
    interval_options = "\n".join(
        f'      <option value="{day}"{_selected_attr(day == research_config.default_interval_days)}>{day}天</option>'
        for day in research_config.allowed_interval_days
    )
    objective_options = "\n".join(
        f'      <option value="{value}"{_selected_attr(value == research_config.objective)}>{label}</option>'
        for value, label in (
            ("balanced", "IC / ICIR 优先"),
            ("rank_ic", "Rank IC"),
            ("rank_icir", "ICIR"),
            ("annualized_return", "回测收益"),
        )
    )
    llm_provider_options = "\n".join(
        (
            f'      <option value="{escape(option["provider"])}"'
            f'{_selected_attr(option["provider"] == active_provider)}>'
            f'{escape(option["provider"])} / {escape(option["model"])}'
            f'{escape(_provider_readiness_label(option))}</option>'
        )
        for option in provider_options
    )
    if not llm_provider_options:
        llm_provider_options = '      <option value="">未配置 LLM provider</option>'
    rd_seed_html = (
        f'<input id="rd-seed" value="{seed_factor_id}">'
        if seed_factor_id
        else '<input id="rd-seed" value="" placeholder="先创建或配置一个因子">'
    )
    rd_campaign_seed_html = (
        f'<textarea id="rd-campaign-seeds">{campaign_seed_text}</textarea>'
        if campaign_seed_ids
        else '<textarea id="rd-campaign-seeds" placeholder="每行或逗号分隔一个因子 ID"></textarea>'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Forge</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #15201a;
      --muted: #64736a;
      --line: #d6e1d8;
      --soft: #f5f8f4;
      --accent: #27633b;
      --accent-2: #205b83;
      --bad: #9c2f2f;
      --panel: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: linear-gradient(180deg, #f8fbf7 0%, #eef5ef 100%);
    }}
    main {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      gap: 0;
    }}
    aside {{
      padding: 28px;
      border-right: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.82);
    }}
    section {{
      padding: 28px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ font-size: 26px; margin-bottom: 8px; }}
    h2 {{ font-size: 18px; color: var(--accent); letter-spacing: 0; }}
    h3 {{ font-size: 15px; margin-bottom: 8px; color: var(--muted); }}
    label {{ display: block; margin: 20px 0 8px; font-weight: 700; }}
    textarea, select, input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    textarea {{
      min-height: 150px;
      resize: vertical;
      padding: 14px;
    }}
    select {{ padding: 10px 12px; }}
    input {{ padding: 10px 12px; }}
    button {{
      width: 100%;
      margin-top: 16px;
      border: 0;
      border-radius: 8px;
      padding: 13px 16px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    code {{
      background: #eef5ef;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 6px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      word-break: break-word;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 14px;
      margin: 18px 0 26px;
    }}
    .tile, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .tile b {{
      display: block;
      margin-top: 8px;
      font-size: 27px;
    }}
    .panel {{ margin-bottom: 16px; }}
    hr {{
      margin: 26px 0;
      border: 0;
      border-top: 1px solid var(--line);
    }}
    .button-row {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .button-row button {{
      padding: 11px 10px;
      font-size: 13px;
    }}
    .pill {{
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 9px;
      margin: 2px 4px 2px 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .ok {{ color: var(--accent-2); font-weight: 800; }}
    .warn {{ color: #9a5a12; font-weight: 800; }}
    .err {{ color: var(--bad); font-weight: 800; white-space: pre-wrap; }}
    .formula {{
      font-size: 22px;
      font-weight: 800;
      margin: 10px 0;
    }}
    @media (max-width: 900px) {{
      main {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
    }}
  </style>
</head>
<body>
<main>
  <aside>
    <h1>Quant Forge</h1>
    <p class="meta">LLM: {provider} / {model}</p>
    <p class="meta">data_root: {data_root}</p>
    <p class="meta">factor_root: {factor_root}</p>
    <p class="meta">factor_values_root: {factor_values_root or '未配置'}</p>
    <p class="meta">factor_values_overlay_root: {factor_values_overlay_root or '未配置'}</p>
    <p class="meta">artifact_root: {artifact_root}</p>
    <label for="idea">因子观点</label>
    <textarea id="idea">非ST的小市值股票未来表现更好</textarea>
    <label for="parser">解析方式</label>
    <select id="parser">
      <option value="llm">LLM 语义解析: {parser_label}</option>
      <option value="rule">本地规则解析</option>
    </select>
    <label for="llm-provider">LLM Provider</label>
    <select id="llm-provider">
{llm_provider_options}
    </select>
    <button id="run">解析并验证</button>
    <p id="status" class="meta"></p>
    <hr>
    <h2>RD</h2>
    <label for="rd-seed">Seed Factor</label>
    {rd_seed_html}
    <label for="rd-objective">目标优先级</label>
    <select id="rd-objective">
{objective_options}
    </select>
    <label for="rd-max">候选数量</label>
    <input id="rd-max" type="number" min="1" max="10" value="{research_config.default_max_candidates}">
    <label for="rd-interval">自动周期</label>
    <select id="rd-interval">
{interval_options}
    </select>
    <div class="button-row">
      <button id="rd-run">运行一次</button>
      <button id="rd-start">开启</button>
      <button id="rd-stop">停止</button>
    </div>
    <p id="rd-status" class="meta"></p>
    <label for="rd-campaign-seeds">Campaign Seeds</label>
    {rd_campaign_seed_html}
    <input id="rd-campaign-seed-source-path" type="hidden" value="{campaign_seed_source_path}">
    <p id="rd-campaign-seed-source-label" class="meta">seed source: {campaign_seed_source_label}</p>
    <p class="meta">test period: {campaign_test_period}</p>
    <label for="rd-rounds">Campaign 轮数</label>
    <input id="rd-rounds" type="number" min="1" max="10" value="{DEFAULT_CAMPAIGN_ROUNDS}">
    <button id="rd-campaign">运行 5 轮 Campaign</button>
    <p id="rd-campaign-status" class="meta"></p>
  </aside>
  <section>
    <h2>最新结果</h2>
    <div id="error" class="err"></div>
    <div id="result">
      <div class="panel">
        <h3>等待输入</h3>
        <p class="meta">运行后这里会展示最新的因子公式、评价指标和回测结果。</p>
      </div>
    </div>
    <h2>RD 研究循环</h2>
    <div id="rd-result">
      <div class="panel">
        <h3>等待运行</h3>
        <p class="meta">RD 候选会展示在这里。</p>
      </div>
    </div>
    <h2>RD Campaign</h2>
    <div id="rd-campaign-result">
      <div class="panel">
        <h3>等待运行</h3>
        <p class="meta">Campaign 的轮次筛选和最终因子会展示在这里。</p>
      </div>
    </div>
  </section>
</main>
<script>
const button = document.getElementById('run');
const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const resultEl = document.getElementById('result');
const rdRun = document.getElementById('rd-run');
const rdStart = document.getElementById('rd-start');
const rdStop = document.getElementById('rd-stop');
const rdStatusEl = document.getElementById('rd-status');
const rdResultEl = document.getElementById('rd-result');
const rdCampaign = document.getElementById('rd-campaign');
const rdCampaignStatusEl = document.getElementById('rd-campaign-status');
const rdCampaignResultEl = document.getElementById('rd-campaign-result');

function pct(value) {{
  return (Number(value) * 100).toFixed(2) + '%';
}}
function num(value, digits = 4) {{
  return Number(value).toFixed(digits);
}}
function esc(value) {{
  return String(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function profilePeriodText(profile) {{
  const start = profile.test_period_start || 'full available data';
  const end = profile.test_period_end || 'latest available data';
  return `${{start}} -> ${{end}}`;
}}
function campaignStatusHtml(payload) {{
  const status = payload.status || 'ok';
  const label = payload.status_label || 'Campaign 完成';
  const cssClass = status === 'warning' ? 'warn' : (status === 'fail' ? 'err' : 'ok');
  return `<span class="${{cssClass}}">${{esc(label)}}</span>`;
}}
function render(payload) {{
  const factor = payload.factor;
  const evaluation = payload.evaluation;
  const backtest = payload.backtest;
  const profile = backtest.simulation_profile || evaluation.simulation_profile || {{}};
  const splitRows = (evaluation.split_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} ICIR ${{num(metric.rank_icir, 2)}} · days ${{metric.ic_days}}</span>`
  ).join('');
  const horizonRows = (evaluation.horizon_metrics || []).map(metric =>
    `<span class="pill">${{metric.horizon_days}}日 IC ${{num(metric.rank_ic_mean)}} / ICIR ${{num(metric.rank_icir, 2)}}</span>`
  ).join('');
  const groupRows = (backtest.group_returns || []).map(metric =>
    `<span class="pill">${{esc(metric.group)}} ${{pct(metric.mean_return)}}</span>`
  ).join('');
  const segmentRows = (backtest.segment_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} net ${{pct(metric.net_annualized_return)}} · sharpe ${{num(metric.net_long_short_sharpe || 0, 2)}}</span>`
  ).join('');
  const warningRows = [...(evaluation.warnings || []), ...(backtest.warnings || [])].map(item =>
    `<span class="pill">${{esc(item)}}</span>`
  ).join('');
  const cacheRows = [
    `eval ${{evaluation.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}`,
    evaluation.factor_values_path ? `eval path ${{evaluation.factor_values_path}}` : '',
    `backtest ${{backtest.score_source || 'computed'}} · cached ${{backtest.score_cached_rows || 0}} · computed ${{backtest.score_computed_rows || 0}}`,
    backtest.factor_values_path ? `backtest path ${{backtest.factor_values_path}}` : ''
  ].filter(Boolean).map(item => `<span class="pill">${{esc(item)}}</span>`).join('');
  resultEl.innerHTML = `
    <div class="panel">
      <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
      <div class="formula">${{esc(factor.formula)}}</div>
      <p>${{esc(factor.description || '')}}</p>
      <p class="meta">horizon_days: ${{factor.horizon_days}} · filters: ${{esc((factor.universe_filters || []).join(', ') || 'none')}}</p>
      <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
      <p class="meta">研究口径，不是生产交易口径。</p>
    </div>
    <div class="grid">
      <div class="tile">Rank IC<b>${{num(evaluation.rank_ic_mean)}}</b></div>
      <div class="tile">ICIR<b>${{num(evaluation.rank_icir, 2)}}</b></div>
      <div class="tile">覆盖率<b>${{pct(evaluation.coverage)}}</b></div>
      <div class="tile">IC Days<b>${{evaluation.ic_days}}</b></div>
      <div class="tile">毛累计收益<b>${{pct(backtest.gross_cumulative_return ?? backtest.cumulative_return)}}</b></div>
      <div class="tile">净累计收益<b>${{pct(backtest.net_cumulative_return || 0)}}</b></div>
      <div class="tile">毛年化收益<b>${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</b></div>
      <div class="tile">净年化收益<b>${{pct(backtest.net_annualized_return || 0)}}</b></div>
      <div class="tile">年化波动<b>${{pct(backtest.annualized_volatility)}}</b></div>
      <div class="tile">最大回撤<b>${{pct(backtest.max_drawdown)}}</b></div>
      <div class="tile">持有期<b>${{backtest.holding_days}}日</b></div>
      <div class="tile">Decay<b>${{profile.decay_days || 0}}</b></div>
      <div class="tile">Top Quantile<b>${{num(profile.top_quantile || backtest.top_quantile || 0, 2)}}</b></div>
      <div class="tile">Delay<b>${{profile.execution_delay_days || 1}}日</b></div>
      <div class="tile">净多空Sharpe<b>${{num(backtest.net_long_short_sharpe || backtest.long_short_sharpe || 0, 2)}}</b></div>
      <div class="tile">调仓率<b>${{pct(backtest.rebalance_rate || 0)}}</b></div>
      <div class="tile">换手率<b>${{pct(backtest.turnover_rate || 0)}}</b></div>
    </div>
    <div class="panel">
      <h3>三段验证</h3>
      <p>${{splitRows || '<span class="pill">暂无</span>'}}</p>
      <h3>回测分段</h3>
      <p>${{segmentRows || '<span class="pill">暂无</span>'}}</p>
      <h3>多周期评价</h3>
      <p>${{horizonRows || '<span class="pill">暂无</span>'}}</p>
      <h3>分组收益</h3>
      <p>${{groupRows || '<span class="pill">暂无</span>'}}</p>
      <h3>风险提示</h3>
      <p>${{warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
      <h3>因子值缓存</h3>
      <p>${{cacheRows || '<span class="pill">computed</span>'}}</p>
    </div>
    <div class="panel">
      <h3>Artifacts</h3>
      <p class="meta">${{esc(evaluation.artifact_path)}}</p>
      <p class="meta">${{esc(backtest.artifact_path)}}</p>
    </div>`;
}}
function renderResearch(payload) {{
  const candidates = payload.candidates || [];
  const accepted = payload.accepted_candidate_ids || [];
  const cards = candidates.map(candidate => {{
    const factor = candidate.factor;
    const evaluation = candidate.evaluation;
    const backtest = candidate.backtest;
    const profile = backtest.simulation_profile || {{}};
    const gate = candidate.gate_passed ? '<span class="ok">candidate</span>' : '<span class="err">draft</span>';
    const cacheText = `${{evaluation.score_source || 'computed'}} / ${{backtest.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}}/${{backtest.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}/${{backtest.score_computed_rows || 0}}`;
    const cachePaths = [evaluation.factor_values_path, backtest.factor_values_path].filter(Boolean).join(' / ');
    const artifacts = [evaluation.artifact_path, backtest.artifact_path].filter(Boolean).join(' / ');
    return `
      <div class="panel">
        <h3>${{esc(factor.factor_id)}} · ${{gate}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(candidate.hypothesis.text)}}</p>
        <p class="meta">${{esc(candidate.hypothesis.rationale)}}</p>
        <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
        <p>
          <span class="pill">score ${{num(candidate.score, 4)}}</span>
          <span class="pill">split ICIR ${{num(candidate.split_weighted_icir || 0, 2)}}</span>
          <span class="pill">IC ${{num(evaluation.rank_ic_mean)}}</span>
          <span class="pill">ICIR ${{num(evaluation.rank_icir, 2)}}</span>
          <span class="pill">decay ${{profile.decay_days || 0}}</span>
          <span class="pill">top ${{num(profile.top_quantile || backtest.top_quantile || 0, 2)}}</span>
          <span class="pill">net LS Sharpe ${{num(backtest.net_long_short_sharpe || backtest.long_short_sharpe || 0, 2)}}</span>
          <span class="pill">gross ${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</span>
          <span class="pill">net ${{pct(backtest.net_annualized_return || 0)}}</span>
          <span class="pill">rebalance rate ${{pct(backtest.rebalance_rate || 0)}}</span>
          <span class="pill">turnover rate ${{pct(backtest.turnover_rate || 0)}}</span>
          <span class="pill">factor cache ${{esc(cacheText)}}</span>
        </p>
        <p class="meta">${{esc((candidate.self_review && candidate.self_review.summary) || '')}}</p>
        <p class="meta">factor_values: ${{esc(cachePaths || 'none')}}</p>
        <p class="meta">artifacts: ${{esc(artifacts || 'not generated')}}</p>
        <p class="meta">${{esc((backtest.warnings || []).join('; ') || 'research semantics, not production trading semantics')}}</p>
        <p class="meta">${{esc((candidate.gate_reasons || []).join('; '))}}</p>
      </div>`;
  }}).join('');
  rdResultEl.innerHTML = `
    <div class="panel">
      <h3>${{esc(payload.seed_factor_id)}} · ${{esc(payload.objective)}}</h3>
      <p class="meta">accepted: ${{esc(accepted.join(', ') || 'none')}}</p>
      <p class="meta">report: ${{esc(payload.report_path || 'not generated')}}</p>
    </div>
    ${{cards || '<div class="panel"><h3>无候选</h3></div>'}}`;
}}
function renderCampaign(payload) {{
  const rounds = payload.round_results || [];
  const finalFactor = payload.final_factor || null;
  const finalEvaluation = payload.final_evaluation || null;
  const finalBacktest = payload.final_backtest || null;
  const profile = payload.simulation_profile || (finalBacktest && finalBacktest.simulation_profile) || {{}};
  const roundCards = rounds.map(round => {{
    const attempts = (round.candidates || []).map(candidate => {{
      const factor = candidate.factor || {{}};
      return `
        <p>
          <span class="pill">seed ${{esc(candidate.seed_factor_id)}}</span>
          <span class="pill">factor ${{esc(factor.factor_id || '')}}</span>
          <span class="pill">score ${{num(candidate.score || 0, 4)}}</span>
          <span class="pill">net ${{pct((candidate.backtest || {{}}).net_annualized_return || 0)}}</span>
          <span class="pill">${{candidate.gate_passed ? 'gate passed' : 'gate failed'}}</span>
        </p>
        <p class="meta">${{esc(factor.formula || '')}}</p>
        <p class="meta">${{esc((candidate.gate_reasons || []).join('; '))}}</p>`;
    }}).join('');
    return `
      <div class="panel">
        <h3>Round ${{round.round_index}}</h3>
        <p class="meta">input: ${{esc((round.input_seed_factor_ids || []).join(', ') || 'none')}}</p>
        <p class="meta">selected: ${{esc((round.selected_factor_ids || []).join(', ') || 'none')}}</p>
        ${{attempts || '<p class="meta">无候选</p>'}}
        <p class="${{(round.errors || []).length ? 'warn' : 'meta'}}">${{esc((round.errors || []).join(' | ') || '')}}</p>
      </div>`;
  }}).join('');
  const finalPanel = finalFactor && finalEvaluation && finalBacktest ? `
    <div class="panel">
      <h3>最终因子 · ${{esc(finalFactor.factor_id)}}</h3>
      <div class="formula">${{esc(finalFactor.formula)}}</div>
      <p class="meta">score ${{num(payload.final_score || 0, 4)}} · source ${{esc(finalFactor.source || '')}}</p>
      <p class="meta">seed source: ${{esc(payload.seed_source_label || 'manual')}}${{payload.seed_source_path ? ` · ${{esc(payload.seed_source_path)}}` : ''}}</p>
      <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
      <p>
        <span class="pill">IC ${{num(finalEvaluation.rank_ic_mean)}}</span>
        <span class="pill">ICIR ${{num(finalEvaluation.rank_icir, 2)}}</span>
        <span class="pill">net ${{pct(finalBacktest.net_annualized_return || 0)}}</span>
        <span class="pill">turnover ${{pct(finalBacktest.turnover_rate || 0)}}</span>
        <span class="pill">rebalance ${{pct(finalBacktest.rebalance_rate || 0)}}</span>
      </p>
      <p class="${{(payload.errors || []).length ? 'warn' : 'meta'}}">${{esc((payload.errors || []).join(' | ') || '')}}</p>
      <p class="meta">artifacts: ${{esc((payload.artifacts || []).join(' / ') || 'none')}}</p>
    </div>` : `
    <div class="panel">
      <h3>最终因子</h3>
      <p class="meta">seed source: ${{esc(payload.seed_source_label || 'manual')}}${{payload.seed_source_path ? ` · ${{esc(payload.seed_source_path)}}` : ''}}</p>
      <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
      <p class="${{(payload.errors || []).length ? 'err' : 'meta'}}">${{esc((payload.errors || []).join(' | ') || '')}}</p>
      <p class="meta">Campaign 未生成可用结果。</p>
    </div>`;
  rdCampaignResultEl.innerHTML = finalPanel + roundCards;
}}
function rdPayload() {{
  return {{
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value)
  }};
}}
function campaignPayload() {{
  const seedSourcePath = document.getElementById('rd-campaign-seed-source-path').value.trim();
  return {{
    seed_factor_ids: document.getElementById('rd-campaign-seeds').value
      .split(/[\\s,]+/)
      .map(item => item.trim())
      .filter(Boolean),
    seed_source_path: seedSourcePath || null,
    objective: document.getElementById('rd-objective').value,
    rounds: Number(document.getElementById('rd-rounds').value || {DEFAULT_CAMPAIGN_ROUNDS})
  }};
}}
async function submitIdea(parserMode) {{
  const response = await fetch('/api/run-idea', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      text: document.getElementById('idea').value,
      parser_mode: parserMode,
      llm_provider: document.getElementById('llm-provider').value
    }})
  }});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'request failed');
  return payload;
}}
button.addEventListener('click', async () => {{
  button.disabled = true;
  errorEl.textContent = '';
  statusEl.textContent = '运行中...';
  const parserMode = document.getElementById('parser').value;
  try {{
    const payload = await submitIdea(parserMode);
    render(payload);
    statusEl.innerHTML = '<span class="ok">验证完成</span>';
  }} catch (error) {{
    if (parserMode === 'llm') {{
      const fallback = window.confirm(`LLM 无法使用：${{error.message}}\n\n是否改用本地规则解析？`);
      if (fallback) {{
        try {{
          const payload = await submitIdea('rule');
          render(payload);
          statusEl.innerHTML = '<span class="ok">已使用本地规则解析完成</span>';
          return;
        }} catch (fallbackError) {{
          errorEl.textContent = fallbackError.message;
          statusEl.textContent = '运行失败';
          return;
        }}
      }}
    }}
    errorEl.textContent = error.message;
    statusEl.textContent = '运行失败';
  }} finally {{
    button.disabled = false;
  }}
}});
rdRun.addEventListener('click', async () => {{
  rdRun.disabled = true;
  rdStatusEl.textContent = 'RD 运行中...';
  try {{
    const response = await fetch('/api/research/run-once', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(rdPayload())
    }});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'request failed');
    renderResearch(payload);
    rdStatusEl.innerHTML = '<span class="ok">RD 完成</span>';
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdRun.disabled = false;
  }}
}});
rdStart.addEventListener('click', async () => {{
  rdStart.disabled = true;
  rdStatusEl.textContent = '调度启动中...';
  try {{
    const payload = rdPayload();
    payload.action = 'start';
    payload.interval_days = Number(document.getElementById('rd-interval').value);
    const response = await fetch('/api/research/schedule', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || 'request failed');
    rdStatusEl.innerHTML = '<span class="ok">调度已开启</span>';
    if (status.last_result) renderResearch(status.last_result);
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdStart.disabled = false;
  }}
}});
rdStop.addEventListener('click', async () => {{
  rdStop.disabled = true;
  try {{
    const response = await fetch('/api/research/schedule', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{action: 'stop'}})
    }});
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || 'request failed');
    rdStatusEl.textContent = status.run_count ? `调度已停止，累计运行 ${{status.run_count}} 次` : '调度已停止';
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdStop.disabled = false;
  }}
}});
document.getElementById('rd-campaign-seeds').addEventListener('input', () => {{
  const seedSourcePathEl = document.getElementById('rd-campaign-seed-source-path');
  if (!seedSourcePathEl.value) return;
  seedSourcePathEl.value = '';
  document.getElementById('rd-campaign-seed-source-label').textContent = 'seed source: Manual seed list';
}});
rdCampaign.addEventListener('click', async () => {{
  rdCampaign.disabled = true;
  rdCampaignStatusEl.textContent = 'Campaign 运行中...';
  try {{
    const response = await fetch('/api/research/campaign', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(campaignPayload())
    }});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'request failed');
    renderCampaign(payload);
    rdCampaignStatusEl.innerHTML = campaignStatusHtml(payload);
  }} catch (error) {{
    rdCampaignStatusEl.textContent = error.message;
  }} finally {{
    rdCampaign.disabled = false;
  }}
}});
</script>
</body>
</html>"""
