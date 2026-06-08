"""Structured LLM payload contracts for RD workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RD_LLM_SCHEMA_VERSION = "qf.rd.llm.v1"


@dataclass(frozen=True)
class NormalizedLLMPayload:
    payload: dict[str, Any]
    normalization_warnings: tuple[str, ...] = ()


def normalize_review_payload(payload: dict[str, Any], *, fallback_summary: str) -> NormalizedLLMPayload:
    """Normalize provider JSON into the RD review contract.

    The prompt asks providers to return the full schema, but local code keeps
    the contract stable when a provider omits optional metadata or summary.
    """

    normalized = dict(payload)
    warnings: list[str] = []
    if str(normalized.get("schema_version", "")).strip() != RD_LLM_SCHEMA_VERSION:
        normalized["schema_version"] = RD_LLM_SCHEMA_VERSION
        warnings.append("schema_version_missing_or_mismatched")
    if str(normalized.get("task_type", "")).strip() != "rd_research_review":
        normalized["task_type"] = "rd_research_review"
        warnings.append("task_type_missing_or_mismatched")
    if not str(normalized.get("summary", "")).strip():
        normalized["summary"] = fallback_summary
        warnings.append("summary_missing")
    for field in ("strengths", "risks", "next_hypotheses", "normalization_warnings"):
        if normalized.get(field) is None:
            normalized[field] = []
            warnings.append(f"{field}_missing")
        elif not isinstance(normalized.get(field), list):
            normalized[field] = [str(normalized[field])]
            warnings.append(f"{field}_coerced_to_list")
    normalized["normalization_warnings"] = warnings
    return NormalizedLLMPayload(normalized, tuple(normalized["normalization_warnings"]))


def normalize_campaign_payload(payload: dict[str, Any], *, fallback_summary: str) -> NormalizedLLMPayload:
    normalized = dict(payload)
    warnings: list[str] = []
    if str(normalized.get("schema_version", "")).strip() != RD_LLM_SCHEMA_VERSION:
        normalized["schema_version"] = RD_LLM_SCHEMA_VERSION
        warnings.append("schema_version_missing_or_mismatched")
    if str(normalized.get("task_type", "")).strip() != "rd_campaign_plan":
        normalized["task_type"] = "rd_campaign_plan"
        warnings.append("task_type_missing_or_mismatched")
    if not str(normalized.get("summary", "")).strip():
        normalized["summary"] = fallback_summary
        warnings.append("summary_missing")
    if normalized.get("strategy_names") is None:
        normalized["strategy_names"] = []
        warnings.append("strategy_names_missing")
    elif not isinstance(normalized.get("strategy_names"), list):
        normalized["strategy_names"] = [str(normalized["strategy_names"])]
        warnings.append("strategy_names_coerced_to_list")
    normalized["normalization_warnings"] = warnings
    return NormalizedLLMPayload(normalized, tuple(normalized["normalization_warnings"]))
