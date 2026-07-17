"""Runtime LLM settings endpoint (``POST /api/settings/llm``).

Pins the security contract of the web key-intake path:

- provider registration/switching works for YAML-configured providers AND
  built-in presets (no config edit needed);
- the API key is injected into the server process environment only — it never
  appears in any response payload and never reaches the workspace on disk;
- the endpoint is control-token gated exactly like every other POST;
- malformed inputs (unknown provider, whitespace keys, non-http(s) base_url,
  preset without a model) are rejected with 400.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import (
    LLMProviderSettings,
    LLMSettings,
    QuantForgeConfig,
    WebSettings,
    llm_settings_with_provider_update,
)
from quant_forge.data.local import create_demo_workspace
from quant_forge.llm_client import builtin_provider_presets

_DS_ENV = "QF_LLMSET_DS_KEY"
_DUMMY_KEY = "qf-dummy-runtime-credential-0123456789"


def _config_with_deepseek(tmp_path: Path) -> QuantForgeConfig:
    return QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/v1",
                    api_key_env=_DS_ENV,
                ),
            },
        )
    ).resolve(tmp_path / "demo")


@pytest.fixture()
def llm_web_app(tmp_path, monkeypatch):
    # The glm preset injects into the REAL preset env name; snapshot both so
    # the test never leaks state into (or reads state from) the dev machine.
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv(_DS_ENV, raising=False)
    create_demo_workspace(tmp_path / "demo")
    server = create_local_web_server(
        host="127.0.0.1", port=0, config=_config_with_deepseek(tmp_path)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_status_options_offer_builtin_presets(llm_web_app) -> None:
    status, body = _get(f"{llm_web_app}/api/status")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    options = {option["provider"]: option for option in payload["llm"]["providers"]}

    assert options["deepseek"]["configured"] == "true"
    for preset in ("glm", "claude", "openai", "minimax", "openai_compatible"):
        assert options[preset]["configured"] == "false", preset
        assert options[preset]["runtime_ready"] == "false", preset


def test_llm_settings_registers_preset_and_switches_active(llm_web_app) -> None:
    status, body = _post(
        f"{llm_web_app}/api/settings/llm",
        {"provider": "glm", "model": "glm-test-model", "api_key": _DUMMY_KEY},
    )
    assert status == 200
    response = json.loads(body.decode("utf-8"))
    assert response["ok"] is True
    assert response["key_updated"] is True
    assert response["llm"]["provider"] == "glm"
    assert response["rd"]["provider"] == "glm"
    # The credential value must never ride back in ANY response body.
    assert _DUMMY_KEY not in body.decode("utf-8")
    assert os.environ.get("GLM_API_KEY") == _DUMMY_KEY

    status, body = _get(f"{llm_web_app}/api/status")
    assert status == 200
    assert _DUMMY_KEY not in body.decode("utf-8")
    payload = json.loads(body.decode("utf-8"))
    assert payload["llm"]["provider"] == "glm"
    glm = next(o for o in payload["llm"]["providers"] if o["provider"] == "glm")
    assert glm["configured"] == "true"
    assert glm["runtime_ready"] == "true"

    # Switching back to a YAML-configured provider needs no key material.
    os.environ[_DS_ENV] = "preexisting-env-credential"
    status, body = _post(f"{llm_web_app}/api/settings/llm", {"provider": "deepseek"})
    assert status == 200
    response = json.loads(body.decode("utf-8"))
    assert response["key_updated"] is False
    assert response["llm"]["provider"] == "deepseek"


def test_llm_settings_accepts_provider_alias(llm_web_app) -> None:
    status, body = _post(
        f"{llm_web_app}/api/settings/llm",
        {"provider": "anthropic", "model": "claude-test-model", "api_key": _DUMMY_KEY + "-c"},
    )
    try:
        assert status == 200
        response = json.loads(body.decode("utf-8"))
        assert response["llm"]["provider"] == "claude"
        assert os.environ.get("ANTHROPIC_API_KEY") == _DUMMY_KEY + "-c"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_llm_settings_rejects_bad_inputs(llm_web_app) -> None:
    cases = (
        ({"provider": ""}, "provider is required"),
        ({"provider": "no-such-llm", "api_key": "k"}, "unknown LLM provider"),
        ({"provider": "glm", "model": "m", "api_key": "bad key"}, "whitespace"),
        ({"provider": "glm", "model": "m", "api_key": "k" * 513}, "too long"),
        (
            {"provider": "openai_compatible", "model": "m", "base_url": "file:///etc/hosts", "api_key": "k"},
            "http:// or https://",
        ),
        ({"provider": "openai", "api_key": "k"}, "model"),
        ({"provider": "rule"}, "built in"),
        ({"provider": "glm", "model": "m", "activate": "yes"}, "boolean"),
    )
    for payload, fragment in cases:
        status, body = _post(f"{llm_web_app}/api/settings/llm", payload)
        assert status == 400, payload
        assert fragment in json.loads(body.decode("utf-8"))["error"], payload
    # None of the rejected submissions may have touched the environment.
    assert os.environ.get("GLM_API_KEY") is None


def test_llm_settings_key_never_reaches_workspace_disk(llm_web_app, tmp_path) -> None:
    status, _ = _post(
        f"{llm_web_app}/api/settings/llm",
        {"provider": "glm", "model": "glm-test-model", "api_key": _DUMMY_KEY},
    )
    assert status == 200
    needle = _DUMMY_KEY.encode("utf-8")
    for path in (tmp_path / "demo").rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes(), f"credential persisted to {path}"


def test_llm_settings_requires_control_token_on_docker_bind(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QF_LLMSET_WEB_TOKEN", "secret-token")
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv(_DS_ENV, raising=False)
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_LLMSET_WEB_TOKEN"),
        llm=_config_with_deepseek(tmp_path).llm,
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, _ = _post(
            f"{base_url}/api/settings/llm",
            {"provider": "glm", "model": "m", "api_key": _DUMMY_KEY},
        )
        assert status == 401
        assert os.environ.get("GLM_API_KEY") is None

        status, body = _post(
            f"{base_url}/api/settings/llm",
            {"provider": "glm", "model": "glm-test-model", "api_key": _DUMMY_KEY},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert status == 200
        assert _DUMMY_KEY not in body.decode("utf-8")
        assert os.environ.get("GLM_API_KEY") == _DUMMY_KEY
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_builtin_presets_expose_metadata_only() -> None:
    presets = builtin_provider_presets()
    names = {preset["provider"] for preset in presets}
    assert {"deepseek", "openai", "openai_compatible", "glm", "claude", "minimax"} <= names
    for preset in presets:
        assert set(preset) == {"provider", "model", "base_url", "api_key_env"}
        # metadata only: the env var NAME, never a value.
        assert preset["api_key_env"].endswith("_API_KEY")


def test_llm_settings_with_provider_update_merges_and_validates() -> None:
    llm = LLMSettings(
        provider="deepseek",
        providers={
            "deepseek": LLMProviderSettings(
                provider="deepseek",
                model="fake-deepseek",
                base_url="http://localhost/v1",
                api_key_env=_DS_ENV,
            )
        },
    )
    updated = llm_settings_with_provider_update(
        llm, provider="glm", model="glm-m", base_url="https://glm.example/v4", api_key_env="GLM_API_KEY"
    )
    assert updated.provider == "glm"
    assert updated.providers["glm"].model == "glm-m"
    # unspecified fields survive a partial update of an existing entry
    repatched = llm_settings_with_provider_update(updated, provider="glm", model="glm-m2", activate=False)
    assert repatched.provider == "glm"
    assert repatched.providers["glm"].model == "glm-m2"
    assert repatched.providers["glm"].base_url == "https://glm.example/v4"
    assert repatched.providers["glm"].api_key_env == "GLM_API_KEY"
    with pytest.raises(ValueError, match="built in"):
        llm_settings_with_provider_update(llm, provider="rule")
    with pytest.raises(ValueError, match="missing required config"):
        llm_settings_with_provider_update(llm, provider="newone", model="m")
