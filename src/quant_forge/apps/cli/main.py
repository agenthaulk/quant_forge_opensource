"""Quant Forge CLI."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.config import (
    PathSettings,
    QuantForgeConfig,
    bootstrap_runtime_config,
    load_config,
    validate_llm_runtime,
)
from quant_forge.data.local import create_demo_workspace, validate_data_root
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_library.catalog import (
    FactorCatalog,
    discover_factor_value_roots,
    discover_precomputed_factors,
    import_precomputed_factors,
    normalize_precomputed_factor_store,
    resolve_factor_values_root,
)
from quant_forge.factor_library.repository import (
    FactorRepository,
    normalize_factor_root_layout,
    parse_idea_to_definition,
)
from quant_forge.research_loop.config import DEFAULT_RD_CONFIG_PATH, load_research_loop_config, weights_for_objective
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.service import ResearchLoopService


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qf", description="Quant Forge public workbench")
    subcommands = parser.add_subparsers(dest="command")

    doctor = subcommands.add_parser("doctor", help="validate local integration readiness")
    _add_runtime_roots(doctor)
    doctor.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    doctor.set_defaults(handler=_cmd_doctor)

    init = subcommands.add_parser("init", help="create a demo workspace")
    init.add_argument("--workspace", type=Path, default=Path("qf_demo"))
    init.add_argument("--config", type=Path)
    init.set_defaults(handler=_cmd_init)

    data = subcommands.add_parser("data", help="data commands")
    data_subcommands = data.add_subparsers(dest="data_command", required=True)
    validate = data_subcommands.add_parser("validate", help="validate local panel data")
    _add_config_options(validate)
    validate.add_argument("--data-root", type=Path)
    validate.set_defaults(handler=_cmd_data_validate)

    factor = subcommands.add_parser("factor", help="factor library commands")
    factor_subcommands = factor.add_subparsers(dest="factor_command", required=True)
    list_cmd = factor_subcommands.add_parser("list", help="list factors")
    _add_config_options(list_cmd)
    list_cmd.add_argument("--factor-root", type=Path)
    list_cmd.add_argument("--factor-values-root", type=Path)
    list_cmd.add_argument("--factor-values-manifest-root", type=Path)
    list_cmd.set_defaults(handler=_cmd_factor_list)
    import_precomputed = factor_subcommands.add_parser(
        "import-precomputed",
        help="register mounted precomputed factor values into factor_root",
    )
    import_precomputed.add_argument("factor_ids", nargs="*")
    import_precomputed.add_argument("--all", action="store_true", help="import every discovered precomputed factor")
    import_precomputed.add_argument("--to", choices=["draft", "candidate"], default="candidate")
    _add_config_options(import_precomputed)
    import_precomputed.add_argument("--factor-root", type=Path)
    import_precomputed.add_argument("--factor-values-root", type=Path)
    import_precomputed.add_argument("--factor-values-manifest-root", type=Path)
    import_precomputed.set_defaults(handler=_cmd_factor_import_precomputed)
    normalize_store = factor_subcommands.add_parser(
        "normalize-store",
        help="create canonical factor_id=<FACTOR_ID> entries for mounted factor values",
    )
    _add_config_options(normalize_store)
    normalize_store.add_argument("--factor-values-root", type=Path)
    normalize_store.add_argument("--factor-values-manifest-root", type=Path)
    normalize_store.add_argument(
        "--source-factor-values-root",
        action="append",
        type=Path,
        default=[],
        help="additional mounted factor-value root to merge into factor_values_root",
    )
    normalize_store.add_argument(
        "--scan-root",
        action="append",
        type=Path,
        default=[],
        help="mounted data tree to scan for factor-value roots before normalization",
    )
    normalize_store.add_argument("--dry-run", action="store_true")
    normalize_store.add_argument("--link-files", action="store_true", help="hardlink files when possible")
    normalize_store.set_defaults(handler=_cmd_factor_normalize_store)
    normalize_root = factor_subcommands.add_parser(
        "normalize-root",
        help="copy factor.yaml definitions into 原始因子/合成因子 category directories",
    )
    _add_config_options(normalize_root)
    normalize_root.add_argument("--factor-root", type=Path)
    normalize_root.add_argument("--dry-run", action="store_true")
    normalize_root.set_defaults(handler=_cmd_factor_normalize_root)
    promote = factor_subcommands.add_parser("promote", help="promote or demote a factor")
    promote.add_argument("factor_id")
    promote.add_argument("--to", required=True, choices=["draft", "candidate", "active", "inactive", "archived"])
    _add_config_options(promote)
    promote.add_argument("--factor-root", type=Path)
    promote.add_argument("--reason", required=True)
    promote.set_defaults(handler=_cmd_factor_promote)
    recommend = factor_subcommands.add_parser("recommend-active", help="show active-promotion recommendation")
    recommend.add_argument("factor_id")
    _add_config_options(recommend)
    recommend.add_argument("--factor-root", type=Path)
    recommend.set_defaults(handler=_cmd_factor_recommend)

    idea = subcommands.add_parser("idea-to-factor", help="parse an idea into a draft factor")
    idea.add_argument("--text", required=True)
    _add_config_options(idea)
    idea.add_argument("--factor-root", type=Path)
    idea.set_defaults(handler=_cmd_idea_to_factor)

    report = subcommands.add_parser("report-to-factor", help="parse a text report into a draft factor")
    report.add_argument("--report", type=Path, required=True)
    _add_config_options(report)
    report.add_argument("--factor-root", type=Path)
    report.set_defaults(handler=_cmd_report_to_factor)

    eval_cmd = subcommands.add_parser("eval-factor", help="evaluate a factor")
    eval_cmd.add_argument("factor_id")
    _add_runtime_roots(eval_cmd)
    eval_cmd.add_argument("--horizon-days", type=int)
    eval_cmd.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    eval_cmd.set_defaults(handler=_cmd_eval_factor)

    backtest = subcommands.add_parser("run-backtest", help="run a lightweight factor backtest")
    backtest.add_argument("factor_id")
    _add_runtime_roots(backtest)
    backtest.add_argument("--top-quantile", type=float)
    backtest.add_argument("--holding-days", type=int)
    backtest.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    backtest.set_defaults(handler=_cmd_run_backtest)

    research = subcommands.add_parser("research", help="research-development loop commands")
    research_subcommands = research.add_subparsers(dest="research_command", required=True)
    run_once = research_subcommands.add_parser("run-once", help="run one local RD iteration")
    run_once.add_argument("seed_factor_id")
    _add_runtime_roots(run_once)
    run_once.add_argument(
        "--objective",
        choices=["balanced", "rank_ic", "rank_icir", "annualized_return"],
        default=None,
    )
    run_once.add_argument("--max-candidates", type=int)
    run_once.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    run_once.set_defaults(handler=_cmd_research_run_once)

    web = subcommands.add_parser("web", help="run local web adapter")
    web.add_argument("--config", type=Path)
    web.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    web.add_argument("--workspace", type=Path)
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    web.set_defaults(handler=_cmd_web)
    return parser


def _add_runtime_roots(parser: argparse.ArgumentParser) -> None:
    _add_config_options(parser)
    parser.add_argument("--factor-root", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--factor-values-root", type=Path)
    parser.add_argument("--factor-values-overlay-root", type=Path)
    parser.add_argument("--factor-values-manifest-root", type=Path)
    parser.add_argument("--artifact-root", type=Path)


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path)


def _cmd_init(args: argparse.Namespace) -> int:
    config = load_config(args.config, args.workspace)
    paths = create_demo_workspace(
        args.workspace,
        data_root=config.paths.data_root,
        factor_root=config.paths.factor_root,
        artifact_root=config.paths.artifact_root,
    )
    _print_json({key: str(value) for key, value in paths.items()})
    return 0


def _cmd_data_validate(args: argparse.Namespace) -> int:
    result = validate_data_root(_runtime_paths(args).data_root)
    _print_dataclass(result)
    return 0 if result.ok else 2


def _cmd_doctor(args: argparse.Namespace) -> int:
    payload = _doctor_payload(args)
    _print_json(payload)
    return 0 if payload["ok"] else 2


def _cmd_factor_list(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    factors = FactorCatalog(
        paths.factor_root,
        factor_values_root=paths.factor_values_root,
        factor_values_manifest_root=paths.factor_values_manifest_root,
    ).list()
    _print_json([asdict(factor) for factor in factors])
    return 0


def _cmd_factor_import_precomputed(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    imported = import_precomputed_factors(
        paths.factor_root,
        factor_values_root=paths.factor_values_root,
        manifest_root=paths.factor_values_manifest_root,
        factor_ids=tuple(args.factor_ids),
        import_all=bool(args.all),
        status=args.to,
    )
    _print_json(
        {
            "imported_count": len(imported),
            "factor_root": str(paths.factor_root),
            "factor_ids": [factor.factor_id for factor in imported],
        }
    )
    return 0


def _cmd_factor_normalize_store(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    result = normalize_precomputed_factor_store(
        paths.factor_values_root,
        manifest_root=paths.factor_values_manifest_root,
        source_roots=_normalization_source_roots(args),
        dry_run=bool(args.dry_run),
        link_files=bool(args.link_files),
    )
    _print_dataclass(result)
    return 0


def _cmd_factor_normalize_root(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    result = normalize_factor_root_layout(paths.factor_root, dry_run=bool(args.dry_run))
    _print_dataclass(result)
    return 0


def _cmd_factor_promote(args: argparse.Namespace) -> int:
    factor = FactorRepository(_runtime_paths(args).factor_root).promote(args.factor_id, args.to, args.reason)
    _print_dataclass(factor)
    return 0


def _cmd_factor_recommend(args: argparse.Namespace) -> int:
    factor = FactorRepository(_runtime_paths(args).factor_root).get(args.factor_id)
    _print_json(
        {
            "factor_id": factor.factor_id,
            "current_status": factor.status,
            "recommendation": "requires_user_decision",
            "reason": "OpenSource workbench does not auto-promote factors.",
        }
    )
    return 0


def _cmd_idea_to_factor(args: argparse.Namespace) -> int:
    factor = parse_idea_to_definition(args.text)
    FactorRepository(_runtime_paths(args).factor_root).save(factor)
    _print_dataclass(factor)
    return 0


def _cmd_report_to_factor(args: argparse.Namespace) -> int:
    if not args.report.exists():
        raise FileNotFoundError(f"report does not exist: {args.report}")
    text = args.report.read_text(encoding="utf-8")
    factor = parse_idea_to_definition(text)
    FactorRepository(_runtime_paths(args).factor_root).save(factor)
    _print_dataclass(factor)
    return 0


def _cmd_eval_factor(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
    result = evaluate_factor(
        args.factor_id,
        factor_root=paths.factor_root,
        data_root=paths.data_root,
        artifact_root=paths.artifact_root,
        horizon_days=args.horizon_days,
        horizon_days_matrix=rd_config.horizon_days_matrix,
        sample_splits=rd_config.sample_splits,
        simulation_profile=rd_config.simulation_profile,
        factor_values_root=paths.factor_values_root,
        factor_values_overlay_root=paths.factor_values_overlay_root,
        factor_values_manifest_root=paths.factor_values_manifest_root,
    )
    _print_dataclass(result)
    return 0


def _cmd_run_backtest(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
    profile = rd_config.simulation_profile
    if args.top_quantile is not None:
        profile = replace(profile, top_quantile=args.top_quantile)
    result = run_factor_backtest(
        args.factor_id,
        factor_root=paths.factor_root,
        data_root=paths.data_root,
        artifact_root=paths.artifact_root,
        simulation_profile=profile,
        holding_days=args.holding_days,
        transaction_costs=rd_config.transaction_costs,
        sample_splits=rd_config.sample_splits,
        factor_values_root=paths.factor_values_root,
        factor_values_overlay_root=paths.factor_values_overlay_root,
        factor_values_manifest_root=paths.factor_values_manifest_root,
    )
    _print_dataclass(result)
    return 0


def _cmd_research_run_once(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
    hypothesis_generator = None
    review_generator = None
    if _rd_generation_mode(rd_config.llm.hypothesis_mode) == "llm":
        hypothesis_generator = LLMHypothesisGenerator(_rd_llm_settings(config, feature="RD hypothesis generation"))
    if _rd_generation_mode(rd_config.llm.review_mode) == "llm":
        review_generator = LLMResearchReviewGenerator(_rd_llm_settings(config, feature="RD self-review"))
    service = ResearchLoopService(
        factor_root=paths.factor_root,
        data_root=paths.data_root,
        artifact_root=paths.artifact_root,
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
        factor_values_root=paths.factor_values_root,
        factor_values_overlay_root=paths.factor_values_overlay_root,
        factor_values_manifest_root=paths.factor_values_manifest_root,
        hypothesis_generator=hypothesis_generator,
        review_generator=review_generator,
    )
    objective = args.objective or rd_config.objective
    weights = weights_for_objective(rd_config, objective)
    result = service.run_once(
        args.seed_factor_id,
        objective=objective,
        max_candidates=args.max_candidates if args.max_candidates is not None else rd_config.default_max_candidates,
        weights=weights,
        gate=rd_config.gate,
    )
    _print_dataclass(result)
    return 0


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


def _config(args: argparse.Namespace) -> QuantForgeConfig:
    return bootstrap_runtime_config(getattr(args, "config", None), getattr(args, "workspace", None))


def _runtime_paths(args: argparse.Namespace) -> PathSettings:
    return _runtime_paths_from_config(args, _config(args))


def _runtime_paths_from_config(args: argparse.Namespace, config: QuantForgeConfig) -> PathSettings:
    paths = config.paths
    return PathSettings(
        data_root=getattr(args, "data_root", None) or paths.data_root,
        factor_root=getattr(args, "factor_root", None) or paths.factor_root,
        factor_values_root=getattr(args, "factor_values_root", None) or paths.factor_values_root,
        factor_values_overlay_root=getattr(args, "factor_values_overlay_root", None)
        or paths.factor_values_overlay_root,
        factor_values_manifest_root=getattr(args, "factor_values_manifest_root", None)
        or paths.factor_values_manifest_root,
        artifact_root=getattr(args, "artifact_root", None) or paths.artifact_root,
        output_root=paths.output_root,
    )


def _normalization_source_roots(args: argparse.Namespace) -> tuple[Path, ...]:
    roots: list[Path] = list(getattr(args, "source_factor_values_root", ()) or ())
    for scan_root in getattr(args, "scan_root", ()) or ():
        roots.extend(discover_factor_value_roots(scan_root))
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        key = root.expanduser().resolve() if root.expanduser().exists() else root.expanduser()
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return tuple(result)


def _cmd_web(args: argparse.Namespace) -> int:
    from quant_forge.apps.web.server import run_local_web

    config = load_config(args.config, args.workspace)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    host = args.host or config.web.host
    port = args.port or config.web.port
    run_local_web(host=host, port=port, config=config, rd_config=rd_config)
    return 0


def _doctor_payload(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    config_path = getattr(args, "config", None)
    workspace = getattr(args, "workspace", None)
    rd_config_path = getattr(args, "rd_config", DEFAULT_RD_CONFIG_PATH)
    try:
        config = load_config(config_path, workspace)
        checks.append(_doctor_check("config", "ok", "runtime config loaded"))
    except Exception as exc:
        checks.append(_doctor_check("config", "error", str(exc)))
        return {
            "ok": False,
            "checks": checks,
            "config_path": str(config_path) if config_path else "<built-in defaults>",
            "rd_config_path": str(rd_config_path),
            "workspace": str(workspace or ""),
            "next_commands": _doctor_next_commands(args),
        }

    paths = _runtime_paths_from_config(args, config)
    payload: dict[str, Any] = {
        "config_path": str(config_path) if config_path else "<built-in defaults>",
        "rd_config_path": str(rd_config_path),
        "workspace": str(workspace or ""),
        "paths": {
            "data_root": str(paths.data_root),
            "factor_root": str(paths.factor_root),
            "factor_values_root": str(paths.factor_values_root or ""),
            "factor_values_overlay_root": str(paths.factor_values_overlay_root or ""),
            "factor_values_manifest_root": str(paths.factor_values_manifest_root or ""),
            "artifact_root": str(paths.artifact_root),
            "output_root": str(paths.output_root),
        },
        "web": asdict(config.web),
        "simulation": asdict(config.simulation),
    }

    try:
        rd_config = load_research_loop_config(rd_config_path, config.research, config.simulation)
        payload["rd"] = {
            "objective": rd_config.objective,
            "horizon_days_matrix": list(rd_config.horizon_days_matrix),
            "sample_splits": [asdict(split) for split in rd_config.sample_splits],
            "transaction_costs": asdict(rd_config.transaction_costs),
            "report_root": str(paths.artifact_root / "research_reports"),
        }
        checks.append(_doctor_check("rd_config", "ok", "RD config loaded"))
    except Exception as exc:
        checks.append(_doctor_check("rd_config", "error", str(exc)))

    data_status = _doctor_data_status(paths.data_root)
    payload["data"] = data_status["payload"]
    checks.append(data_status["check"])

    factor_status = _doctor_factor_status(paths)
    payload["factor_root"] = factor_status["payload"]
    checks.append(factor_status["check"])
    seed_factor_id = _doctor_seed_factor_id(factor_status["payload"])

    factor_values_status = _doctor_factor_values_status(paths)
    payload["factor_values"] = factor_values_status["payload"]
    checks.append(factor_values_status["check"])

    llm_status = _doctor_llm_status(config)
    payload["llm"] = llm_status["payload"]
    checks.append(llm_status["check"])

    artifact_status = {
        "path": str(paths.artifact_root),
        "exists": paths.artifact_root.expanduser().exists(),
        "report_root": str(paths.artifact_root / "research_reports"),
    }
    payload["artifact_root"] = artifact_status
    checks.append(_doctor_check("artifact_root", "ok", "artifact root will be created on write", artifact_status))

    payload["checks"] = checks
    payload["ok"] = not any(check["status"] == "error" for check in checks)
    payload["next_commands"] = _doctor_next_commands(args, seed_factor_id=seed_factor_id)
    return payload


def _doctor_data_status(data_root: Path) -> dict[str, Any]:
    try:
        validation = validate_data_root(data_root)
    except Exception as exc:
        return {
            "payload": {"data_root": str(data_root), "ok": False, "error": str(exc)},
            "check": _doctor_check("data", "error", str(exc)),
        }
    payload = _json_safe(validation)
    if validation.ok:
        return {"payload": payload, "check": _doctor_check("data", "ok", "local panel data is valid", payload)}
    missing = ", ".join(validation.missing_columns) or "no rows"
    return {
        "payload": payload,
        "check": _doctor_check("data", "error", f"invalid data_root {data_root}: missing {missing}", payload),
    }


def _doctor_factor_status(paths: PathSettings) -> dict[str, Any]:
    root = paths.factor_root.expanduser()
    try:
        local_factors = FactorRepository(root).list() if root.exists() else []
        precomputed_factors = discover_precomputed_factors(
            paths.factor_values_root,
            manifest_root=paths.factor_values_manifest_root,
        )
        factors = FactorCatalog(
            root,
            factor_values_root=paths.factor_values_root,
            factor_values_manifest_root=paths.factor_values_manifest_root,
        ).list()
    except Exception as exc:
        return {
            "payload": {"path": str(root), "exists": root.exists(), "error": str(exc)},
            "check": _doctor_check("factor_root", "error", str(exc)),
        }
    payload = {
        "path": str(root),
        "exists": root.exists(),
        "factor_count": len(factors),
        "local_factor_count": len(local_factors),
        "precomputed_factor_count": len(precomputed_factors),
        "sample_factor_ids": [factor.factor_id for factor in factors[:10]],
    }
    if factors:
        message = "factor catalog is readable"
        if not local_factors and precomputed_factors:
            message = "factor catalog is readable from factor_values_root"
        return {"payload": payload, "check": _doctor_check("factor_root", "ok", message, payload)}
    return {
        "payload": payload,
        "check": _doctor_check(
            "factor_root",
            "error",
            f"no factors found under factor_root {root} or factor_values_root {paths.factor_values_root or ''}",
            payload,
        ),
    }


def _doctor_factor_values_status(paths: PathSettings) -> dict[str, Any]:
    configured_root = paths.factor_values_root.expanduser() if paths.factor_values_root is not None else None
    root = resolve_factor_values_root(configured_root)
    precomputed_count = len(discover_precomputed_factors(root, manifest_root=paths.factor_values_manifest_root))
    overlay_root = paths.factor_values_overlay_root.expanduser() if paths.factor_values_overlay_root is not None else None
    payload = {
        "configured": configured_root is not None,
        "configured_path": str(configured_root or ""),
        "path": str(root or ""),
        "exists": root.exists() if root is not None else False,
        "overlay_root": str(overlay_root or ""),
        "overlay_exists": overlay_root.exists() if overlay_root is not None else False,
        "manifest_root": str(paths.factor_values_manifest_root or ""),
        "precomputed_factor_count": precomputed_count,
    }
    if root is None:
        return {
            "payload": payload,
            "check": _doctor_check("factor_values", "warning", "factor_values_root is not configured; formulas will compute locally", payload),
        }
    if root.exists():
        return {"payload": payload, "check": _doctor_check("factor_values", "ok", "factor value cache path exists", payload)}
    return {
        "payload": payload,
        "check": _doctor_check("factor_values", "warning", "factor value cache path does not exist yet; incremental output will be created when needed", payload),
    }


def _doctor_llm_status(config: QuantForgeConfig) -> dict[str, Any]:
    options = config.llm.public_provider_options()
    active = config.llm.select_provider()
    ready: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for option in options:
        provider = option["provider"]
        try:
            selected = config.llm.select_provider(provider)
            validate_llm_runtime(config.llm, provider)
            ready.append(
                {
                    "provider": provider,
                    "model": selected.model,
                    "api_key_env": selected.api_key_env,
                }
            )
        except Exception as exc:
            missing.append({"provider": provider, "api_key_env": option.get("api_key_env", ""), "error": str(exc)})
    payload = {
        "active_provider": config.llm.provider,
        "active_requires_api_key": active.api_key_required,
        "runtime_ready_providers": ready,
        "missing_providers": missing,
    }
    if not options:
        return {"payload": payload, "check": _doctor_check("llm", "warning", "no LLM providers configured; rule parser remains available", payload)}
    if ready:
        return {"payload": payload, "check": _doctor_check("llm", "ok", "at least one LLM provider is runtime-ready", payload)}
    if not active.api_key_required:
        return {
            "payload": payload,
            "check": _doctor_check(
                "llm",
                "warning",
                "active parser is rule; optional LLM providers are not runtime-ready",
                payload,
            ),
        }
    return {"payload": payload, "check": _doctor_check("llm", "error", "no configured LLM provider is runtime-ready", payload)}


def _doctor_check(name: str, status: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "message": message}
    if details is not None:
        payload["details"] = _json_safe(details)
    return payload


def _doctor_seed_factor_id(factor_payload: dict[str, Any]) -> str | None:
    sample_ids = factor_payload.get("sample_factor_ids") or []
    return str(sample_ids[0]) if sample_ids else None


def _doctor_next_commands(args: argparse.Namespace, *, seed_factor_id: str | None = None) -> list[str]:
    config_arg = f" --config {args.config}" if getattr(args, "config", None) else ""
    workspace_arg = f" --workspace {args.workspace}" if getattr(args, "workspace", None) else ""
    rd_config_arg = f" --rd-config {getattr(args, 'rd_config', DEFAULT_RD_CONFIG_PATH)}"
    factor_list_root_args = "".join(
        [
            f" --factor-root {args.factor_root}" if getattr(args, "factor_root", None) else "",
            f" --factor-values-root {args.factor_values_root}" if getattr(args, "factor_values_root", None) else "",
            f" --factor-values-manifest-root {args.factor_values_manifest_root}"
            if getattr(args, "factor_values_manifest_root", None)
            else "",
        ]
    )
    root_args = "".join(
        [
            f" --data-root {args.data_root}" if getattr(args, "data_root", None) else "",
            f" --factor-root {args.factor_root}" if getattr(args, "factor_root", None) else "",
            f" --factor-values-root {args.factor_values_root}" if getattr(args, "factor_values_root", None) else "",
            f" --factor-values-overlay-root {args.factor_values_overlay_root}"
            if getattr(args, "factor_values_overlay_root", None)
            else "",
            f" --factor-values-manifest-root {args.factor_values_manifest_root}"
            if getattr(args, "factor_values_manifest_root", None)
            else "",
            f" --artifact-root {args.artifact_root}" if getattr(args, "artifact_root", None) else "",
        ]
    )
    runtime_args = f"{config_arg}{workspace_arg}{root_args}"
    commands = [
        f"qf data validate{config_arg}{workspace_arg}{f' --data-root {args.data_root}' if getattr(args, 'data_root', None) else ''}",
        f"qf factor list{config_arg}{workspace_arg}{factor_list_root_args}",
        f"qf web{config_arg}{workspace_arg}{rd_config_arg}",
    ]
    if seed_factor_id:
        commands[2:2] = [
            f"qf eval-factor {seed_factor_id}{runtime_args}{rd_config_arg}",
            f"qf run-backtest {seed_factor_id}{runtime_args}{rd_config_arg}",
            f"qf research run-once {seed_factor_id}{runtime_args}{rd_config_arg}",
        ]
    return commands


def _print_dataclass(value: Any) -> None:
    _print_json(_json_safe(value))


def _print_json(payload: Any) -> None:
    print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True))


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


if __name__ == "__main__":
    raise SystemExit(main())
