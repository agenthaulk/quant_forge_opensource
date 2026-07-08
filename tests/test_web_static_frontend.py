"""Characterization tests for the CP6-1 static ES-module frontend (D8).

Pins the frontend-skeleton contract:

- zero external resources: the served page and every served static module
  contain no ``http://`` or ``https://`` references;
- the served page keeps every panel section, control, and empty state that
  existed before the inline-script extraction, and references the frontend
  only through ``<script type="module">`` plus a JSON page-config block (no
  inline application script);
- the static handler serves exactly the known module set with the correct
  MIME type, without a control token, and rejects traversal (including
  percent-encoded dot segments), absolute paths, unknown paths, and
  directory listings with HTTP 404;
- FP-4 single-renderer rule: the MetricValue rendering helpers are defined
  exactly once across all served frontend code, in ``metric.js``.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig, WebSettings
from quant_forge.data.local import create_demo_workspace


JSON_CONTENT_TYPE = "application/json; charset=utf-8"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
JS_CONTENT_TYPE = "text/javascript; charset=utf-8"

# The complete CP6-1 module set. A new module must be added here so the
# no-external-resources and single-renderer sweeps keep covering everything.
EXPECTED_STATIC_MODULES = (
    "api.js",
    "app.js",
    "metric.js",
    "views/bench.js",
    "views/factor.js",
    "views/history.js",
    "views/research.js",
)

# One definition site for every MetricValue rendering helper (metric.js).
METRIC_RENDERER_DEFINITIONS = (
    "function esc(",
    "function pct(",
    "function num(",
    "function metricNum(",
    "function valueOr(",
    "function metricPill(",
    "function pctMetric(",
    "function metricValueText(",
    "function metricStatusSuffix(",
)


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


@pytest.fixture()
def web_app(web_config):
    server = create_local_web_server(host="127.0.0.1", port=0, config=web_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _raw_get(base_url: str, path: str) -> tuple[int, str, bytes]:
    """GET with the request path sent verbatim (no client-side normalization),
    so traversal probes like ``/static/../`` reach the server unchanged."""

    host, _, port = base_url.removeprefix("http://").partition(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", "") or "", response.read()
    finally:
        connection.close()


def _get_index_html(base_url: str) -> str:
    with urllib.request.urlopen(base_url + "/", timeout=10) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


def _served_modules(base_url: str) -> dict[str, str]:
    served: dict[str, str] = {}
    for name in EXPECTED_STATIC_MODULES:
        status, content_type, body = _raw_get(base_url, f"/static/{name}")
        assert status == 200, f"/static/{name} -> {status}"
        assert content_type == JS_CONTENT_TYPE, f"/static/{name} -> {content_type}"
        served[name] = body.decode("utf-8")
    return served


def test_static_directory_contains_exactly_the_expected_modules() -> None:
    on_disk = sorted(
        path.relative_to(web_server.STATIC_ROOT).as_posix()
        for path in web_server.STATIC_ROOT.rglob("*")
        if path.is_file()
    )
    assert on_disk == sorted(EXPECTED_STATIC_MODULES)


def test_served_frontend_contains_no_external_resource_references(web_app) -> None:
    html = _get_index_html(web_app)
    assert "http://" not in html
    assert "https://" not in html
    for name, text in _served_modules(web_app).items():
        assert "http://" not in text, f"external reference in {name}"
        assert "https://" not in text, f"external reference in {name}"


def test_index_page_keeps_all_panel_sections_and_controls(web_app) -> None:
    html = _get_index_html(web_app)
    # Workbench panels and empty states.
    for marker in (
        'id="result"',
        'id="staggered-result"',
        'id="rd-result"',
        'id="history-result"',
        'id="bench-result"',
        'id="error"',
        "Factor Tape",
        "RD Loop",
        "研究历史",
        "Benchmark",
        "等待输入",
        "等待运行",
        "暂无研究历史",
        "暂无 bench 结果",
    ):
        assert marker in html, marker
    # Control rail forms and runtime strip.
    for marker in (
        'id="idea"',
        'id="parser"',
        'id="llm-provider"',
        'id="llm-api-key-mode"',
        'id="llm-api-key"',
        'id="validation-controls"',
        'id="run"',
        'id="validate-run"',
        'id="staggered-run"',
        'id="cancel-run"',
        'id="status"',
        'id="rd-seed"',
        'id="rd-objective"',
        'id="rd-max"',
        'id="rd-iterations"',
        'id="rd-interval"',
        'id="rd-run"',
        'id="rd-start"',
        'id="rd-stop"',
        'id="rd-cancel"',
        'id="rd-status"',
        'id="runtime-llm"',
        'id="runtime-rd"',
        'id="runtime-data-root"',
        'id="runtime-factor-root"',
        'id="runtime-artifact-root"',
        "01 Parse",
        "02 Research",
    ):
        assert marker in html, marker


def test_index_page_has_no_inline_application_script(web_app) -> None:
    html = _get_index_html(web_app)
    assert '<script type="module" src="/static/app.js"></script>' in html
    assert '<script type="application/json" id="qf-page-config">' in html
    # Exactly the JSON config block and the module entry tag; nothing else.
    assert html.count("<script") == 2
    assert "addEventListener" not in html
    config_start = html.index('id="qf-page-config">') + len('id="qf-page-config">')
    config_text = html[config_start : html.index("</script>", config_start)]
    page_config = json.loads(config_text)
    assert set(page_config) == {"controlTokenRequired", "llmProviderOptions"}
    assert page_config["controlTokenRequired"] is False


def test_static_handler_serves_each_module_with_correct_mime(web_app) -> None:
    served = _served_modules(web_app)
    for name in EXPECTED_STATIC_MODULES:
        on_disk = (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")
        assert served[name] == on_disk


def test_static_assets_do_not_require_the_control_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_WEB_TOKEN", "secret-token")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_WEB_TOKEN")
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, content_type, body = _raw_get(base_url, "/static/app.js")
        assert status == 200
        assert content_type == JS_CONTENT_TYPE
        text = body.decode("utf-8")
        assert "secret-token" not in text
        assert "QF_TEST_WEB_TOKEN" not in text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.parametrize(
    "probe",
    [
        "/static/../html.py",
        "/static/../../web/html.py",
        "/static/../routing.py",
        "/static/views/../../routing.py",
        "/static/%2e%2e/html.py",
        "/static/%2e%2e%2f%2e%2e%2fserver.py",
        "/static/..%2fhtml.py",
        "/static//etc/passwd",
        "/static/%2fetc%2fpasswd",
        "/static/views/..\\..\\html.py",
    ],
)
def test_static_handler_rejects_traversal_and_absolute_paths(web_app, probe: str) -> None:
    status, content_type, body = _raw_get(web_app, probe)
    assert status == 404, probe
    assert content_type == JSON_CONTENT_TYPE
    text = body.decode("utf-8")
    assert "unknown static asset" in json.loads(text)["error"]
    # Never leak module source through a rejected path.
    assert "def " not in text


@pytest.mark.parametrize(
    "probe",
    ["/static/missing.js", "/static/views/missing.js", "/static/app.py", "/static/app.js.txt"],
)
def test_static_handler_unknown_asset_returns_404(web_app, probe: str) -> None:
    status, content_type, body = _raw_get(web_app, probe)
    assert status == 404, probe
    assert content_type == JSON_CONTENT_TYPE
    assert "unknown static asset" in json.loads(body.decode("utf-8"))["error"]


@pytest.mark.parametrize("probe", ["/static/", "/static/views", "/static/views/"])
def test_static_handler_serves_no_directory_listing(web_app, probe: str) -> None:
    status, content_type, body = _raw_get(web_app, probe)
    assert status == 404, probe
    assert content_type == JSON_CONTENT_TYPE
    text = body.decode("utf-8")
    assert "unknown static asset" in json.loads(text)["error"]
    for name in EXPECTED_STATIC_MODULES:
        assert name.split("/")[-1] not in text


def test_non_static_unknown_paths_still_fall_back_to_the_index(web_app) -> None:
    status, content_type, body = _raw_get(web_app, "/definitely-not-a-route")
    assert status == 200
    assert content_type == HTML_CONTENT_TYPE
    assert body.decode("utf-8").startswith("<!doctype html>")
    # "/static" without the trailing slash is outside the static namespace
    # and keeps the pre-CP6 catch-all behavior.
    status, content_type, body = _raw_get(web_app, "/static")
    assert status == 200
    assert content_type == HTML_CONTENT_TYPE


def test_metric_renderer_helpers_defined_once_in_metric_module(web_app) -> None:
    html = _get_index_html(web_app)
    served = _served_modules(web_app)
    for definition in METRIC_RENDERER_DEFINITIONS:
        assert definition not in html
        total = sum(text.count(definition) for text in served.values())
        assert total == 1, f"{definition} defined {total} times"
        assert definition in served["metric.js"]
    # FP-4: the shared renderer keeps null-not-zero and status-over-scalar.
    metric_js = served["metric.js"]
    assert "if (status && status !== 'available' && status !== 'legacy') return esc(status);" in metric_js
    assert "value === undefined || value === null" in metric_js


def test_server_module_re_exports_static_frontend_names() -> None:
    for name in ("STATIC_ROOT", "STATIC_URL_PREFIX", "STATIC_CONTENT_TYPES", "_static_asset", "_page_config_json"):
        assert hasattr(web_server, name), f"missing re-export: {name}"
