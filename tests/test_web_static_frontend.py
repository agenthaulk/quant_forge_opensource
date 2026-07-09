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
import os
import threading
import urllib.request
from pathlib import Path

import pytest

import quant_forge.apps.web.routing as web_routing
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig, WebSettings
from quant_forge.data.local import create_demo_workspace


JSON_CONTENT_TYPE = "application/json; charset=utf-8"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
JS_CONTENT_TYPE = "text/javascript; charset=utf-8"

# The complete CP6-1 module set (+ CP6-2 Lab chrome + CP6-3 data/registry
# views + CP6-4 docs/extensions views). A new module must be added here so
# the no-external-resources and single-renderer sweeps keep covering
# everything.
EXPECTED_STATIC_MODULES = (
    "api.js",
    "app.js",
    "metric.js",
    "views/bench.js",
    "views/charts.js",
    "views/data.js",
    "views/docs.js",
    "views/dsl.js",
    "views/extensions.js",
    "views/factor.js",
    "views/history.js",
    "views/lab.js",
    "views/registry.js",
    "views/research.js",
    "views/spark.js",
    "views/tags.js",
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
    "function statusBadgeHtml(",
    "function metricCellHtml(",
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
    # CP6-2 Lab chrome (CP9-2 IA consolidation: the workbench tab keeps id
    # lab-tab-factor under the LLM 因子工作台 label; the former RD 循环 /
    # Benchmark tabs live on inside its 单因子研究 module as the
    # #workbench-rd / #report-comparison sections; 多因子策略回测 is the
    # reserved CP10 module slot).
    for marker in (
        'class="lab-stepper"',
        'data-step="idea"',
        'data-step="parse"',
        'data-step="validate"',
        'data-step="report"',
        'data-step="rd"',
        'role="tablist"',
        'id="lab-tab-factor"',
        'id="lab-tab-history"',
        'id="lab-panel-factor"',
        'id="lab-panel-history"',
        'id="lab-module-single"',
        'id="lab-module-multi"',
        'id="lab-module-panel-single"',
        'id="lab-module-panel-multi"',
        'id="multi-result"',
        'id="report-comparison"',
        'id="workbench-rd"',
        'LLM 因子工作台',
        '单因子研究',
        '多因子策略回测',
        '即将上线',
        '工作台模块',
        'RD 循环',
        '研究流程',
    ):
        assert marker in html, marker
    # CP6-3 Data console + Registry tabs, panels, and mounts.
    for marker in (
        'id="lab-tab-data"',
        'id="lab-tab-registry"',
        'id="lab-panel-data"',
        'id="lab-panel-registry"',
        'id="data-result"',
        'id="registry-result"',
        '数据控制台',
        '注册表',
        '等待加载',
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
    # FP-4: the shared renderer keeps null-not-zero and status-over-scalar. A
    # withheld status now renders through statusLabelHtml (a titled span so long
    # labels wrap inside a tile) — still its label, never a fabricated scalar.
    metric_js = served["metric.js"]
    assert "if (status && status !== 'available' && status !== 'legacy') return statusLabelHtml(status);" in metric_js
    assert 'class="metric-status" title="${esc(status)}">${esc(status)}</span>' in metric_js
    assert "value === undefined || value === null" in metric_js


def test_server_module_re_exports_static_frontend_names() -> None:
    for name in ("STATIC_ROOT", "STATIC_URL_PREFIX", "STATIC_CONTENT_TYPES", "_static_asset", "_page_config_json"):
        assert hasattr(web_server, name), f"missing re-export: {name}"


def test_static_asset_rejects_symlink_escape_and_serves_real_module(monkeypatch, tmp_path) -> None:
    """D8 containment: a symlink inside ``static/`` that points OUTSIDE the
    root is rejected (its target's bytes are never served), while a real
    whitelisted module beside it is still served.

    This locks in the end-to-end containment contract and mirrors the sibling
    guards' symlink-escape tests (``_read_bench_artifact``,
    ``_docs_document_payload``). The ``resolve()``+``is_relative_to``
    pre-check already rejects a *statically present* escape symlink, so this
    case holds both before and after the O_NOFOLLOW hardening; the race-window
    regression is pinned separately in
    ``test_static_asset_rejects_symlink_swapped_after_containment_check``.
    """

    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "app.js").write_bytes(b"export const ok = 1;\n")
    secret = tmp_path / "secret.js"
    secret.write_bytes(b"export const SECRET = 'leak';\n")
    (static_root / "evil.js").symlink_to(secret)
    monkeypatch.setattr(web_routing, "STATIC_ROOT", static_root)

    body, content_type = web_routing._static_asset("/static/app.js")
    assert body == b"export const ok = 1;\n"
    assert content_type == JS_CONTENT_TYPE

    with pytest.raises(KeyError):
        web_routing._static_asset("/static/evil.js")


def test_static_asset_rejects_symlink_swapped_after_containment_check(monkeypatch, tmp_path) -> None:
    """TOCTOU regression: if the final path component is swapped for a symlink
    AFTER the ``resolve()``+``is_relative_to`` containment check, the
    O_NOFOLLOW open guard must refuse it instead of following the link and
    leaking a file outside the static root.

    The pre-hardening code called ``candidate.read_bytes()``, which follows a
    freshly-planted symlink; against that code this test would *return the
    outside file's bytes* (no exception) and so fail the ``pytest.raises``
    below. With the O_NOFOLLOW open the swapped symlink maps to the same
    ``KeyError`` (HTTP 404) as a missing asset. The race is made deterministic
    by swapping the file during the ``is_file()`` check --- the last step
    before the read --- reproducing the swap without a concurrent attacker.
    """

    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "race.js").write_bytes(b"export const real = 1;\n")
    secret = tmp_path / "secret.js"
    secret.write_bytes(b"export const SECRET = 'leak';\n")
    monkeypatch.setattr(web_routing, "STATIC_ROOT", static_root)

    real_is_file = Path.is_file

    def racing_is_file(self):
        result = real_is_file(self)
        # Swap the just-validated regular file for a symlink pointing outside
        # the static root, exactly once, at the last check before the read.
        if self.name == "race.js" and not self.is_symlink():
            os.unlink(self)
            os.symlink(secret, self)
        return result

    monkeypatch.setattr(Path, "is_file", racing_is_file)

    with pytest.raises(KeyError):
        web_routing._static_asset("/static/race.js")
