"""Shared LLM chat client for public Quant Forge workflows."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any
import urllib.error
import urllib.request

from quant_forge.config import LLMSettings


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
class LLMChatResult:
    content: str
    provider: str
    model: str


def generate_chat_text(
    llm: LLMSettings,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 1200,
) -> LLMChatResult:
    """Call the configured LLM and return text content.

    The public workbench only supports OpenAI-compatible chat completions and
    Anthropic messages. Local OpenAI-compatible endpoints may set
    ``api_key_required=False`` and will receive no Authorization header.
    """

    settings, credential, protocol = resolve_llm_runtime(llm)
    if protocol == "openai_compatible":
        content = _generate_openai_compatible_text(
            settings,
            credential,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    elif protocol == "anthropic_messages":
        content = _generate_anthropic_messages_text(
            settings,
            credential,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    else:
        raise ValueError(f"unsupported LLM protocol: {protocol}")
    return LLMChatResult(content=content, provider=settings.provider, model=settings.model)


def resolve_llm_runtime(llm: LLMSettings) -> tuple[LLMSettings, str, str]:
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

    credential, api_key_env = (
        _read_api_key(provider, llm.api_key_env, tuple(defaults["api_key_envs"]))
        if llm.api_key_required
        else ("", llm.api_key_env.strip())
    )
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


def extract_json_object(content: str) -> dict[str, Any]:
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


def _generate_openai_compatible_text(
    settings: LLMSettings,
    credential: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(
        _chat_completions_url(settings.base_url),
        data=json.dumps(
            {
                "model": settings.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers=headers,
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
    return _message_content(payload)


def _generate_anthropic_messages_text(
    settings: LLMSettings,
    credential: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    system = "\n\n".join(message["content"] for message in messages if message.get("role") == "system")
    user_content = "\n\n".join(message["content"] for message in messages if message.get("role") != "system")
    headers = {
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    if credential:
        headers["x-api-key"] = credential
    request = urllib.request.Request(
        _anthropic_messages_url(settings.base_url),
        data=json.dumps(
            {
                "model": settings.model,
                "system": system,
                "messages": [{"role": "user", "content": user_content}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers=headers,
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
    return _anthropic_message_content(payload)


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
