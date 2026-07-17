"""Shared LLM chat client for public Quant Forge workflows."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
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


def canonical_provider_name(provider: str) -> str:
    """Public alias-aware normalization (``anthropic`` -> ``claude``)."""

    return _canonical_provider(provider)


def builtin_provider_presets() -> tuple[dict[str, str], ...]:
    """Public metadata for the built-in provider presets — never key material.

    The web settings surface offers these so a user can enable a provider
    without editing YAML. Each entry carries only defaults from
    ``_PROVIDER_DEFAULTS``: the canonical name, default ``base_url``/``model``
    ("" when the preset has no default and the user must supply one), and the
    default environment-variable NAME the credential is read from.
    """

    return tuple(
        {
            "provider": name,
            "model": str(defaults.get("default_model", "")),
            "base_url": str(defaults.get("base_url", "")),
            "api_key_env": str(defaults["api_key_envs"][0]),
        }
        for name, defaults in _PROVIDER_DEFAULTS.items()
    )

_TRANSIENT_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_MAX_HTTP_ATTEMPTS = 3
_HTTP_RETRY_BACKOFF_SECONDS = (0.25, 1.0)
# Provider error bodies are non-model provider-channel bytes; only a short
# single-line extract may enter exception text (P3), because str(exc) can
# propagate into plan blocking reasons and operator-facing surfaces.
_HTTP_ERROR_BODY_MAX_CHARS = 120


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
    retry_timeouts: bool = True,
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
            retry_timeouts=retry_timeouts,
        )
    elif protocol == "anthropic_messages":
        content = _generate_anthropic_messages_text(
            settings,
            credential,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            retry_timeouts=retry_timeouts,
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
        parsed = _decode_first_json_object(cleaned)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response JSON must be an object")
    return parsed


def _decode_first_json_object(cleaned: str) -> Any:
    """Return the first complete JSON object embedded in ``cleaned``.

    Scans from each ``{`` in order, attempting a raw decode so that trailing
    prose or stray braces after a valid object do not corrupt the parse.
    """

    decoder = json.JSONDecoder()
    index = cleaned.find("{")
    while index != -1:
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            index = cleaned.find("{", index + 1)
            continue
        return parsed
    raise RuntimeError("LLM response is not valid JSON")


def _generate_openai_compatible_text(
    settings: LLMSettings,
    credential: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    retry_timeouts: bool,
) -> str:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    body = json.dumps(
        {
            "model": settings.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    payload = _urlopen_json_with_retries(
        lambda: urllib.request.Request(
            _chat_completions_url(settings.base_url),
            data=body,
            headers=headers,
            method="POST",
        ),
        timeout_seconds=settings.timeout_seconds,
        retry_timeouts=retry_timeouts,
    )
    return _message_content(payload)


def _generate_anthropic_messages_text(
    settings: LLMSettings,
    credential: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    retry_timeouts: bool,
) -> str:
    system = "\n\n".join(message["content"] for message in messages if message.get("role") == "system")
    user_content = "\n\n".join(message["content"] for message in messages if message.get("role") != "system")
    headers = {
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    if credential:
        headers["x-api-key"] = credential
    body = json.dumps(
        {
            "model": settings.model,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    payload = _urlopen_json_with_retries(
        lambda: urllib.request.Request(
            _anthropic_messages_url(settings.base_url),
            data=body,
            headers=headers,
            method="POST",
        ),
        timeout_seconds=settings.timeout_seconds,
        retry_timeouts=retry_timeouts,
    )
    return _anthropic_message_content(payload)


def _urlopen_json_with_retries(
    request_factory: Any,
    *,
    timeout_seconds: float,
    retry_timeouts: bool = True,
) -> dict[str, Any]:
    for attempt in range(1, _MAX_HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request_factory(), timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if _should_retry_http(exc.code, attempt):
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {_error_body_extract(body)}") from exc
        except urllib.error.URLError as exc:
            if attempt < _MAX_HTTP_ATTEMPTS:
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            if retry_timeouts and attempt < _MAX_HTTP_ATTEMPTS:
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError("LLM request timed out") from exc
    raise RuntimeError("LLM request failed after retries")


def _error_body_extract(body: str) -> str:
    """Single-line, length-capped extract of a provider HTTP error body."""

    return " ".join(body.split())[:_HTTP_ERROR_BODY_MAX_CHARS]


def _should_retry_http(status_code: int, attempt: int) -> bool:
    return status_code in _TRANSIENT_HTTP_STATUS_CODES and attempt < _MAX_HTTP_ATTEMPTS


def _sleep_before_retry(attempt: int) -> None:
    delay_index = min(max(attempt - 1, 0), len(_HTTP_RETRY_BACKOFF_SECONDS) - 1)
    time.sleep(_HTTP_RETRY_BACKOFF_SECONDS[delay_index])


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
