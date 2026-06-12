"""LLM adapters for public RD research loops."""

from __future__ import annotations

import json
from typing import Any

from quant_forge.config import LLMSettings
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition
from quant_forge.factor_library.catalog import is_precomputed_formula
from quant_forge.llm_client import extract_json_object, generate_chat_text
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.contracts import ResearchContext
from quant_forge.research_loop.llm_contracts import (
    RD_LLM_SCHEMA_VERSION,
    normalize_review_payload,
)
from quant_forge.research_loop.service import (
    ResearchGenerationMetadata,
    ResearchHypothesis,
    ResearchSelfReview,
)


class LLMHypothesisGenerator:
    """Generate bounded RD hypotheses with the configured shared LLM."""

    def __init__(self, llm: LLMSettings) -> None:
        self.llm = _require_non_rule_llm(llm, feature="RD LLM hypothesis generation")
        self._metadata = ResearchGenerationMetadata(
            source="llm_hypothesis",
            provider=self.llm.provider,
            model=self.llm.model,
        )

    def metadata(self) -> ResearchGenerationMetadata:
        return self._metadata

    def generate(
        self,
        seed: FactorDefinition,
        *,
        objective: str,
        max_candidates: int,
    ) -> tuple[ResearchHypothesis, ...]:
        return self.generate_with_context(seed, context=None, objective=objective, max_candidates=max_candidates)

    def generate_with_context(
        self,
        seed: FactorDefinition,
        *,
        context: ResearchContext | None,
        objective: str,
        max_candidates: int,
    ) -> tuple[ResearchHypothesis, ...]:
        result = generate_chat_text(
            self.llm,
            _hypothesis_messages(seed, context=context, objective=objective, max_candidates=max_candidates),
            temperature=0.2,
            max_tokens=1200,
        )
        payload = extract_json_object(result.content)
        hypotheses = _hypotheses_from_payload(payload, max_candidates=max_candidates)
        self._metadata = ResearchGenerationMetadata(
            source="llm_hypothesis",
            provider=result.provider,
            model=result.model,
            raw_response=result.content,
        )
        return hypotheses

    def repair_invalid_hypothesis(
        self,
        seed: FactorDefinition,
        *,
        hypothesis: ResearchHypothesis,
        context: ResearchContext,
        objective: str,
        validation_error: str,
        attempt: int,
        max_attempts: int,
    ) -> ResearchHypothesis | None:
        result = generate_chat_text(
            self.llm,
            _repair_messages(
                seed=seed,
                hypothesis=hypothesis,
                context=context,
                objective=objective,
                validation_error=validation_error,
                attempt=attempt,
                max_attempts=max_attempts,
            ),
            temperature=0.1,
            max_tokens=900,
        )
        payload = extract_json_object(result.content)
        repaired = _hypotheses_from_payload(payload, max_candidates=1)
        self._metadata = ResearchGenerationMetadata(
            source="llm_formula_repair",
            provider=result.provider,
            model=result.model,
            raw_response=result.content,
        )
        return repaired[0] if repaired else None


class LLMResearchReviewGenerator:
    """Review RD candidate evidence with the configured shared LLM."""

    def __init__(self, llm: LLMSettings) -> None:
        self.llm = _require_non_rule_llm(llm, feature="RD LLM self-review")

    def review(
        self,
        *,
        seed: FactorDefinition,
        candidate: FactorDefinition,
        evaluation: EvaluationResult,
        backtest: BacktestResult,
        split_weighted_icir: float,
        score: float,
        gate_passed: bool,
        gate_reasons: tuple[str, ...],
    ) -> ResearchSelfReview:
        result = generate_chat_text(
            self.llm,
            _review_messages(
                seed=seed,
                candidate=candidate,
                evaluation=evaluation,
                backtest=backtest,
                split_weighted_icir=split_weighted_icir,
                score=score,
                gate_passed=gate_passed,
                gate_reasons=gate_reasons,
            ),
            temperature=0.1,
            max_tokens=1200,
        )
        payload = extract_json_object(result.content)
        fallback_summary = _fallback_review_summary(
            candidate=candidate,
            score=score,
            split_weighted_icir=split_weighted_icir,
            gate_passed=gate_passed,
        )
        normalized = normalize_review_payload(payload, fallback_summary=fallback_summary)
        payload = normalized.payload
        return ResearchSelfReview(
            source="llm_self_review",
            summary=str(payload["summary"]).strip(),
            strengths=_string_tuple(payload.get("strengths")),
            risks=_string_tuple(payload.get("risks")),
            next_hypotheses=_string_tuple(payload.get("next_hypotheses")),
            normalization_warnings=normalized.normalization_warnings,
        )


def _require_non_rule_llm(llm: LLMSettings, *, feature: str) -> LLMSettings:
    selected = llm.select_provider()
    if selected.provider.lower() in {"rule", "deterministic"}:
        raise RuntimeError(f"{feature} requires a configured LLM provider; selected provider is local rule.")
    return selected


def _hypothesis_messages(
    seed: FactorDefinition,
    *,
    context: ResearchContext | None = None,
    objective: str,
    max_candidates: int,
) -> list[dict[str, str]]:
    field_catalog = _catalog_for_prompt(
        context.field_catalog if context is not None and context.field_catalog else tuple(list_available_fields())
    )
    operator_catalog = _catalog_for_prompt(
        context.operator_catalog
        if context is not None and context.operator_catalog
        else tuple(list_available_operators())
    )
    effective_ideas = list(context.effective_ideas)[:10] if context is not None else []
    next_hints = list(context.next_focus_hints)[:10] if context is not None else []
    system = (
        "You are Quant Forge's RD hypothesis generator. Return one JSON object only. "
        "Generate bounded factor research hypotheses with executable formula_dsl. "
        "Use financial analyst reasoning, effective-idea replay, and operator-aware variants. "
        "Use only listed fields, listed operators, and safe arithmetic (+, -, *, /). "
        "Never include precomputed: factor references inside formula_dsl; the local system handles seed "
        "parameter search separately. Do not use placeholder fields such as seed, seed_score, or factor_score. "
        "Mention non-ST only when the seed uses an is_st filter. "
        "Do not invent data fields or unsupported formulas. Do not request parameter-search fallback; "
        "if no executable idea is available, return an empty hypotheses list."
    )
    user = (
        f"Seed factor:\n{json.dumps(_factor_summary(seed), ensure_ascii=False)}\n\n"
        f"Objective: {objective}\n"
        f"Available fields: {json.dumps(field_catalog, ensure_ascii=False)}\n"
        f"Available operators: {json.dumps(operator_catalog, ensure_ascii=False)}\n"
        f"Effective ideas: {json.dumps(effective_ideas, ensure_ascii=False)}\n"
        f"Recent failure hints: {json.dumps(next_hints, ensure_ascii=False)}\n"
        f"Generate up to {max_candidates} distinct hypotheses. "
        "Return JSON shape: "
        "{\"schema_version\":\"qf.rd.llm.v1\",\"task_type\":\"rd_research_hypotheses\","
        "\"hypotheses\":[{\"text\":\"...\",\"rationale\":\"...\","
        "\"formula_dsl\":\"rank(delta(close, 5))\","
        "\"input_fields\":[\"close\"],\"expected_direction\":\"positive\","
        "\"universe_constraints\":[\"is_st == false\"],"
        "\"source\":\"financial_analyst|effective_idea|operator_mcp\","
        "\"source_detail\":\"...\",\"parameter_search_fallback\":false}]}. "
        "Always set parameter_search_fallback=false for LLM hypotheses."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _repair_messages(
    *,
    seed: FactorDefinition,
    hypothesis: ResearchHypothesis,
    context: ResearchContext,
    objective: str,
    validation_error: str,
    attempt: int,
    max_attempts: int,
) -> list[dict[str, str]]:
    field_catalog = _catalog_for_prompt(context.field_catalog or tuple(list_available_fields()))
    operator_catalog = _catalog_for_prompt(context.operator_catalog or tuple(list_available_operators()))
    system = (
        "You are Quant Forge's RD formula repair adapter. Return one JSON object only. "
        "Repair the given hypothesis so formula_dsl passes local validation. "
        "Use only listed fields, listed operators, numeric window arguments where required, and safe arithmetic. "
        "Do not change the research intent unless needed to make the formula executable. "
        "Do not request parameter-search fallback. If you cannot repair it, return an empty hypotheses list."
    )
    user = json.dumps(
        {
            "seed": _factor_summary(seed),
            "objective": objective,
            "repair_attempt": attempt,
            "max_repair_attempts": max_attempts,
            "validation_error": validation_error,
            "invalid_hypothesis": {
                "text": hypothesis.text,
                "rationale": hypothesis.rationale,
                "formula_dsl": hypothesis.formula_dsl,
                "input_fields": hypothesis.input_fields,
                "expected_direction": hypothesis.expected_direction,
                "universe_constraints": hypothesis.universe_constraints,
                "source": hypothesis.source,
                "source_detail": hypothesis.source_detail,
            },
            "available_fields": field_catalog,
            "available_operators": operator_catalog,
            "return_shape": {
                "schema_version": RD_LLM_SCHEMA_VERSION,
                "task_type": "rd_research_hypotheses",
                "hypotheses": [
                    {
                        "text": "same research idea, repaired",
                        "rationale": "why this executable repair preserves the idea",
                        "formula_dsl": "rank(delta(close, 5))",
                        "input_fields": ["close"],
                        "expected_direction": "positive",
                        "universe_constraints": ["is_st == false"],
                        "source": "llm",
                        "source_detail": "formula_repair",
                        "parameter_search_fallback": False,
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _catalog_for_prompt(items: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        catalog.append(
            {
                "name": name,
                "description": str(item.get("description", "")).strip(),
            }
        )
    return catalog


def _review_messages(
    *,
    seed: FactorDefinition,
    candidate: FactorDefinition,
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    split_weighted_icir: float,
    score: float,
    gate_passed: bool,
    gate_reasons: tuple[str, ...],
) -> list[dict[str, str]]:
    system = (
        "You are Quant Forge's RD reviewer. Return one JSON object only. "
        "Use the provided local evaluation/backtest evidence. Do not claim production readiness. "
        "Return exactly the requested schema keys. Do not wrap the JSON in markdown."
    )
    user = json.dumps(
        {
            "seed": _factor_summary(seed),
            "candidate": _factor_summary(candidate),
            "evidence": {
                "rank_ic_mean": evaluation.rank_ic_mean,
                "rank_icir": evaluation.rank_icir,
                "coverage": evaluation.coverage,
                "ic_days": evaluation.ic_days,
                "split_weighted_icir": split_weighted_icir,
                "score": score,
                "net_annualized_return": backtest.net_annualized_return,
                "net_long_short_sharpe": backtest.net_long_short_sharpe,
                "turnover_rate": backtest.turnover_rate,
                "rebalance_rate": backtest.rebalance_rate,
                "max_drawdown": backtest.max_drawdown,
                "warnings": tuple(backtest.warnings),
                "gate_passed": gate_passed,
                "gate_reasons": gate_reasons,
            },
            "return_shape": {
                "schema_version": RD_LLM_SCHEMA_VERSION,
                "task_type": "rd_research_review",
                "summary": "one sentence",
                "strengths": ["short bullets"],
                "risks": ["short bullets"],
                "next_hypotheses": ["bounded next research ideas"],
                "normalization_warnings": [],
            },
        },
        ensure_ascii=False,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _factor_summary(factor: FactorDefinition) -> dict[str, Any]:
    return {
        "factor_id": factor.factor_id,
        "name": factor.name,
        "formula": _formula_for_prompt(factor.formula),
        "description": factor.description,
        "horizon_days": factor.horizon_days,
        "universe_filters": factor.universe_filters,
        "source": factor.source,
    }


def _formula_for_prompt(formula: str) -> str:
    if is_precomputed_formula(formula):
        return "<mounted_precomputed_reference_not_usable_in_formula>"
    return formula


def _hypotheses_from_payload(payload: dict[str, Any], *, max_candidates: int) -> tuple[ResearchHypothesis, ...]:
    raw = payload.get("hypotheses")
    if not isinstance(raw, list):
        raise RuntimeError("RD LLM response must include hypotheses list")
    parsed: list[ResearchHypothesis] = []
    provider_fallback_seen: list[bool] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("each RD LLM hypothesis must be an object")
        text = _required_str(item, "text")
        rationale = str(item.get("rationale", "")).strip()
        if text in seen:
            continue
        raw_source = item.get("source")
        formula_dsl = str(item.get("formula_dsl", "")).strip()
        provider_requested_parameter_search = bool(item.get("parameter_search_fallback", False)) or (
            str(raw_source or "").strip() == "parameter_search"
        )
        parsed.append(
            ResearchHypothesis(
                text=text,
                rationale=rationale,
                source=_hypothesis_source(raw_source),
                source_detail=str(item.get("source_detail", "")).strip(),
                parameter_search_fallback=False,
                formula_dsl=formula_dsl,
                input_fields=_string_tuple(item.get("input_fields")),
                expected_direction=_expected_direction(item.get("expected_direction")),
                universe_constraints=_string_tuple(item.get("universe_constraints")),
            )
        )
        provider_fallback_seen.append(provider_requested_parameter_search)
        seen.add(text)
    hypotheses = _drop_provider_fallbacks_when_regular_ideas_exist(parsed, provider_fallback_seen)[:max_candidates]
    return tuple(hypotheses)


def _drop_provider_fallbacks_when_regular_ideas_exist(
    hypotheses: list[ResearchHypothesis],
    provider_fallback_seen: list[bool],
) -> list[ResearchHypothesis]:
    if any(not is_fallback for is_fallback in provider_fallback_seen):
        return [item for item, is_fallback in zip(hypotheses, provider_fallback_seen, strict=True) if not is_fallback]
    return hypotheses


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise RuntimeError(f"LLM response missing required field: {key}")
    return value


def _fallback_review_summary(
    *,
    candidate: FactorDefinition,
    score: float,
    split_weighted_icir: float,
    gate_passed: bool,
) -> str:
    status = "passed" if gate_passed else "did not pass"
    return (
        f"{candidate.factor_id} {status} the research gate with score {score:.4f}; "
        f"weighted split ICIR is {split_weighted_icir:.4f}."
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError("LLM response list field must be a list")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _hypothesis_source(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"financial_analyst", "effective_idea", "operator_mcp", "llm"}:
        return text
    return "llm"


def _expected_direction(value: Any) -> str:
    text = str(value or "positive").strip().lower()
    if text in {"positive", "negative", "unknown"}:
        return text
    return "positive"
