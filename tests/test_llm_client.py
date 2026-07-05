from __future__ import annotations

from io import BytesIO
import urllib.error

import pytest

from quant_forge.config import LLMSettings
from quant_forge import llm_client
from quant_forge.llm_client import extract_json_object, generate_chat_text


def test_extract_json_object_ignores_trailing_prose_braces() -> None:
    content = 'Here is my answer: {"result": "ok"}. Note: use {curly} braces.'
    assert extract_json_object(content) == {"result": "ok"}


def test_extract_json_object_parses_fenced_block() -> None:
    content = '```json\n{"a": 1, "b": {"c": 2}}\n```'
    assert extract_json_object(content) == {"a": 1, "b": {"c": 2}}


def test_extract_json_object_skips_leading_non_object_braces() -> None:
    content = 'noise {not json} then the real one: {"value": 42} tail'
    assert extract_json_object(content) == {"value": 42}


def test_extract_json_object_raises_without_json() -> None:
    with pytest.raises(RuntimeError):
        extract_json_object("there is no object here at all")


def test_openai_compatible_llm_retries_transient_http_error(monkeypatch) -> None:
    monkeypatch.setenv("QF_TEST_LLM_API_KEY", "not-a-real-key")
    monkeypatch.setattr(llm_client.time, "sleep", lambda _seconds: None)
    attempts = {"count": 0}

    def fake_urlopen(_request, *, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _http_error(503, b'{"error":{"message":"busy"}}')
        return _Response(b'{"choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

    result = generate_chat_text(
        LLMSettings(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key_env="QF_TEST_LLM_API_KEY",
            timeout_seconds=1.0,
        ),
        [{"role": "user", "content": "hello"}],
    )

    assert result.content == "ok"
    assert attempts["count"] == 2


def test_openai_compatible_llm_does_not_retry_auth_error(monkeypatch) -> None:
    monkeypatch.setenv("QF_TEST_LLM_API_KEY", "not-a-real-key")
    attempts = {"count": 0}

    def fake_urlopen(_request, *, timeout):
        attempts["count"] += 1
        raise _http_error(401, b'{"error":{"message":"bad key"}}')

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        generate_chat_text(
            LLMSettings(
                provider="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key_env="QF_TEST_LLM_API_KEY",
                timeout_seconds=1.0,
            ),
            [{"role": "user", "content": "hello"}],
        )

    assert attempts["count"] == 1


def test_openai_compatible_llm_retries_timeout_before_standardizing_error(monkeypatch) -> None:
    monkeypatch.setenv("QF_TEST_LLM_API_KEY", "not-a-real-key")
    monkeypatch.setattr(llm_client.time, "sleep", lambda _seconds: None)
    attempts = {"count": 0}

    def fake_urlopen(_request, *, timeout):
        attempts["count"] += 1
        raise TimeoutError("read operation timed out")

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="LLM request timed out"):
        generate_chat_text(
            LLMSettings(
                provider="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key_env="QF_TEST_LLM_API_KEY",
                timeout_seconds=1.0,
            ),
            [{"role": "user", "content": "hello"}],
        )

    assert attempts["count"] == 3


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.example.invalid/v1/chat/completions",
        code=code,
        msg="error",
        hdrs={},
        fp=BytesIO(body),
    )
