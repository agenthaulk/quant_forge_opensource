from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from quant_forge.core.contracts import (
    BacktestResult,
    BacktestSegmentMetric,
    EvaluationResult,
    FactorDefinition,
    SimulationProfile,
)
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.research_loop.candidate_gate import CandidateGateConfig, evaluate_candidate
from quant_forge.research_loop.context_builder import ResearchContextBuilder
from quant_forge.research_loop.contracts import (
    FactorExperimentPlan,
    FactorExperimentResult,
    ResearchContext,
    ResearchTraceEntry,
    StructuredResearchHypothesis,
)
from quant_forge.research_loop.experiment_planner import ExperimentPlanner, default_context
from quant_forge.research_loop.feedback_builder import build_feedback
import quant_forge.research_loop.llm as rd_llm
from quant_forge.research_loop.llm_contracts import normalize_review_payload
from quant_forge.research_loop.operator_drafts import write_operator_draft_artifacts
from quant_forge.research_loop.service import (
    ResearchDeduplicationConfig,
    ResearchGate,
    ResearchGenerationMetadata,
    ResearchHypothesis,
    ResearchLoopService,
    ResearchSelfReview,
    _candidate_from_hypothesis,
    apply_gate,
)
from quant_forge.research_loop.trace_store import ResearchTraceStore, utc_timestamp


def test_trace_store_writes_metadata_and_rejects_raw_factor_values(tmp_path: Path) -> None:
    store = ResearchTraceStore(tmp_path / "rd_trace")
    entry = ResearchTraceEntry(
        run_id="rd_test",
        lane_id="h1-p01",
        phase="experiment_plan",
        timestamp=utc_timestamp(),
        formula_dsl="rank(close)",
        inputs=("close",),
    )

    path = store.append_trace(entry)
    rows = store.read_trace_entries("rd_test")

    assert path.exists()
    assert rows[0]["formula_dsl"] == "rank(close)"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["schema_version"] == "qf.research_loop.trace.v1"
    with pytest.raises(ValueError, match="factor_values"):
        store.append_trace({"run_id": "rd_test", "lane_id": "bad", "phase": "bad", "factor_values": [1, 2, 3]})
    with pytest.raises(ValueError, match="factor_values"):
        store.append_trace(
            {
                "run_id": "rd_test",
                "lane_id": "bad_nested",
                "phase": "bad",
                "items": ({"factor_values": [1, 2, 3]},),
            }
        )


def test_experiment_planner_maps_non_st_and_canonicalizes_negative_direction() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="small_cap",
        text="小市值非ST股票表现更好",
        rationale="financial analyst small-cap idea",
        formula_dsl="rank(market_cap)",
        input_fields=("market_cap",),
        expected_direction="negative",
        universe_constraints=("非ST",),
        source="financial_analyst",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.formula_dsl == "-rank(market_cap)"
    assert plan.expected_direction == "positive"
    assert plan.universe_filters == ("is_st == false",)
    assert plan.metadata["hypothesis_source"] == "financial_analyst"
    assert plan.metadata["formula_canonicalized_to_positive_alpha"] is True


def test_experiment_planner_canonicalizes_negative_non_rank_formulas_to_executor_grammar() -> None:
    context = default_context()
    zscore = StructuredResearchHypothesis(
        hypothesis_id="low_close_zscore",
        text="low close zscore",
        formula_dsl="zscore(close)",
        expected_direction="negative",
    )
    plain_field = StructuredResearchHypothesis(
        hypothesis_id="low_close",
        text="low close",
        formula_dsl="close",
        expected_direction="negative",
    )

    assert ExperimentPlanner().plan(zscore, context).formula_dsl == "-zscore(close)"
    assert ExperimentPlanner().plan(plain_field, context).formula_dsl == "-close"


def test_experiment_planner_blocks_unknown_operator_field_and_st_numeric_feature() -> None:
    context = default_context()

    bad_operator = StructuredResearchHypothesis(
        hypothesis_id="bad_op",
        text="bad operator",
        formula_dsl="magic_rank(close)",
        expected_direction="positive",
        source="operator_mcp",
    )
    bad_field = StructuredResearchHypothesis(
        hypothesis_id="bad_field",
        text="bad field",
        formula_dsl="rank(pe_ttm)",
        expected_direction="positive",
        source="operator_mcp",
    )
    bad_st = StructuredResearchHypothesis(
        hypothesis_id="bad_st",
        text="bad ST formula",
        formula_dsl="rank(is_st)",
        expected_direction="positive",
        source="operator_mcp",
    )

    assert ExperimentPlanner().plan(bad_operator, context).status == "requires_operator_draft_review"
    assert ExperimentPlanner().plan(bad_field, context).status == "blocked_missing_field"
    assert ExperimentPlanner().plan(bad_st, context).status == "blocked_pit_event_feature_required"


def test_operator_draft_artifacts_are_written_for_unknown_operator(tmp_path: Path) -> None:
    context = default_context()
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="draft_op",
        text="needs draft operator",
        formula_dsl="magic_rank(close)",
        expected_direction="positive",
        source="operator_mcp",
    )
    plan = ExperimentPlanner().plan(hypothesis, context)

    artifacts = write_operator_draft_artifacts(tmp_path, plan)

    assert artifacts is not None
    assert Path(artifacts.manifest_path).exists()
    manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
    assert manifest["security_boundary"] == "not_imported_not_executed_until_reviewed"
    assert manifest["audit_status"] == "draft"
    assert Path(artifacts.semantics_request_path).exists()
    assert Path(artifacts.review_path).read_text(encoding="utf-8").startswith("# Draft Operator Review")
    assert not (Path(artifacts.draft_root) / "operator.py").exists()
    assert "operator_drafts" in artifacts.draft_root


def test_operator_draft_artifacts_use_nested_unknown_resolution_status(tmp_path: Path) -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="nested_draft_op",
        text="needs nested draft operator",
        formula_dsl="rank(industry_neutralize(return_1d))",
        expected_direction="positive",
        source="operator_mcp",
    )
    plan = ExperimentPlanner().plan(hypothesis, default_context())

    artifacts = write_operator_draft_artifacts(tmp_path, plan)

    assert artifacts is not None
    manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
    assert manifest["unknown_operator"] == "industry_neutralize"
    assert manifest["resolution_status"] == "unknown_requires_draft"


def test_experiment_planner_canonicalizes_safe_alias_before_ready_plan() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="alias_stddev",
        text="external DSL stddev alias",
        formula_dsl="rank(-ts_stddev(return_1d, 20))",
        expected_direction="positive",
        source="operator_mcp",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.raw_formula_dsl == "rank(-ts_stddev(return_1d, 20))"
    assert plan.formula_dsl == "rank(-stddev(return_1d, 20))"
    assert plan.metadata["operator_resolution"]["executable"] is True


def test_experiment_planner_canonicalizes_safe_alias_for_parameter_search_fallback() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="alias_stddev_fallback",
        text="parameter search around alias-based seed",
        formula_dsl="rank(-ts_stddev(return_1d, 20))",
        expected_direction="positive",
        source="parameter_search",
        parameter_search_fallback=True,
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.raw_formula_dsl == "rank(-ts_stddev(return_1d, 20))"
    assert plan.formula_dsl == "rank(-stddev(return_1d, 20))"
    assert plan.metadata["operator_resolution"]["executable"] is True
    assert plan.metadata["parameter_search_fallback"] is True


def test_experiment_planner_blocks_likely_alias_without_draft_execution() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="rolling_std",
        text="ambiguous rolling std alias",
        formula_dsl="rolling_std(return_1d, 20)",
        expected_direction="positive",
        source="operator_mcp",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "blocked_formula_invalid"
    assert plan.operator_validation["unknown_operators"] == []
    assert plan.metadata["operator_resolution"]["executable"] is False


def test_experiment_planner_accepts_nested_multi_argument_formula() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="nested_formula",
        text="rank rolling correlation",
        formula_dsl="rank(correlation(close, volume, 2))",
        expected_direction="positive",
        source="operator_mcp",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.inputs == ("close", "volume")
    assert plan.operator_validation["used_operators"] == ["rank", "correlation"]


def test_experiment_planner_accepts_safe_binary_formula() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="binary_formula",
        text="size adjusted reversal",
        formula_dsl="zscore(rank(market_cap) * -rank(return_5d))",
        expected_direction="positive",
        source="financial_analyst",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.inputs == ("market_cap", "return_5d")
    assert plan.operator_validation["used_operators"] == ["zscore", "rank"]


def test_experiment_planner_accepts_grouped_arithmetic_formula() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="grouped_formula",
        text="average close and volume ranks",
        formula_dsl="zscore((rank(close) + rank(volume)) / 2)",
        expected_direction="positive",
        source="financial_analyst",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.inputs == ("close", "volume")
    assert plan.operator_validation["used_operators"] == ["zscore", "rank"]


def test_experiment_planner_preserves_dotted_field_leaf_compatibility() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="dotted_field",
        text="rank local close",
        formula_dsl="rank(local.close)",
        expected_direction="positive",
        source="financial_analyst",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.inputs == ("close",)
    assert plan.operator_validation["used_operators"] == ["rank"]


@pytest.mark.parametrize(
    "formula",
    [
        "rank(close).__class__",
        "close > volume",
        "[close]",
        "rank(close, window=2)",
        "rank(close ** 2)",
        "rank(close // 2)",
        "close if volume else market_cap",
        "np.log(close)",
        "+rank(close)",
    ],
)
def test_experiment_planner_blocks_unsafe_ast_formula_syntax(formula: str) -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="unsafe_formula",
        text="unsafe formula",
        formula_dsl=formula,
        expected_direction="positive",
        source="financial_analyst",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "blocked_formula_invalid"
    assert "formula validation failed" in plan.blocking_reasons


def test_experiment_planner_allows_whole_precomputed_parameter_search_seed() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="precomputed_seed",
        text="parameter search around existing seed",
        formula_dsl="precomputed:factor_id=FTR_0CDD5B36",
        expected_direction="positive",
        source="parameter_search",
        parameter_search_fallback=True,
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.inputs == ()
    assert plan.metadata["parameter_search_fallback"] is True


def test_experiment_planner_blocks_mixed_precomputed_parameter_search_formula() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="bad_precomputed_mix",
        text="unsafe mixed precomputed formula",
        formula_dsl="precomputed:factor_id=FTR_0CDD5B36 + rank(close)",
        expected_direction="positive",
        source="parameter_search",
        parameter_search_fallback=True,
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "blocked_formula_invalid"
    assert "precomputed formulas can only be used as whole seed factors" in plan.blocking_reasons


def test_experiment_planner_blocks_precomputed_subexpression() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="bad_precomputed_child",
        text="unsafe precomputed child",
        formula_dsl="rank(precomputed:factor_id=FTR_0CDD5B36)",
        expected_direction="positive",
        source="financial_analyst",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "blocked_formula_invalid"
    assert "precomputed formulas can only be used as whole seed factors" in plan.blocking_reasons


@pytest.mark.parametrize(
    "formula",
    [
        "rank(close, volume)",
        "correlation(close, volume)",
        "delay(close, volume)",
        "ts_rank(close, volume)",
    ],
)
def test_experiment_planner_blocks_bad_known_operator_signatures(formula: str) -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="bad_signature",
        text="bad signature",
        formula_dsl=formula,
        expected_direction="positive",
        source="operator_mcp",
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "blocked_formula_invalid"
    assert plan.operator_validation["unknown_operators"] == []


def test_llm_hypothesis_payload_preserves_formula_dsl() -> None:
    hypotheses = rd_llm._hypotheses_from_payload(  # noqa: SLF001 - lock structured LLM contract.
        {
            "schema_version": "qf.rd.llm.v1",
            "task_type": "rd_research_hypotheses",
            "hypotheses": [
                {
                    "text": "Use close/volume rolling correlation",
                    "rationale": "Operator-aware liquidity-price interaction.",
                    "formula_dsl": "rank(correlation(close, volume, 2))",
                    "input_fields": ["close", "volume"],
                    "expected_direction": "positive",
                    "source": "operator_mcp",
                }
            ],
        },
        max_candidates=1,
    )

    candidate = _candidate_from_hypothesis(hypotheses[0], horizon_days=5)

    assert hypotheses[0].formula_dsl == "rank(correlation(close, volume, 2))"
    assert hypotheses[0].input_fields == ("close", "volume")
    assert candidate.formula == "rank(correlation(close, volume, 2))"


def test_llm_hypothesis_prompt_redacts_precomputed_seed_formula() -> None:
    seed = FactorDefinition(
        factor_id="FTR_PRE",
        name="precomputed_seed",
        formula="precomputed:factor_id=FTR_PRE",
        description="mounted seed",
        status="candidate",
    )

    prompt = "\n".join(
        message["content"]
        for message in rd_llm._hypothesis_messages(  # noqa: SLF001 - lock prompt safety contract.
            seed,
            context=None,
            objective="balanced",
            max_candidates=1,
        )
    )

    assert "precomputed:factor_id=FTR_PRE" not in prompt
    assert "<mounted_precomputed_reference_not_usable_in_formula>" in prompt
    assert "Do not use placeholder fields such as seed, seed_score, or factor_score" in prompt


def test_llm_hypothesis_prompt_guides_mechanism_first_rd() -> None:
    seed = FactorDefinition(
        factor_id="FTR_LINEAR_RANK",
        name="linear_rank_seed",
        formula="-rank(market_cap) + -rank(volatility_5d) + rank(return_5d)",
        description="small cap low volatility stable return seed",
        status="candidate",
    )

    prompt = "\n".join(
        message["content"]
        for message in rd_llm._hypothesis_messages(  # noqa: SLF001 - lock RD prompt contract.
            seed,
            context=None,
            objective="improve OOS stability without cosmetic rank additions",
            max_candidates=3,
        )
    )

    assert "mechanism-first research workflow" in prompt
    assert "interaction_conjunction" in prompt
    assert "stability_smoothing" in prompt
    assert "relationship_consistency" in prompt
    assert "Do not merely append another rank term to a linear rank-sum seed" in prompt
    assert "rank((1 - rank(market_cap)) * (1 - rank(volatility_5d)) * rank(return_5d))" in prompt
    assert "rank(covariance(1 - rank(market_cap), 1 - rank(volatility_5d), 20))" in prompt
    assert '"seed_formula_shape": "linear_rank_sum"' in prompt
    assert "source_detail" in prompt
    assert "expected failure mode" in prompt


def test_llm_mechanism_guidance_examples_are_planner_ready() -> None:
    seed = FactorDefinition(
        factor_id="FTR_LINEAR_RANK",
        name="linear_rank_seed",
        formula="-rank(market_cap) + -rank(volatility_5d) + rank(return_5d)",
        description="small cap low volatility stable return seed",
        status="candidate",
    )

    guidance = rd_llm._mechanism_guidance_for_prompt(seed)  # noqa: SLF001 - lock RD prompt examples.
    formulas = [lane["example_formula"] for lane in guidance["preferred_lanes"]]

    assert guidance["seed_formula_shape"] == "linear_rank_sum"
    for index, formula in enumerate(formulas):
        plan = ExperimentPlanner().plan(
            StructuredResearchHypothesis(
                hypothesis_id=f"mechanism_example_{index}",
                text=f"mechanism example {index}",
                formula_dsl=formula,
                expected_direction="positive",
                source="operator_mcp",
            ),
            default_context(),
        )

        assert plan.status == "ready", formula


def test_llm_mechanism_guidance_does_not_misclassify_non_additive_seed() -> None:
    seed = FactorDefinition(
        factor_id="FTR_INTERACTION",
        name="interaction_seed",
        formula="rank((1 - rank(market_cap)) * rank(return_5d))",
        status="candidate",
    )

    guidance = rd_llm._mechanism_guidance_for_prompt(seed)  # noqa: SLF001 - lock RD prompt examples.

    assert guidance["seed_formula_shape"] == "other"


def test_llm_hypothesis_payload_ignores_provider_parameter_fallback_flag() -> None:
    hypotheses = rd_llm._hypotheses_from_payload(  # noqa: SLF001 - lock structured LLM contract.
        {
            "hypotheses": [
                {
                    "text": "use parameter search around the seed",
                    "source": "parameter_search",
                    "parameter_search_fallback": True,
                },
            ]
        },
        max_candidates=1,
    )

    assert len(hypotheses) == 1
    assert hypotheses[0].source == "llm"
    assert hypotheses[0].parameter_search_fallback is False
    assert hypotheses[0].formula_dsl == ""


def test_llm_hypothesis_payload_allows_empty_hypotheses_for_system_fallback() -> None:
    hypotheses = rd_llm._hypotheses_from_payload(  # noqa: SLF001 - lock structured LLM contract.
        {"hypotheses": []},
        max_candidates=3,
    )

    assert hypotheses == ()


def test_experiment_planner_marks_parameter_search_fallback_explicitly() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="parameter_fallback",
        text="no better new idea, tune profile",
        formula_dsl="rank(return_5d)",
        expected_direction="positive",
        source="parameter_search",
        parameter_search_fallback=True,
    )

    plan = ExperimentPlanner().plan(hypothesis, default_context())

    assert plan.status == "ready"
    assert plan.metadata["parameter_search_fallback"] is True
    assert "hypothesis uses parameter-search fallback" in plan.warnings


def test_candidate_gate_and_feedback_explain_blocking_reasons() -> None:
    hypothesis = StructuredResearchHypothesis(
        hypothesis_id="turnover",
        text="high turnover candidate",
        formula_dsl="rank(volume)",
        expected_direction="positive",
    )
    plan = ExperimentPlanner().plan(hypothesis, default_context())
    result = FactorExperimentResult(
        plan=plan,
        evaluation_status="completed",
        evaluation_metrics={
            "observations": 100,
            "coverage": 0.9,
            "ic_days": 20,
            "rank_ic_mean": 0.03,
            "rank_icir": 0.4,
        },
        backtest_status="completed",
        backtest_metrics={
            "net_annualized_return": 0.02,
            "net_long_short_sharpe": 0.5,
            "turnover_rate": 2.0,
            "rebalance_rate": 0.9,
            "max_drawdown": -0.1,
        },
        correlation_summary={"max_abs_corr_with_active": 0.2},
    )
    decision = evaluate_candidate(result, CandidateGateConfig(max_turnover_rate=1.0, max_rebalance_rate=0.8))
    feedback = build_feedback(FactorExperimentResult(plan=plan, gate_decision=decision))

    assert decision.accepted is False
    assert any("turnover_rate" in reason for reason in decision.blocking_reasons)
    assert any("rebalance_rate" in reason for reason in decision.blocking_reasons)
    assert "trading intensity" in feedback.next_hypothesis_hint


def test_structured_gate_matches_existing_oos_decay_rejection(tmp_path: Path) -> None:
    evaluation = EvaluationResult(
        factor_id="FTR_SYNTH",
        observations=10,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.1,
        rank_icir=1.0,
        ic_days=5,
        artifact_path=tmp_path / "eval.json",
    )
    backtest = BacktestResult(
        factor_id="FTR_SYNTH",
        periods=2,
        holding_days=5,
        cumulative_return=0.1,
        annualized_return=0.1,
        annualized_volatility=0.0,
        max_drawdown=-0.1,
        artifact_path=tmp_path / "backtest.json",
        net_annualized_return=0.1,
        segment_metrics=(
            BacktestSegmentMetric(
                name="IS",
                start_date="2024-01-01",
                end_date="2024-01-05",
                periods=1,
                gross_cumulative_return=0.1,
                gross_annualized_return=0.1,
                gross_long_short_sharpe=0.0,
                gross_max_drawdown=-0.1,
                net_cumulative_return=0.1,
                net_annualized_return=0.1,
                net_long_short_sharpe=0.0,
                net_max_drawdown=-0.1,
            ),
            BacktestSegmentMetric(
                name="OOS1",
                start_date="2024-01-08",
                end_date="2024-01-12",
                periods=1,
                gross_cumulative_return=-0.1,
                gross_annualized_return=-0.1,
                gross_long_short_sharpe=0.0,
                gross_max_drawdown=-0.1,
                net_cumulative_return=-0.1,
                net_annualized_return=-0.1,
                net_long_short_sharpe=0.0,
                net_max_drawdown=-0.1,
            ),
        ),
    )
    old_passed, old_reasons = apply_gate(evaluation, backtest, 0.1, ResearchGate(max_oos_net_return_decay=0.5))
    plan = ExperimentPlanner().plan(
        StructuredResearchHypothesis(
            hypothesis_id="synth",
            text="synth",
            formula_dsl="rank(close)",
            expected_direction="positive",
        ),
        default_context(),
    )
    structured = FactorExperimentResult(
        plan=plan,
        evaluation_status="completed",
        evaluation_metrics={"observations": 10, "coverage": 1.0, "ic_days": 5, "rank_ic_mean": 0.1, "rank_icir": 1.0},
        backtest_status="completed",
        backtest_metrics={
            "net_annualized_return": 0.1,
            "segment_metrics": [
                {"name": metric.name, "net_annualized_return": metric.net_annualized_return}
                for metric in backtest.segment_metrics
            ],
        },
    )
    decision = evaluate_candidate(structured, CandidateGateConfig(max_oos_net_return_decay=0.5))

    assert old_passed is False
    assert any("OOS net return decay" in reason for reason in old_reasons)
    assert decision.accepted is False
    assert any("OOS net return decay" in reason for reason in decision.blocking_reasons)


def test_context_builder_includes_effective_ideas_and_operator_context(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    repo = FactorRepository(paths["factor_root"])
    repo.promote("FTR_DEMO_SMALL_CAP", "candidate", "test effective idea")
    repo.save(
        FactorDefinition(
            factor_id="FTR_PRE",
            name="mounted_precomputed",
            formula="precomputed:factor_id=FTR_PRE",
            status="candidate",
        )
    )
    context = ResearchContextBuilder(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
    ).build(seed_factor_ids=("FTR_DEMO_SMALL_CAP",))

    assert "rank" in context.available_operators
    assert "market_cap" in context.available_fields
    assert any(item["name"] == "market_cap" and "capitalization" in item["description"] for item in context.field_catalog)
    assert any(item["name"] == "rank" and "percentile" in item["description"] for item in context.operator_catalog)
    assert context.seed_factor_summary[0]["factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert any(item["factor_id"] == "FTR_DEMO_SMALL_CAP" for item in context.effective_ideas)
    assert not any("precomputed:" in str(item.get("formula")) for item in context.effective_ideas)
    messages = rd_llm._hypothesis_messages(  # noqa: SLF001 - lock the prompt contract for public RD safety.
        repo.get("FTR_DEMO_SMALL_CAP"),
        context=context,
        objective="balanced",
        max_candidates=1,
    )
    prompt = "\n".join(message["content"] for message in messages)
    assert "Point-in-time market capitalization" in prompt
    assert "Cross-sectional percentile rank" in prompt
    assert "parameter_search fallback" not in prompt
    assert "Always set parameter_search_fallback=false" in prompt
    assert "Never include precomputed:" in prompt


def test_llm_repair_prompt_includes_validation_error(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    repo = FactorRepository(paths["factor_root"])
    context = ResearchContextBuilder(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
    ).build(seed_factor_ids=("FTR_DEMO_SMALL_CAP",))
    messages = rd_llm._repair_messages(  # noqa: SLF001 - lock repair feedback contract.
        seed=repo.get("FTR_DEMO_SMALL_CAP"),
        hypothesis=ResearchHypothesis(
            text="bad volume reversal",
            rationale="invalid window argument",
            source="llm",
            formula_dsl="rank(delta(return_5d, volatility_5d))",
            input_fields=("return_5d", "volatility_5d"),
        ),
        context=context,
        objective="balanced",
        validation_error="delta argument 2 must be a number",
        attempt=1,
        max_attempts=2,
    )
    prompt = "\n".join(message["content"] for message in messages)

    assert "delta argument 2 must be a number" in prompt
    assert "rank(delta(return_5d, volatility_5d))" in prompt
    assert "Do not request parameter-search fallback" in prompt
    assert "Preserve the selected research lane" in prompt
    assert "mechanism_guidance" in prompt
    assert "interaction_conjunction: formula_repair" in prompt


def test_llm_hypothesis_payload_drops_provider_fallback_when_regular_ideas_exist() -> None:
    hypotheses = rd_llm._hypotheses_from_payload(  # noqa: SLF001 - regression for LLM payload normalization.
        {
            "hypotheses": [
                {
                    "text": "use parameter search around the seed",
                    "source": "parameter_search",
                    "parameter_search_fallback": True,
                },
                {
                    "text": "非ST的小市值股票未来表现更好",
                    "source": "financial_analyst",
                    "parameter_search_fallback": False,
                },
            ]
        },
        max_candidates=3,
    )

    assert len(hypotheses) == 1
    assert hypotheses[0].text == "非ST的小市值股票未来表现更好"
    assert hypotheses[0].parameter_search_fallback is False


def test_llm_review_summary_falls_back_when_payload_omits_summary() -> None:
    factor = FactorDefinition(
        factor_id="FTR_TEST",
        name="test",
        formula="rank(close)",
        status="draft",
    )

    fallback = rd_llm._fallback_review_summary(  # noqa: SLF001 - lock DeepSeek payload normalization.
        candidate=factor,
        score=0.1234,
        split_weighted_icir=0.5,
        gate_passed=True,
    )
    normalized = normalize_review_payload(
        {
            "strengths": ["positive IC"],
            "normalization_warnings": ["provider echoed private prompt"],
        },
        fallback_summary=fallback,
    )

    assert "FTR_TEST passed the research gate" in normalized.payload["summary"]
    assert "0.1234" in normalized.payload["summary"]
    assert "summary_missing" in normalized.normalization_warnings
    assert "provider echoed private prompt" not in normalized.normalization_warnings


def test_context_builder_only_classifies_terminal_trace_entries_and_redacts_roots(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    store = ResearchTraceStore(tmp_path / "trace")
    store.append_trace(
        ResearchTraceEntry(
            run_id="rd_test",
            lane_id="plan",
            phase="experiment_plan",
            timestamp=utc_timestamp(),
            formula_dsl="rank(close)",
        )
    )
    context = ResearchContextBuilder(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        trace_store=store,
    ).build()

    assert context.recent_failures == ()
    assert context.recent_successes == ()
    assert context.data_root == "<configured:data_root>"
    assert context.factor_root == "<configured:factor_root>"


def test_research_loop_records_blocked_plans_without_running_backtest(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        experiment_planner=_BlockingPlanner(),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates == ()
    assert len(result.blocked_plans) == 1
    assert result.blocked_plans[0].gate_decision is not None
    assert result.blocked_plans[0].gate_decision.accepted is False
    assert result.trace_root is not None
    trace_path = result.trace_root / "trace.jsonl"
    assert trace_path.exists()
    assert "plan_blocked" in trace_path.read_text(encoding="utf-8")


def test_research_loop_happy_path_has_unique_trace_and_candidate_transition(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
        deduplication=ResearchDeduplicationConfig(enabled=False),
    )

    first = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)
    second = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert first.blocked_plans == ()
    assert second.blocked_plans == ()
    assert first.trace_root != second.trace_root
    assert first.trace_root is not None
    trace_rows = [
        json.loads(line)
        for line in (first.trace_root / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    experiment_rows = [row for row in trace_rows if row["phase"] == "experiment_result"]
    assert experiment_rows
    assert experiment_rows[0]["gate_decision"]["accepted"] is True
    assert experiment_rows[0]["gate_decision"]["should_transition_to_candidate"] is True
    assert "factor_values_path" not in json.dumps(trace_rows)
    assert str(paths["workspace"]) not in (first.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert str(paths["workspace"]) not in (first.trace_root / "run.json").read_text(encoding="utf-8")
    assert str(paths["workspace"]) not in (first.trace_root / "context.json").read_text(encoding="utf-8")


def test_research_loop_cancel_writes_terminal_run_status(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    cancel_event = threading.Event()
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        hypothesis_generator=_CancellingGenerator(cancel_event),
        cancel_event=cancel_event,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    run_paths = sorted((paths["artifact_root"] / "research_loop" / "runs").glob("rd_FTR_DEMO_SMALL_CAP_*/run.json"))
    assert run_paths
    payload = json.loads(run_paths[-1].read_text(encoding="utf-8"))
    assert payload["status"] == "cancelled"
    assert payload["finished_at"]


def test_research_loop_cancel_cleans_new_candidate_factor(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    cancel_event = threading.Event()
    repo = FactorRepository(paths["factor_root"])
    before = {factor.factor_id for factor in repo.list()}
    service = _CancellingAfterCandidateSaveService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(enabled=False),
        hypothesis_generator=_FixedFormulaGenerator(),
        cancel_event=cancel_event,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    after = {factor.factor_id for factor in repo.list()}
    assert after == before


def test_research_loop_cancel_does_not_promote_existing_draft_candidate(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    cancel_event = threading.Event()
    repo = FactorRepository(paths["factor_root"])
    hypothesis = _FixedFormulaGenerator().generate_with_context()[0]
    draft = _candidate_from_hypothesis(hypothesis, horizon_days=5)
    repo.save(draft)
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(enabled=False),
        hypothesis_generator=_FixedFormulaGenerator(),
        review_generator=_CancellingReview(cancel_event),
        cancel_event=cancel_event,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        service.run_once(
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            gate=ResearchGate(min_ic_days=0, min_coverage=0.0, min_backtest_periods=0, min_score=-999.0),
        )

    assert repo.get(draft.factor_id).status == "draft"


def test_research_loop_cancel_during_promote_rolls_back_existing_draft_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    cancel_event = threading.Event()
    repo = FactorRepository(paths["factor_root"])
    hypothesis = _FixedFormulaGenerator().generate_with_context()[0]
    draft = _candidate_from_hypothesis(hypothesis, horizon_days=5)
    repo.save(draft)
    original_promote = FactorRepository.promote

    def cancelling_promote(self, *args, **kwargs):
        result = original_promote(self, *args, **kwargs)
        cancel_event.set()
        return result

    monkeypatch.setattr(FactorRepository, "promote", cancelling_promote)
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(enabled=False),
        hypothesis_generator=_FixedFormulaGenerator(),
        cancel_event=cancel_event,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        service.run_once(
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            gate=ResearchGate(min_ic_days=0, min_coverage=0.0, min_backtest_periods=0, min_score=-999.0),
        )

    assert repo.get(draft.factor_id).status == "draft"


def test_research_loop_cancel_after_promote_returns_rolls_back_existing_draft_candidate(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    cancel_event = threading.Event()
    repo = FactorRepository(paths["factor_root"])
    hypothesis = _FixedFormulaGenerator().generate_with_context()[0]
    draft = _candidate_from_hypothesis(hypothesis, horizon_days=5)
    repo.save(draft)
    service = _CancellingAfterPromoteService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(enabled=False),
        hypothesis_generator=_FixedFormulaGenerator(),
        cancel_event=cancel_event,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        service.run_once(
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            gate=ResearchGate(min_ic_days=0, min_coverage=0.0, min_backtest_periods=0, min_score=-999.0),
        )

    assert repo.get(draft.factor_id).status == "draft"


def test_research_loop_uses_parameter_search_fallback_when_no_ideas(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    FactorRepository(paths["factor_root"]).promote(
        "FTR_DEMO_SMALL_CAP",
        "candidate",
        "test existing candidate fallback",
    )
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
        hypothesis_generator=_EmptyGenerator(),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates
    assert result.candidates[0].factor.factor_id == "FTR_DEMO_SMALL_CAP"
    assert result.candidates[0].hypothesis.parameter_search_fallback is True
    assert result.accepted_candidate_ids == ()
    assert result.optimization_performed is False
    assert result.no_optimization_performed is True
    assert result.trace_root is not None
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["status"] == "no_optimization_performed"
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert '"parameter_search_fallback": true' in trace_text
    trace_rows = [json.loads(line) for line in trace_text.splitlines() if line.strip()]
    experiment_rows = [row for row in trace_rows if row["phase"] == "experiment_result"]
    assert experiment_rows[0]["gate_decision"]["should_transition_to_candidate"] is False
    report = result.report_path.read_text(encoding="utf-8") if result.report_path else ""
    assert "No Optimization Performed: yes" in report
    assert "failed or smoke-only research attempt" in report


def test_research_loop_repairs_invalid_llm_formula_before_evaluation(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    generator = _InvalidThenRepairGenerator()
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
        hypothesis_generator=generator,
        deduplication=ResearchDeduplicationConfig(enabled=False),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert generator.repair_errors
    assert "delta argument 2 must be a number" in generator.repair_errors[0]
    assert result.blocked_plans == ()
    assert result.candidates
    assert result.candidates[0].factor.formula == "rank(return_5d)"
    assert result.optimization_performed is True
    assert result.no_optimization_performed is False
    assert result.accepted_candidate_ids
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert "delta(return_5d, volatility_5d)" in trace_text
    assert "rank(return_5d)" in trace_text


def test_research_loop_repairs_missing_llm_formula_before_blocking(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    generator = _MissingFormulaThenRepairGenerator()
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        hypothesis_generator=generator,
        deduplication=ResearchDeduplicationConfig(enabled=False),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert generator.repair_errors == ["formula_dsl is missing"]
    assert result.blocked_plans == ()
    assert result.candidates
    assert result.candidates[0].factor.formula == "rank(return_5d)"
    assert result.optimization_performed is True


def test_research_loop_does_not_parameter_fallback_after_invalid_llm_formula(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
        hypothesis_generator=_InvalidNoRepairGenerator(),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates == ()
    assert len(result.blocked_plans) == 1
    assert result.blocked_plans[0].plan.status == "blocked_formula_invalid"
    assert "delta argument 2 must be a number" in result.blocked_plans[0].error
    assert result.optimization_performed is False
    assert result.no_optimization_performed is True
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert '"parameter_search_fallback": true' not in trace_text
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["candidate_count"] == 0
    assert run_payload["optimization_performed"] is False


def test_research_loop_falls_back_only_after_repeated_llm_formula_failures(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    FactorRepository(paths["factor_root"]).promote(
        "FTR_DEMO_SMALL_CAP",
        "candidate",
        "test existing candidate fallback after failed repair",
    )
    generator = _InvalidRepairFailsThenFallbackGenerator()
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(top_quantile=0.2),
        simulation_profiles=(SimulationProfile(top_quantile=0.2), SimulationProfile(top_quantile=0.3)),
        parameter_search_enabled=True,
        hypothesis_generator=generator,
        llm_formula_repair_attempts=2,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert len(generator.repair_errors) == 2
    assert all("delta argument 2 must be a number" in error for error in generator.repair_errors)
    assert result.blocked_plans
    assert result.blocked_plans[0].plan.status == "blocked_formula_invalid"
    assert result.candidates
    assert len(result.candidates) == 2
    assert result.candidates[0].factor.factor_id == "FTR_DEMO_SMALL_CAP"
    assert result.candidates[0].hypothesis.parameter_search_fallback is True
    assert result.accepted_candidate_ids == ()
    assert result.optimization_performed is False
    assert result.no_optimization_performed is True
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert trace_text.count("delta(return_5d, volatility_5d)") >= 3
    assert '"parameter_search_fallback": true' in trace_text
    config_snapshot = json.loads((result.trace_root / "config_snapshot.json").read_text(encoding="utf-8"))
    assert config_snapshot["llm_formula_repair_attempts"] == 2
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["status"] == "no_optimization_performed"


def test_research_loop_refreshes_generation_metadata_after_generation(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        hypothesis_generator=_MetadataChangingGenerator(),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.generation.provider == "after"
    assert result.trace_root is not None
    config_snapshot = json.loads((result.trace_root / "config_snapshot.json").read_text(encoding="utf-8"))
    assert config_snapshot["generation"]["provider"] == "after"
    assert "raw_response" not in config_snapshot["generation"]


def test_research_loop_records_failed_trial_without_leaving_run_running(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        experiment_planner=_BadReadyPlanner(),
        parameter_search_enabled=False,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates == ()
    assert result.blocked_plans
    assert "factor formula failed operator registry gate" in result.blocked_plans[0].error
    assert "delay expects 2 arguments" in result.blocked_plans[0].error
    assert result.trace_root is not None
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["status"] == "partial"


def test_research_loop_failed_trial_trace_includes_feedback_for_next_iteration(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        experiment_planner=_BadReadyPlanner(),
        parameter_search_enabled=False,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.trace_root is not None
    rows = [
        json.loads(line)
        for line in (result.trace_root / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failed = next(row for row in rows if row["phase"] == "experiment_failed")
    assert "factor formula failed operator registry gate" in failed["error"]
    assert "delay expects 2 arguments" in failed["error"]
    assert failed["feedback"]["status"] == "failed"
    assert "factor formula failed operator registry gate" in failed["feedback"]["summary"]
    assert "delay expects 2 arguments" in failed["feedback"]["summary"]
    assert (
        failed["next_hypothesis_hint"]
        == "Repair the runtime or validation error before proposing related variants."
    )


def test_research_loop_blocks_injected_llm_hypothesis_without_formula_dsl(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
    )

    result = service.run_once(
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        hypotheses=(
            ResearchHypothesis(
                text="非ST的小市值股票在未来5日表现更好",
                rationale="Injected LLM hypothesis without formula.",
                source="llm",
                parameter_search_fallback=True,
            ),
        ),
    )

    assert result.candidates == ()
    assert len(result.blocked_plans) == 1
    assert result.blocked_plans[0].plan.status == "blocked_missing_formula"
    assert result.blocked_plans[0].error == "formula_dsl is missing"
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert '"parameter_search_fallback": true' not in trace_text


def test_research_loop_skips_duplicate_formula_before_evaluation(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    seed = FactorRepository(paths["factor_root"]).get("FTR_DEMO_SMALL_CAP")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    result = service.run_once(
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        hypotheses=(
            ResearchHypothesis(
                text="same executable formula as seed",
                rationale="duplicate formula should not spend compute",
                formula_dsl=seed.formula,
                universe_constraints=seed.universe_filters,
                expected_direction="positive",
            ),
        ),
    )

    assert result.candidates == ()
    assert len(result.blocked_plans) == 1
    assert result.blocked_plans[0].plan.status == "blocked_duplicate_formula"
    assert result.deduplication["formula_skipped"] == 1
    assert result.trace_root is not None
    report = result.report_path.read_text(encoding="utf-8") if result.report_path else ""
    assert "Formula Fingerprint Skips: 1" in report
    assert "blocked_duplicate_formula" in report


def test_research_loop_skips_low_diversity_candidates_before_evaluation(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(
            formula_fingerprint=False,
            result_signature=False,
            candidate_diversity=True,
            max_same_shape_per_run=1,
        ),
    )

    result = service.run_once(
        "FTR_DEMO_SMALL_CAP",
        max_candidates=2,
        hypotheses=(
            ResearchHypothesis(
                text="close strength",
                rationale="first close shape",
                formula_dsl="rank(close)",
                expected_direction="positive",
            ),
            ResearchHypothesis(
                text="negative close strength",
                rationale="same field/operator shape should be skipped",
                formula_dsl="-rank(close)",
                expected_direction="positive",
            ),
        ),
    )

    assert result.candidates
    assert len(result.blocked_plans) == 1
    assert result.blocked_plans[0].plan.status == "blocked_candidate_diversity"
    assert result.deduplication["diversity_skipped"] == 1


def test_research_loop_blocks_duplicate_result_signature_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(
            formula_fingerprint=False,
            result_signature=True,
            candidate_diversity=False,
        ),
    )
    evaluation = EvaluationResult(
        factor_id="FTR_SYNTH",
        observations=10,
        coverage=1.0,
        rank_ic_mean=0.05,
        rank_ic_std=0.01,
        rank_icir=5.0,
        ic_days=10,
        artifact_path=tmp_path / "eval.json",
    )
    backtest = BacktestResult(
        factor_id="FTR_SYNTH",
        periods=10,
        holding_days=5,
        cumulative_return=0.1,
        annualized_return=0.1,
        annualized_volatility=0.02,
        max_drawdown=-0.01,
        artifact_path=tmp_path / "backtest.json",
        net_annualized_return=0.1,
        net_long_short_sharpe=1.0,
        net_max_drawdown=-0.01,
        rebalance_rate=0.2,
        turnover_rate=0.3,
    )

    def fake_score_trial(trial, objective_weights, *, horizon_days_matrix, sample_splits):
        return SimpleNamespace(
            trial=trial,
            evaluation=evaluation,
            backtest=backtest,
            split_weighted_icir=1.0,
            score=0.5,
        )

    monkeypatch.setattr(service, "_score_trial", fake_score_trial)

    result = service.run_once(
        "FTR_DEMO_SMALL_CAP",
        max_candidates=2,
        hypotheses=(
            ResearchHypothesis(
                text="close candidate",
                rationale="first result signature",
                formula_dsl="rank(close)",
                expected_direction="positive",
            ),
            ResearchHypothesis(
                text="volume candidate",
                rationale="duplicate result signature",
                formula_dsl="rank(volume)",
                expected_direction="positive",
            ),
        ),
    )

    assert len(result.candidates) == 2
    assert result.candidates[0].gate_passed is True
    assert result.candidates[1].gate_passed is False
    assert result.candidates[0].formula_fingerprint
    assert result.candidates[0].result_signature
    assert result.candidates[0].candidate_shape_fingerprint
    assert "duplicate result signature matches" in "; ".join(result.candidates[1].gate_reasons)
    assert result.deduplication["result_duplicates"] == 1
    assert len(result.accepted_candidate_ids) == 1
    report = result.report_path.read_text(encoding="utf-8") if result.report_path else ""
    assert "Formula Fingerprint:" in report
    assert "Result Signature:" in report
    assert "Candidate Shape Fingerprint:" in report


class _BlockingPlanner:
    def plan(self, hypothesis: StructuredResearchHypothesis, context: ResearchContext) -> FactorExperimentPlan:
        return FactorExperimentPlan(
            plan_id=f"{hypothesis.hypothesis_id}-blocked",
            hypothesis_id=hypothesis.hypothesis_id,
            status="requires_operator_draft_review",
            factor_name="blocked",
            formula_dsl=hypothesis.formula_dsl,
            inputs=hypothesis.input_fields,
            expected_direction="positive",
            blocking_reasons=("unknown operator: ts_rank",),
        )


class _BadReadyPlanner:
    def plan(self, hypothesis: StructuredResearchHypothesis, context: ResearchContext) -> FactorExperimentPlan:
        return FactorExperimentPlan(
            plan_id=f"{hypothesis.hypothesis_id}-bad-ready",
            hypothesis_id=hypothesis.hypothesis_id,
            status="ready",
            factor_name="bad_ready",
            formula_dsl="delay(close)",
            inputs=("close",),
            expected_direction="positive",
        )


class _EmptyGenerator:
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="empty_generator", provider="test", model="none")

    def generate_with_context(self, *args, **kwargs):
        return ()


class _FixedFormulaGenerator:
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="fixed_generator", provider="test", model="fixed")

    def generate_with_context(self, *args, **kwargs):
        return (
            ResearchHypothesis(
                text="fixed momentum idea",
                rationale="fixed executable formula for cancellation tests",
                source="financial_analyst",
                formula_dsl="rank(return_5d)",
                input_fields=("return_5d",),
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
        )


class _CancellingGenerator(_FixedFormulaGenerator):
    def __init__(self, cancel_event: threading.Event) -> None:
        self.cancel_event = cancel_event

    def generate_with_context(self, *args, **kwargs):
        self.cancel_event.set()
        return super().generate_with_context(*args, **kwargs)


class _CancellingAfterCandidateSaveService(ResearchLoopService):
    def _load_or_save_candidate(self, repo: FactorRepository, draft: FactorDefinition) -> FactorDefinition:
        candidate = super()._load_or_save_candidate(repo, draft)
        if self.cancel_event is not None:
            self.cancel_event.set()
        return candidate


class _CancellingAfterPromoteService(ResearchLoopService):
    def _promote_candidate(self, repo: FactorRepository, candidate: FactorDefinition, *, reason: str) -> FactorDefinition:
        promoted = super()._promote_candidate(repo, candidate, reason=reason)
        if self.cancel_event is not None:
            self.cancel_event.set()
        return promoted


class _CancellingReview:
    def __init__(self, cancel_event: threading.Event) -> None:
        self.cancel_event = cancel_event

    def review(self, **kwargs):
        self.cancel_event.set()
        return ResearchSelfReview(
            source="test",
            summary="cancel during review",
            strengths=(),
            risks=("cancelled",),
            next_hypotheses=(),
        )


class _InvalidNoRepairGenerator:
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="invalid-no-repair")

    def generate_with_context(self, *args, **kwargs):
        return (
            ResearchHypothesis(
                text="invalid llm formula",
                rationale="should be blocked without seed fallback",
                source="llm",
                formula_dsl="rank(volume) * -rank(delta(return_5d, volatility_5d))",
                input_fields=("volume", "return_5d", "volatility_5d"),
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
        )


class _InvalidThenRepairGenerator(_InvalidNoRepairGenerator):
    def __init__(self) -> None:
        self.repair_errors: list[str] = []

    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="invalid-then-repair")

    def repair_invalid_hypothesis(self, *args, **kwargs):
        self.repair_errors.append(str(kwargs["validation_error"]))
        return ResearchHypothesis(
            text="repaired llm formula",
            rationale="repair replaces invalid delta argument with an executable return signal",
            source="llm",
            source_detail="formula_repair",
            formula_dsl="rank(return_5d)",
            input_fields=("return_5d",),
            expected_direction="positive",
            universe_constraints=("is_st == false",),
        )


class _MissingFormulaThenRepairGenerator:
    def __init__(self) -> None:
        self.repair_errors: list[str] = []

    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="missing-then-repair")

    def generate_with_context(self, *args, **kwargs):
        return (
            ResearchHypothesis(
                text="missing formula llm idea",
                rationale="should be repaired before blocking",
                source="llm",
                formula_dsl="",
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
        )

    def repair_invalid_hypothesis(self, *args, **kwargs):
        self.repair_errors.append(str(kwargs["validation_error"]))
        return ResearchHypothesis(
            text="repaired missing formula",
            rationale="repair adds executable formula",
            source="llm",
            source_detail="formula_repair",
            formula_dsl="rank(return_5d)",
            input_fields=("return_5d",),
            expected_direction="positive",
            universe_constraints=("is_st == false",),
        )


class _InvalidRepairFailsThenFallbackGenerator(_InvalidNoRepairGenerator):
    def __init__(self) -> None:
        self.repair_errors: list[str] = []

    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="invalid-repairs-fail")

    def repair_invalid_hypothesis(self, *args, **kwargs):
        self.repair_errors.append(str(kwargs["validation_error"]))
        return ResearchHypothesis(
            text=f"still invalid repair {len(self.repair_errors)}",
            rationale="repair attempt intentionally keeps an invalid formula",
            source="llm",
            source_detail="formula_repair",
            formula_dsl="rank(volume) * -rank(delta(return_5d, volatility_5d))",
            input_fields=("volume", "return_5d", "volatility_5d"),
            expected_direction="positive",
            universe_constraints=("is_st == false",),
        )


class _MetadataChangingGenerator:
    def __init__(self) -> None:
        self.provider = "before"

    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(
            source="metadata_changing",
            provider=self.provider,
            model="test",
            raw_response="server-only raw model text",
        )

    def generate_with_context(self, *args, **kwargs):
        self.provider = "after"
        from quant_forge.research_loop.service import ResearchHypothesis

        return (
            ResearchHypothesis(
                text="非ST的小市值股票在未来5日表现更好",
                rationale="metadata refresh regression",
                source="financial_analyst",
            ),
        )
