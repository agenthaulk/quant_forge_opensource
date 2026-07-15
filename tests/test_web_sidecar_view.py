"""Frontend contract for the P2 sidecar narration renderer (agent_sidecar_frontend.md §5.5/§9).

Mirrors ``tests/test_web_pipeline_view.py``'s conventions:

- string-contract pins for ``static/views/narration.js``: pure-render-functions
  first / ``[controller]`` last, no fetch/DOM above the marker, and it is NOT a
  number renderer (imports ``esc`` from metric.js, defines no metric helpers);
- served-page pins for the drawer mount + its token-only CSS (both themes,
  375px-safe);
- a stdlib Node smoke that imports the REAL narration.js and drives its pure
  ``renderNarrationDrawer`` with a fixture (clarify card + narration stream),
  asserting the fieldset/legend a11y shape, the blocking banner, a ref link,
  and that NO number leaks into narration text.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


NARRATION_JS_PATH = web_server.STATIC_ROOT / "views" / "narration.js"


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


def _narration_js() -> str:
    return NARRATION_JS_PATH.read_text(encoding="utf-8")


def _index_html(base_url: str) -> str:
    with urllib.request.urlopen(base_url + "/", timeout=10) as response:
        return response.read().decode("utf-8")


def test_narration_module_is_pure_first_controller_last() -> None:
    text = _narration_js()
    # Match the CODE divider ("// [controller]"), not the docstring prose that
    # also names "[controller]" ahead of everything (same convention as
    # tests/test_web_pipeline_view.py).
    marker = text.index("// [controller]")
    head = text[:marker]
    # No fetch/DOM in the pure render section.
    assert "fetch(" not in head
    assert "document." not in head
    # The pure top-level exports the render entrypoint a design lane can drive.
    assert "export function renderNarrationDrawer(" in head
    assert "export function renderClarifyCard(" in head


def test_narration_is_not_a_number_renderer() -> None:
    text = _narration_js()
    # It consumes the shared escaper but never redefines a metric renderer
    # (FE-L2: numbers become pixels only in metric.js/charts.js/dsl.js).
    assert "import { esc } from '../metric.js'" in text
    for forbidden in ("function pct(", "function num(", "function metricCellHtml(", "function metricPill("):
        assert forbidden not in text


def test_narration_has_no_external_resource_reference() -> None:
    text = _narration_js()
    assert "http://" not in text
    assert "https://" not in text


def test_index_mounts_the_narration_drawer_with_token_only_css(web_app) -> None:
    html = _index_html(web_app)
    assert 'id="narration-drawer"' in html
    assert 'aria-live="polite"' in html  # throttled live region (spec §9)
    # Token-only CSS, both themes, 375px-safe (no new color literals).
    for marker in (".narration-drawer", ".clarify-card", ".clarify-tier--blocking", ".clarify-option"):
        assert marker in html
    # 44px touch targets on the clarify/ref controls (spec §9).
    assert "min-height: 44px" in html


NODE_HARNESS = r"""
const NARRATION_URL = process.argv[2];
globalThis.document = { getElementById: () => null, createElement: () => ({}), addEventListener() {} };
globalThis.window = { addEventListener() {} };

let failed = 0;
function check(name, cond, detail) { if (cond) console.log('PASS ' + name); else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); } }

const mod = await import(NARRATION_URL);

const state = {
  readiness: 'unavailable',
  clarify: {
    questions: [
      { question_key: 'clarify.mktcap.basis', tier: 'blocking',
        options: [{ id: 'float', label: '流通市值', is_default: true }, { id: 'total', label: '总市值', is_default: false }] }
    ],
    answers: [],
    blocking_unanswered: ['clarify.mktcap.basis']
  },
  narration: [
    { kind: 'status', message_key: 'sidecar.tool.parse_idea', args: ['parse_idea'] },
    { kind: 'ref', message_key: 'narration.ref.see', args: ['IC'], ref: { component_id: 'factor-tape', artifact_ref: 'eval.json' } }
  ]
};

const html = mod.renderNarrationDrawer(state);

check('clarify_uses_fieldset_legend', html.includes('<fieldset') && html.includes('<legend>'));
check('blocking_tier_badged', html.includes('clarify-tier--blocking'));
check('blocking_banner_shown', html.includes('阻塞'));
check('default_option_marked', html.includes('默认'));
check('skip_button_present', html.includes('clarify-skip'));
check('status_node_rendered', html.includes('已解析因子想法'));
check('ref_link_rendered', html.includes('data-narration-ref="factor-tape"'));
check('readiness_line_rendered', html.includes('data-readiness="unavailable"'));
// FE-L2: no number ever leaks into narration text (the value lives in the
// canonical component the ref points at, not here).
check('no_number_leak', !/[>\s]0\.05[<\s]/.test(html) && !html.includes('0.05'));

// A question node in the stream is NOT rendered as a loose status line.
const streamOnly = mod.renderNarrationStream(state.narration.concat([{ kind: 'question', message_key: 'clarify.x', options: [] }]));
check('question_excluded_from_stream', !streamOnly.includes('clarify.x'));

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_narration_render_smoke(tmp_path) -> None:
    harness = tmp_path / "narration_smoke.mjs"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), NARRATION_JS_PATH.as_uri()],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SMOKE RESULT: 0 failed" in result.stdout
