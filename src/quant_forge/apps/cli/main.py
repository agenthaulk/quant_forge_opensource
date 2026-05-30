"""Quant Forge CLI."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.config import PathSettings, QuantForgeConfig, load_config
from quant_forge.data.local import create_demo_workspace, validate_data_root
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_library.repository import FactorRepository, parse_idea_to_definition
from quant_forge.research_loop.config import DEFAULT_RD_CONFIG_PATH, load_research_loop_config, weights_for_objective
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
    list_cmd.set_defaults(handler=_cmd_factor_list)
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


def _cmd_factor_list(args: argparse.Namespace) -> int:
    factors = FactorRepository(_runtime_paths(args).factor_root).list()
    _print_json([asdict(factor) for factor in factors])
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
    )
    _print_dataclass(result)
    return 0


def _cmd_research_run_once(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
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


def _config(args: argparse.Namespace) -> QuantForgeConfig:
    return load_config(getattr(args, "config", None), getattr(args, "workspace", None))


def _runtime_paths(args: argparse.Namespace) -> PathSettings:
    paths = _config(args).paths
    return PathSettings(
        data_root=getattr(args, "data_root", None) or paths.data_root,
        factor_root=getattr(args, "factor_root", None) or paths.factor_root,
        artifact_root=getattr(args, "artifact_root", None) or paths.artifact_root,
        output_root=paths.output_root,
    )


def _cmd_web(args: argparse.Namespace) -> int:
    from quant_forge.apps.web.server import run_local_web

    config = load_config(args.config, args.workspace)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    host = args.host or config.web.host
    port = args.port or config.web.port
    run_local_web(host=host, port=port, config=config, rd_config=rd_config)
    return 0


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
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    raise SystemExit(main())
