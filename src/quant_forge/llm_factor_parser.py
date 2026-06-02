"""LLM-assisted natural-language factor parsing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from quant_forge.config import LLMSettings
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.mcp.read_models import list_available_fields, list_available_operators


_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "protocol": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key_envs": ("OPENAI_API_KEY",),
        "model_envs": ("OPENAI_MODEL",),
        "base_url_envs": ("OPENAI_BASE_URL",),
    },
    "openai_compatible": {
        "protocol": "openai_compatible",
        "base_url": "",
        "api_key_envs": ("OPENAI_COMPATIBLE_API_KEY",),
        "model_envs": ("OPENAI_COMPATIBLE_MODEL",),
        "base_url_envs": ("OPENAI_COMPATIBLE_BASE_URL",),
    },
    "deepseek": {
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "api_key_envs": ("DEEPSEEK_API_KEY",),
        "model_envs": ("DEEPSEEK_MODEL",),
        "base_url_envs": ("DEEPSEEK_BASE_URL",),
        "default_model": "deepseek-chat",
    },
    "glm": {
        "protocol": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_envs": ("GLM_API_KEY", "ZHIPUAI_API_KEY"),
        "model_envs": ("GLM_MODEL", "ZHIPUAI_MODEL"),
        "base_url_envs": ("GLM_BASE_URL", "ZHIPUAI_BASE_URL"),
    },
    "claude": {
        "protocol": "anthropic_messages",
        "base_url": "https://api.anthropic.com",
        "api_key_envs": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
        "model_envs": ("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
        "base_url_envs": ("ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL"),
    },
    "minimax": {
        "protocol": "openai_compatible",
        "base_url": "https://api.minimax.io/v1",
        "api_key_envs": ("MINIMAX_API_KEY",),
        "model_envs": ("MINIMAX_MODEL",),
        "base_url_envs": ("MINIMAX_BASE_URL",),
        "default_model": "MiniMax-M2",
    },
}

_PROVIDER_ALIASES = {
    "anthropic": "claude",
    "zhipu": "glm",
    "zhipuai": "glm",
    "bigmodel": "glm",
}


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
        factor = parse_idea_to_definition(text)
        return ParsedFactor(factor=factor, source="rule", provider="rule", model="deterministic")
    return _parse_with_configured_llm(text, selected_llm)


def _parse_with_configured_llm(text: str, llm: LLMSettings) -> ParsedFactor:
    settings, credential, protocol = _resolved_llm_settings(llm)
    if protocol == "openai_compatible":
        return _parse_with_openai_compatible_llm(text, settings, credential)
    if protocol == "anthropic_messages":
        return _parse_with_anthropic_messages_llm(text, settings, credential)
    raise ValueError(f"unsupported LLM protocol: {protocol}")


def _parse_with_openai_compatible_llm(text: str, settings: LLMSettings, credential: str) -> ParsedFactor:
    request = urllib.request.Request(
        _chat_completions_url(settings.base_url),
        data=json.dumps(
            {
                "model": settings.model,
                "messages": _messages(text),
                "temperature": 0,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc

    content = _message_content(payload)
    factor = _factor_from_llm_json(_extract_json_object(content), text)
    return ParsedFactor(
        factor=factor,
        source="llm",
        provider=settings.provider,
        model=settings.model,
        raw_response=content,
    )


def _parse_with_anthropic_messages_llm(text: str, settings: LLMSettings, credential: str) -> ParsedFactor:
    system, user = _prompt_parts(text)
    request = urllib.request.Request(
        _anthropic_messages_url(settings.base_url),
        data=json.dumps(
            {
                "model": settings.model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "temperature": 0,
                "max_tokens": 1000,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "x-api-key": credential,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc

    content = _anthropic_message_content(payload)
    factor = _factor_from_llm_json(_extract_json_object(content), text)
    return ParsedFactor(
        factor=factor,
        source="llm",
        provider=settings.provider,
        model=settings.model,
        raw_response=content,
    )


def _resolved_llm_settings(llm: LLMSettings) -> tuple[LLMSettings, str, str]:
    provider = _canonical_provider(llm.provider)
    defaults = _PROVIDER_DEFAULTS.get(provider)
    if defaults is None:
        raise ValueError(f"unsupported LLM provider: {llm.provider}")

    model = _configured_value(llm.model, defaults["model_envs"], default=defaults.get("default_model", ""))
    if not model:
        model_envs = ", ".join(defaults["model_envs"])
        raise ValueError(f"LLM model is required for provider {provider}. Set llm.model or one of: {model_envs}.")
    base_url = _configured_value(llm.base_url, defaults["base_url_envs"], default=str(defaults.get("base_url", "")))
    if not base_url:
        base_url_envs = ", ".join(defaults["base_url_envs"])
        raise ValueError(f"LLM base_url is required for provider {provider}. Set llm.base_url or one of: {base_url_envs}.")

    credential, api_key_env = _read_api_key(provider, llm.api_key_env, tuple(defaults["api_key_envs"]))
    return (
        LLMSettings(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_seconds=llm.timeout_seconds,
            api_key_required=llm.api_key_required,
        ),
        credential,
        str(defaults["protocol"]),
    )


def _canonical_provider(provider: str) -> str:
    normalized = provider.lower().strip()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _configured_value(configured: str, env_names: tuple[str, ...], *, default: str = "") -> str:
    normalized = configured.strip()
    if normalized and normalized != "deterministic":
        return normalized
    for env_name in env_names:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return default


def _read_api_key(provider: str, configured_env_name: str, default_env_names: tuple[str, ...]) -> tuple[str, str]:
    env_names = (configured_env_name.strip(),) if configured_env_name.strip() else default_env_names
    for env_name in env_names:
        credential = os.environ.get(env_name)
        if credential:
            return credential, env_name
    expected = ", ".join(env_names)
    raise RuntimeError(
        f"Missing API key for active LLM provider {provider}. Expected environment variable: {expected}. "
        "Declare runtime.env_files in the local config before starting Quant Forge."
    )


def _messages(text: str) -> list[dict[str, str]]:
    system, user = _prompt_parts(text)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _prompt_parts(text: str) -> tuple[str, str]:
    fields = ", ".join(field["name"] for field in list_available_fields())
    operators = ", ".join(operator["name"] for operator in list_available_operators())
    system = (
        "You convert Chinese or English factor ideas into Quant Forge factor JSON. "
        "Return one JSON object only. Do not include markdown. "
        "Allowed formulas are intentionally small for this public workbench: "
        "-rank(market_cap) for small-cap ideas, rank(return_5d) for recent momentum, "
        "-rank(volatility_5d) for low-volatility ideas, rank(volume) for trading-volume strength, "
        "and rank(close) for close-price strength. "
        "Use universe_filters [\"is_st == false\"] only when the idea excludes ST stocks. "
        "Treat one month or next month as 21 trading days unless the user gives an explicit day count. "
        f"Available fields: {fields}. Available operators: {operators}."
    )
    user = (
        "请将下述文档或观点，解析为金融交易时的因子。"
        "对于多个表达式和观点需解析为对应数额的因子；对于模糊观点则可以解析为1-3个意思最为接近的因子。"
        "本次只返回最匹配的一个因子，JSON字段必须是 name, formula, description, horizon_days, universe_filters。\n\n"
        f"观点：{text}"
    )
    return system, user


def _message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("LLM response does not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RuntimeError("LLM response does not contain message content")
    return message["content"]


def _anthropic_message_content(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        raise RuntimeError("Claude response does not contain content blocks")
    texts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
    if not texts:
        raise RuntimeError("Claude response does not contain text content")
    return "\n".join(texts)


def _extract_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise RuntimeError("LLM response is not valid JSON")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response JSON must be an object")
    return parsed


def _factor_from_llm_json(payload: dict[str, Any], text: str) -> FactorDefinition:
    name = _slug(str(payload.get("name", "llm_factor")))
    formula = str(payload["formula"]).strip()
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


def _chat_completions_url(base_url: str) -> str:
    if not base_url:
        raise ValueError("LLM base_url is required")
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if re.search(r"/v\d+$", normalized):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _anthropic_messages_url(base_url: str) -> str:
    if not base_url:
        raise ValueError("LLM base_url is required")
    normalized = base_url.rstrip("/")
    if normalized.endswith("/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


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
