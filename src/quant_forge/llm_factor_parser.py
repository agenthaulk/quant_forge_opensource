"""LLM-assisted natural-language factor parsing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from quant_forge.config import LLMSettings
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.llm_client import extract_json_object, generate_chat_text
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.operator_registry.resolver import resolve_formula_operators


@dataclass(frozen=True)
class ParsedFactor:
    factor: FactorDefinition
    source: str
    provider: str
    model: str
    raw_response: str = ""


def parse_factor_idea(text: str, llm: LLMSettings, *, mode: str = "llm") -> ParsedFactor:
    """Parse user text into a validated factor definition."""

    if mode == "rule":
        factor = parse_idea_to_definition(text)
        return ParsedFactor(factor=factor, source="rule", provider="rule", model="deterministic")
    if mode != "llm":
        raise ValueError(f"unsupported parser mode: {mode}")
    selected_llm = llm.select_provider()
    if selected_llm.provider.lower() in {"rule", "deterministic"}:
        raise RuntimeError("LLM parser was requested, but the selected provider is the local rule parser.")
    return _parse_with_configured_llm(text, selected_llm)


def _parse_with_configured_llm(text: str, llm: LLMSettings) -> ParsedFactor:
    result = generate_chat_text(llm, _messages(text), temperature=0, max_tokens=1000)
    factor = _factor_from_llm_json(extract_json_object(result.content), text)
    return ParsedFactor(
        factor=factor,
        source="llm",
        provider=result.provider,
        model=result.model,
        raw_response=result.content,
    )


def _messages(text: str) -> list[dict[str, str]]:
    system, user = _prompt_parts(text)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _prompt_parts(text: str) -> tuple[str, str]:
    fields = ", ".join(field["name"] for field in list_available_fields())
    operators = json.dumps(list_available_operators(), ensure_ascii=False)
    system = (
        "You convert Chinese or English factor ideas into Quant Forge factor JSON. "
        "Return one JSON object only. Do not include markdown. "
        "Use only canonical operator names from operator_catalog[].name. "
        "Aliases may appear in aliases_for_recognition_only, but you must never generate aliases. "
        "Do not invent operators or fields. "
        "Allowed formulas are intentionally small for this public workbench: "
        "-rank(market_cap) for small-cap ideas, rank(return_5d) for recent momentum, "
        "-rank(volatility_5d) for low-volatility ideas, rank(volume) for trading-volume strength, "
        "and rank(close) for close-price strength. "
        "Use universe_filters [\"is_st == false\"] only when the idea excludes ST stocks. "
        "Treat one month or next month as 21 trading days unless the user gives an explicit day count. "
        f"Available fields: {fields}. operator_catalog: {operators}."
    )
    user = (
        "请将下述文档或观点，解析为金融交易时的因子。"
        "对于多个表达式和观点需解析为对应数额的因子；对于模糊观点则可以解析为1-3个意思最为接近的因子。"
        "本次只返回最匹配的一个因子，JSON字段必须是 name, formula, description, horizon_days, universe_filters。\n\n"
        f"观点：{text}"
    )
    return system, user


def _factor_from_llm_json(payload: dict[str, Any], text: str) -> FactorDefinition:
    name = _slug(str(payload.get("name", "llm_factor")))
    raw_formula = str(payload["formula"]).strip()
    resolution = resolve_formula_operators(raw_formula)
    if not resolution.executable:
        details = json.dumps(resolution.to_dict(), ensure_ascii=False, sort_keys=True)
        raise RuntimeError(f"LLM formula failed operator registry gate: {details}")
    formula = resolution.canonical_formula
    description = str(payload.get("description", "")).strip()
    horizon_days = _normalize_horizon_days(int(payload.get("horizon_days", 5)), text)
    filters_raw = payload.get("universe_filters", [])
    if not isinstance(filters_raw, list):
        raise RuntimeError("LLM field universe_filters must be a list")
    filters = tuple(str(item) for item in filters_raw)
    digest = hashlib.sha1(f"{name}:{formula}:{horizon_days}:{filters}:{text}".encode("utf-8")).hexdigest()[:8].upper()
    return FactorDefinition(
        factor_id=f"FTR_LLM_{digest}",
        name=name,
        formula=formula,
        status="draft",
        description=description,
        horizon_days=horizon_days,
        universe_filters=filters,
        source="llm",
    )

def _normalize_horizon_days(horizon_days: int, text: str) -> int:
    if horizon_days < 1:
        raise ValueError("horizon_days must be positive")
    normalized = text.lower()
    has_explicit_day_count = re.search(r"\d+\s*(?:个)?(?:交易日|日|天|trading\s+days?)", normalized)
    has_month_phrase = re.search(r"(?:一个|1|一)\s*个月|未来\s*一月|next\s+month|one\s+month", normalized)
    if has_month_phrase and not has_explicit_day_count:
        return 21
    return horizon_days


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return value.strip("_") or "llm_factor"
