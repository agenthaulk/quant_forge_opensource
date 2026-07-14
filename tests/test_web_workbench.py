from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.cli.main as cli_main
import quant_forge.apps.web.server as web_server
import quant_forge.research_loop.llm as rd_llm
from quant_forge.apps.web.server import (
    create_local_web_server,
    run_idea_parse_workflow,
    run_idea_validation_workflow,
    run_idea_workflow,
    run_research_once_workflow,
)
from quant_forge.config import LLMProviderSettings, LLMSettings, PathSettings, QuantForgeConfig, WebSettings
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    HorizonEvaluationMetric,
    MetricValue,
    SimulationProfile,
)
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.llm_client import LLMChatResult
from quant_forge.llm_factor_parser import ParsedFactor
from quant_forge.research_loop.config import (
    ResearchLLMConfig,
    ResearchLoopConfig,
    ResearchParameterSearchConfig,
    load_research_loop_config,
)
from quant_forge.research_loop.service import (
    ResearchCandidateResult,
    ResearchGate,
    ResearchHypothesis,
    ResearchLoopResult,
    ResearchObjectiveWeights,
    ResearchSelfReview,
)


def _write_stub_artifact(path: Path) -> Path:
    """BUG #007: web recording hashes the file at ``artifact_path`` the same
    way the real evaluate_factor/run_factor_backtest always leave one there,
    so seam fakes that only construct a result object must also leave a real
    (if trivial) file at the path they claim.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


# CP6-1 (D8): executable frontend code is delivered as static ES modules
# referenced by the page (script type="module" src="/static/app.js").
# Assertions about JS *delivery* (code text) therefore target the served
# module files; assertions about server-rendered *semantics* (panel ids,
# form fields, option values, Chinese UI text) stay on the page HTML.
def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


def _frontend_js_bundle() -> str:
    modules = sorted(web_server.STATIC_ROOT.rglob("*.js"))
    assert modules, "static frontend modules missing"
    return "\n".join(path.read_text(encoding="utf-8") for path in modules)


def test_web_workbench_rule_workflow_runs_end_to_end(tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_idea_workflow(config, "非ST的小市值股票未来表现更好", parser_mode="rule")

    assert result["parser"]["source"] == "rule"
    assert result["factor"]["formula"] == "-rank(market_cap)"
    assert result["factor"]["universe_filters"] == ["is_st == false"]
    assert result["evaluation"]["observations"] > 0
    assert result["in_sample_backtest"]["sample_role"] == "in_sample_backtest"
    assert result["in_sample_backtest"]["periods"] > 0
    assert result["backtest"]["sample_role"] == "external_oos_backtest"
    assert result["backtest"]["periods"] > 0
    assert "gross_annualized_return" in result["backtest"]
    assert "net_annualized_return" in result["backtest"]
    assert "rebalance_rate" in result["backtest"]
    assert "turnover_rate" in result["backtest"]
    assert {metric["name"] for metric in result["backtest"]["segment_metrics"]} == {"IS", "OOS1", "OOS2"}
    assert result["backtest"]["assumptions"]
    assert paths["factor_root"].exists()


def test_web_parse_workflow_returns_editable_defaults_without_evaluation(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    def fail_evaluate(*args, **kwargs):
        raise AssertionError("parse-only workflow must not evaluate")

    def fail_backtest(*args, **kwargs):
        raise AssertionError("parse-only workflow must not backtest")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    monkeypatch.setattr(web_server, "run_factor_backtest", fail_backtest)

    result = run_idea_parse_workflow(config, "非ST的小市值股票未来表现更好", parser_mode="rule")

    assert result["parser"]["source"] == "rule"
    assert result["factor"]["formula"] == "-rank(market_cap)"
    assert result["parameters"]["holding_days"] == 5
    assert result["parameters"]["execution_delay_days"] == 1
    assert result["parameters"]["decay_days"] == 0
    assert result["parameters"]["top_quantile"] == 0.3
    assert result["parameters"]["evaluation_start"] is None
    assert result["parameters"]["evaluation_end"] is None
    assert result["parameters"]["backtest_start"] is None
    assert result["parameters"]["backtest_end"] is None
    assert result["parameters"]["commission_bps"] == 0.0
    assert result["parameters"]["slippage_bps"] == 0.0
    assert result["parameters"]["short_borrow_bps_annual"] == 0.0
    assert result["parameters"]["evaluation"] == {
        "simulation": {"execution_delay_days": 1, "decay_days": 0, "top_quantile": 0.3},
        "test_period": {"start": None, "end": None},
    }
    assert result["parameters"]["backtest"] == {
        "simulation": {"execution_delay_days": 1, "decay_days": 0, "top_quantile": 0.3},
        "test_period": {"start": None, "end": None},
    }
    assert result["parameters"]["transaction_costs"] == {
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "short_borrow_bps_annual": 0.0,
    }
    with pytest.raises(FileNotFoundError):
        FactorRepository(config.paths.factor_root).get(result["factor"]["factor_id"])


def test_web_parse_payload_surfaces_generic_fallback_warning(tmp_path) -> None:
    # F-010 no-silent-fallback: unrecognized text lands on the generic
    # rank(close) formula; the parse payload must carry the warning so the
    # frontend can never present the fallback as a confident parse. The
    # message text is pinned because it is the user-facing warning contract
    # defined once in specs/nl_flow.py.
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_idea_parse_workflow(config, "今天天气很好，和因子研究无关的一句话。", parser_mode="rule")

    assert result["factor"]["formula"] == "rank(close)"
    assert result["warnings"] == [
        "idea parsed to the generic fallback formula rank(close); the parser may "
        "not have understood the idea - review before running"
    ]


def test_web_parse_payload_warnings_field_is_empty_for_recognized_idea(tmp_path) -> None:
    # The field is always present (empty means no fallback), so the frontend
    # can rely on it without a silent default.
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_idea_parse_workflow(config, "非ST的小市值股票未来表现更好", parser_mode="rule")

    assert result["factor"]["formula"] == "-rank(market_cap)"
    assert result["warnings"] == []


def test_web_parse_workflow_returns_distinct_role_scoped_defaults(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = ResearchLoopConfig(
        evaluation_simulation_profile=SimulationProfile(
            execution_delay_days=1,
            top_quantile=0.1,
            decay_days=0,
            test_period_start="2025-01-01",
        ),
        backtest_simulation_profile=SimulationProfile(
            execution_delay_days=2,
            top_quantile=0.2,
            decay_days=3,
            test_period_start="2026-01-01",
        ),
    )

    result = run_idea_parse_workflow(
        config,
        "非ST的小市值股票未来表现更好",
        parser_mode="rule",
        rd_config=rd_config,
    )

    assert result["parameters"]["evaluation"]["simulation"] == {
        "execution_delay_days": 1,
        "decay_days": 0,
        "top_quantile": 0.1,
    }
    assert result["parameters"]["evaluation"]["test_period"] == {"start": "2025-01-01", "end": None}
    assert result["parameters"]["backtest"]["simulation"] == {
        "execution_delay_days": 2,
        "decay_days": 3,
        "top_quantile": 0.2,
    }
    assert result["parameters"]["backtest"]["test_period"] == {"start": "2026-01-01", "end": None}


def test_web_parse_workflow_ignores_parameter_search_variants_in_editable_defaults(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = ResearchLoopConfig(
        evaluation_simulation_profile=SimulationProfile(
            execution_delay_days=1,
            top_quantile=0.1,
            decay_days=0,
            test_period_start="2025-01-01",
        ),
        backtest_simulation_profile=SimulationProfile(
            execution_delay_days=2,
            top_quantile=0.2,
            decay_days=3,
            test_period_start="2026-01-01",
        ),
        parameter_search=ResearchParameterSearchConfig(
            enabled=True,
            top_quantile=(0.15, 0.35),
            decay_days=(0, 2),
        ),
    )

    result = run_idea_parse_workflow(
        config,
        "非ST的小市值股票未来表现更好",
        parser_mode="rule",
        rd_config=rd_config,
    )

    assert result["parameters"]["evaluation"]["simulation"] == {
        "execution_delay_days": 1,
        "decay_days": 0,
        "top_quantile": 0.1,
    }
    assert result["parameters"]["backtest"]["simulation"] == {
        "execution_delay_days": 2,
        "decay_days": 3,
        "top_quantile": 0.2,
    }
    assert result["parameters"]["top_quantile"] == 0.2


def test_web_validation_workflow_uses_edited_parameters(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    factor = FactorDefinition(
        factor_id="FTR_EDITABLE_PARAMS",
        name="editable_params",
        formula="-rank(market_cap)",
        horizon_days=5,
        universe_filters=("is_st == false",),
        source="llm",
    )
    captured: dict[str, object] = {}

    def fake_evaluate_factor(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        horizon_days,
        horizon_days_matrix,
        sample_splits,
        simulation_profile,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        captured["evaluation_horizon_days"] = horizon_days
        captured["evaluation_profile"] = simulation_profile
        return EvaluationResult(
            factor_id=factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=_write_stub_artifact(Path(artifact_root) / "evaluations" / f"{factor_id}.json"),
            simulation_profile=simulation_profile,
        )

    def fake_run_factor_backtest(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        simulation_profile,
        holding_days,
        transaction_costs,
        sample_splits,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
        sample_role="external_oos_backtest",
        include_partial_final_period=False,
    ):
        captured["backtest_holding_days"] = holding_days
        captured["backtest_profile"] = simulation_profile
        captured["transaction_costs"] = transaction_costs
        captured.setdefault("backtest_sample_roles", []).append(sample_role)
        return BacktestResult(
            factor_id=factor_id,
            periods=1,
            holding_days=holding_days,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=_write_stub_artifact(Path(artifact_root) / "backtests" / f"{factor_id}.json"),
            top_quantile=simulation_profile.top_quantile,
            transaction_costs=transaction_costs,
            simulation_profile=simulation_profile,
        )

    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(web_server, "run_factor_backtest", fake_run_factor_backtest)

    rd_config = ResearchLoopConfig(
        evaluation_simulation_profile=SimulationProfile(
            execution_delay_days=1,
            top_quantile=0.1,
            decay_days=0,
            test_period_start="2025-01-01",
            test_period_end="2025-12-31",
        ),
        backtest_simulation_profile=SimulationProfile(
            execution_delay_days=4,
            top_quantile=0.25,
            decay_days=5,
            test_period_start="2026-01-01",
            test_period_end="2026-12-31",
        ),
    )
    result = run_idea_validation_workflow(
        config,
        factor,
        parser={"source": "llm", "provider": "deepseek", "model": "fake"},
        parameters={
            "holding_days": 21,
            "decay_days": 3,
            "top_quantile": 0.2,
            "execution_delay_days": 2,
            "evaluation_start": "2025-02-01",
            "evaluation_end": "2025-03-31",
            "backtest_start": "2026-02-01",
            "backtest_end": "2026-04-30",
            "commission_bps": 1.5,
            "slippage_bps": 2.5,
            "short_borrow_bps_annual": 30.0,
        },
        rd_config=rd_config,
    )

    evaluation_profile = captured["evaluation_profile"]
    backtest_profile = captured["backtest_profile"]
    costs = captured["transaction_costs"]
    assert captured["evaluation_horizon_days"] == 21
    assert captured["backtest_holding_days"] == 21
    assert captured["backtest_sample_roles"] == ["in_sample_backtest", "external_oos_backtest"]
    assert isinstance(evaluation_profile, SimulationProfile)
    assert evaluation_profile.decay_days == 0
    assert evaluation_profile.top_quantile == 0.1
    assert evaluation_profile.execution_delay_days == 1
    assert evaluation_profile.test_period_start == "2025-02-01"
    assert evaluation_profile.test_period_end == "2025-03-31"
    assert isinstance(backtest_profile, SimulationProfile)
    assert backtest_profile.decay_days == 3
    assert backtest_profile.top_quantile == 0.2
    assert backtest_profile.execution_delay_days == 2
    assert backtest_profile.test_period_start == "2026-02-01"
    assert backtest_profile.test_period_end == "2026-04-30"
    assert costs.commission_bps == 1.5
    assert costs.slippage_bps == 2.5
    assert costs.short_borrow_bps_annual == 30.0
    assert result["factor"]["horizon_days"] == 5
    assert result["backtest"]["holding_days"] == 21
    assert result["parameters"]["holding_days"] == 21
    assert result["parameters"]["top_quantile"] == 0.2
    assert result["parameters"]["evaluation_start"] == "2025-02-01"
    assert result["parameters"]["evaluation_end"] == "2025-03-31"
    assert result["parameters"]["backtest_start"] == "2026-02-01"
    assert result["parameters"]["backtest_end"] == "2026-04-30"
    assert result["parameters"]["evaluation"]["simulation"] == {
        "execution_delay_days": 1,
        "decay_days": 0,
        "top_quantile": 0.1,
    }
    assert result["parameters"]["backtest"]["simulation"] == {
        "execution_delay_days": 2,
        "decay_days": 3,
        "top_quantile": 0.2,
    }
    assert FactorRepository(config.paths.factor_root).get(factor.factor_id).horizon_days == 5


def test_web_validation_settings_accept_role_scoped_profile_overrides() -> None:
    factor = FactorDefinition(
        factor_id="FTR_ROLE_SCOPED_PARAMS",
        name="role_scoped_params",
        formula="-rank(market_cap)",
        horizon_days=5,
    )
    rd_config = ResearchLoopConfig(
        evaluation_simulation_profile=SimulationProfile(
            execution_delay_days=1,
            top_quantile=0.1,
            decay_days=0,
            test_period_start="2025-01-01",
        ),
        backtest_simulation_profile=SimulationProfile(
            execution_delay_days=4,
            top_quantile=0.25,
            decay_days=5,
            test_period_start="2026-01-01",
        ),
    )

    settings = web_server._idea_validation_settings(
        factor,
        {
            "holding_days": 21,
            "decay_days": 9,
            "top_quantile": 0.4,
            "execution_delay_days": 5,
            "commission_bps": 9.0,
            "slippage_bps": 9.0,
            "short_borrow_bps_annual": 90.0,
            "evaluation": {
                "simulation": {"execution_delay_days": 2, "decay_days": 1, "top_quantile": 0.15},
                "test_period": {"start": "2025-02-01", "end": "2025-03-31"},
            },
            "backtest": {
                "simulation": {"execution_delay_days": 3, "decay_days": 2, "top_quantile": 0.2},
                "test_period": {"start": "2026-02-01", "end": "2026-04-30"},
            },
            "transaction_costs": {
                "commission_bps": 1.5,
                "slippage_bps": 2.5,
                "short_borrow_bps_annual": 30.0,
            },
        },
        rd_config,
    )

    assert settings.holding_days == 21
    assert settings.evaluation_profile.execution_delay_days == 2
    assert settings.evaluation_profile.decay_days == 1
    assert settings.evaluation_profile.top_quantile == 0.15
    assert settings.evaluation_profile.test_period_start == "2025-02-01"
    assert settings.evaluation_profile.test_period_end == "2025-03-31"
    assert settings.backtest_profile.execution_delay_days == 3
    assert settings.backtest_profile.decay_days == 2
    assert settings.backtest_profile.top_quantile == 0.2
    assert settings.backtest_profile.test_period_start == "2026-02-01"
    assert settings.backtest_profile.test_period_end == "2026-04-30"
    assert settings.transaction_costs.commission_bps == 1.5
    assert settings.transaction_costs.slippage_bps == 2.5
    assert settings.transaction_costs.short_borrow_bps_annual == 30.0
    assert settings.parameters["evaluation"]["simulation"]["top_quantile"] == 0.15
    assert settings.parameters["backtest"]["simulation"]["top_quantile"] == 0.2


def test_web_validation_rejects_invalid_parameters_before_evaluation(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    factor = FactorDefinition(
        factor_id="FTR_INVALID_EDITABLE_PARAMS",
        name="invalid_editable_params",
        formula="-rank(market_cap)",
        horizon_days=5,
    )

    def fail_evaluate(*args, **kwargs):
        raise AssertionError("invalid validation parameters must not evaluate")

    def fail_backtest(*args, **kwargs):
        raise AssertionError("invalid validation parameters must not backtest")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    monkeypatch.setattr(web_server, "run_factor_backtest", fail_backtest)

    with pytest.raises(ValueError, match="top_quantile"):
        run_idea_validation_workflow(config, factor, parameters={"top_quantile": 0.8})

    with pytest.raises(ValueError, match="evaluation_start"):
        run_idea_validation_workflow(config, factor, parameters={"evaluation_start": "2025/01/01"})

    with pytest.raises(ValueError, match="test_period_start"):
        run_idea_validation_workflow(
            config,
            factor,
            parameters={"backtest_start": "2026-04-30", "backtest_end": "2026-01-01"},
        )

    with pytest.raises(ValueError, match="evaluation\\.simulation"):
        run_idea_validation_workflow(config, factor, parameters={"evaluation": {"simulation": "bad"}})

    with pytest.raises(ValueError, match="transaction_costs"):
        run_idea_validation_workflow(config, factor, parameters={"transaction_costs": "bad"})

    with pytest.raises(FileNotFoundError):
        FactorRepository(config.paths.factor_root).get(factor.factor_id)


def test_web_run_idea_workflow_preserves_distinct_default_profiles(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    captured: dict[str, object] = {}

    def fake_parse_factor_idea(text, llm, *, mode):
        factor = FactorDefinition(
            factor_id="FTR_COMPAT_PROFILE",
            name="compat_profile",
            formula="-rank(market_cap)",
            horizon_days=9,
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider="rule", model="deterministic")

    def fake_evaluate_factor(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        horizon_days,
        horizon_days_matrix,
        sample_splits,
        simulation_profile,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        captured["evaluation_profile"] = simulation_profile
        return EvaluationResult(
            factor_id=factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=_write_stub_artifact(Path(artifact_root) / "evaluations" / f"{factor_id}.json"),
            simulation_profile=simulation_profile,
        )

    def fake_run_factor_backtest(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        simulation_profile,
        holding_days,
        transaction_costs,
        sample_splits,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
        sample_role="external_oos_backtest",
        include_partial_final_period=False,
    ):
        captured["backtest_profile"] = simulation_profile
        captured.setdefault("backtest_sample_roles", []).append(sample_role)
        return BacktestResult(
            factor_id=factor_id,
            periods=1,
            holding_days=holding_days,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=_write_stub_artifact(Path(artifact_root) / "backtests" / f"{factor_id}.json"),
            top_quantile=simulation_profile.top_quantile,
            simulation_profile=simulation_profile,
        )

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)
    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(web_server, "run_factor_backtest", fake_run_factor_backtest)

    rd_config = ResearchLoopConfig(
        evaluation_simulation_profile=SimulationProfile(
            execution_delay_days=1,
            top_quantile=0.1,
            decay_days=0,
            test_period_start="2025-01-01",
        ),
        backtest_simulation_profile=SimulationProfile(
            execution_delay_days=2,
            top_quantile=0.2,
            decay_days=3,
            test_period_start="2026-01-01",
        ),
    )

    result = run_idea_workflow(config, "小市值", parser_mode="rule", rd_config=rd_config)

    evaluation_profile = captured["evaluation_profile"]
    backtest_profile = captured["backtest_profile"]
    assert isinstance(evaluation_profile, SimulationProfile)
    assert evaluation_profile.execution_delay_days == 1
    assert evaluation_profile.top_quantile == 0.1
    assert evaluation_profile.decay_days == 0
    assert evaluation_profile.test_period_start == "2025-01-01"
    assert isinstance(backtest_profile, SimulationProfile)
    assert backtest_profile.execution_delay_days == 2
    assert backtest_profile.top_quantile == 0.2
    assert backtest_profile.decay_days == 3
    assert backtest_profile.test_period_start == "2026-01-01"
    assert result["parameters"]["holding_days"] == 9
    assert captured["backtest_sample_roles"] == ["in_sample_backtest", "external_oos_backtest"]
    assert result["parameters"]["evaluation_start"] == "2025-01-01"
    assert result["parameters"]["backtest_start"] == "2026-01-01"
    assert result["backtest"]["holding_days"] == 9


def test_web_research_once_workflow_runs_end_to_end(tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        rd_config=_local_rd_config(config),
    )

    assert result["rd_stage"] == "research"
    assert result["workflow_type"] == "research"
    assert result["seed_factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert result["candidates"][0]["factor"]["formula"] != "-rank(market_cap)"
    assert result["candidates"][0]["factor"]["formula"] in {"rank(return_5d)", "-rank(volatility_5d)"}
    assert result["candidates"][0]["factor"]["status"] == "candidate"
    assert result["accepted_candidate_ids"]
    assert result["comparison_rows"]
    assert result["comparison_rows"][0]["role"] == "seed"
    assert {row["role"] for row in result["comparison_rows"]} == {"seed", "candidate"}
    assert result["iteration_chain"]["rounds"][0]["comparison_rows"]
    backtest = result["candidates"][0]["backtest"]
    assert "net_annualized_return" in backtest
    assert "rebalance_rate" in backtest
    assert "turnover_rate" in backtest
    assert backtest["segment_metrics"]
    assert Path(result["report_path"]).exists()
    assert paths["factor_root"].exists()


def test_web_research_workflow_can_chain_iterations(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_run_research_once(
        config,
        rd_config,
        seed_factor_id,
        *,
        objective,
        max_candidates,
        cancel_event=None,
    ):
        calls.append(seed_factor_id)
        candidate_id = f"{seed_factor_id}_NEXT"
        return _fake_research_result(
            seed_factor_id=seed_factor_id,
            candidate_id=candidate_id,
            report_path=tmp_path / f"{seed_factor_id}.md",
        )

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    result = run_research_once_workflow(
        config,
        "FTR_SEED",
        max_candidates=1,
        iterations=2,
        rd_config=ResearchLoopConfig(),
    )

    assert calls == ["FTR_SEED", "FTR_SEED_NEXT"]
    assert result["seed_factor_id"] == "FTR_SEED"
    assert result["original_seed_factor_id"] == "FTR_SEED"
    assert result["last_round_seed_factor_id"] == "FTR_SEED_NEXT"
    assert result["iteration_count"] == 2
    assert result["requested_iterations"] == 2
    assert result["final_factor_id"] == "FTR_SEED_NEXT_NEXT"
    assert result["recommended_factor_id"] == "FTR_SEED_NEXT_NEXT"
    assert result["recommendation_basis"] == "accepted_candidate"
    assert result["last_accepted_factor_id"] == "FTR_SEED_NEXT_NEXT"
    assert result["last_explored_factor_id"] == "FTR_SEED_NEXT_NEXT"
    assert result["next_exploration_seed_factor_id"] == "FTR_SEED_NEXT_NEXT"
    assert result["next_exploration_seed_reason"] == "accepted_candidate"
    assert result["next_exploration_seed_gate_passed"] is True
    assert result["iteration_chain"]["rounds"][0]["selected_next_seed_factor_id"] == "FTR_SEED_NEXT"
    assert result["iteration_chain"]["rounds"][1]["seed_factor_id"] == "FTR_SEED_NEXT"
    assert result["accepted_candidate_ids"] == ["FTR_SEED_NEXT", "FTR_SEED_NEXT_NEXT"]
    assert result["optimization_status"] == "performed"
    assert result["optimization_performed"] is True
    assert result["no_optimization_performed"] is False
    assert result["chain_optimization_status"] == "performed"
    assert result["chain_optimization_performed"] is True
    assert result["chain_no_optimization_performed"] is False


def test_web_research_workflow_keeps_recommendation_on_last_accepted_candidate(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_run_research_once(
        config,
        rd_config,
        seed_factor_id,
        *,
        objective,
        max_candidates,
        cancel_event=None,
    ):
        calls.append(seed_factor_id)
        if seed_factor_id == "FTR_SEED":
            return _fake_research_result(
                seed_factor_id=seed_factor_id,
                candidate_id="FTR_ACCEPTED",
                accepted=True,
                report_path=tmp_path / f"{seed_factor_id}.md",
            )
        return _fake_research_result(
            seed_factor_id=seed_factor_id,
            candidate_id="FTR_REJECTED",
            accepted=False,
            report_path=tmp_path / f"{seed_factor_id}.md",
        )

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    result = run_research_once_workflow(
        config,
        "FTR_SEED",
        max_candidates=1,
        iterations=2,
        rd_config=ResearchLoopConfig(),
    )

    assert calls == ["FTR_SEED", "FTR_ACCEPTED"]
    assert result["seed_factor_id"] == "FTR_SEED"
    assert result["last_round_seed_factor_id"] == "FTR_ACCEPTED"
    assert result["accepted_candidate_ids"] == ["FTR_ACCEPTED"]
    assert result["last_accepted_factor_id"] == "FTR_ACCEPTED"
    assert result["last_explored_factor_id"] == "FTR_REJECTED"
    assert result["recommended_factor_id"] == "FTR_ACCEPTED"
    assert result["recommendation_basis"] == "accepted_candidate"
    assert result["final_factor_id"] == "FTR_ACCEPTED"
    assert result["iteration_chain"]["last_accepted_factor_id"] == "FTR_ACCEPTED"
    assert result["iteration_chain"]["last_explored_factor_id"] == "FTR_REJECTED"
    assert result["iteration_chain"]["recommended_factor_id"] == "FTR_ACCEPTED"
    assert result["iteration_chain"]["recommendation_basis"] == "accepted_candidate"
    assert result["next_exploration_seed_factor_id"] == "FTR_REJECTED"
    assert result["next_exploration_seed_reason"] == "fallback_best_score"
    assert result["next_exploration_seed_gate_passed"] is False
    assert result["optimization_status"] == "performed"
    assert result["optimization_performed"] is True
    assert result["no_optimization_performed"] is False
    assert result["iteration_chain"]["rounds"][0]["selection_reason"] == "accepted_candidate"
    assert result["iteration_chain"]["rounds"][0]["accepted_candidate_ids"] == ["FTR_ACCEPTED"]
    assert result["iteration_chain"]["rounds"][1]["seed_factor_id"] == "FTR_ACCEPTED"
    assert result["iteration_chain"]["rounds"][1]["selection_reason"] == "fallback_best_score"
    assert result["iteration_chain"]["rounds"][1]["accepted_candidate_ids"] == []
    assert result["iteration_chain"]["rounds"][1]["top_candidate_id"] == "FTR_REJECTED"
    assert result["iteration_chain"]["rounds"][1]["selected_next_seed_factor_id"] == "FTR_REJECTED"
    assert result["final_factor_id"] != result["iteration_chain"]["rounds"][1]["selected_next_seed_factor_id"]


def test_web_research_workflow_marks_attempted_chain_without_acceptance(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_run_research_once(
        config,
        rd_config,
        seed_factor_id,
        *,
        objective,
        max_candidates,
        cancel_event=None,
    ):
        calls.append(seed_factor_id)
        if seed_factor_id == "FTR_SEED":
            return _fake_research_result(
                seed_factor_id=seed_factor_id,
                candidate_id="FTR_SEED_NEXT",
                accepted=False,
                report_path=tmp_path / f"{seed_factor_id}.md",
            )
        return _empty_research_result(seed_factor_id, report_path=tmp_path / f"{seed_factor_id}.md")

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    result = run_research_once_workflow(
        config,
        "FTR_SEED",
        max_candidates=1,
        iterations=2,
        rd_config=ResearchLoopConfig(),
    )

    assert calls == ["FTR_SEED", "FTR_SEED_NEXT"]
    assert result["optimization_status"] == "attempted_no_acceptance"
    assert result["chain_candidate_generation_performed"] is True
    assert result["optimization_performed"] is False
    assert result["no_optimization_performed"] is False
    assert result["iteration_chain"]["optimization_status"] == "attempted_no_acceptance"
    assert result["iteration_chain"]["chain_no_optimization_performed"] is False
    assert result["iteration_chain"]["rounds"][0]["selection_reason"] == "fallback_best_score"
    assert result["iteration_chain"]["rounds"][1]["selection_reason"] == "no_candidates"
    assert result["accepted_candidate_ids"] == []
    assert result["last_accepted_factor_id"] == ""
    assert result["last_explored_factor_id"] == "FTR_SEED_NEXT"
    assert result["recommended_factor_id"] == "FTR_SEED"
    assert result["recommendation_basis"] == "original_seed_retained"
    assert result["final_factor_id"] == "FTR_SEED"
    assert result["next_exploration_seed_factor_id"] == ""
    assert result["next_exploration_seed_reason"] == "no_candidates"
    assert result["next_exploration_seed_gate_passed"] is None
    assert [str(path) for path in result["round_report_paths"]] == [
        str(tmp_path / "FTR_SEED.md"),
        str(tmp_path / "FTR_SEED_NEXT.md"),
    ]


def test_web_research_workflow_keeps_completed_rounds_when_later_round_fails(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_run_research_once(
        config,
        rd_config,
        seed_factor_id,
        *,
        objective,
        max_candidates,
        cancel_event=None,
    ):
        calls.append(seed_factor_id)
        if len(calls) == 1:
            return _fake_research_result(
                seed_factor_id=seed_factor_id,
                candidate_id="FTR_FIRST",
                accepted=False,
                report_path=tmp_path / f"{seed_factor_id}.md",
            )
        raise RuntimeError("LLM request timed out")

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    result = run_research_once_workflow(
        config,
        "FTR_SEED",
        max_candidates=1,
        iterations=3,
        rd_config=ResearchLoopConfig(),
    )

    assert calls == ["FTR_SEED", "FTR_FIRST"]
    assert result["iteration_count"] == 1
    assert result["requested_iterations"] == 3
    assert result["failed_round_index"] == 2
    assert result["partial_result"] is True
    assert result["chain_error"] == "LLM request timed out"
    assert result["stopped_reason"] == "iteration_failed"
    assert result["iteration_chain"]["failed_round_index"] == 2
    assert result["iteration_chain"]["chain_error"] == "LLM request timed out"
    assert result["iteration_chain"]["rounds"][0]["top_candidate_id"] == "FTR_FIRST"
    assert result["candidates"][0]["factor"]["factor_id"] == "FTR_FIRST"


def test_web_research_workflow_marks_pure_no_optimization(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    def fake_run_research_once(
        config,
        rd_config,
        seed_factor_id,
        *,
        objective,
        max_candidates,
        cancel_event=None,
    ):
        return _empty_research_result(seed_factor_id, report_path=tmp_path / f"{seed_factor_id}.md")

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    result = run_research_once_workflow(
        config,
        "FTR_SEED",
        max_candidates=1,
        iterations=3,
        rd_config=ResearchLoopConfig(),
    )

    assert result["iteration_count"] == 1
    assert result["stopped_reason"] == "no_candidates"
    assert result["optimization_status"] == "no_optimization_performed"
    assert result["chain_candidate_generation_performed"] is False
    assert result["optimization_performed"] is False
    assert result["no_optimization_performed"] is True
    assert result["chain_no_optimization_performed"] is True


def test_web_research_workflow_stops_chain_without_new_seed(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_run_research_once(
        config,
        rd_config,
        seed_factor_id,
        *,
        objective,
        max_candidates,
        cancel_event=None,
    ):
        calls.append(seed_factor_id)
        return _fake_research_result(
            seed_factor_id=seed_factor_id,
            candidate_id=seed_factor_id,
            accepted=False,
            report_path=tmp_path / f"{seed_factor_id}.md",
        )

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    result = run_research_once_workflow(
        config,
        "FTR_SEED",
        max_candidates=1,
        iterations=3,
        rd_config=ResearchLoopConfig(),
    )

    assert calls == ["FTR_SEED"]
    assert result["iteration_count"] == 1
    assert result["requested_iterations"] == 3
    assert result["stopped_reason"] == "no_new_seed"


def test_web_research_workflow_rejects_invalid_iteration_count(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    with pytest.raises(ValueError, match="iterations"):
        run_research_once_workflow(
            config,
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            iterations=0,
            rd_config=ResearchLoopConfig(),
        )

    with pytest.raises(ValueError, match="iterations"):
        run_research_once_workflow(
            config,
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            iterations=1.5,
            rd_config=ResearchLoopConfig(),
        )

    with pytest.raises(ValueError, match="iterations"):
        run_research_once_workflow(
            config,
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            iterations="1.5",
            rd_config=ResearchLoopConfig(),
        )

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        iterations=2.0,
        rd_config=ResearchLoopConfig(),
    )
    assert result["requested_iterations"] == 2

    with pytest.raises(ValueError, match="iterations"):
        run_research_once_workflow(
            config,
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            iterations=web_server.MAX_RD_ITERATIONS + 1,
            rd_config=ResearchLoopConfig(),
        )


def test_web_research_once_rejects_invalid_explicit_candidate_count(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    with pytest.raises(ValueError, match="max_candidates must be between 1 and 10"):
        run_research_once_workflow(config, "FTR_DEMO_SMALL_CAP", max_candidates=0, rd_config=_local_rd_config(config))


def test_web_html_uses_default_rd_config_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_DEEPSEEK_KEY", "set")
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/v1",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")
    rd_path = tmp_path / "rd.yaml"
    rd_path.write_text(
        """
objective: rank_icir
default_max_candidates: 2
default_interval_days: 5
allowed_interval_days: [5]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_server, "DEFAULT_RD_CONFIG_PATH", rd_path)
    html = web_server._index_html(config)
    # CP6-1 (D8): JS is delivered through the served static modules; the page
    # must reference the entry module, and the code-text assertions below
    # target the module bundle instead of inline page script.
    bundle = _frontend_js_bundle()

    assert '<script type="module" src="/static/app.js"></script>' in html
    assert "/api/jobs/research-run-once" in bundle
    assert "/api/jobs/parse-idea" in bundle
    assert "/api/jobs/validate-idea" in bundle
    assert "/api/jobs/staggered-entry" in bundle
    assert "/api/research/campaign" not in html
    assert "/api/research/campaign" not in bundle
    assert "/api/research/schedule" in bundle
    assert "解析因子" in html
    assert "验证并评测" in html
    assert "首月逐日建仓稳健性回测" in html
    assert "param-holding-days" in html
    assert "param-decay-days" in html
    assert "param-top-quantile" in html
    assert "param-evaluation-start" in html
    assert "param-evaluation-end" in html
    assert "param-backtest-start" in html
    assert "param-backtest-end" in html
    assert "llm-api-key-mode" in html
    assert "llm-api-key" in html
    assert 'data-secret-policy="not-submitted"' in html
    assert "手动输入不会保存或提交" in bundle
    assert "中断本次运行" in html
    assert "中断本次RD" in html
    assert "function clearGlobalError" in bundle
    assert "function resetRdResult" in bundle
    assert "function optimizationStatusText" in bundle
    assert "function valueOr" in bundle
    assert "function parserDefaultParameterMessage" in bundle
    assert "function assumptionLabel" in bundle
    assert "解析器已生成默认评测参数" in bundle
    assert "调仓率 = 相邻调仓的成分替换率" in bundle
    assert "换手率 = 基于组合权重变化估算的真实换手率" in bundle
    assert "attempted_no_acceptance" in bundle
    assert "recommended factor" in bundle
    assert "last accepted" in bundle
    assert "last explored" in bundle
    assert "next exploration seed" in bundle
    assert "未过 gate，仅用于探索" in bundle
    assert "round reports" in bundle
    assert "aggregateAccepted" in bundle
    assert "join(' ')" in bundle
    assert "RD 已中断" in bundle
    assert "本次 RD 已取消，未产生新的候选结果" in bundle
    assert "已运行超过10秒" in bundle
    assert "LLM 语义解析" in html
    assert "本地规则解析" in html
    assert "是否改用本地规则解析" in bundle
    assert "rd-objective" in html
    assert "rd-iterations" in html
    assert "RD迭代次数" in html
    assert f'max="{web_server.MAX_RD_ITERATIONS}" step="1"' in html
    assert "rd-campaign-seeds" not in html
    assert "rd-campaign" not in html
    assert "RD Campaign" not in html
    assert "rd-campaign" not in bundle
    assert "RD Campaign" not in bundle
    assert 'value="5"' in html
    assert 'value="2"' in html
    assert '<option value="rank_icir" selected>ICIR</option>' in html
    assert '<option value="balanced" selected>' not in html
    assert '<option value="5" selected>5天</option>' in html
    assert "LLM parser: deepseek / fake-deepseek" in html
    assert "RD optimizer: research local deterministic" in html
    assert '<option value="deepseek" selected>deepseek / fake-deepseek · env QF_TEST_DEEPSEEK_KEY</option>' in html
    assert '<option value="glm">glm / fake-glm · env QF_TEST_GLM_KEY</option>' in html
    assert "QF_TEST_DEEPSEEK_KEY" in html
    assert ">set<" not in html
    assert "毛年化收益" in bundle
    assert "净年化收益" in bundle
    assert "HAC t-stat" in bundle
    assert "完整持有期数" in bundle
    assert "numIfStable" in bundle
    assert "pctIfStable" in bundle
    assert "调仓率" in bundle
    assert "换手率" in bundle
    assert "风险提示" in bundle
    assert "口径说明" in bundle
    assert "研究口径，不是生产交易口径" in bundle
    assert "RD 因子迭代对比" in bundle
    assert "function comparisonRows" in bundle
    assert "function renderComparisonTable" in bundle
    assert "external_oos_net_cumulative_return" in bundle


def test_web_html_contract_clears_stale_errors_before_new_submissions() -> None:
    # CP6-1 (D8): the click handlers moved verbatim into the served entry
    # module; the clear-before-submit ordering contract is asserted there.
    app_js = _static_module_text("app.js")

    parse_click = app_js.index("button.addEventListener('click', async () => {")
    parse_clear = app_js.index("clearGlobalError();", parse_click)
    parse_submit = app_js.index("const payload = await submitParse(parserMode);", parse_click)
    validate_click = app_js.index("validateButton.addEventListener('click', async () => {")
    validate_clear = app_js.index("clearGlobalError();", validate_click)
    validate_submit = app_js.index("const payload = await submitValidation();", validate_click)
    rd_run_click = app_js.index("rdRun.addEventListener('click', async () => {")
    rd_run_clear = app_js.index("clearGlobalError();", rd_run_click)
    rd_run_submit = app_js.index("const job = await postJson('/api/jobs/research-run-once', rdPayload());", rd_run_click)

    assert parse_clear < parse_submit
    assert validate_clear < validate_submit
    assert rd_run_clear < rd_run_submit


def test_web_html_contract_keeps_aggregate_status_fallback_for_single_round_payloads() -> None:
    # CP6-1 (D8): the aggregate-status fallback moved verbatim into the
    # research view module.
    research_js = _static_module_text("views/research.js")

    assert "const status = payload.optimization_status || (payload.optimization_performed ? 'performed' : 'no_optimization_performed');" in research_js


def test_web_research_scheduler_http_start_stop_and_validation(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/research/schedule",
            {
                "action": "start",
                "seed_factor_id": "FTR_DEMO_SMALL_CAP",
                "objective": "balanced",
                "interval_days": 1,
                "max_candidates": 1,
                "iterations": 2,
            },
        )
        assert started["enabled"] is True
        assert started["run_count"] == 1
        assert started["request"]["iterations"] == 2
        assert started["last_result"]["requested_iterations"] == 2
        assert started["last_result"]["optimization_status"] == "performed"
        assert started["last_result"]["chain_optimization_performed"] is True
        assert started["last_result"]["iteration_chain"]["rounds"][0]["accepted_candidate_ids"]
        assert Path(started["last_result"]["report_path"]).exists()

        stopped = _post_json(f"{base_url}/api/research/schedule", {"action": "stop"})
        assert stopped["enabled"] is False
        assert stopped["run_count"] == 1

        try:
            _post_json(
                f"{base_url}/api/research/schedule",
                {
                    "action": "start",
                    "seed_factor_id": "FTR_DEMO_SMALL_CAP",
                    "objective": "balanced",
                    "interval_days": 2,
                    "max_candidates": 1,
                },
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode("utf-8"))
            assert "interval_days must be one of" in body["error"]
        else:
            raise AssertionError("invalid interval should return HTTP 400")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_research_unknown_endpoint_returns_404(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        try:
            _post_json(
                f"{base_url}/api/research/not-supported",
                {"seed_factor_id": "FTR_DEMO_SMALL_CAP"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            body = json.loads(exc.read().decode("utf-8"))
            assert "unknown endpoint" in body["error"]
        else:
            raise AssertionError("unknown endpoint should return HTTP 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_run_idea_endpoint_completes(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/jobs/run-idea",
            {"text": "非ST的小市值股票未来表现更好", "parser_mode": "rule"},
        )
        assert started["kind"] == "run_idea"
        assert started["status"] in {"running", "completed"}

        completed = _wait_for_job(base_url, started["job_id"])

        assert completed["status"] == "completed"
        assert completed["result"]["factor"]["formula"] == "-rank(market_cap)"
        assert completed["result"]["evaluation"]["observations"] > 0
        assert completed["result"]["backtest"]["periods"] > 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_parse_then_validate_endpoint_completes(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started_parse = _post_json(
            f"{base_url}/api/jobs/parse-idea",
            {"text": "非ST的小市值股票未来表现更好", "parser_mode": "rule"},
        )
        parsed = _wait_for_job(base_url, started_parse["job_id"])

        assert parsed["status"] == "completed"
        assert "evaluation" not in parsed["result"]
        assert parsed["result"]["parameters"]["holding_days"] == 5

        parameters = dict(parsed["result"]["parameters"])
        parameters["holding_days"] = 7
        parameters["backtest"]["simulation"]["top_quantile"] = 0.2
        started_validate = _post_json(
            f"{base_url}/api/jobs/validate-idea",
            {
                "factor": parsed["result"]["factor"],
                "parser": parsed["result"]["parser"],
                "parameters": parameters,
            },
        )
        completed = _wait_for_job(base_url, started_validate["job_id"])

        assert completed["status"] == "completed"
        assert completed["result"]["factor"]["horizon_days"] == 5
        assert completed["result"]["backtest"]["holding_days"] == 7
        assert completed["result"]["parameters"]["holding_days"] == 7
        assert completed["result"]["backtest"]["top_quantile"] == 0.2
        assert completed["result"]["parameters"]["top_quantile"] == 0.2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_research_run_once_endpoint_completes(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/jobs/research-run-once",
            {"seed_factor_id": "FTR_DEMO_SMALL_CAP", "objective": "balanced", "max_candidates": 1},
        )
        completed = _wait_for_job(base_url, started["job_id"])

        assert completed["status"] == "completed"
        assert completed["result"]["seed_factor_id"] == "FTR_DEMO_SMALL_CAP"
        assert completed["result"]["candidates"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_staggered_entry_endpoint_completes(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/jobs/staggered-entry",
            {
                "factor_id": "FTR_DEMO_SMALL_CAP",
                "parameters": {
                    "holding_days": 21,
                    "backtest": {"simulation": {"top_quantile": 0.3}},
                },
                "formation_trading_days": 5,
            },
        )
        completed = _wait_for_job(base_url, started["job_id"])

        assert completed["status"] == "completed"
        assert completed["result"]["sample_role"] == "staggered_entry_backtest"
        assert completed["result"]["factor_id"] == "FTR_DEMO_SMALL_CAP"
        assert completed["result"]["cohort_count"] == 5
        assert completed["result"]["daily_nav"]
        assert completed["result"]["artifact_path"].endswith(".json")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_research_endpoint_passes_iteration_count(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    captured: dict[str, int | str | None] = {}

    def fake_research_workflow(
        config,
        seed_factor_id,
        *,
        objective=None,
        max_candidates=None,
        iterations=None,
        rd_config=None,
        cancel_event=None,
    ):
        captured["seed_factor_id"] = seed_factor_id
        captured["objective"] = objective
        captured["max_candidates"] = max_candidates
        captured["iterations"] = iterations
        return {
            "rd_stage": "research",
            "workflow_type": "research",
            "seed_factor_id": seed_factor_id,
            "objective": objective,
            "candidates": [],
            "accepted_candidate_ids": [],
            "requested_iterations": iterations,
            "iteration_count": iterations,
            "iteration_chain": {"rounds": []},
        }

    monkeypatch.setattr(web_server, "run_research_once_workflow", fake_research_workflow)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/jobs/research-run-once",
            {"seed_factor_id": "FTR_DEMO_SMALL_CAP", "objective": "balanced", "max_candidates": 2, "iterations": 3},
        )
        completed = _wait_for_job(base_url, started["job_id"])

        assert completed["status"] == "completed"
        assert captured == {
            "seed_factor_id": "FTR_DEMO_SMALL_CAP",
            "objective": "balanced",
            "max_candidates": 2,
            "iterations": 3,
        }
        assert completed["result"]["requested_iterations"] == 3

        started = _post_json(
            f"{base_url}/api/jobs/research-run-once",
            {"seed_factor_id": "FTR_DEMO_SMALL_CAP", "objective": "balanced", "max_candidates": 2, "iterations": 1.5},
        )
        completed = _wait_for_job(base_url, started["job_id"])
        assert completed["status"] == "failed"
        assert "iterations must be an integer" in completed["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_http_cancel_endpoint_cancels_running_job(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    def slow_workflow(config, text, *, parser_mode="llm", llm_provider=None, rd_config=None, cancel_event=None):
        while cancel_event is not None and not cancel_event.is_set():
            time.sleep(0.005)
        raise web_server._WebJobCancelled("cancelled for test")

    monkeypatch.setattr(web_server, "run_idea_workflow", slow_workflow)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/jobs/run-idea",
            {"text": "非ST的小市值股票未来表现更好", "parser_mode": "rule"},
        )
        requested = _post_json(f"{base_url}/api/jobs/{started['job_id']}/cancel", {})
        assert requested["status"] in {"cancel_requested", "cancelled"}

        final = _wait_for_job(base_url, started["job_id"])
        assert final["status"] == "cancelled"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_job_manager_cancel_requests_running_job() -> None:
    manager = web_server._WebJobManager(slow_after_seconds=0.01)

    def wait_for_cancel(cancel_event):
        while not cancel_event.is_set():
            time.sleep(0.005)
        raise web_server._WebJobCancelled("cancelled for test")

    started = manager.start("test", wait_for_cancel)
    cancelled = manager.cancel(started["job_id"])

    assert cancelled["status"] in {"cancel_requested", "cancelled"}
    final = _wait_for_manager_job(manager, started["job_id"])
    assert final["status"] == "cancelled"


def test_web_job_manager_rejects_same_kind_until_terminal() -> None:
    manager = web_server._WebJobManager(slow_after_seconds=0.01)
    release = threading.Event()

    def wait(cancel_event):
        release.wait(timeout=1)
        return {"ok": True}

    started = manager.start("test", wait)
    with pytest.raises(ValueError, match="test job already running"):
        manager.start("test", lambda cancel_event: {"ok": False})

    release.set()
    final = _wait_for_manager_job(manager, started["job_id"])
    assert final["status"] == "completed"


def test_web_job_manager_reports_slow_running_state() -> None:
    manager = web_server._WebJobManager(slow_after_seconds=0.0)
    release = threading.Event()

    def wait(cancel_event):
        release.wait(timeout=1)
        return {"ok": True}

    started = manager.start("test", wait)
    running = manager.get(started["job_id"])
    assert running["slow"] is True
    assert running["message"] == "system is still running"

    release.set()
    completed = _wait_for_manager_job(manager, started["job_id"])
    assert completed["status"] == "completed"
    assert completed["slow"] is False
    assert completed["message"] == ""


def test_web_job_manager_freezes_terminal_runtime() -> None:
    manager = web_server._WebJobManager(slow_after_seconds=0.01)
    completed = _wait_for_manager_job(manager, manager.start("test", lambda cancel_event: {"ok": True})["job_id"])
    runtime = completed["runtime_seconds"]

    time.sleep(0.02)
    later = manager.get(completed["job_id"])

    assert later["status"] == "completed"
    assert later["runtime_seconds"] == runtime


def test_web_job_manager_fails_terminal_when_result_publication_fails(monkeypatch) -> None:
    manager = web_server._WebJobManager(slow_after_seconds=0.0)

    def fail_public_json(value):
        raise TypeError("bad result payload")

    monkeypatch.setattr(web_server, "_web_public_json", fail_public_json)

    started = manager.start("test", lambda cancel_event: {"ok": True})
    failed = _wait_for_manager_job(manager, started["job_id"])

    assert failed["status"] == "failed"
    assert failed["error"] == "job result serialization failed"
    assert failed["slow"] is False


def test_web_job_manager_cancel_before_recording_records_nothing_and_cancels() -> None:
    # PF-F4 (completion-wins, rule a): a cancel observed BEFORE any recording
    # call (the runner's cooperative _raise_if_cancelled checkpoint, mirrored
    # here) finishes cancelled and records nothing.
    manager = web_server._WebJobManager(slow_after_seconds=0.01)
    rows: list[str] = []

    def runner(cancel_event):
        while not cancel_event.is_set():
            time.sleep(0.005)
        if cancel_event.is_set():
            raise web_server._WebJobCancelled("cancelled for test")
        rows.append("recorded")

    started = manager.start("test", runner)
    manager.cancel(started["job_id"])
    final = _wait_for_manager_job(manager, started["job_id"])

    assert final["status"] == "cancelled"
    assert rows == []


def test_web_job_manager_late_cancel_after_recording_keeps_completed_result() -> None:
    # PF-F4 (completion-wins, rule b): the runner already returned here, so
    # any recording it performed has already committed. A cancel_event that
    # only flips true in the window between that return and terminal-state
    # publication must not relabel the completed workflow as cancelled - the
    # recorded row(s) would otherwise dangle under a "cancelled" job.
    manager = web_server._WebJobManager(slow_after_seconds=0.01)
    rows: list[str] = []

    def runner(cancel_event):
        rows.append("recorded")
        cancel_event.set()
        return {"ok": True}

    started = manager.start("test", runner)
    final = _wait_for_manager_job(manager, started["job_id"])

    assert final["status"] == "completed"
    assert rows == ["recorded"]


def test_web_job_manager_publication_failure_after_recording_surfaces_failed_with_rows_standing(
    monkeypatch,
) -> None:
    # PF-F4 (completion-wins, rule c): if terminal-state publication itself
    # fails after recording already committed, the job surfaces "failed"
    # with that error while the recorded rows stand untouched - RunIndex
    # reflects computed truth, job state reflects delivery, and the two may
    # honestly diverge in exactly this case.
    manager = web_server._WebJobManager(slow_after_seconds=0.0)
    rows: list[str] = []

    def fail_public_json(value):
        raise TypeError("bad result payload")

    monkeypatch.setattr(web_server, "_web_public_json", fail_public_json)

    def runner(cancel_event):
        rows.append("recorded")
        return {"ok": True}

    started = manager.start("test", runner)
    failed = _wait_for_manager_job(manager, started["job_id"])

    assert failed["status"] == "failed"
    assert failed["error"] == "job result serialization failed"
    assert rows == ["recorded"]


def test_web_public_json_normalizes_nonstandard_values(tmp_path) -> None:
    payload = web_server._web_public_json(
        {
            "artifact_path": tmp_path / "private" / "result.json",
            "factor_values_path": {"unexpected": tmp_path / "nested" / "scores.parquet"},
            "round_report_paths": [
                tmp_path / "private" / "round-1.md",
                str(tmp_path / "private" / "round-2.md"),
            ],
            "iteration_chain": {
                "round_report_paths": [tmp_path / "private" / "chain-round.md"],
            },
            "raw_response": "provider body",
            "seen": {"b", "a"},
            "stamp": web_server.datetime(2026, 1, 2, tzinfo=web_server.UTC),
            "blob": b"ok",
        }
    )

    assert payload["artifact_path"] == "result.json"
    assert payload["factor_values_path"] == {"unexpected": "scores.parquet"}
    assert payload["round_report_paths"] == ["round-1.md", "round-2.md"]
    assert payload["iteration_chain"]["round_report_paths"] == ["chain-round.md"]
    assert payload["seen"] == ["a", "b"]
    assert payload["stamp"] == "2026-01-02T00:00:00+00:00"
    assert payload["blob"] == "ok"
    assert "raw_response" not in payload


def test_web_job_result_reduces_private_paths(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    def path_payload(config, text, *, parser_mode="llm", llm_provider=None, rd_config=None, cancel_event=None):
        return {
            "artifact_path": tmp_path / "secret" / "artifact.json",
            "nested": {"factor_values_path": str(tmp_path / "secret" / "2025.parquet")},
            "round_report_paths": [
                tmp_path / "secret" / "round-1.md",
                str(tmp_path / "secret" / "round-2.md"),
            ],
            "iteration_chain": {
                "round_report_paths": [tmp_path / "secret" / "chain-round.md"],
            },
            "raw_response": "provider body",
        }

    monkeypatch.setattr(web_server, "run_idea_workflow", path_payload)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(f"{base_url}/api/jobs/run-idea", {"text": "x", "parser_mode": "rule"})
        completed = _wait_for_job(base_url, started["job_id"])

        assert completed["result"]["artifact_path"] == "artifact.json"
        assert completed["result"]["nested"]["factor_values_path"] == "2025.parquet"
        assert completed["result"]["round_report_paths"] == ["round-1.md", "round-2.md"]
        assert completed["result"]["iteration_chain"]["round_report_paths"] == ["chain-round.md"]
        assert "raw_response" not in completed["result"]
        assert str(tmp_path) not in json.dumps(completed, ensure_ascii=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_server_rejects_docker_bind_host_by_default(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    with pytest.raises(ValueError, match="allow_docker_bind"):
        create_local_web_server(host="0.0.0.0", port=0, config=config)


def test_web_server_allows_explicit_docker_bind_host(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(web=WebSettings(allow_docker_bind=True)).resolve(tmp_path / "demo")

    with pytest.raises(ValueError, match="control_token_env"):
        create_local_web_server(host="0.0.0.0", port=0, config=config)


def test_web_server_requires_control_token_for_docker_bind(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_WEB_TOKEN", "secret-token")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_WEB_TOKEN")
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        assert server.server_address[1] > 0
        health = _get_json(f"{base_url}/health")
        assert health["ok"] is True
        html = _get_text(base_url)
        assert "protected" in html
        assert str(tmp_path) not in html
        assert "QF_TEST_WEB_TOKEN" not in html
        assert "api_key_env" not in html
        assert "runtime-llm" in html
        # CP6-1 (D8): the runtime-refresh logic is delivered via the served
        # entry module, which the redacted page must still reference.
        assert '<script type="module" src="/static/app.js"></script>' in html
        assert "refreshRuntimeStatus" in _get_text(f"{base_url}/static/app.js")
        with pytest.raises(urllib.error.HTTPError) as status_exc:
            _get_json(f"{base_url}/api/status")
        assert status_exc.value.code == 401

        status = _get_json(f"{base_url}/api/status", headers={"Authorization": "Bearer secret-token"})
        assert status["paths"]["data_root"] == str(config.paths.data_root)

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(f"{base_url}/api/jobs/run-idea", {"text": "x", "parser_mode": "rule"})
        assert excinfo.value.code == 401

        authorized = _post_json(
            f"{base_url}/api/jobs/run-idea",
            {"text": "非ST的小市值股票未来表现更好", "parser_mode": "rule"},
            headers={"Authorization": "Bearer secret-token"},
        )
        completed = _wait_for_job(
            base_url,
            authorized["job_id"],
            headers={"Authorization": "Bearer secret-token"},
        )
        assert completed["status"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_control_token_check_accepts_correct_and_rejects_wrong(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_WEB_TOKEN", "secret-token")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_WEB_TOKEN")
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        # Correct token is accepted (constant-time compare path exercised).
        status = _get_json(
            f"{base_url}/api/status", headers={"Authorization": "Bearer secret-token"}
        )
        assert status["paths"]["data_root"] == str(config.paths.data_root)

        # A wrong token of the same length is rejected with 401.
        with pytest.raises(urllib.error.HTTPError) as wrong_exc:
            _get_json(
                f"{base_url}/api/status",
                headers={"Authorization": "Bearer wrong0token0"},
            )
        assert wrong_exc.value.code == 401

        # A prefix of the real token is also rejected (no early accept).
        with pytest.raises(urllib.error.HTTPError) as prefix_exc:
            _get_json(
                f"{base_url}/api/status", headers={"Authorization": "Bearer secret"}
            )
        assert prefix_exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_rejects_oversized_request_body_with_413(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    # Declare an oversized Content-Length but send only a tiny body: the server
    # must reject on the declared length (before reading the body) with 413, so
    # no oversized allocation happens. Use a raw socket because urllib would try
    # to stream the full declared length.
    declared = web_server.MAX_REQUEST_BODY_BYTES + 1
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            request = (
                "POST /api/parse-idea HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {declared}\r\n"
                "Connection: close\r\n"
                "\r\n"
                "{}"
            )
            sock.sendall(request.encode("ascii"))
            sock.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        status_line = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
        assert "413" in status_line, status_line
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_server_rejects_nonlocal_bind_host(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    nonlocal_host = ".".join(("192", "0", "2", "1"))

    with pytest.raises(ValueError, match="local-only"):
        create_local_web_server(host=nonlocal_host, port=0, config=config)


def test_web_status_keeps_active_llm_provider_when_key_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_TEST_DEEPSEEK_KEY", raising=False)
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/deepseek",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status = _get_json(f"{base_url}/api/status")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert status["llm"]["provider"] == "deepseek"
    assert status["rd"]["research_stage"] == "research"
    assert "synthesis_stage" not in status["rd"]
    assert "campaign_mode" not in status["rd"]
    assert "synthesis_campaign_mode" not in status["rd"]
    providers = {provider["provider"]: provider for provider in status["llm"]["providers"]}
    assert providers["deepseek"]["runtime_ready"] == "false"
    assert "QF_TEST_DEEPSEEK_KEY" in providers["deepseek"]["runtime_error"]
    assert providers["glm"]["runtime_ready"] == "true"


def test_web_client_error_message_preserves_llm_runtime_detail() -> None:
    message = web_server._client_error_message(
        RuntimeError(
            "Missing API key for active LLM provider deepseek. "
            "Expected environment variable: DEEPSEEK_API_KEY."
        ),
        fallback="job failed",
    )

    assert "DEEPSEEK_API_KEY" in message
    assert "Missing API key" in message

    timeout_message = web_server._client_error_message(RuntimeError("LLM request timed out"), fallback="job failed")

    assert timeout_message == "LLM request timed out"


def test_web_workbench_uses_llm_factor_horizon(monkeypatch, tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    captured: dict[str, object] = {}

    def fake_parse_factor_idea(text, llm, *, mode):
        factor = FactorDefinition(
            factor_id="FTR_LLM_HORIZON",
            name="llm_horizon",
            formula="-rank(market_cap)",
            horizon_days=11,
            universe_filters=("is_st == false",),
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider="deepseek", model="fake")

    def fake_evaluate_factor(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        horizon_days,
        horizon_days_matrix,
        sample_splits,
        simulation_profile,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        assert factor_values_root == config.paths.factor_values_root
        assert factor_values_overlay_root == config.paths.factor_values_overlay_root
        assert factor_values_manifest_root == config.paths.factor_values_manifest_root
        captured["horizon_days"] = horizon_days
        captured["horizon_count"] = len(horizon_days_matrix)
        captured["split_count"] = len(sample_splits)
        captured["decay_days"] = simulation_profile.decay_days
        captured["evaluation_start"] = simulation_profile.test_period_start
        captured["evaluation_end"] = simulation_profile.test_period_end
        return EvaluationResult(
            factor_id=factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=_write_stub_artifact(Path(artifact_root) / "evaluations" / f"{factor_id}.json"),
        )

    def fake_run_factor_backtest(factor_id, *, factor_root, data_root, artifact_root, simulation_profile):
        return BacktestResult(
            factor_id=factor_id,
            periods=1,
            holding_days=11,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=_write_stub_artifact(Path(artifact_root) / "backtests" / f"{factor_id}.json"),
            simulation_profile=simulation_profile,
            net_annualized_return=0.01,
            net_long_short_sharpe=0.5,
            rebalance_rate=0.25,
            turnover_rate=1.0,
            warnings=("research semantics",),
        )

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)
    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)

    def fake_run_factor_backtest_with_holding(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        simulation_profile,
        holding_days,
        transaction_costs,
        sample_splits,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
        sample_role="external_oos_backtest",
        include_partial_final_period=False,
    ):
        assert holding_days == 11
        assert transaction_costs.commission_bps == 0.0
        assert len(sample_splits) == 3
        assert factor_values_root == config.paths.factor_values_root
        assert factor_values_overlay_root == config.paths.factor_values_overlay_root
        assert factor_values_manifest_root == config.paths.factor_values_manifest_root
        captured["top_quantile_basis_points"] = int(simulation_profile.top_quantile * 10000)
        captured["backtest_start"] = simulation_profile.test_period_start
        captured["backtest_end"] = simulation_profile.test_period_end
        captured.setdefault("backtest_sample_roles", []).append(sample_role)
        return fake_run_factor_backtest(
            factor_id,
            factor_root=factor_root,
            data_root=data_root,
            artifact_root=artifact_root,
            simulation_profile=simulation_profile,
        )

    monkeypatch.setattr(web_server, "run_factor_backtest", fake_run_factor_backtest_with_holding)

    rd_config = ResearchLoopConfig(
        evaluation_simulation_profile=SimulationProfile(test_period_start="2025-01-01", test_period_end="2025-12-31"),
        backtest_simulation_profile=SimulationProfile(test_period_start="2026-01-01", test_period_end="2026-12-31"),
    )
    result = run_idea_workflow(config, "非ST的小市值股票未来表现更好", parser_mode="llm", rd_config=rd_config)

    assert captured["horizon_days"] == 11
    assert captured["horizon_count"] == 4
    assert captured["split_count"] == 3
    assert captured["decay_days"] == 0
    assert captured["evaluation_start"] == "2025-01-01"
    assert captured["evaluation_end"] == "2025-12-31"
    assert captured["backtest_start"] == "2026-01-01"
    assert captured["backtest_end"] == "2026-12-31"
    assert captured["backtest_sample_roles"] == ["in_sample_backtest", "external_oos_backtest"]
    assert captured["top_quantile_basis_points"] == 3000
    assert result["factor"]["horizon_days"] == 11
    assert result["backtest"]["holding_days"] == 11
    assert paths["factor_root"].exists()


def test_web_llm_mode_does_not_silently_fallback_to_rule_parser(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(llm=LLMSettings(provider="rule")).resolve(tmp_path / "demo")

    with pytest.raises(RuntimeError, match="local rule parser"):
        run_idea_workflow(config, "小市值", parser_mode="llm")


def test_web_research_once_uses_shared_llm_for_hypothesis_and_review(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="openai_compatible",
            providers={
                "openai_compatible": LLMProviderSettings(
                    provider="openai_compatible",
                    model="fake-local",
                    base_url="http://localhost/v1",
                    api_key_required=False,
                )
            },
        )
    ).resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200, retry_timeouts=True):
        text = messages[-1]["content"]
        calls.append(text)
        if "Generate up to" in text:
            return LLMChatResult(
                content=json.dumps(
                    {
                        "hypotheses": [
                            {
                                "text": "非ST的动量股票未来表现更好",
                                "rationale": "Momentum should be compared against the seed without duplicating it.",
                                "formula_dsl": "rank(return_5d)",
                                "input_fields": ["return_5d"],
                                "expected_direction": "positive",
                                "universe_constraints": ["is_st == false"],
                            }
                        ]
                    }
                ),
                provider="openai_compatible",
                model="fake-local",
            )
        return LLMChatResult(
            content=json.dumps(
                {
                    "summary": "LLM review accepted the local evidence.",
                    "strengths": ["positive evidence"],
                    "risks": ["research only"],
                    "next_hypotheses": ["compare a lower-turnover variant"],
                }
            ),
            provider="openai_compatible",
            model="fake-local",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        rd_config=_llm_rd_config(config),
    )

    assert len(calls) == 2
    assert result["generation"]["source"] == "llm_hypothesis"
    assert result["generation"]["provider"] == "openai_compatible"
    assert "raw_response" not in result["generation"]
    assert result["candidates"][0]["self_review"]["source"] == "llm_self_review"
    assert result["candidates"][0]["factor"]["formula"] == "rank(return_5d)"


def test_rd_review_prompt_requests_chinese_report_language(tmp_path) -> None:
    factor = FactorDefinition(
        factor_id="FTR_PROMPT_SEED",
        name="prompt_seed",
        formula="-rank(market_cap)",
    )
    evaluation = EvaluationResult(
        factor_id=factor.factor_id,
        observations=1,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.0,
        rank_icir=1.0,
        ic_days=1,
        artifact_path=tmp_path / "evaluation.json",
    )
    backtest = BacktestResult(
        factor_id=factor.factor_id,
        periods=1,
        holding_days=5,
        cumulative_return=0.01,
        annualized_return=0.01,
        annualized_volatility=0.0,
        max_drawdown=0.0,
        artifact_path=tmp_path / "backtest.json",
    )

    messages = rd_llm._review_messages(
        seed=factor,
        candidate=factor,
        evaluation=evaluation,
        backtest=backtest,
        split_weighted_icir=1.0,
        score=1.0,
        gate_passed=True,
        gate_reasons=(),
    )

    assert rd_llm.CHINESE_RD_REPORT_PROMPT in messages[0]["content"]
    assert "Write summary, strengths, risks, and next_hypotheses in Chinese" in messages[0]["content"]


def test_web_research_llm_missing_formula_dsl_repairs_then_stops_without_fallback(
    monkeypatch, tmp_path
) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="openai_compatible",
            providers={
                "openai_compatible": LLMProviderSettings(
                    provider="openai_compatible",
                    model="fake-local",
                    base_url="http://localhost/v1",
                    api_key_required=False,
                )
            },
        )
    ).resolve(tmp_path / "demo")

    prompts: list[str] = []

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200, retry_timeouts=True):
        prompts.append("\n".join(message["content"] for message in messages))
        return LLMChatResult(
            content=json.dumps(
                {
                    "hypotheses": [
                        {
                            "text": "unmapped alpha idea",
                            "rationale": "LLM requested parameter search instead of formula_dsl.",
                            "source": "parameter_search",
                            "parameter_search_fallback": True,
                        }
                    ]
                }
            ),
            provider="openai_compatible",
            model="fake-local",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)

    rd_config = _llm_rd_config(config)
    rd_config = replace(
        rd_config,
        llm=ResearchLLMConfig(hypothesis_mode="llm", review_mode="local"),
        parameter_search=replace(rd_config.parameter_search, enabled=True),
    )

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        rd_config=rd_config,
    )

    assert len(prompts) == 3
    assert "formula_dsl is missing" in prompts[1]
    assert result["candidates"] == []
    assert result["accepted_candidate_ids"] == []
    assert result["optimization_performed"] is False
    assert result["no_optimization_performed"] is True
    assert result["optimization_status"] == "no_optimization_performed"
    assert result["blocked_plans"][0]["plan"]["status"] == "blocked_missing_formula"
    assert result["blocked_plans"][0]["error"] == "formula_dsl is missing"
    assert result["blocked_plans"][0]["plan"]["metadata"]["parameter_search_fallback"] is False


def test_web_research_llm_mode_requires_configured_provider_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_MISSING_RD_KEY", raising=False)
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="https://api.deepseek.com",
                    api_key_env="QF_MISSING_RD_KEY",
                )
            },
        )
    ).resolve(tmp_path / "demo")

    with pytest.raises(RuntimeError, match="Missing API key"):
        run_research_once_workflow(
            config,
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            rd_config=_llm_rd_config(config),
        )


def test_cli_web_startup_does_not_validate_active_llm_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_MISSING_WEB_START_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: deepseek
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: QF_MISSING_WEB_START_KEY
""",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_local_web(*, host, port, config, rd_config):
        captured["host"] = host
        captured["port"] = port
        captured["provider"] = config.llm.provider
        captured["rd_objective"] = rd_config.objective

    monkeypatch.setattr(web_server, "run_local_web", fake_run_local_web)

    assert cli_main.main(["web", "--config", str(config_path), "--port", "8766"]) == 0
    assert captured == {
        "host": "127.0.0.1",
        "port": 8766,
        "provider": "deepseek",
        "rd_objective": "balanced",
    }


def test_web_workbench_uses_selected_llm_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/deepseek",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")
    captured: dict[str, str] = {}

    def fake_parse_factor_idea(text, llm, *, mode):
        captured["provider"] = llm.provider
        captured["model"] = llm.model
        factor = FactorDefinition(
            factor_id="FTR_LLM_PROVIDER",
            name="llm_provider",
            formula="-rank(market_cap)",
            horizon_days=5,
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider=llm.provider, model=llm.model)

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)

    result = run_idea_workflow(config, "小市值", parser_mode="llm", llm_provider="glm")

    assert captured == {"provider": "glm", "model": "fake-glm"}
    assert result["parser"]["provider"] == "glm"


def test_run_idea_workflow_cleans_new_factor_on_cancel(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    cancel_event = threading.Event()

    def fake_parse_factor_idea(text, llm, *, mode):
        factor = FactorDefinition(
            factor_id="FTR_CANCELLED_PARSE",
            name="cancelled_parse",
            formula="-rank(market_cap)",
            horizon_days=5,
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider="rule", model="deterministic")

    def fake_evaluate_factor(*args, **kwargs):
        cancel_event.set()
        return EvaluationResult(
            factor_id="FTR_CANCELLED_PARSE",
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.0,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=tmp_path / "evaluation.json",
        )

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)
    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)

    with pytest.raises(web_server._WebJobCancelled):
        run_idea_workflow(config, "cancel me", parser_mode="rule", cancel_event=cancel_event)

    with pytest.raises(FileNotFoundError):
        FactorRepository(config.paths.factor_root).get("FTR_CANCELLED_PARSE")


def test_run_idea_workflow_restores_existing_factor_on_cancel(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    repo = FactorRepository(config.paths.factor_root)
    original = FactorDefinition(
        factor_id="FTR_CANCEL_RESTORE",
        name="cancel_restore",
        formula="rank(close)",
        status="candidate",
        description="Existing candidate should survive cancellation.",
        horizon_days=10,
        universe_filters=("is_st == false",),
        source="research_loop",
    )
    original_path = repo.save(original)
    cancel_event = threading.Event()

    def fake_parse_factor_idea(text, llm, *, mode):
        factor = FactorDefinition(
            factor_id=original.factor_id,
            name="cancelled_reparse",
            formula="-rank(market_cap)",
            status="draft",
            description="Replacement draft must not survive cancellation.",
            horizon_days=5,
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider="rule", model="deterministic")

    def fake_evaluate_factor(*args, **kwargs):
        cancel_event.set()
        return EvaluationResult(
            factor_id=original.factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.0,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=tmp_path / "evaluation.json",
        )

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)
    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)

    with pytest.raises(web_server._WebJobCancelled):
        run_idea_workflow(config, "cancel me", parser_mode="rule", cancel_event=cancel_event)

    assert repo.get(original.factor_id) == original
    assert original_path.exists()
    assert sorted(config.paths.factor_root.glob("**/FTR_CANCEL_RESTORE/factor.yaml")) == [original_path]


def test_validation_restores_existing_factor_on_evaluation_failure(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    repo = FactorRepository(config.paths.factor_root)
    original = FactorDefinition(
        factor_id="FTR_VALIDATE_RESTORE",
        name="validate_restore",
        formula="rank(close)",
        status="candidate",
        description="Existing candidate should survive failed validation.",
        horizon_days=10,
        universe_filters=("is_st == false",),
        source="research_loop",
    )
    repo.save(original)
    edited = FactorDefinition(
        factor_id=original.factor_id,
        name="validate_restore_bad_edit",
        formula="unsupported(close)",
        status="draft",
        description="Failed edit must not survive validation.",
        horizon_days=5,
        source="llm",
    )

    def fail_evaluate_factor(*args, **kwargs):
        raise ValueError("formula validation failed")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate_factor)

    with pytest.raises(ValueError, match="formula validation failed"):
        run_idea_validation_workflow(config, edited, parser={"source": "llm"}, rd_config=ResearchLoopConfig())

    assert repo.get(original.factor_id) == original


def test_validation_removes_new_factor_on_backtest_failure(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    repo = FactorRepository(config.paths.factor_root)
    factor = FactorDefinition(
        factor_id="FTR_VALIDATE_REMOVE",
        name="validate_remove",
        formula="rank(close)",
        status="draft",
        description="New failed edit must be removed.",
        horizon_days=5,
        source="llm",
    )

    def fake_evaluate_factor(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        horizon_days,
        horizon_days_matrix,
        sample_splits,
        simulation_profile,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        return EvaluationResult(
            factor_id=factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.0,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=Path(artifact_root) / "evaluations" / f"{factor_id}.json",
            simulation_profile=simulation_profile,
        )

    def fail_run_factor_backtest(*args, **kwargs):
        raise ValueError("backtest failed")

    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(web_server, "run_factor_backtest", fail_run_factor_backtest)

    with pytest.raises(ValueError, match="backtest failed"):
        run_idea_validation_workflow(config, factor, parser={"source": "llm"}, rd_config=ResearchLoopConfig())

    with pytest.raises(FileNotFoundError):
        repo.get(factor.factor_id)


def test_web_html_keeps_active_llm_provider_visible_when_key_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_TEST_DEEPSEEK_KEY", raising=False)
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/deepseek",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")

    html = web_server._index_html(config)

    assert "LLM parser: deepseek / fake-deepseek" in html
    assert "RD optimizer: research local deterministic" in html
    assert (
        '<option value="deepseek" selected>deepseek / fake-deepseek · missing env QF_TEST_DEEPSEEK_KEY</option>'
        in html
    )
    assert '<option value="glm">glm / fake-glm · env QF_TEST_GLM_KEY</option>' in html


def test_web_html_uses_existing_factor_as_default_rd_seed(tmp_path) -> None:
    factor_root = tmp_path / "factor_root"
    web_server.FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_CUSTOM_SEED",
            name="custom_seed",
            formula="rank(return_5d)",
            source="test",
        )
    )
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=tmp_path / "data",
            factor_root=factor_root,
            artifact_root=tmp_path / "artifacts",
        )
    )

    html = web_server._index_html(config)

    assert 'id="rd-seed" value="FTR_CUSTOM_SEED"' in html


def test_web_html_does_not_fake_rd_seed_when_factor_root_empty(tmp_path) -> None:
    factor_root = tmp_path / "factor_root"
    factor_root.mkdir()
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=tmp_path / "data",
            factor_root=factor_root,
            artifact_root=tmp_path / "artifacts",
        )
    )

    html = web_server._index_html(config)

    assert 'id="rd-seed" value="FTR_DEMO_SMALL_CAP"' not in html
    assert 'placeholder="先创建或配置一个因子"' in html


def test_web_research_cards_include_cache_paths_and_artifacts() -> None:
    # CP6-1 (D8): candidate cards render from the served research view module.
    research_js = _static_module_text("views/research.js")

    assert "factor_values:" in research_js
    assert "artifacts:" in research_js


def test_web_result_sections_separate_roles_and_diagnostics() -> None:
    # CP6-1 (D8): the result sections render from the served factor view module.
    factor_js = _static_module_text("views/factor.js")

    assert "样本内研究评价" in factor_js
    assert "外部样本外组合评测" in factor_js
    assert "样本充分性与诊断" in factor_js
    assert "research_evaluation" in factor_js
    assert "external_oos_backtest" in factor_js
    assert "HAC t-stat" in factor_js
    assert "外部样本外仅包含 1 个完整持有期" in factor_js


def test_web_parse_result_renders_fallback_warning_notice() -> None:
    # F-010 no-silent-fallback: the parse renderer must surface payload
    # warnings as a labeled design-system warn notice ahead of the report
    # hero (text label, never color alone).
    factor_js = _static_module_text("views/factor.js")

    assert "renderParseWarnings(payload.warnings)" in factor_js
    assert '<div class="notice warn"><span class="status-pill status-pill--running">警告</span>' in factor_js


def test_web_backtest_metric_labels_match_metric_units() -> None:
    # CP6-1 (D8): metric tile labels render from the served frontend modules;
    # the negatives sweep the whole bundle so no module reintroduces them.
    factor_js = _static_module_text("views/factor.js")
    bundle = _frontend_js_bundle()

    assert "年化Sharpe" in factor_js
    assert "年化波动率" in factor_js
    assert "净值最大回撤" in factor_js
    assert "日频Sharpe" not in bundle
    assert "日频波动率" not in bundle
    assert "日频最大回撤" not in bundle


def test_web_does_not_coerce_unavailable_metrics_to_zero() -> None:
    # CP6-1 (D8): FP-4 null-not-zero must hold across every served module.
    bundle = _frontend_js_bundle()

    assert "valueOr(evaluation.rank_ic_t_stat, 0)" not in bundle
    assert "valueOr(backtest.net_annualized_return, 0)" not in bundle
    assert "valueOr(backtest.net_long_short_sharpe" not in bundle
    assert "valueOr(backtest.rebalance_rate, 0)" not in bundle
    assert "valueOr(backtest.turnover_rate, 0)" not in bundle


def _insufficient_sample_evaluation(artifact_path: Path) -> EvaluationResult:
    return EvaluationResult(
        factor_id="demo_factor",
        observations=3,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.0,
        rank_icir=0.0,
        ic_days=1,
        artifact_path=artifact_path,
        rank_ic_t_stat=0.0,
        horizon_metrics=(
            HorizonEvaluationMetric(
                horizon_days=5,
                observations=1,
                coverage=1.0,
                rank_ic_mean=0.1,
                rank_ic_std=0.0,
                rank_icir=0.0,
                ic_days=1,
                rank_ic_t_stat=0.0,
                metrics={
                    "rank_icir": MetricValue(
                        value=None,
                        unit="ratio",
                        status="insufficient_sample",
                        observation_count=1,
                        minimum_required=2,
                    ),
                },
            ),
        ),
        metrics={
            "rank_ic_mean": MetricValue(
                value=0.1,
                unit="correlation",
                status="available",
                observation_count=1,
            ),
            "rank_icir": MetricValue(
                value=None,
                unit="ratio",
                status="insufficient_sample",
                observation_count=1,
                minimum_required=2,
            ),
            "rank_ic_t_stat": MetricValue(
                value=None,
                unit="t_stat",
                status="insufficient_sample",
                observation_count=1,
                minimum_required=2,
            ),
        },
    )


def test_web_evaluation_payload_reports_insufficient_metrics_as_null(tmp_path) -> None:
    evaluation = _insufficient_sample_evaluation(tmp_path / "evaluation.json")

    payload = web_server._evaluation_payload(evaluation)

    assert payload["rank_icir"] is None
    assert payload["rank_icir_status"] == "insufficient_sample"
    assert payload["rank_ic_t_stat"] is None
    assert payload["rank_ic_t_stat_status"] == "insufficient_sample"
    assert payload["rank_ic_mean"] == pytest.approx(0.1)
    assert payload["rank_ic_mean_status"] == "available"
    assert payload["horizon_metrics"][0]["rank_icir"] is None
    assert payload["horizon_metrics"][0]["rank_icir_status"] == "insufficient_sample"
    encoded = json.loads(json.dumps(web_server._json_safe(payload)))
    assert encoded["rank_icir"] is None
    assert encoded["rank_icir_status"] == "insufficient_sample"


def test_web_evaluation_payload_keeps_legacy_scalars_without_metric_map(tmp_path) -> None:
    legacy = EvaluationResult(
        factor_id="demo_factor",
        observations=3,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.2,
        rank_icir=0.5,
        ic_days=10,
        artifact_path=tmp_path / "evaluation.json",
        rank_ic_t_stat=1.5,
    )

    payload = web_server._evaluation_payload(legacy)

    assert payload["rank_icir"] == pytest.approx(0.5)
    assert payload["rank_icir_status"] == "legacy"
    assert payload["rank_ic_mean"] == pytest.approx(0.1)
    assert payload["rank_ic_mean_status"] == "legacy"
    assert payload["rank_ic_t_stat"] == pytest.approx(1.5)
    assert payload["rank_ic_t_stat_status"] == "legacy"


def test_web_validation_payload_reports_insufficient_metrics_as_null(tmp_path) -> None:
    evaluation = _insufficient_sample_evaluation(tmp_path / "evaluation.json")
    factor = FactorDefinition(factor_id="demo_factor", name="demo", formula="rank(market_cap)")
    backtest = BacktestResult(
        factor_id="demo_factor",
        periods=1,
        holding_days=5,
        cumulative_return=0.01,
        annualized_return=0.01,
        annualized_volatility=0.0,
        max_drawdown=0.0,
        artifact_path=tmp_path / "backtest.json",
    )

    payload = web_server._validation_payload(
        factor,
        parser=None,
        evaluation=evaluation,
        in_sample_backtest=backtest,
        backtest=backtest,
        parameters={},
    )

    evaluation_payload = payload["evaluation"]
    assert evaluation_payload["rank_icir"] is None
    assert evaluation_payload["rank_icir_status"] == "insufficient_sample"
    assert evaluation_payload["rank_ic_t_stat"] is None
    assert evaluation_payload["rank_ic_t_stat_status"] == "insufficient_sample"
    assert evaluation_payload["rank_ic_mean"] == pytest.approx(0.1)
    assert evaluation_payload["rank_ic_mean_status"] == "available"
    horizon_payload = evaluation_payload["horizon_metrics"][0]
    assert horizon_payload["rank_icir"] is None
    assert horizon_payload["rank_icir_status"] == "insufficient_sample"
    assert horizon_payload["rank_ic_mean"] == pytest.approx(0.1)
    assert horizon_payload["rank_ic_mean_status"] == "legacy"


def test_web_html_renders_metric_status_instead_of_placeholder_zero() -> None:
    # CP6-1 (D8): metricNum is defined once in the served metric module and
    # every call site in the view modules goes through it; the negatives
    # sweep the whole bundle so no module bypasses the status-aware renderer.
    metric_js = _static_module_text("metric.js")
    factor_js = _static_module_text("views/factor.js")
    bundle = _frontend_js_bundle()

    assert "function metricNum(" in metric_js
    assert bundle.count("function metricNum(") == 1
    assert "metricNum(evaluation.rank_ic_mean, evaluation.rank_ic_mean_status)" in factor_js
    assert "metricNum(evaluation.rank_icir, evaluation.rank_icir_status, 2)" in factor_js
    assert "metricNum(evaluation.rank_ic_t_stat, evaluation.rank_ic_t_stat_status, 2)" in factor_js
    assert "num(evaluation.rank_ic_mean)" not in bundle
    assert "num(evaluation.rank_icir, 2)" not in bundle
    assert "num(evaluation.rank_ic_t_stat, 2)" not in bundle


def _fake_research_result(
    *,
    seed_factor_id: str,
    candidate_id: str,
    report_path: Path,
    accepted: bool = True,
    score: float = 1.0,
) -> ResearchLoopResult:
    factor = FactorDefinition(
        factor_id=candidate_id,
        name=candidate_id.lower(),
        formula="rank(return_5d)",
        status="candidate" if accepted else "draft",
        source="research_loop",
    )
    evaluation = EvaluationResult(
        factor_id=candidate_id,
        observations=1,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.0,
        rank_icir=1.0,
        ic_days=1,
        artifact_path=report_path.parent / f"{candidate_id}_evaluation.json",
    )
    backtest = BacktestResult(
        factor_id=candidate_id,
        periods=1,
        holding_days=5,
        cumulative_return=0.01,
        annualized_return=0.01,
        annualized_volatility=0.0,
        max_drawdown=0.0,
        artifact_path=report_path.parent / f"{candidate_id}_backtest.json",
    )
    candidate = ResearchCandidateResult(
        hypothesis=ResearchHypothesis(
            text=f"improve {seed_factor_id}",
            rationale="test chain handoff",
            formula_dsl=factor.formula,
        ),
        factor=factor,
        evaluation=evaluation,
        backtest=backtest,
        split_weighted_icir=1.0,
        score=score,
        gate_passed=accepted,
        gate_reasons=(),
        self_review=ResearchSelfReview(
            source="local_self_review",
            summary="accepted" if accepted else "draft",
            strengths=(),
            risks=(),
            next_hypotheses=(),
        ),
    )
    return ResearchLoopResult(
        rd_stage="research",
        seed_factor_id=seed_factor_id,
        objective="balanced",
        objective_weights=ResearchObjectiveWeights(),
        gate=ResearchGate(),
        candidates=(candidate,),
        accepted_candidate_ids=(candidate_id,) if accepted else (),
        report_path=report_path,
        optimization_performed=accepted,
        no_optimization_performed=not accepted,
    )


def _empty_research_result(seed_factor_id: str, *, report_path: Path) -> ResearchLoopResult:
    report_path.write_text("empty research report", encoding="utf-8")
    return ResearchLoopResult(
        rd_stage="research",
        seed_factor_id=seed_factor_id,
        objective="balanced",
        objective_weights=ResearchObjectiveWeights(),
        gate=ResearchGate(),
        candidates=(),
        accepted_candidate_ids=(),
        report_path=report_path,
        optimization_performed=False,
        no_optimization_performed=True,
    )


def _post_json(url: str, payload: dict, *, headers: dict[str, str] | None = None) -> dict:
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def _wait_for_job(
    base_url: str,
    job_id: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get_json(f"{base_url}/api/jobs/{job_id}", headers=headers)
        if status["status"] in {"completed", "failed", "cancelled"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _wait_for_manager_job(manager, job_id: str, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.get(job_id)
        if status["status"] in {"completed", "failed", "cancelled"}:
            return status
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def _local_rd_config(config: QuantForgeConfig):
    return replace(
        load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation),
        llm=ResearchLLMConfig(hypothesis_mode="local", review_mode="local"),
    )


def _llm_rd_config(config: QuantForgeConfig):
    return replace(
        load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation),
        llm=ResearchLLMConfig(hypothesis_mode="llm", review_mode="llm"),
    )
