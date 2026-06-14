"""Minimal local-only web/API adapter."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from html import escape
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.service import ResearchLoopResult, ResearchLoopService


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


def create_local_web_server(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> ThreadingHTTPServer:
    allowed_hosts = {"127.0.0.1", "localhost"}
    if config.web.allow_docker_bind:
        allowed_hosts.add("0.0.0.0")
    if host not in allowed_hosts:
        raise ValueError(
            "OpenSource web adapter is local-only; use 127.0.0.1 or localhost. "
            "Set web.allow_docker_bind only for Docker containers published to host loopback."
        )

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
                        "rd": _rd_status_payload(config, research_config),
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
        deduplication=rd_config.deduplication,
        llm_formula_repair_attempts=rd_config.llm.max_formula_repair_attempts,
        hypothesis_generator=hypothesis_generator,
        review_generator=review_generator,
    )
    weights = weights_for_objective(rd_config, objective)
    return service.run_once(
        seed_factor_id,
        objective=objective,
        max_candidates=max_candidates,
        weights=weights,
        gate=rd_config.gate,
    )


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
        return {str(key): _json_safe(item) for key, item in value.items() if str(key) != "raw_response"}
    return value


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


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
    rd_optimizer_label = escape(_rd_optimizer_label(config, research_config))
    seed_factor_id = escape(_default_seed_factor_id(config))
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
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Forge</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17211d;
      --muted: #65736e;
      --faint: #87948e;
      --line: #d9e0dc;
      --line-strong: #b7c4be;
      --surface: #fbfcfa;
      --panel: #ffffff;
      --wash: #f2f6f1;
      --accent: #134b3c;
      --accent-2: #1f6f63;
      --blue: #265f8f;
      --bad: #9b2f31;
      --warn: #a36213;
      --mono: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-width: 320px; }}
    body {{
      margin: 0;
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(19,75,60,.045) 1px, transparent 1px),
        linear-gradient(180deg, rgba(38,95,143,.04) 1px, transparent 1px),
        var(--surface);
      background-size: 40px 40px;
    }}
    .app-shell {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(300px, 388px) minmax(0, 1fr);
    }}
    .control-rail {{
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
      padding: 22px;
      border-right: 1px solid var(--line);
      background: rgba(251, 252, 250, .94);
      backdrop-filter: blur(8px);
    }}
    .workbench {{
      min-width: 0;
      padding: 22px 28px 32px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{
      margin-bottom: 4px;
      font-size: 26px;
      line-height: 1.08;
      letter-spacing: 0;
    }}
    h2 {{
      margin-bottom: 8px;
      font-size: 15px;
      color: var(--ink);
      letter-spacing: 0;
    }}
    h3 {{
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    label {{
      display: block;
      margin: 14px 0 7px;
      font-size: 12px;
      font-weight: 800;
      color: var(--muted);
    }}
    textarea, select, input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }}
    textarea:focus, select:focus, input:focus {{
      border-color: var(--accent-2);
      box-shadow: 0 0 0 3px rgba(31, 111, 99, .12);
    }}
    textarea {{
      min-height: 126px;
      resize: vertical;
      padding: 12px;
    }}
    select {{ padding: 10px 12px; }}
    input {{ padding: 10px 12px; }}
    button {{
      width: 100%;
      margin-top: 14px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 13px 16px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
      transition: transform .12s ease, background .12s ease;
    }}
    button:hover {{ background: #0f3f32; }}
    button:active {{ transform: translateY(1px); }}
    button.secondary {{
      border-color: var(--line-strong);
      background: #fff;
      color: var(--ink);
    }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    code {{
      background: #eef5ef;
      border: 1px solid var(--line);
      border-radius: 5px;
      padding: 2px 6px;
    }}
    .brand {{
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .brand-mark {{
      display: inline-grid;
      place-items: center;
      width: 36px;
      height: 36px;
      margin-bottom: 12px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: #fff;
      color: var(--accent);
      font-family: var(--mono);
      font-weight: 900;
    }}
    .brand-subtitle {{
      margin-bottom: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .sr-only {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }}
    .runtime-strip {{
      display: grid;
      gap: 8px;
      margin: 18px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .runtime-row {{
      display: grid;
      grid-template-columns: 78px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      min-width: 0;
      font-size: 12px;
    }}
    .runtime-row span:first-child {{
      color: var(--faint);
      font-weight: 800;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      word-break: break-word;
    }}
    .path-meta {{
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      line-height: 1.45;
      word-break: break-all;
    }}
    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      margin: 0 0 14px;
    }}
    .section-title p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .form-block {{
      margin: 18px 0;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(132px, 1fr));
      gap: 10px;
      margin: 14px 0 20px;
    }}
    .tile, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .tile {{
      min-height: 94px;
      padding: 14px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }}
    .tile b {{
      display: block;
      margin-top: 10px;
      color: var(--ink);
      font-family: var(--mono);
      font-size: clamp(20px, 2.3vw, 28px);
      line-height: 1.05;
      letter-spacing: 0;
    }}
    .panel {{
      margin-bottom: 14px;
      padding: 18px;
    }}
    .hero-panel {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      border-top: 4px solid var(--accent);
    }}
    .hero-panel > p {{
      grid-column: 1 / -1;
      margin: 0;
    }}
    hr {{
      margin: 20px 0;
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
      border-radius: 6px;
      padding: 4px 8px;
      margin: 2px 4px 2px 0;
      color: var(--muted);
      background: #fff;
      font-size: 11px;
      font-family: var(--mono);
    }}
    .ok {{ color: var(--accent-2); font-weight: 800; }}
    .warn {{ color: var(--warn); font-weight: 800; }}
    .err {{ color: var(--bad); font-weight: 800; white-space: pre-wrap; }}
    .formula {{
      max-width: 100%;
      overflow-wrap: anywhere;
      color: var(--accent);
      font-family: var(--mono);
      font-size: clamp(18px, 2vw, 24px);
      font-weight: 800;
      margin: 10px 0;
    }}
    .formula-badge {{
      justify-self: end;
      min-width: 104px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--wash);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      text-align: right;
    }}
    .evidence-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .empty-state {{
      min-height: 240px;
      display: grid;
      align-content: center;
      border-style: dashed;
      background: rgba(255, 255, 255, .72);
    }}
    .empty-state h3 {{
      color: var(--accent);
      font-size: 13px;
      letter-spacing: .08em;
    }}
    @media (max-width: 900px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .control-rail {{
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      .workbench {{ padding: 18px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .evidence-grid {{ grid-template-columns: 1fr; }}
      .hero-panel {{ grid-template-columns: 1fr; }}
      .formula-badge {{ justify-self: start; text-align: left; }}
    }}
  </style>
</head>
<body>
<main class="app-shell">
  <aside class="control-rail">
    <div class="brand">
      <div class="brand-mark">QF</div>
      <h1>Quant Forge</h1>
      <p class="brand-subtitle">Factor research console</p>
    </div>
    <p class="sr-only">LLM parser: {provider} / {model}</p>
    <p class="sr-only">RD optimizer: {rd_optimizer_label}</p>
    <div class="runtime-strip">
      <div class="runtime-row"><span>LLM</span><strong>{provider} / {model}</strong></div>
      <div class="runtime-row"><span>RD</span><strong>{rd_optimizer_label}</strong></div>
      <div class="runtime-row"><span>data</span><div class="path-meta">{data_root}</div></div>
      <div class="runtime-row"><span>factors</span><div class="path-meta">{factor_root}</div></div>
      <div class="runtime-row"><span>values</span><div class="path-meta">{factor_values_root or '未配置'}</div></div>
      <div class="runtime-row"><span>overlay</span><div class="path-meta">{factor_values_overlay_root or '未配置'}</div></div>
      <div class="runtime-row"><span>artifacts</span><div class="path-meta">{artifact_root}</div></div>
    </div>
    <div class="form-block">
      <div class="section-title">
        <h2>01 Parse</h2>
        <p>idea → factor</p>
      </div>
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
    </div>
    <div class="form-block">
      <div class="section-title">
        <h2>02 Research</h2>
        <p>seed → candidate</p>
      </div>
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
        <button id="rd-start" class="secondary">开启</button>
        <button id="rd-stop" class="secondary">停止</button>
      </div>
      <p id="rd-status" class="meta"></p>
    </div>
  </aside>
  <section class="workbench">
    <div class="section-title">
      <h2>Factor Tape</h2>
      <p>解析、评价、回测集中展示</p>
    </div>
    <div id="error" class="err"></div>
    <div id="result">
      <div class="panel empty-state">
        <h3>等待输入</h3>
        <p class="meta">输入因子观点后运行，公式、IC、回测收益、缓存路径会在这里展开。</p>
      </div>
    </div>
    <div class="section-title">
      <h2>RD Loop</h2>
      <p>候选因子与研究证据</p>
    </div>
    <div id="rd-result">
      <div class="panel empty-state">
        <h3>等待运行</h3>
        <p class="meta">RD 候选、gate、report path 和分段证据会展示在这里。</p>
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
    <div class="panel hero-panel">
      <div>
        <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(factor.description || '')}}</p>
        <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${{factor.horizon_days}}<br>
        ${{esc((factor.universe_filters || []).join(' · ') || 'FULL')}}
      </div>
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
    <div class="evidence-grid">
      <div class="panel">
        <h3>三段验证</h3>
        <p>${{splitRows || '<span class="pill">暂无</span>'}}</p>
        <h3>回测分段</h3>
        <p>${{segmentRows || '<span class="pill">暂无</span>'}}</p>
        <h3>多周期评价</h3>
        <p>${{horizonRows || '<span class="pill">暂无</span>'}}</p>
      </div>
      <div class="panel">
        <h3>分组收益</h3>
        <p>${{groupRows || '<span class="pill">暂无</span>'}}</p>
        <h3>风险提示</h3>
        <p>${{warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
        <h3>因子值缓存</h3>
        <p>${{cacheRows || '<span class="pill">computed</span>'}}</p>
      </div>
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
    const reviewWarnings = ((candidate.self_review && candidate.self_review.normalization_warnings) || []).join('; ');
    return `
      <div class="panel hero-panel">
        <div>
          <h3>${{esc(factor.factor_id)}} · ${{gate}}</h3>
          <div class="formula">${{esc(factor.formula)}}</div>
          <p>${{esc(candidate.hypothesis.text)}}</p>
          <p class="meta">${{esc(candidate.hypothesis.rationale)}}</p>
          <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
          <p class="meta">研究口径，不是生产交易口径。</p>
        </div>
        <div class="formula-badge">
          score<br>${{num(candidate.score, 4)}}
        </div>
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
        <p class="meta">review normalization: ${{esc(reviewWarnings || 'none')}}</p>
        <p class="meta">factor_values: ${{esc(cachePaths || 'none')}}</p>
        <p class="meta">artifacts: ${{esc(artifacts || 'not generated')}}</p>
        <p class="meta">${{esc((backtest.warnings || []).join('; ') || 'research semantics, not production trading semantics')}}</p>
        <p class="meta">${{esc((candidate.gate_reasons || []).join('; '))}}</p>
      </div>`;
  }}).join('');
  rdResultEl.innerHTML = `
    <div class="panel">
      <h3>${{esc(payload.seed_factor_id)}} · ${{esc(payload.objective)}}</h3>
      <p class="meta">workflow: ${{esc(payload.workflow_type || payload.rd_stage || 'research')}}</p>
      <p class="meta">optimization: ${{payload.optimization_performed ? 'performed' : 'no_optimization_performed'}}</p>
      <p class="meta">accepted: ${{esc(accepted.join(', ') || 'none')}}</p>
      <p class="meta">report: ${{esc(payload.report_path || 'not generated')}}</p>
    </div>
    ${{cards || '<div class="panel"><h3>无候选</h3></div>'}}`;
}}
function rdPayload() {{
  return {{
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value)
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
</script>
</body>
</html>"""
