"""Quant Forge CLI."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant_forge.config import (
    PathSettings,
    QuantForgeConfig,
    bootstrap_runtime_config,
    load_config,
    validate_llm_runtime,
)
from quant_forge.data.local import create_demo_workspace, validate_data_root
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
from quant_forge.integrations.contracts import (
    BACKEND_NOT_CONFIGURED,
    SUBMIT_NOT_CONFIRMED,
    SimulationRequest,
    SubmitRequest,
)
from quant_forge.integrations.dry_run import (
    DryRunOutcome,
    run_translate_prescreen,
    warning_hint,
)
from quant_forge.integrations.registry import list_backends
from quant_forge.lineage.store import (
    LineageStore,
    RUN_KINDS,
    RunIndex,
    artifact_id_for,
    canonical_fingerprint,
    metric_highlight,
    new_run_id,
    redact_free_text,
    relative_artifact_path,
)
from quant_forge.core.contracts import SimulationProfile
from quant_forge.llm_factor_parser import parse_factor_idea
from quant_forge.research_loop.config import (
    DEFAULT_RD_CONFIG_PATH,
    ResearchLoopConfig,
    load_research_loop_config,
    weights_for_objective,
)
from quant_forge.research_loop.goals import (
    GOAL_AUDIT_RESULTS,
    GoalCompletionError,
    GoalCriterion,
    ResearchGoalStore,
)
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.service import ResearchLoopService
from quant_forge.utils import write_json, write_text
from quant_forge.workbench.service import (
    EVALUATION_HIGHLIGHT_METRICS,
    WorkbenchService,
    evaluation_data_window,
    result_warnings_count,
)


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

    build_fund = data_subcommands.add_parser(
        "build-fundamentals",
        help="materialize a point-in-time fundamentals overlay from a local source layer",
    )
    _add_config_options(build_fund)
    build_fund.add_argument("--data-root", type=Path)
    build_fund.add_argument(
        "--output",
        type=Path,
        help="overlay parquet path (default: fundamentals.parquet next to the panel)",
    )
    build_fund.set_defaults(handler=_cmd_data_build_fundamentals)

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
    bench = factor_subcommands.add_parser(
        "bench",
        help="evaluate several factors serially through one shared config and write a bench artifact",
    )
    bench.add_argument("--factor-ids", help="comma-separated factor ids to benchmark")
    bench.add_argument("--status", choices=["draft", "candidate", "active"], help="also benchmark factors with this status")
    _add_runtime_roots(bench)
    bench.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    bench.set_defaults(handler=_cmd_factor_bench)
    submit = factor_subcommands.add_parser(
        "submit",
        help="translate + prescreen a factor for an external backend (dry run by default)",
    )
    submit.add_argument("factor_id")
    submit.add_argument(
        "--target",
        required=True,
        dest="target_backend",
        help="backend id from `qf backends list`",
    )
    _add_config_options(submit)
    submit.add_argument("--factor-root", type=Path)
    submit.add_argument("--artifact-root", type=Path)
    submit.add_argument(
        "--data-region",
        help="region of the local data the backtest ran on (reported to prescreen)",
    )
    submit.add_argument(
        "--target-region",
        help="target platform region (defaults to the backend's first declared region)",
    )
    submit.add_argument(
        "--confirm-submit",
        action="store_true",
        help=(
            "actually submit after translate + prescreen; without this flag the "
            "flow is a dry run and no outward submission is attempted"
        ),
    )
    submit.add_argument("--json", action="store_true", dest="as_json")
    submit.set_defaults(handler=_cmd_factor_submit)

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

    llm_smoke = subcommands.add_parser("llm-smoke", help="verify configured LLM runtime with a real parse call")
    llm_smoke.add_argument(
        "--text",
        default="选择市值较小、近期波动较低的股票，构造低波动小市值因子。",
        help="short natural-language idea to parse",
    )
    llm_smoke.add_argument("--provider", help="optional configured provider name, for example deepseek")
    _add_config_options(llm_smoke)
    llm_smoke.set_defaults(handler=_cmd_llm_smoke)

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
    backtest.add_argument(
        "--include-partial-final-period",
        action="store_true",
        help="include the final incomplete holding period marked to market (D3 opt-in)",
    )
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

    runs = subcommands.add_parser("runs", help="read-only research run history commands")
    runs_subcommands = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_subcommands.add_parser("list", help="list recorded runs, newest first")
    _add_config_options(runs_list)
    runs_list.add_argument("--artifact-root", type=Path)
    runs_list.add_argument("--limit", type=int, default=20)
    runs_list.set_defaults(handler=_cmd_runs_list)
    runs_show = runs_subcommands.add_parser("show", help="show one recorded run in detail")
    runs_show.add_argument("run_id")
    _add_config_options(runs_show)
    runs_show.add_argument("--artifact-root", type=Path)
    runs_show.set_defaults(handler=_cmd_runs_show)
    runs_search = runs_subcommands.add_parser("search", help="search runs by factor id and kind")
    runs_search.add_argument("--factor", required=True, help="factor id to search for")
    # Derived from the lineage store's RUN_KINDS so every recorded run kind
    # (including rd/falsification) is searchable without a second hardcoded list.
    runs_search.add_argument("--kind", choices=list(RUN_KINDS))
    _add_config_options(runs_search)
    runs_search.add_argument("--artifact-root", type=Path)
    runs_search.add_argument("--limit", type=int, default=20)
    runs_search.set_defaults(handler=_cmd_runs_search)

    # ------------------------------------------------------------------
    # Research goal commands (Lane G). Keep this block self-contained so it
    # does not collide with the runs/bench commands registered above.
    # ------------------------------------------------------------------
    goal = subcommands.add_parser("goal", help="immutable research goal artifact commands")
    goal_subcommands = goal.add_subparsers(dest="goal_command", required=True)
    goal_create = goal_subcommands.add_parser("create", help="create an immutable research goal artifact")
    goal_create.add_argument("--objective", required=True, help="what this research goal is trying to achieve")
    goal_create.add_argument(
        "--criteria",
        action="append",
        required=True,
        metavar="TEXT",
        help="required completion criterion (repeatable; ids are assigned c1..cN in order)",
    )
    goal_create.add_argument(
        "--optional-criteria",
        action="append",
        default=[],
        metavar="TEXT",
        help="non-required criterion (repeatable; ids continue the c1..cN sequence)",
    )
    goal_create.add_argument("--seed", required=True, dest="seed_factor_id", help="seed factor id")
    _add_config_options(goal_create)
    goal_create.add_argument("--artifact-root", type=Path)
    goal_create.add_argument("--rd-config", type=Path, default=DEFAULT_RD_CONFIG_PATH)
    goal_create.set_defaults(handler=_cmd_goal_create)
    goal_list = goal_subcommands.add_parser("list", help="list research goals with effective status")
    _add_config_options(goal_list)
    goal_list.add_argument("--artifact-root", type=Path)
    goal_list.set_defaults(handler=_cmd_goal_list)
    goal_show = goal_subcommands.add_parser("show", help="show one goal with its audit log")
    goal_show.add_argument("goal_id")
    _add_config_options(goal_show)
    goal_show.add_argument("--artifact-root", type=Path)
    goal_show.set_defaults(handler=_cmd_goal_show)
    goal_audit = goal_subcommands.add_parser("audit", help="append one criterion audit row")
    goal_audit.add_argument("goal_id")
    goal_audit.add_argument("--criterion", required=True, help="criterion id, for example c1")
    goal_audit.add_argument("--result", required=True, choices=list(GOAL_AUDIT_RESULTS))
    goal_audit.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="PATH",
        help="evidence path relative to artifact_root (repeatable; existence is validated)",
    )
    goal_audit.add_argument("--notes", default="")
    _add_config_options(goal_audit)
    goal_audit.add_argument("--artifact-root", type=Path)
    goal_audit.set_defaults(handler=_cmd_goal_audit)
    goal_complete = goal_subcommands.add_parser("complete", help="complete a goal via the audited completion rule")
    goal_complete.add_argument("goal_id")
    _add_config_options(goal_complete)
    goal_complete.add_argument("--artifact-root", type=Path)
    goal_complete.set_defaults(handler=_cmd_goal_complete)
    # -------------------------- end Lane G ----------------------------

    # ------------------------------------------------------------------
    # Research memory review commands (SE-P4a): CLI parity for the SE-iii
    # governance surface -- the Web review tab is the primary surface (P4b),
    # this is the scriptable/offline equivalent. Prefix resolution is the
    # anti-fat-finger confirmation R3 wants: no interactive prompts, an
    # ambiguous or absent prefix simply fails with the candidate list.
    # ------------------------------------------------------------------
    memory = subcommands.add_parser("memory", help="research memory review commands")
    memory_subcommands = memory.add_subparsers(dest="memory_command", required=True)
    memory_rules = memory_subcommands.add_parser("rules", help="rule governance (activate/deactivate/retire)")
    memory_rules_subcommands = memory_rules.add_subparsers(dest="memory_rules_command", required=True)

    memory_rules_list = memory_rules_subcommands.add_parser("list", help="list promoted rule rows and their state")
    memory_rules_state_filter = memory_rules_list.add_mutually_exclusive_group()
    memory_rules_state_filter.add_argument(
        "--pending", action="store_true", help="show only rules not currently active"
    )
    memory_rules_state_filter.add_argument("--active", action="store_true", help="show only currently active rules")
    _add_config_options(memory_rules_list)
    memory_rules_list.add_argument("--artifact-root", type=Path)
    memory_rules_list.set_defaults(handler=_cmd_memory_rules_list)

    memory_rules_activate = memory_rules_subcommands.add_parser(
        "activate", help="activate a rule signature (or unambiguous prefix) for steering"
    )
    memory_rules_activate.add_argument("signature_prefix", metavar="signature-or-unambiguous-prefix")
    memory_rules_activate.add_argument("--actor", required=True, help="reviewer identity (redacted, required)")
    memory_rules_activate.add_argument("--rationale", default="", help="optional review rationale (redacted)")
    _add_config_options(memory_rules_activate)
    memory_rules_activate.add_argument("--artifact-root", type=Path)
    memory_rules_activate.set_defaults(handler=_cmd_memory_rules_activate)

    memory_rules_deactivate = memory_rules_subcommands.add_parser(
        "deactivate", help="deactivate a rule signature (or unambiguous prefix)"
    )
    memory_rules_deactivate.add_argument("signature_prefix", metavar="signature-or-unambiguous-prefix")
    memory_rules_deactivate.add_argument("--actor", required=True, help="reviewer identity (redacted, required)")
    memory_rules_deactivate.add_argument("--rationale", default="", help="optional review rationale (redacted)")
    _add_config_options(memory_rules_deactivate)
    memory_rules_deactivate.add_argument("--artifact-root", type=Path)
    memory_rules_deactivate.set_defaults(handler=_cmd_memory_rules_deactivate)

    # SE-P5 (ruling SE-v): computed, never-persisted priors read surface.
    memory_priors = memory_subcommands.add_parser(
        "priors", help="quantitative priors view over the outcomes ledger (computed, read-only)"
    )
    memory_priors.add_argument(
        "--dimension",
        action="append",
        dest="dimensions",
        choices=("factor_family", "settings_profile", "asset_class", "universe"),
        help="restrict to specific dimensions (repeatable; default: all)",
    )
    memory_priors.add_argument("--json", action="store_true", help="emit the full view as JSON")
    _add_config_options(memory_priors)
    memory_priors.add_argument("--artifact-root", type=Path)
    memory_priors.set_defaults(handler=_cmd_memory_priors)

    memory_rules_retire = memory_rules_subcommands.add_parser(
        "retire", help="retire a finding or failure signature (or unambiguous prefix)"
    )
    memory_rules_retire.add_argument("target_kind", choices=["finding", "failure"])
    memory_rules_retire.add_argument("signature_prefix", metavar="signature-prefix")
    memory_rules_retire.add_argument("--actor", required=True, help="reviewer identity (redacted, required)")
    memory_rules_retire.add_argument("--rationale", default="", help="optional review rationale (redacted)")
    _add_config_options(memory_rules_retire)
    memory_rules_retire.add_argument("--artifact-root", type=Path)
    memory_rules_retire.set_defaults(handler=_cmd_memory_rules_retire)

    memory_rules_unretire = memory_rules_subcommands.add_parser(
        "unretire", help="reverse a retirement for a finding or failure signature (or unambiguous prefix)"
    )
    memory_rules_unretire.add_argument("target_kind", choices=["finding", "failure"])
    memory_rules_unretire.add_argument("signature_prefix", metavar="signature-prefix")
    memory_rules_unretire.add_argument("--actor", required=True, help="reviewer identity (redacted, required)")
    memory_rules_unretire.add_argument("--rationale", default="", help="optional review rationale (redacted)")
    _add_config_options(memory_rules_unretire)
    memory_rules_unretire.add_argument("--artifact-root", type=Path)
    memory_rules_unretire.set_defaults(handler=_cmd_memory_rules_unretire)
    # ---------------------- end research memory review -----------------

    backends = subcommands.add_parser(
        "backends", help="external factor-backend status commands"
    )
    backends_subcommands = backends.add_subparsers(dest="backends_command", required=True)
    backends_list = backends_subcommands.add_parser(
        "list", help="list every reviewed backend id with its availability status"
    )
    backends_list.add_argument("--json", action="store_true", dest="as_json")
    backends_list.set_defaults(handler=_cmd_backends_list)

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
    config = bootstrap_runtime_config(args.config, args.workspace)
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


def _cmd_data_build_fundamentals(args: argparse.Namespace) -> int:
    """Materialize the PIT fundamentals overlay from a locally-materialized
    source layer (its path comes from the local config only) and write the
    daily [trade_date, instrument, <fields>] overlay next to the panel."""

    import sys

    import pandas as pd

    from quant_forge.data.fundamentals import build_fundamentals_overlay
    from quant_forge.data.local import FUNDAMENTALS_OVERLAY_FILE, resolve_panel_path

    paths = _runtime_paths(args)
    source_root = paths.fundamentals_source_root
    if source_root is None:
        print(
            "fundamentals_source_root is not configured; set paths.fundamentals_source_root "
            "in a local config that points at a locally-materialized source layer",
            file=sys.stderr,
        )
        return 2
    panel_path = resolve_panel_path(paths.data_root)
    if not panel_path.exists():
        print(f"panel not found under data_root (looked for {panel_path.name})", file=sys.stderr)
        return 2

    panel_keys = pd.read_parquet(panel_path, columns=["trade_date", "instrument"])
    overlay = build_fundamentals_overlay(source_root, panel_keys)

    if args.output is not None:
        output = args.output
    elif paths.fundamentals_overlay_root is not None:
        output = paths.fundamentals_overlay_root / FUNDAMENTALS_OVERLAY_FILE
    else:
        output = panel_path.parent / FUNDAMENTALS_OVERLAY_FILE
    output.parent.mkdir(parents=True, exist_ok=True)
    overlay.to_parquet(output, index=False)

    fields = [c for c in overlay.columns if c not in ("trade_date", "instrument")]
    _print_json(
        {
            "output": output.name,
            "rows": int(len(overlay)),
            "instruments": int(overlay["instrument"].nunique()),
            "fields": fields,
            "non_null_counts": {c: int(overlay[c].notna().sum()) for c in fields},
        }
    )
    return 0


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


def _cmd_llm_smoke(args: argparse.Namespace) -> int:
    config = _config(args)
    selected = config.llm.select_provider(args.provider)
    validate_llm_runtime(config.llm, args.provider)
    parsed = parse_factor_idea(args.text, selected, mode="llm")
    _print_json(
        {
            "ok": True,
            "provider": parsed.provider,
            "model": parsed.model,
            "api_key_env": selected.api_key_env,
            "factor": {
                "factor_id": parsed.factor.factor_id,
                "name": parsed.factor.name,
                "formula": parsed.factor.formula,
                "horizon_days": parsed.factor.horizon_days,
                "source": parsed.factor.source,
            },
        }
    )
    return 0


def _cmd_eval_factor(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
    workbench = _workbench(paths, rd_config, profile=rd_config.evaluation_profile)
    result = workbench.evaluate(args.factor_id, horizon_days=args.horizon_days)
    _print_dataclass(result)
    return 0


def _cmd_run_backtest(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
    workbench = _workbench(paths, rd_config, profile=rd_config.backtest_profile)
    result = workbench.run_backtest(
        args.factor_id,
        top_quantile=args.top_quantile,
        holding_days=args.holding_days,
        include_partial_final_period=args.include_partial_final_period,
    )
    _print_dataclass(result)
    return 0


def _workbench(paths: PathSettings, rd_config: ResearchLoopConfig, *, profile: SimulationProfile) -> WorkbenchService:
    return WorkbenchService(
        factor_root=paths.factor_root,
        data_root=paths.data_root,
        artifact_root=paths.artifact_root,
        factor_values_root=paths.factor_values_root,
        factor_values_overlay_root=paths.factor_values_overlay_root,
        factor_values_manifest_root=paths.factor_values_manifest_root,
        simulation_profile=profile,
        transaction_costs=rd_config.transaction_costs,
        sample_splits=rd_config.sample_splits,
        horizon_days_matrix=rd_config.horizon_days_matrix,
    )


def _cmd_factor_bench(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    paths = _runtime_paths(args)
    workbench = _workbench(paths, rd_config, profile=rd_config.evaluation_profile)
    factor_ids = _bench_factor_ids(args, workbench)
    if not factor_ids:
        print("no factors selected: pass --factor-ids and/or --status")
        return 2

    created_at = datetime.now(timezone.utc)
    created_at_iso = created_at.isoformat()
    shared_config = {
        "kind": "bench",
        "factor_ids": list(factor_ids),
        "horizon_days_matrix": list(rd_config.horizon_days_matrix),
        "sample_splits": [asdict(split) for split in rd_config.sample_splits],
        "simulation_profile": asdict(rd_config.evaluation_profile),
    }
    fingerprint = canonical_fingerprint(shared_config)
    run_id = new_run_id("bench", created_at, fingerprint)
    artifact_root = paths.artifact_root.expanduser()

    factor_rows: list[dict[str, Any]] = []
    evaluation_artifact_ids: list[str] = []
    window_starts: list[str] = []
    window_ends: list[str] = []
    warnings_total = 0
    for factor_id in factor_ids:
        try:
            result = workbench.evaluate(factor_id)
        except Exception as exc:  # per-factor isolation: one bad factor must not sink the bench
            factor_rows.append(
                {
                    "factor_id": factor_id,
                    "status": "error",
                    "error": redact_free_text(str(exc)),
                    "metrics": {},
                    "warnings_count": None,
                    "artifact_path_rel": None,
                }
            )
            continue
        window = evaluation_data_window(result)
        if window["status"] == "available":
            window_starts.append(str(window["start_date"]))
            window_ends.append(str(window["end_date"]))
        warnings_count = result_warnings_count(result)
        warnings_total += warnings_count
        evaluation_artifact_ids.append(artifact_id_for(path=result.artifact_path))
        factor_rows.append(
            {
                "factor_id": factor_id,
                "status": "evaluated",
                "metrics": {
                    name: metric_highlight(result.metrics[name])
                    for name in EVALUATION_HIGHLIGHT_METRICS
                    if name in result.metrics
                },
                "warnings_count": warnings_count,
                "artifact_path_rel": relative_artifact_path(artifact_root, result.artifact_path),
            }
        )

    summary = _bench_summary(factor_rows)
    bench_payload = {
        "schema_version": "qf.bench.v1",
        "run_id": run_id,
        "created_at": created_at_iso,
        "config_fingerprint": fingerprint,
        "shared_config": shared_config,
        "factors": factor_rows,
        "summary": summary,
    }
    json_rel = f"bench/{run_id}.json"
    markdown_rel = f"bench/{run_id}.md"
    json_path = artifact_root / json_rel
    markdown_path = artifact_root / markdown_rel
    write_json(json_path, bench_payload)
    write_text(markdown_path, _render_bench_markdown(run_id, created_at_iso, factor_rows, summary))

    store = LineageStore(artifact_root)
    bench_record = store.record_artifact(
        artifact_type="bench_report",
        path=json_path,
        created_at=created_at_iso,
        generated_by="cli.factor_bench",
        parents=tuple(evaluation_artifact_ids),
    )
    store.record_artifact(
        artifact_type="bench_report_markdown",
        path=markdown_path,
        created_at=created_at_iso,
        generated_by="cli.factor_bench",
        parents=(bench_record.artifact_id,),
    )
    data_window: dict[str, Any] = (
        {"start_date": min(window_starts), "end_date": max(window_ends), "status": "available"}
        if window_starts and window_ends
        else {"start_date": None, "end_date": None, "status": "unavailable"}
    )
    RunIndex(artifact_root).append_run(
        run_id=run_id,
        kind="bench",
        factor_ids=factor_ids,
        created_at=created_at_iso,
        data_window=data_window,
        config_fingerprint=fingerprint,
        metric_highlights=_bench_run_highlights(factor_rows),
        artifact_paths_rel=(json_rel, markdown_rel),
        warnings_count=warnings_total + summary["error_factor_count"],
    )

    print(f"bench run_id: {run_id}")
    print(f"bench artifact (json): {json_rel}")
    print(f"bench artifact (markdown): {markdown_rel}")
    _print_bench_table(factor_rows)
    print(
        "summary: "
        f"evaluated={summary['evaluated_factor_count']} "
        f"errors={summary['error_factor_count']} "
        f"metrics_available={summary['available_metric_count']} "
        f"metrics_insufficient={summary['insufficient_metric_count']} "
        f"metrics_other_status={summary['other_status_metric_count']}"
    )
    return 0 if summary["error_factor_count"] == 0 else 2


def _bench_factor_ids(args: argparse.Namespace, workbench: WorkbenchService) -> list[str]:
    selected: list[str] = []
    if getattr(args, "factor_ids", None):
        selected.extend(item.strip() for item in str(args.factor_ids).split(",") if item.strip())
    if getattr(args, "status", None):
        selected.extend(
            factor.factor_id for factor in workbench.list_factors() if factor.status == args.status
        )
    return list(dict.fromkeys(selected))


def _bench_summary(factor_rows: list[dict[str, Any]]) -> dict[str, int]:
    available = insufficient = other = 0
    for row in factor_rows:
        for entry in (row.get("metrics") or {}).values():
            status = entry.get("status")
            if status == "available":
                available += 1
            elif status == "insufficient_sample":
                insufficient += 1
            else:
                other += 1
    return {
        "evaluated_factor_count": sum(1 for row in factor_rows if row["status"] == "evaluated"),
        "error_factor_count": sum(1 for row in factor_rows if row["status"] == "error"),
        "available_metric_count": available,
        "insufficient_metric_count": insufficient,
        "other_status_metric_count": other,
    }


def _bench_run_highlights(factor_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    highlights: dict[str, dict[str, Any]] = {}
    for row in factor_rows:
        for name in ("rank_ic_mean", "rank_icir"):
            entry = (row.get("metrics") or {}).get(name)
            if entry is not None:
                highlights[f"{row['factor_id']}:{name}"] = entry
    return highlights


def _render_bench_markdown(
    run_id: str,
    created_at_iso: str,
    factor_rows: list[dict[str, Any]],
    summary: dict[str, int],
) -> str:
    lines = [
        f"# Factor Bench {run_id}",
        "",
        f"- created_at: {created_at_iso}",
        f"- factors evaluated: {summary['evaluated_factor_count']}",
        f"- factors errored: {summary['error_factor_count']}",
        f"- metric statuses: available={summary['available_metric_count']}, "
        f"insufficient_sample={summary['insufficient_metric_count']}, "
        f"other={summary['other_status_metric_count']}",
        "",
        "| factor_id | status | " + " | ".join(EVALUATION_HIGHLIGHT_METRICS) + " | warnings |",
        "| --- | --- | " + " | ".join("---" for _ in EVALUATION_HIGHLIGHT_METRICS) + " | --- |",
    ]
    for row in factor_rows:
        if row["status"] == "error":
            cells = ["error: " + str(row.get("error") or "")] + ["-" for _ in EVALUATION_HIGHLIGHT_METRICS] + ["-"]
        else:
            cells = ["evaluated"]
            for name in EVALUATION_HIGHLIGHT_METRICS:
                entry = (row.get("metrics") or {}).get(name)
                cells.append(_format_metric_text(entry) if entry else "not_recorded")
            cells.append(str(row.get("warnings_count")))
        lines.append("| " + row["factor_id"] + " | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def _print_bench_table(factor_rows: list[dict[str, Any]]) -> None:
    headers = ["factor_id", "status", *EVALUATION_HIGHLIGHT_METRICS, "warnings"]
    table: list[list[str]] = []
    for row in factor_rows:
        if row["status"] == "error":
            table.append([row["factor_id"], "error: " + str(row.get("error") or ""), *["-"] * len(EVALUATION_HIGHLIGHT_METRICS), "-"])
            continue
        cells = [row["factor_id"], "evaluated"]
        for name in EVALUATION_HIGHLIGHT_METRICS:
            entry = (row.get("metrics") or {}).get(name)
            cells.append(_format_metric_text(entry) if entry else "not_recorded")
        cells.append(str(row.get("warnings_count")))
        table.append(cells)
    _print_text_table(headers, table)


# ---------------------------------------------------------------------------
# External factor-backend commands (CP3). Honest degradation: every state
# renders its closed warning code verbatim plus a one-line hint; the submit
# flow is dry (translate + prescreen) unless --confirm-submit is passed, and
# the backend layer's own gates still decide whether a submission happens
# (FP-D: outward submission stays explicit and human-gated).
# ---------------------------------------------------------------------------


def _cmd_backends_list(args: argparse.Namespace) -> int:
    rows = list_backends()
    if args.as_json:
        _print_json(rows)
        return 0
    headers = [
        "backend_id",
        "status",
        "warning_code",
        "enable_env_var",
        "module",
        "label",
        "regions",
        "capabilities",
    ]
    table = [
        [
            str(row.get("backend_id", "")),
            str(row.get("status", "")),
            str(row.get("warning_code") or "-"),
            str(row.get("enable_env_var") or "-"),
            str(row.get("module") or "-"),
            str(row.get("label") or "-"),
            ",".join(row.get("regions") or []) or "-",
            ",".join(row.get("capabilities") or []) or "-",
        ]
        for row in rows
    ]
    _print_text_table(headers, table)
    return 0


def _cmd_factor_submit(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    outcome = run_translate_prescreen(
        args.target_backend,
        args.factor_id,
        factor_root=paths.factor_root,
        artifact_root=paths.artifact_root,
        data_region=args.data_region,
        target_region=args.target_region,
    )
    payload = dict(outcome.payload)
    payload["mode"] = "submit" if args.confirm_submit else "dry_run"
    payload["submission"] = None
    exit_code = 0 if outcome.ok else 2
    if args.confirm_submit and outcome.ok:
        payload["submission"], exit_code = _perform_backend_submission(outcome)
    if args.as_json:
        _print_json(payload)
        return exit_code
    _render_submit_flow_text(payload)
    return exit_code


def _perform_backend_submission(outcome: DryRunOutcome) -> tuple[dict[str, Any], int]:
    """Run the confirmed submission stage; the adapter's own gates rule.

    The receipt is reported verbatim. A ``refused`` status or a
    ``SUBMIT_NOT_CONFIRMED`` / ``BACKEND_NOT_CONFIGURED`` warning on the
    receipt is a blocked submission (non-zero exit), never papered over.
    """

    resolution = outcome.resolution
    factor = outcome.factor
    translation = outcome.translation
    assert resolution is not None and resolution.port is not None
    assert factor is not None and translation is not None
    port = resolution.port
    descriptor = port.describe()
    if not descriptor.supports("submit"):
        return (
            {
                "status": "unsupported",
                "error": (
                    f"backend '{descriptor.backend_id}' does not declare the submit capability"
                ),
            },
            2,
        )
    backend_ref = translation.expression
    simulation_payload: dict[str, Any] | None = None
    if descriptor.supports("simulate"):
        simulation = port.simulate(
            SimulationRequest(
                factor_id=factor.factor_id,
                expression=translation.expression,
                settings=translation.target_settings,
            )
        )
        simulation_payload = {
            "backend_ref": simulation.backend_ref,
            "metrics": dict(simulation.metrics),
            "warnings": list(simulation.warnings),
            "notes": list(simulation.notes),
        }
        backend_ref = simulation.backend_ref
        if simulation.warnings or not backend_ref:
            # A degraded simulation (warning codes, or no platform object id)
            # cannot honestly feed a submission: chaining ahead would hand
            # submit an empty/unvetted backend_ref. Blocked, not attempted.
            return (
                {
                    "status": "blocked",
                    "error": (
                        "simulation degraded — submission not attempted "
                        f"(warnings: {', '.join(simulation.warnings) or 'none'}; "
                        f"backend_ref: {simulation.backend_ref!r})"
                    ),
                    "simulation": simulation_payload,
                },
                2,
            )
    receipt = port.submit(
        SubmitRequest(
            factor_id=factor.factor_id,
            backend_ref=backend_ref,
            confirm=True,
            provenance=factor.provenance,
        )
    )
    receipt_payload = {
        "submission_ref": receipt.submission_ref,
        "status": receipt.status,
        "warnings": list(receipt.warnings),
        "notes": list(receipt.notes),
        "provenance_carried": receipt.provenance is not None,
    }
    # Codex B-1: success is the narrow case, not the default. ANY warning code
    # (SUBMIT_NOT_CONFIRMED / BACKEND_NOT_CONFIGURED / BACKEND_ERROR / ...), an
    # empty platform object id, or a non-submitted status must exit 2 — a
    # rejected receipt looking successful to shell automation is the hazard.
    blocked = (
        bool(receipt.warnings)
        or not str(receipt.submission_ref or "").strip()
        or receipt.status in ("refused", "rejected", "not_submitted", "error")
    )
    return ({"simulation": simulation_payload, "receipt": receipt_payload}, 2 if blocked else 0)


def _render_submit_flow_text(payload: dict[str, Any]) -> None:
    resolution = payload.get("resolution") or {}
    print(f"backend: {payload.get('backend_id')}  status: {resolution.get('status')}")
    descriptor = resolution.get("descriptor")
    if descriptor:
        regions = ", ".join(descriptor.get("regions") or []) or "-"
        capabilities = ", ".join(descriptor.get("capabilities") or []) or "-"
        print(
            f"  label: {descriptor.get('label')}  regions: {regions}  "
            f"capabilities: {capabilities}"
        )
    if resolution.get("warning_code"):
        print(f"  {resolution['warning_code']} — {resolution.get('hint') or ''}")
    factor = payload.get("factor")
    if factor is not None:
        if "error" in factor:
            print(f"factor: {payload.get('factor_id')}")
            print(f"  error: {factor['error']}")
        else:
            print(
                f"factor: {factor.get('factor_id')} ({factor.get('kind')})  "
                f"horizon_days: {factor.get('horizon_days')}"
            )
            print(f"  formula: {factor.get('formula')}")
            if factor.get("report_artifact_rel"):
                print(f"  report artifact: {factor['report_artifact_rel']}")
            for note in factor.get("notes") or []:
                print(f"  note: {note}")
    translation = payload.get("translation")
    if translation is not None:
        print("translation:")
        if translation.get("supported") is False:
            print("  capability not declared by this backend")
        else:
            print(f"  expression: {translation.get('expression')}")
            settings = translation.get("target_settings") or {}
            if settings:
                print(
                    "  target_settings: "
                    + json.dumps(_json_safe(settings), ensure_ascii=False, sort_keys=True)
                )
            for code in translation.get("warnings") or []:
                print(f"  {code} — {_backend_flow_hint(code, resolution)}")
            for note in translation.get("notes") or []:
                print(f"  note: {note}")
    prescreen = payload.get("prescreen")
    if prescreen is not None:
        print("prescreen:")
        if prescreen.get("supported") is False:
            print("  capability not declared by this backend")
        else:
            print(
                f"  data_region: {prescreen.get('data_region')}  "
                f"target_region: {prescreen.get('target_region')}  "
                f"region_alignment: {prescreen.get('region_alignment')}"
            )
            for code in prescreen.get("warning_codes") or []:
                print(f"  {code} — {_backend_flow_hint(code, resolution)}")
            checks = prescreen.get("checks") or []
            if checks:
                _print_text_table(
                    ["check", "value", "threshold", "status", "passed"],
                    [
                        [
                            str(check.get("name", "")),
                            _format_check_number(check.get("value")),
                            _format_check_number(check.get("threshold")),
                            str(check.get("status", "")),
                            "-" if check.get("passed") is None else str(check["passed"]).lower(),
                        ]
                        for check in checks
                    ],
                )
    for note in payload.get("notes") or []:
        print(f"note: {note}")
    if payload.get("mode") == "dry_run":
        if payload.get("ok"):
            print(
                "mode: dry run (translate + prescreen only); pass --confirm-submit to "
                "request an actual submission"
            )
        return
    print("submission:")
    submission = payload.get("submission")
    if submission is None:
        print("  not attempted (the flow did not reach a submittable state)")
        return
    if "error" in submission:
        print(f"  {submission['error']}")
        return
    simulation = submission.get("simulation")
    if simulation:
        print(f"  simulate backend_ref: {simulation.get('backend_ref')}")
        for code in simulation.get("warnings") or []:
            print(f"  {code} — {_backend_flow_hint(code, resolution)}")
        for note in simulation.get("notes") or []:
            print(f"  note: {note}")
    receipt = submission.get("receipt") or {}
    print(
        f"  status: {receipt.get('status')}  submission_ref: {receipt.get('submission_ref')}"
    )
    for code in receipt.get("warnings") or []:
        print(f"  {code} — {_backend_flow_hint(code, resolution)}")
    for note in receipt.get("notes") or []:
        print(f"  note: {note}")


def _backend_flow_hint(code: str, resolution: dict[str, Any]) -> str:
    return warning_hint(
        code,
        enable_env_var=resolution.get("enable_env_var"),
        module=resolution.get("module"),
    )


def _format_check_number(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.6g}"


def _cmd_runs_list(args: argparse.Namespace) -> int:
    rows = RunIndex(_runtime_paths(args).artifact_root).read_rows()
    rows = _newest_first(rows, args.limit)
    if not rows:
        print("no runs recorded")
        return 0
    _print_runs_table(rows)
    return 0


def _cmd_runs_show(args: argparse.Namespace) -> int:
    row = RunIndex(_runtime_paths(args).artifact_root).find(args.run_id)
    if row is None:
        print(f"run not found: {args.run_id}")
        return 2
    print(f"run_id: {row.get('run_id')}")
    print(f"schema_version: {row.get('schema_version')}")
    print(f"kind: {row.get('kind')}")
    print(f"factor_ids: {', '.join(row.get('factor_ids') or [])}")
    print(f"created_at: {row.get('created_at')}")
    print(f"data_window: {_format_window(row.get('data_window') or {})}")
    print(f"config_fingerprint: {row.get('config_fingerprint')}")
    print(f"warnings_count: {row.get('warnings_count')}")
    print("metric_highlights:")
    highlights = row.get("metric_highlights") or {}
    if not highlights:
        print("  (none recorded)")
    for name in sorted(highlights):
        print(f"  - {name}: {_format_metric_text(highlights[name])}")
    print("artifact_paths_rel:")
    paths_rel = row.get("artifact_paths_rel") or []
    if not paths_rel:
        print("  (none recorded)")
    for path_rel in paths_rel:
        print(f"  - {path_rel}")
    return 0


def _cmd_runs_search(args: argparse.Namespace) -> int:
    index = RunIndex(_runtime_paths(args).artifact_root)
    rows = index.search(factor_id=args.factor, kind=args.kind)
    rows = _newest_first(rows, args.limit)
    if not rows:
        print("no runs matched")
        return 0
    _print_runs_table(rows)
    return 0


def _newest_first(rows: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    ordered = list(reversed(rows))
    if limit is not None and limit >= 0:
        ordered = ordered[:limit]
    return ordered


def _print_runs_table(rows: list[dict[str, Any]]) -> None:
    headers = ["run_id", "kind", "factor_ids", "created_at", "data_window", "warnings", "metric_highlights"]
    table = [
        [
            str(row.get("run_id", "")),
            str(row.get("kind", "")),
            ",".join(row.get("factor_ids") or []),
            str(row.get("created_at", "")),
            _format_window(row.get("data_window") or {}),
            str(row.get("warnings_count", "")),
            _format_highlights(row.get("metric_highlights") or {}),
        ]
        for row in rows
    ]
    _print_text_table(headers, table)


def _print_text_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], len(cell))
    line = "  ".join(header.ljust(widths[column]) for column, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)))


def _format_window(window: dict[str, Any]) -> str:
    status = str(window.get("status") or "unavailable")
    if status == "available":
        return f"{window.get('start_date')} .. {window.get('end_date')} ({status})"
    return f"({status})"


def _format_highlights(highlights: dict[str, Any]) -> str:
    return "; ".join(f"{name}={_format_metric_text(highlights[name])}" for name in sorted(highlights))


def _format_metric_text(entry: dict[str, Any]) -> str:
    value = entry.get("value")
    status = str(entry.get("status") or "")
    if value is None:
        return f"null ({status})"
    return f"{float(value):.4f} ({status})"


# ---------------------------------------------------------------------------
# Research goal handlers (Lane G): immutable goal artifacts + audit log.
# ---------------------------------------------------------------------------


def _cmd_goal_create(args: argparse.Namespace) -> int:
    config = _config(args)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    store = ResearchGoalStore(_runtime_paths_from_config(args, config).artifact_root)
    try:
        goal = store.create_goal(
            objective=args.objective,
            criteria=_goal_criteria(args),
            seed_factor_id=args.seed_factor_id,
            runtime_config_hash=_goal_runtime_config_hash(rd_config),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    except (ValueError, FileExistsError) as exc:
        print(f"goal create failed: {exc}")
        return 2
    _print_json(store.describe(goal.goal_id))
    return 0


def _cmd_goal_list(args: argparse.Namespace) -> int:
    _print_json(ResearchGoalStore(_runtime_paths(args).artifact_root).list_goals())
    return 0


def _cmd_goal_show(args: argparse.Namespace) -> int:
    store = ResearchGoalStore(_runtime_paths(args).artifact_root)
    try:
        payload = store.describe(args.goal_id)
    except FileNotFoundError:
        print(f"goal not found: {args.goal_id}")
        return 2
    _print_json(payload)
    return 0


def _cmd_goal_audit(args: argparse.Namespace) -> int:
    store = ResearchGoalStore(_runtime_paths(args).artifact_root)
    try:
        row = store.append_audit(
            args.goal_id,
            criterion_id=args.criterion,
            result=args.result,
            evidence_refs=_goal_evidence_refs(args, store.artifact_root),
            notes=args.notes,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
    except (ValueError, FileExistsError) as exc:
        print(f"goal audit failed: {exc}")
        return 2
    _print_json(row)
    return 0


def _cmd_goal_complete(args: argparse.Namespace) -> int:
    store = ResearchGoalStore(_runtime_paths(args).artifact_root)
    try:
        row = store.complete_goal(args.goal_id, recorded_at=datetime.now(timezone.utc).isoformat())
    except GoalCompletionError as exc:
        print(f"goal completion blocked: {exc}")
        return 2
    _print_json(row)
    return 0


def _goal_criteria(args: argparse.Namespace) -> tuple[GoalCriterion, ...]:
    entries = [(text, True) for text in args.criteria or []]
    entries.extend((text, False) for text in getattr(args, "optional_criteria", None) or [])
    return tuple(
        GoalCriterion(criterion_id=f"c{index}", text=text, required=required)
        for index, (text, required) in enumerate(entries, start=1)
    )


def _goal_runtime_config_hash(rd_config: ResearchLoopConfig) -> str:
    return canonical_fingerprint(
        {
            "backtest_profile": asdict(rd_config.backtest_profile),
            "evaluation_profile": asdict(rd_config.evaluation_profile),
            "horizon_days_matrix": list(rd_config.horizon_days_matrix),
            "objective": rd_config.objective,
            "sample_splits": [asdict(split) for split in rd_config.sample_splits],
            "transaction_costs": asdict(rd_config.transaction_costs),
        }
    )


def _goal_evidence_refs(args: argparse.Namespace, artifact_root: Path) -> tuple[str, ...]:
    refs: list[str] = []
    for raw in getattr(args, "evidence", None) or []:
        candidate = Path(raw)
        if candidate.is_absolute() or raw.startswith("~"):
            rel = relative_artifact_path(artifact_root, candidate)
            if rel is None:
                raise ValueError(f"evidence path is outside artifact_root: {raw}")
            refs.append(rel)
        else:
            refs.append(raw)
    return tuple(refs)


# ------------------------------ end Lane G ---------------------------------


# ---------------------------------------------------------------------------
# Research memory review handlers (SE-P4a): CLI parity for the SE-iii
# governance surface. Rows are never mutated; every command appends exactly
# one review event under the store's advisory lock.
# ---------------------------------------------------------------------------


def _cmd_memory_rules_list(args: argparse.Namespace) -> int:
    store = ResearchMemoryStore(_runtime_paths(args).artifact_root)
    # Atomic (R2 rework items R2-1/R2-7): ONE snapshot read from the store,
    # never a separately-locked row read layered on a separately-locked
    # event read -- the split-snapshot race that fix closes applies to this
    # listing path exactly as much as it applies to the active_rules
    # steering channel (a promote_pending() landing between two separate
    # reads could otherwise pair a stale activation label with new,
    # unreviewed row content).
    snapshot = store.rule_review_snapshot()
    entries: list[tuple[dict[str, Any], str]] = []
    for info in snapshot.values():
        state = info["state"]
        if args.active and state != "active":
            continue
        if args.pending and state == "active":
            continue
        entries.append((info["row"], state))
    if not entries:
        print("no rules recorded")
        return 0
    entries.sort(key=lambda pair: str(pair[0].get("last_seen") or ""), reverse=True)
    _print_memory_rules_table(entries)
    return 0


def _cmd_memory_priors(args: argparse.Namespace) -> int:
    from quant_forge.research_loop.priors import PRIOR_DIMENSIONS, PriorsQuery, compute_priors

    store = ResearchMemoryStore(_runtime_paths(args).artifact_root)
    dimensions = tuple(args.dimensions) if args.dimensions else PRIOR_DIMENSIONS
    view = compute_priors(store, PriorsQuery(dimensions=dimensions))
    if args.json:
        print(json.dumps(view.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(
        f"priors as_of={view.as_of} envelopes={view.total_envelopes} "
        f"evidence_runs={view.total_evidence_runs} oos_excluded={view.oos_excluded} "
        f"invalid_rows={view.invalid_rows}"
    )
    for table in view.tables:
        print(f"\n[{table.dimension}] (unbucketed: {table.unbucketed})")
        if not table.cells:
            print("  no cells")
            continue
        for cell in table.cells:
            rate = "n/a" if cell.pass_rate is None else f"{cell.pass_rate:.2f}"
            weighted = "n/a" if cell.weighted_pass_rate is None else f"{cell.weighted_pass_rate:.2f}"
            counts = cell.verdict_counts
            reasons = ", ".join(f"{code}x{count}" for code, count in cell.top_blocked_reasons) or "-"
            print(
                f"  {cell.bucket}: runs={cell.evidence_runs} passed={counts.get('passed', 0)} "
                f"blocked={counts.get('blocked', 0)} unknown={counts.get('unknown', 0)} "
                f"not_applicable={counts.get('not_applicable', 0)} "
                f"rate={rate} weighted={weighted}"
                + (" (insufficient_sample)" if cell.insufficient_sample else "")
                + f" | blocked_reasons: {reasons}"
            )
    return 0


def _cmd_memory_rules_activate(args: argparse.Namespace) -> int:
    return _cmd_memory_rule_review(args, target_kind="rule", action="activate")


def _cmd_memory_rules_deactivate(args: argparse.Namespace) -> int:
    return _cmd_memory_rule_review(args, target_kind="rule", action="deactivate")


def _cmd_memory_rules_retire(args: argparse.Namespace) -> int:
    return _cmd_memory_rule_review(args, target_kind=args.target_kind, action="retire")


def _cmd_memory_rules_unretire(args: argparse.Namespace) -> int:
    return _cmd_memory_rule_review(args, target_kind=args.target_kind, action="unretire")


def _cmd_memory_rule_review(args: argparse.Namespace, *, target_kind: str, action: str) -> int:
    store = ResearchMemoryStore(_runtime_paths(args).artifact_root)
    try:
        # Atomic (P4a rework item 8): prefix resolution, row binding, and
        # event append all happen inside ONE lock hold, so two processes
        # racing to review the same prefix cannot interleave between
        # "resolve" and "append" -- see resolve_validate_append's docstring.
        event = store.resolve_validate_append(
            target_kind=target_kind,
            prefix=args.signature_prefix,
            action=action,
            actor=args.actor,
            rationale=args.rationale,
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
    except ValueError as exc:
        print(f"memory rules {action} failed: {exc}")
        return 2
    _print_json({"event_id": event.event_id(), **event.to_dict()})
    return 0


_MEMORY_RULE_STATE_LABELS = {
    "active": "active",
    # P4a rework item 1 + R2-7: a signature that merely reached the rule
    # tier -- in ANY non-active state -- already silences its lower tiers;
    # every label below says so, and additionally names WHY it is not
    # currently steering (never reviewed at all, explicitly deactivated, or
    # reviewed once but the row's content has since changed and needs
    # re-review -- three genuinely different reasons a reviewer would act
    # on differently).
    "never_reviewed": "pending -- lower tiers silenced",
    "deactivated": "deactivated -- lower tiers silenced",
    "lapsed_pending_re_review": "lapsed -- needs re-review (row content changed) -- lower tiers silenced",
}


def _memory_rule_state_label(state: str) -> str:
    return _MEMORY_RULE_STATE_LABELS.get(state, state)


def _print_memory_rules_table(entries: list[tuple[dict[str, Any], str]]) -> None:
    headers = ["signature_prefix", "scope", "statement", "observation_count", "state"]
    table = [
        [
            str(row.get("signature") or "")[:12],
            str(row.get("scope") or "global"),
            str(row.get("statement") or ""),
            # ``.get(key, "")``, not ``... or ""``: observation_count is
            # never legitimately 0 (PromotionDecision requires >= 2), but the
            # `or` form would still misrender a genuine 0 as blank -- matches
            # _print_runs_table's own null-not-falsy convention.
            str(row.get("observation_count", "")),
            _memory_rule_state_label(state),
        ]
        for row, state in entries
    ]
    _print_text_table(headers, table)


# -------------------------- end research memory review ----------------------


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
        factor_values_root=paths.factor_values_root,
        factor_values_overlay_root=paths.factor_values_overlay_root,
        factor_values_manifest_root=paths.factor_values_manifest_root,
        llm_formula_repair_attempts=rd_config.llm.max_formula_repair_attempts,
        strategy_selector_enabled=rd_config.strategy_selector_enabled,
        research_memory_enabled=rd_config.research_memory_enabled,
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
    from quant_forge.apps.web.server import WebControlTokenError, run_local_web

    config = bootstrap_runtime_config(args.config, args.workspace)
    rd_config = load_research_loop_config(args.rd_config, config.research, config.simulation)
    host = args.host or config.web.host
    port = args.port or config.web.port
    try:
        run_local_web(host=host, port=port, config=config, rd_config=rd_config)
    except WebControlTokenError as exc:
        # Predictable startup misconfiguration: the refusal to start is
        # correct and stays; only the presentation changes from a raw
        # traceback to one actionable line. Every other exception type
        # still propagates unchanged -- no blanket catch.
        print(f"qf web startup blocked: {exc}")
        return 2
    return 0


def _doctor_payload(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    config_path = getattr(args, "config", None)
    workspace = getattr(args, "workspace", None)
    rd_config_path = getattr(args, "rd_config", DEFAULT_RD_CONFIG_PATH)
    try:
        config = bootstrap_runtime_config(config_path, workspace)
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
            "simulation_profile": asdict(rd_config.simulation_profile),
            "evaluation_profile": asdict(rd_config.evaluation_profile),
            "backtest_profile": asdict(rd_config.backtest_profile),
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
