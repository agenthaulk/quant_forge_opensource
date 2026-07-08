from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from quant_forge.config import LLMSettings
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
    LocalSelfReviewGenerator,
    ResearchDeduplicationConfig,
    ResearchGate,
    ResearchGenerationMetadata,
    ResearchHypothesis,
    ResearchLoopService,
    ResearchSelfReview,
    _backtest_metrics,
    _candidate_from_hypothesis,
    _evaluation_metrics,
    _result_signature_from_scored,
    _result_signature_from_trace_entry,
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


def test_llm_review_disables_timeout_retries(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200, retry_timeouts=True):
        seen["retry_timeouts"] = retry_timeouts
        return SimpleNamespace(
            content=json.dumps(
                {
                    "summary": "review ok",
                    "strengths": ["stable"],
                    "risks": ["none"],
                    "next_hypotheses": ["continue"],
                }
            ),
            provider="deepseek",
            model="deepseek-chat",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)
    factor = FactorDefinition(factor_id="FTR_TEST", name="test", formula="rank(close)", status="draft")
    reviewer = rd_llm.LLMResearchReviewGenerator(
        LLMSettings(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key_env="QF_TEST_DEEPSEEK_KEY",
        )
    )

    review = reviewer.review(
        seed=factor,
        candidate=factor,
        evaluation=EvaluationResult(
            factor_id="FTR_TEST",
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=1.0,
            ic_days=1,
            artifact_path=Path("evaluation.json"),
        ),
        backtest=BacktestResult(
            factor_id="FTR_TEST",
            periods=1,
            holding_days=5,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=Path("backtest.json"),
        ),
        split_weighted_icir=0.1,
        score=0.2,
        gate_passed=True,
        gate_reasons=(),
    )

    assert seen["retry_timeouts"] is False
    assert review.source == "llm_self_review"


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


def test_research_loop_repairs_duplicate_llm_formula_before_empty_run(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    generator = _DuplicateFormulaThenRepairGenerator()
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        hypothesis_generator=generator,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert len(generator.repair_errors) == 1
    assert "formula fingerprint already exists: FTR_DEMO_SMALL_CAP" in generator.repair_errors[0]
    assert "forbidden_formula_dsl" in generator.repair_errors[0]
    assert result.blocked_plans == ()
    assert result.candidates
    assert result.candidates[0].factor.formula == "-rank(volatility_5d)"
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


def test_research_loop_does_not_parameter_fallback_after_repeated_invalid_llm_repairs(tmp_path: Path) -> None:
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
    assert result.candidates == ()
    assert result.accepted_candidate_ids == ()
    assert result.optimization_performed is False
    assert result.no_optimization_performed is True
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert trace_text.count("delta(return_5d, volatility_5d)") >= 3
    assert '"parameter_search_fallback": true' not in trace_text
    config_snapshot = json.loads((result.trace_root / "config_snapshot.json").read_text(encoding="utf-8"))
    assert config_snapshot["llm_formula_repair_attempts"] == 2
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["status"] == "partial"
    assert run_payload["candidate_count"] == 0


def test_research_loop_does_not_seed_fallback_after_missing_field_repair_timeout(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
        hypothesis_generator=_MissingFieldRepairTimeoutGenerator(),
        llm_formula_repair_attempts=1,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates == ()
    assert len(result.blocked_plans) == 1
    assert result.blocked_plans[0].plan.status == "blocked_missing_field"
    assert "market_cp" in result.blocked_plans[0].error
    assert "LLM formula repair failed" in result.blocked_plans[0].error
    assert result.optimization_performed is False
    assert result.no_optimization_performed is True
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert "FTR_DEMO_SMALL_CAP_h01-p01" not in trace_text
    assert '"parameter_search_fallback": true' not in trace_text


def test_research_loop_keeps_candidate_when_llm_review_times_out(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        review_generator=_TimeoutReviewGenerator(),
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates
    assert result.candidates[0].self_review.source == "llm_self_review_error"
    assert "LLM self-review failed" in result.candidates[0].self_review.risks[0]
    assert result.trace_root is not None


def test_research_loop_falls_back_after_repeated_duplicate_llm_repairs(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    FactorRepository(paths["factor_root"]).promote(
        "FTR_DEMO_SMALL_CAP",
        "candidate",
        "test existing candidate fallback after duplicate repair exhaustion",
    )
    generator = _DuplicateFormulaRepairStillDuplicatesGenerator()
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
    assert all("forbidden_formula_dsl" in error for error in generator.repair_errors)
    assert result.blocked_plans
    assert result.blocked_plans[0].plan.status == "blocked_duplicate_formula"
    assert result.candidates
    assert result.candidates[0].hypothesis.parameter_search_fallback is False
    assert result.candidates[0].hypothesis.source_detail.startswith("duplicate_exhaustion_bounded_formula_fallback")
    assert result.candidates[0].factor.factor_id != "FTR_DEMO_SMALL_CAP"
    assert result.candidates[0].factor.formula != "-rank(market_cap)"
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert '"parameter_search_fallback": true' not in trace_text
    assert "duplicate_exhaustion_bounded_formula_fallback" in trace_text
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["candidate_count"] > 0


def test_research_loop_skips_duplicate_fallback_when_other_trials_exist(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    FactorRepository(paths["factor_root"]).promote(
        "FTR_DEMO_SMALL_CAP",
        "candidate",
        "test duplicate fallback is not eager when a valid LLM candidate exists",
    )
    generator = _DuplicateAndUniqueFormulaGenerator()
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(top_quantile=0.2),
        simulation_profiles=(SimulationProfile(top_quantile=0.2), SimulationProfile(top_quantile=0.3)),
        parameter_search_enabled=True,
        hypothesis_generator=generator,
        llm_formula_repair_attempts=1,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=2)

    assert result.candidates
    assert all(candidate.factor.factor_id != "FTR_DEMO_SMALL_CAP" for candidate in result.candidates)
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert '"parameter_search_fallback": true' not in trace_text


def test_research_loop_continues_when_duplicate_repair_times_out(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    FactorRepository(paths["factor_root"]).promote(
        "FTR_DEMO_SMALL_CAP",
        "candidate",
        "test duplicate repair timeout does not fail run",
    )
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        parameter_search_enabled=True,
        hypothesis_generator=_DuplicateRepairTimeoutWithUniqueFormulaGenerator(),
        llm_formula_repair_attempts=1,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=2)

    assert result.candidates
    assert result.blocked_plans
    assert "LLM formula repair failed: TimeoutError" in result.blocked_plans[0].error
    assert result.trace_root is not None
    run_payload = json.loads((result.trace_root / "run.json").read_text(encoding="utf-8"))
    assert run_payload["candidate_count"] > 0


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

    def fake_score_trial(
        trial,
        objective_weights,
        *,
        horizon_days_matrix,
        sample_splits,
        include_external_oos=False,
    ):
        return SimpleNamespace(
            trial=trial,
            evaluation=evaluation,
            backtest=backtest,
            split_weighted_icir=1.0,
            score=0.5,
            external_oos_backtest=backtest if include_external_oos else None,
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


class _DuplicateFormulaThenRepairGenerator:
    def __init__(self) -> None:
        self.repair_errors: list[str] = []

    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="duplicate-then-repair")

    def generate_with_context(self, *args, **kwargs):
        return (
            ResearchHypothesis(
                text="duplicate seed formula",
                rationale="initial LLM idea accidentally repeats the seed formula",
                source="llm",
                formula_dsl="-rank(market_cap)",
                input_fields=("market_cap",),
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
        )

    def repair_invalid_hypothesis(self, *args, **kwargs):
        self.repair_errors.append(str(kwargs["validation_error"]))
        return ResearchHypothesis(
            text="repaired low volatility formula",
            rationale="repair changes the duplicated seed into a distinct defensive formula",
            source="llm",
            source_detail="formula_repair",
            formula_dsl="-rank(volatility_5d)",
            input_fields=("volatility_5d",),
            expected_direction="positive",
            universe_constraints=("is_st == false",),
        )


class _DuplicateFormulaRepairStillDuplicatesGenerator(_DuplicateFormulaThenRepairGenerator):
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="duplicate-repairs-fail")

    def repair_invalid_hypothesis(self, *args, **kwargs):
        self.repair_errors.append(str(kwargs["validation_error"]))
        return ResearchHypothesis(
            text="still duplicate seed formula",
            rationale="repair attempt intentionally repeats the duplicate formula",
            source="llm",
            source_detail="formula_repair",
            formula_dsl="-rank(market_cap)",
            input_fields=("market_cap",),
            expected_direction="positive",
            universe_constraints=("is_st == false",),
        )


class _DuplicateAndUniqueFormulaGenerator(_DuplicateFormulaRepairStillDuplicatesGenerator):
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="duplicate-and-unique")

    def generate_with_context(self, *args, **kwargs):
        return (
            ResearchHypothesis(
                text="duplicate seed formula",
                rationale="first LLM idea repeats the seed formula",
                source="llm",
                formula_dsl="-rank(market_cap)",
                input_fields=("market_cap",),
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
            ResearchHypothesis(
                text="unique return formula",
                rationale="second LLM idea is executable and distinct",
                source="llm",
                formula_dsl="rank(return_5d)",
                input_fields=("return_5d",),
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
        )


class _DuplicateRepairTimeoutWithUniqueFormulaGenerator(_DuplicateAndUniqueFormulaGenerator):
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="duplicate-timeout-unique")

    def repair_invalid_hypothesis(self, *args, **kwargs):
        raise TimeoutError("test repair timeout")


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


class _MissingFieldRepairTimeoutGenerator:
    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata(source="llm_hypothesis", provider="test", model="missing-field-timeout")

    def generate_with_context(self, *args, **kwargs):
        return (
            ResearchHypothesis(
                text="llm misspells market_cap",
                rationale="should be blocked without seed fallback when repair times out",
                source="llm",
                formula_dsl="rank((1 - rank(market_cp)) * rank(return_5d))",
                input_fields=("market_cp", "return_5d"),
                expected_direction="positive",
                universe_constraints=("is_st == false",),
            ),
        )

    def repair_invalid_hypothesis(self, *args, **kwargs):
        raise TimeoutError("test missing-field repair timeout")


class _TimeoutReviewGenerator:
    def review(self, **kwargs):
        raise TimeoutError("test LLM review timeout")


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


def _segment_metric(
    name: str,
    *,
    net_annualized_return: float | None,
    periods: int = 1,
) -> BacktestSegmentMetric:
    return BacktestSegmentMetric(
        name=name,
        start_date="2024-01-01",
        end_date="2024-01-05",
        periods=periods,
        gross_cumulative_return=0.1,
        gross_annualized_return=net_annualized_return,
        gross_long_short_sharpe=0.0,
        gross_max_drawdown=-0.1,
        net_cumulative_return=0.1,
        net_annualized_return=net_annualized_return,
        net_long_short_sharpe=0.0,
        net_max_drawdown=-0.1,
    )


def test_result_signature_round_trips_between_scored_and_trace_entry(tmp_path: Path) -> None:
    # COR-1 regression: the persisted trace side must carry the same canonical
    # net_max_drawdown key as the live/read side so a byte-identical rerun
    # produces an identical signature (cross-run dedup actually fires).
    evaluation = EvaluationResult(
        factor_id="FTR_SIG",
        observations=10,
        coverage=0.95,
        rank_ic_mean=0.12,
        rank_ic_std=0.1,
        rank_icir=1.2,
        ic_days=5,
        artifact_path=tmp_path / "eval.json",
    )
    backtest = BacktestResult(
        factor_id="FTR_SIG",
        periods=3,
        holding_days=5,
        cumulative_return=0.2,
        annualized_return=0.18,
        annualized_volatility=0.1,
        max_drawdown=-0.11,
        artifact_path=tmp_path / "backtest.json",
        net_annualized_return=0.15,
        net_long_short_sharpe=0.9,
        net_max_drawdown=-0.137,
        rebalance_rate=0.42,
        turnover_rate=0.63,
    )
    scored = SimpleNamespace(evaluation=evaluation, backtest=backtest)
    entry = {
        "evaluation_summary": _evaluation_metrics(evaluation),
        "backtest_summary": _backtest_metrics(backtest),
    }

    assert _result_signature_from_trace_entry(entry, precision=6) == _result_signature_from_scored(
        scored, precision=6
    )


def test_apply_gate_blocks_is_strong_oos_weak_when_external_oos_supplied(tmp_path: Path) -> None:
    # COR-2 regression: OOS gate clauses must judge the external OOS backtest,
    # not the in-sample-role backtest.
    evaluation = EvaluationResult(
        factor_id="FTR_OOS",
        observations=10,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.1,
        rank_icir=1.0,
        ic_days=5,
        artifact_path=tmp_path / "eval.json",
    )
    is_backtest = BacktestResult(
        factor_id="FTR_OOS",
        periods=4,
        holding_days=5,
        cumulative_return=0.2,
        annualized_return=0.2,
        annualized_volatility=0.1,
        max_drawdown=-0.1,
        artifact_path=tmp_path / "is_backtest.json",
        net_annualized_return=0.2,
        segment_metrics=(
            _segment_metric("IS", net_annualized_return=0.2),
            _segment_metric("OOS1", net_annualized_return=0.2),
        ),
    )
    oos_backtest = BacktestResult(
        factor_id="FTR_OOS",
        periods=4,
        holding_days=5,
        cumulative_return=-0.05,
        annualized_return=-0.05,
        annualized_volatility=0.1,
        max_drawdown=-0.2,
        artifact_path=tmp_path / "oos_backtest.json",
        net_annualized_return=-0.05,
        segment_metrics=(
            _segment_metric("IS", net_annualized_return=0.2),
            _segment_metric("OOS1", net_annualized_return=-0.05),
        ),
    )
    gate = ResearchGate(min_oos_net_annualized_return=0.05)

    # IS-strong alone (no external OOS window) passes the OOS clause.
    passed_is, _ = apply_gate(evaluation, is_backtest, 0.5, gate)
    assert passed_is is True

    # With a distinct external OOS window that is weak, the gate must block.
    passed_oos, reasons = apply_gate(evaluation, is_backtest, 0.5, gate, oos_backtest=oos_backtest)
    assert passed_oos is False
    assert any("net_annualized_return" in reason for reason in reasons)


def test_local_self_review_survives_null_backtest_metrics(tmp_path: Path) -> None:
    # COR-3 regression: short-window backtests carry None-valued metrics; the
    # local reviewer must not raise (which was previously swallowed into a
    # failed self-review).
    seed = FactorDefinition(factor_id="FTR_SEED", name="seed", formula="rank(close)", status="candidate")
    candidate = FactorDefinition(factor_id="FTR_CAND", name="cand", formula="rank(close)", status="draft")
    evaluation = EvaluationResult(
        factor_id="FTR_CAND",
        observations=1,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.0,
        rank_icir=1.0,
        ic_days=1,
        artifact_path=tmp_path / "eval.json",
    )
    backtest = BacktestResult(
        factor_id="FTR_CAND",
        periods=1,
        holding_days=5,
        cumulative_return=0.01,
        annualized_return=0.01,
        annualized_volatility=0.0,
        max_drawdown=0.0,
        artifact_path=tmp_path / "backtest.json",
        net_long_short_sharpe=None,
        rebalance_rate=None,
        turnover_rate=None,
        net_max_drawdown=None,
    )

    review = LocalSelfReviewGenerator().review(
        seed=seed,
        candidate=candidate,
        evaluation=evaluation,
        backtest=backtest,
        split_weighted_icir=0.1,
        score=0.2,
        gate_passed=True,
        gate_reasons=(),
    )

    assert review.source == "local_self_review"


def test_hypotheses_from_payload_warns_on_schema_or_task_mismatch() -> None:
    # COR-10 regression: schema/task drift is recorded (warned), not silently
    # accepted, but parsing still succeeds.
    valid_body = [
        {
            "text": "Use close/volume rolling correlation",
            "formula_dsl": "rank(correlation(close, volume, 2))",
            "expected_direction": "positive",
        }
    ]
    with pytest.warns(UserWarning):
        hypotheses = rd_llm._hypotheses_from_payload(  # noqa: SLF001 - lock schema contract.
            {"hypotheses": valid_body},
            max_candidates=1,
        )
    assert len(hypotheses) == 1
    assert hypotheses[0].formula_dsl == "rank(correlation(close, volume, 2))"

    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        ok = rd_llm._hypotheses_from_payload(  # noqa: SLF001 - correctly-versioned payload must not warn.
            {
                "schema_version": "qf.rd.llm.v1",
                "task_type": "rd_research_hypotheses",
                "hypotheses": valid_body,
            },
            max_candidates=1,
        )
    assert len(ok) == 1


def test_hypothesis_generator_temperature_defaults_to_zero(monkeypatch) -> None:
    # COR-11 regression: hypothesis-gen must default to deterministic temperature.
    seen: dict[str, object] = {}

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200, retry_timeouts=True):
        seen["temperature"] = temperature
        return SimpleNamespace(
            content=json.dumps(
                {
                    "schema_version": "qf.rd.llm.v1",
                    "task_type": "rd_research_hypotheses",
                    "hypotheses": [
                        {
                            "text": "Use close/volume rolling correlation",
                            "formula_dsl": "rank(correlation(close, volume, 2))",
                            "expected_direction": "positive",
                        }
                    ],
                }
            ),
            provider="deepseek",
            model="deepseek-chat",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)
    settings = LLMSettings(
        provider="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key_env="QF_TEST_DEEPSEEK_KEY",
    )
    seed = FactorDefinition(factor_id="FTR_SEED", name="seed", formula="rank(close)", status="candidate")

    generator = rd_llm.LLMHypothesisGenerator(settings)
    generator.generate_with_context(seed, context=None, objective="balanced", max_candidates=1)
    assert seen["temperature"] == 0.0

    explicit = rd_llm.LLMHypothesisGenerator(settings, hypothesis_temperature=0.2)
    explicit.generate_with_context(seed, context=None, objective="balanced", max_candidates=1)
    assert seen["temperature"] == 0.2


def _trace_rows_for(trace_root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (trace_root / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_research_loop_records_strategy_decision_and_rd_run_history(tmp_path: Path) -> None:
    from quant_forge.lineage.store import RunIndex
    from quant_forge.research_loop.strategy_selector import STRATEGY_VOCABULARY

    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    first = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)
    second = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    # Round 0: no prior evidence exists, so the selector must explore (R1) —
    # any other pick would fabricate evidence about an incumbent (FP-2).
    assert first.strategy_decision is not None
    assert first.strategy_decision["schema_version"] == "qf.rd.strategy.v1"
    assert first.strategy_decision["strategy"] == "explore"
    assert str(first.strategy_decision["reason"]).startswith("R1")
    assert first.strategy_trail
    assert first.strategy_trail[-1]["strategy"] == "explore"

    assert first.trace_root is not None
    assert second.trace_root is not None
    first_rows = _trace_rows_for(first.trace_root)
    decision_rows = [row for row in first_rows if row["phase"] == "strategy_decision"]
    assert len(decision_rows) == 1
    assert decision_rows[0]["schema_version"] == "qf.research_loop.trace.v1"
    assert decision_rows[0]["strategy_decision"]["strategy"] == "explore"
    assert decision_rows[0]["strategy_context"]["round_index"] == 0

    # The per-round summary is persisted run history for the next selector.
    summary_rows = [row for row in first_rows if row["phase"] == "round_summary"]
    assert len(summary_rows) == 1
    assert summary_rows[0]["round_summary"]["candidate_count"] == len(first.candidates)
    assert 0.0 <= summary_rows[0]["round_summary"]["duplicate_rate"] <= 1.0

    # Candidate results carry the additive OOS decay evidence key; the value
    # may be null (unknown), never a fabricated number.
    result_rows = [row for row in first_rows if row["phase"] == "experiment_result"]
    assert result_rows
    assert "oos_net_return_decay" in result_rows[0]["backtest_summary"]

    # Strategy hints reach the persisted prompt context as structured hints.
    context_payload = json.loads((first.trace_root / "context.json").read_text(encoding="utf-8"))
    assert any(str(hint).startswith("strategy_selector:") for hint in context_payload["next_focus_hints"])

    # Round 1 sees exactly one prior round and records its own decision.
    second_rows = _trace_rows_for(second.trace_root)
    second_decisions = [row for row in second_rows if row["phase"] == "strategy_decision"]
    assert len(second_decisions) == 1
    assert second_decisions[0]["strategy_context"]["round_index"] == 1
    assert second_decisions[0]["strategy_context"]["candidate_count"] >= 1
    assert second_decisions[0]["strategy_decision"]["strategy"] in STRATEGY_VOCABULARY
    assert len(second.strategy_trail) == 2

    # The report renders the compact per-round strategy trail.
    assert second.report_path is not None
    report_text = second.report_path.read_text(encoding="utf-8")
    assert "## Strategy Trail" in report_text
    assert str(second.strategy_trail[-1]["strategy"]) in report_text

    # Both RD runs appended one honest run-history row of kind "rd".
    rd_rows = [row for row in RunIndex(paths["artifact_root"]).read_rows() if row["kind"] == "rd"]
    assert len(rd_rows) == 2
    assert rd_rows[0]["run_id"] == first.trace_root.name
    assert rd_rows[1]["run_id"] == second.trace_root.name
    for row in rd_rows:
        assert "FTR_DEMO_SMALL_CAP" in row["factor_ids"]
        assert len(row["config_fingerprint"]) == 64
        assert row["artifact_paths_rel"]
        for path_rel in row["artifact_paths_rel"]:
            assert not Path(path_rel).is_absolute()
            assert ".." not in Path(path_rel).parts
        assert row["data_window"]["status"] in {"available", "unavailable"}
        if row["data_window"]["status"] == "unavailable":
            assert row["data_window"]["start_date"] is None
            assert row["data_window"]["end_date"] is None
        for highlight in row["metric_highlights"].values():
            if highlight["status"] == "available":
                assert highlight["value"] is not None
            else:
                assert highlight["value"] is None
        assert row["warnings_count"] >= 0


def test_research_loop_strategy_selector_disabled_removes_decision(tmp_path: Path) -> None:
    from quant_forge.lineage.store import RunIndex

    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        strategy_selector_enabled=False,
    )

    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.strategy_decision is None
    assert result.strategy_trail == ()
    assert result.trace_root is not None
    trace_text = (result.trace_root / "trace.jsonl").read_text(encoding="utf-8")
    assert "strategy_decision" not in trace_text
    context_text = (result.trace_root / "context.json").read_text(encoding="utf-8")
    assert "strategy_selector:" not in context_text
    snapshot = json.loads((result.trace_root / "config_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["strategy_selector_enabled"] is False
    assert result.report_path is not None
    assert "Strategy Trail" not in result.report_path.read_text(encoding="utf-8")

    # Run history is independent of the selector: the rd row is still written.
    rd_rows = [row for row in RunIndex(paths["artifact_root"]).read_rows() if row["kind"] == "rd"]
    assert len(rd_rows) == 1
    rows = _trace_rows_for(result.trace_root)
    assert [row["phase"] for row in rows if row["phase"] == "round_summary"] == ["round_summary"]


def test_rd_config_maps_strategy_selector_flag(tmp_path: Path) -> None:
    from quant_forge.research_loop.config import ResearchLoopConfig, load_research_loop_config

    assert ResearchLoopConfig().strategy_selector_enabled is True
    config_path = tmp_path / "rd.yaml"
    config_path.write_text("strategy_selector_enabled: false\n", encoding="utf-8")
    assert load_research_loop_config(config_path).strategy_selector_enabled is False


def test_strategy_context_is_scoped_to_the_current_seed_run_chain(tmp_path: Path) -> None:
    # F3: two sequential runs with DIFFERENT seeds must produce independent
    # strategy contexts — run 1's rounds, gate reasons, round summaries, and
    # fingerprints must not leak into run 2's context.
    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    first = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)
    second = service.run_once("FTR_DEMO_MOMENTUM", max_candidates=1)

    assert first.trace_root is not None
    assert second.trace_root is not None
    first_rows = _trace_rows_for(first.trace_root)
    second_rows = _trace_rows_for(second.trace_root)
    first_context = next(row for row in first_rows if row["phase"] == "strategy_decision")["strategy_context"]
    second_context = next(row for row in second_rows if row["phase"] == "strategy_decision")["strategy_context"]

    # Run 2 is round 0 of its OWN seed chain despite run 1's persisted trace.
    assert first_context["round_index"] == 0
    assert second_context["round_index"] == 0
    assert second_context["candidate_count"] == 0
    assert second_context["gate_blocking_reasons"] == []
    assert second_context["successful_mechanisms"] == []
    assert second_context["recent_fingerprints"] == []
    assert second.strategy_decision is not None
    assert str(second.strategy_decision["reason"]).startswith("R1")
    # The decision trail is per-seed as well: run 2 shows only its own round.
    assert len(second.strategy_trail) == 1

    # Same-seed continuation still works: a third run on seed 1 sees exactly
    # one prior round of that seed, not two runs pooled across seeds.
    third = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)
    assert third.trace_root is not None
    third_rows = _trace_rows_for(third.trace_root)
    third_context = next(row for row in third_rows if row["phase"] == "strategy_decision")["strategy_context"]
    assert third_context["round_index"] == 1
    assert third_context["candidate_count"] >= 1


def test_strategy_context_duplicate_rate_uses_attempted_plan_denominator() -> None:
    # F8: without a round summary the fallback duplicate rate must divide
    # duplicate-blocked plans by ALL attempted plans (results + failures +
    # blocked), not by the planner-call count that repair retries inflate.
    from quant_forge.research_loop.service import _strategy_context_from_trace_entries

    def _plan_row(phase: str, status: str = "ready") -> dict:
        return {
            "run_id": "rd_FTR_SEED_20240101T000000000000Z_00000000",
            "phase": phase,
            "experiment_plan": {"status": status, "metadata": {}},
        }

    entries = [
        # One lane: initial plan + two repair retries, all traced as plans,
        # ending duplicate-blocked.
        _plan_row("experiment_plan"),
        _plan_row("experiment_plan"),
        _plan_row("experiment_plan"),
        _plan_row("plan_blocked", status="blocked_duplicate_formula"),
        # Second lane: ready plan that ran to a result.
        _plan_row("experiment_plan"),
        {
            "run_id": "rd_FTR_SEED_20240101T000000000000Z_00000000",
            "phase": "experiment_result",
            "gate_decision": {"accepted": False, "blocking_reasons": []},
            "backtest_summary": {},
            "artifact_refs": {},
        },
    ]

    context = _strategy_context_from_trace_entries(entries)
    # 1 duplicate out of 2 attempted plans; the old planner-call denominator
    # (4 experiment_plan rows) would have reported 0.25.
    assert context.duplicate_rate == pytest.approx(0.5)


def test_strategy_context_uses_round_winner_not_last_traced_result() -> None:
    # C1: candidates are traced in EVALUATION order while the round is sorted
    # afterwards, so a losing candidate traced last must never drive the next
    # round's selector. Rounds persisted after the fix carry the WINNER's
    # decay/gate reasons in the round summary; the selector prefers those.
    from quant_forge.research_loop.service import _strategy_context_from_trace_entries

    run_id = "rd_FTR_SEED_20240101T000000000000Z_00000000"
    winner_row = {
        "run_id": run_id,
        "phase": "experiment_result",
        "gate_decision": {"accepted": True, "blocking_reasons": []},
        "backtest_summary": {"oos_net_return_decay": 0.1},
        "artifact_refs": {},
    }
    loser_row_traced_last = {
        "run_id": run_id,
        "phase": "experiment_result",
        "gate_decision": {
            "accepted": False,
            "blocking_reasons": ["OOS net return decay exceeds 0.500000", "turnover_rate 2.000000 > 1.500000"],
        },
        "backtest_summary": {"oos_net_return_decay": 0.9},
        "artifact_refs": {},
    }
    summary_payload = {
        "seed_factor_id": "FTR_SEED",
        "round_index": 0,
        "planned_count": 2,
        "candidate_count": 2,
        "duplicate_count": 0,
        "duplicate_rate": 0.0,
        "seed_score": 0.5,
        "best_score": 1.0,
        "best_score_delta_vs_seed": 0.5,
        "accepted_candidate_ids": ["FTR_WINNER"],
        "winner_candidate_ref": "FTR_WINNER",
        "winner_oos_net_return_decay": 0.1,
        "winner_gate_blocking_reasons": [],
    }
    entries = [
        winner_row,
        loser_row_traced_last,
        {"run_id": run_id, "phase": "round_summary", "round_summary": summary_payload},
    ]

    context = _strategy_context_from_trace_entries(entries)
    # The WINNER's evidence, not the last-traced loser's.
    assert context.oos_net_return_decay == pytest.approx(0.1)
    assert context.gate_blocking_reasons == ()
    assert context.turnover_breach is False
    assert context.best_score_delta_vs_seed == pytest.approx(0.5)

    # Legacy traces (round summaries written BEFORE the winner-evidence keys)
    # keep the old best-effort fallback to the last-traced experiment_result.
    legacy_payload = {
        key: value for key, value in summary_payload.items() if not key.startswith("winner_")
    }
    legacy = _strategy_context_from_trace_entries(
        [winner_row, loser_row_traced_last, {"run_id": run_id, "phase": "round_summary", "round_summary": legacy_payload}]
    )
    assert legacy.oos_net_return_decay == pytest.approx(0.9)
    assert legacy.turnover_breach is True


def test_round_summary_persists_winner_evidence_for_next_round(tmp_path: Path) -> None:
    # C1 end-to-end: the persisted round summary names the round winner
    # (results[0] after sorting) and carries the winner's own decay evidence.
    from quant_forge.research_loop.service import _oos_net_return_decay_value

    paths = create_demo_workspace(tmp_path / "demo")
    service = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    result = service.run_once("FTR_DEMO_SMALL_CAP", max_candidates=2)

    assert result.trace_root is not None
    assert result.candidates
    rows = _trace_rows_for(result.trace_root)
    summary_rows = [row for row in rows if row["phase"] == "round_summary"]
    assert len(summary_rows) == 1
    summary = summary_rows[0]["round_summary"]
    winner = result.candidates[0]
    assert summary["winner_candidate_ref"] == winner.factor.factor_id
    expected_decay = _oos_net_return_decay_value(winner.backtest)
    if expected_decay is None:
        assert summary["winner_oos_net_return_decay"] is None
    else:
        assert summary["winner_oos_net_return_decay"] == pytest.approx(expected_decay)
    assert isinstance(summary["winner_gate_blocking_reasons"], list)
    if winner.gate_passed:
        assert summary["winner_gate_blocking_reasons"] == []


def test_read_recent_entries_seed_scoping_survives_other_seed_floods(tmp_path: Path) -> None:
    # C2: seed-chain scoping must happen BEFORE the read window limit. One
    # same-seed entry followed by 200+ other-seed entries used to erase the
    # seed's entire history from the selector context.
    import os

    from quant_forge.research_loop.service import _run_id_in_seed_chain

    store = ResearchTraceStore(tmp_path / "rd_trace")
    same_seed_run = "rd_FTR_SEED_20240101T000000000000Z_00000000"
    other_seed_run = "rd_FTR_OTHER_20240102T000000000000Z_00000001"
    store.append_trace({"run_id": same_seed_run, "phase": "round_summary", "round_summary": {"round_index": 0}})
    for index in range(205):
        store.append_trace({"run_id": other_seed_run, "phase": "experiment_plan", "lane_id": f"lane-{index}"})
    # Pin the same-seed trace file older so the unfiltered control below is
    # deterministic under mtime-ordered reads.
    same_seed_path = store.run_dir(same_seed_run) / "trace.jsonl"
    stat = same_seed_path.stat()
    os.utime(same_seed_path, (stat.st_atime - 3600, stat.st_mtime - 3600))

    def seed_filter(run_id: str) -> bool:
        return _run_id_in_seed_chain(
            run_id,
            seed_factor_id="FTR_SEED",
            current_run_id="rd_FTR_SEED_20240103T000000000000Z_00000002",
        )

    # Control (old behavior): the global window is saturated by the other seed.
    unfiltered = store.read_recent_entries(limit=200)
    assert all(entry["run_id"] == other_seed_run for entry in unfiltered)

    scoped = store.read_recent_entries(limit=200, run_id_filter=seed_filter)
    assert [entry["run_id"] for entry in scoped] == [same_seed_run]
    assert scoped[0]["round_summary"]["round_index"] == 0
