"""Contract tests for the CP9-1 honest inline-SVG charting module (charts.js).

Two layers:

- Python source-contract pins on the served module (``charts.js``) and on the
  consumers that wire it in (``factor.js`` / ``research.js`` / ``bench.js``),
  plus the ``.qf-chart`` CSS in the served page. These hold with no runtime.
- A stdlib-only Node SVG-path smoke: a headless harness imports the real
  module, exercises the FP-4 fixtures, and checks XML well-formedness, path
  geometry (x strictly increasing within a subpath, every y inside the plot
  rect), plotted-vertex counts (a gap is a new subpath, never a 0), honest-axis
  behavior, the bar null-vs-zero distinction, theme tokens, a11y nodes, and the
  absence of any external reference. No browser, no npm.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


CHARTS_PATH = web_server.STATIC_ROOT / "views" / "charts.js"

# CSS variables the module must reference so charts read in both themes (each
# is defined in both :root and the prefers-color-scheme: dark block).
SERIES_COLOR_VARS = ("var(--accent)", "var(--blue)", "var(--bad)", "var(--warn)", "var(--accent-2)")
STRUCTURE_COLOR_VARS = ("var(--line)", "var(--line-strong)", "var(--muted)", "var(--ink)")

# Tokens that would introduce an external dependency (D8 forbids all of them).
EXTERNAL_REFERENCE_TOKENS = ("http", "url(", "xlink", "<image", "<script", "<foreignObject")


def _charts_src() -> str:
    return CHARTS_PATH.read_text(encoding="utf-8")


def _module_src(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


# ---------------------------------------------------------------------------
# Source-contract pins (no runtime)
# ---------------------------------------------------------------------------


def test_charts_module_exports_the_three_functions_and_palette() -> None:
    src = _charts_src()
    assert "export function lineChart(" in src
    assert "export function barChart(" in src
    assert "export function emptyState(" in src
    assert "export const DEFAULT_SERIES_COLORS" in src
    # Reuses metric.js helpers; never redefines them (single-renderer rule).
    assert "import { esc, num } from '../metric.js';" in src
    assert "function esc(" not in src
    assert "function num(" not in src


def test_charts_module_is_theme_aware_with_no_hardcoded_colors() -> None:
    src = _charts_src()
    for var in SERIES_COLOR_VARS:
        assert var in src, var
    for var in STRUCTURE_COLOR_VARS:
        assert var in src, var
    # No hex color literal anywhere (a `#RGB`/`#RRGGBB` token); esc()'s numeric
    # entities are produced at runtime, not written in source.
    assert re.search(r"#[0-9a-fA-F]{3,6}\b", src) is None


def test_charts_module_has_no_external_references() -> None:
    src = _charts_src()
    for token in EXTERNAL_REFERENCE_TOKENS:
        assert token not in src, token
    assert "xmlns" not in src


def test_charts_module_encodes_fp4_gap_and_empty_state_discipline() -> None:
    src = _charts_src()
    # Finite-only gate mirrors spark.js discipline.
    assert "typeof value === 'number' && Number.isFinite(value)" in src
    # A gap ends the current subpath; a fresh finite point starts a new one.
    assert "runs.push(current)" in src
    # Empty-state is an explicit box, reachable from every entry point, and
    # carries the same <desc> disclosure contract as populated charts.
    assert "export function emptyState(" in src
    assert "qf-chart--empty" in src
    assert "no plottable data" in src
    # Every chart is a labeled image with a missing-data disclosure.
    assert 'role="img"' in src
    assert "<title>" in src
    assert "<desc>" in src
    assert "aria-label=" in src
    # The truncation note and the (conditional) zero line are both present.
    assert "not zero-based" in src
    assert "qf-zero" in src
    assert "qf-na" in src


def test_consumers_wire_the_charts_module_at_the_named_mounts() -> None:
    factor_js = _module_src("views/factor.js")
    research_js = _module_src("views/research.js")
    bench_js = _module_src("views/bench.js")
    # factor.js: C1/C2 line charts replace the staggered sparkline; C3–C6 bars.
    assert "from './charts.js'" in factor_js
    assert "lineChart(" in factor_js
    assert "barChart(" in factor_js
    assert "sparklineSvg(" not in factor_js
    assert "from './spark.js'" not in factor_js
    # research.js: C7 candidate selection-score bar above the comparison table.
    assert "from './charts.js'" in research_js
    assert "barChart(" in research_js
    # bench.js: C8 canonical factor-quality metric bar across factors.
    assert "from './charts.js'" in bench_js
    assert "barChart(" in bench_js


def test_served_page_ships_the_qf_chart_css_tokens(web_config) -> None:
    html = web_server._index_html(web_config)
    assert ".qf-chart" in html
    assert ".qf-chart-row" in html
    # SVG <text> inherits the app font (D8: no external/serif fallback).
    assert "font-family: inherit" in html


# ---------------------------------------------------------------------------
# Node SVG-path smoke (stdlib only)
# ---------------------------------------------------------------------------


_SMOKE_HARNESS = r"""
const url = process.env.QF_CHARTS_URL;
const mod = await import(url);
const { lineChart, barChart, emptyState } = mod;

// Plot gutters fixed by the CP9-1 spec (charts.js §2.3).
const L = 48, R = 12, T = 24, B = 28;

let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); }
}

// Minimal XML well-formedness: every tag opens/self-closes/closes in order.
// esc() converts any literal '>' in text/attrs to an entity, so no raw '>'
// appears inside a tag and a simple scanner is sufficient.
function xmlWellFormed(s) {
  const re = /<(\/?)([a-zA-Z][\w-]*)([^>]*?)(\/?)>/g;
  const stack = [];
  let m;
  while ((m = re.exec(s)) !== null) {
    if (m[1] === '/') { if (!stack.length || stack.pop() !== m[2]) return false; }
    else if (m[4] !== '/') { stack.push(m[2]); }
  }
  return stack.length === 0;
}
function pathDs(svg) { return [...svg.matchAll(/<path[^>]*\sd="([^"]*)"/g)].map(m => m[1]); }
function verts(d) {
  return [...d.matchAll(/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g)].map(m => [Number(m[1]), Number(m[2])]);
}
function subpaths(d) { return d.split('M').map(s => s.trim()).filter(Boolean).map(verts); }
function circles(svg) {
  return [...svg.matchAll(/<circle[^>]*cx="([\d.]+)"[^>]*cy="([\d.]+)"/g)].map(m => [Number(m[1]), Number(m[2])]);
}

// --- gap => path break, never a 0 baseline -------------------------------
{
  const svg = lineChart([{ name: 's', values: [1, null, 3] }], { x: ['a', 'b', 'c'], ariaLabel: 't' });
  check('line.xml', xmlWellFormed(svg));
  const d = pathDs(svg)[0] || '';
  check('line.gap.subpaths', (d.match(/M/g) || []).length === 2, 'M=' + (d.match(/M/g) || []).length);
  const vertexCount = (d.match(/M/g) || []).length + (d.match(/L/g) || []).length;
  check('line.gap.vertices==finite', vertexCount === 2, 'vertices=' + vertexCount);
  // The middle index (x = 358 for N=3, W=680) is a gap: no vertex there.
  const all = verts(d);
  check('line.gap.no_x1_vertex', all.every(([x]) => Math.abs(x - 358) > 0.5), JSON.stringify(all));
  // A gap never fabricates a value: only the true endpoints x0=48, x2=668.
  check('line.gap.endpoints', all.some(([x]) => Math.abs(x - 48) < 0.5) && all.some(([x]) => Math.abs(x - 668) < 0.5));
  // Geometry: every plotted point sits inside the plot rectangle.
  const pts = all.concat(circles(svg));
  check('line.geom.in_rect', pts.every(([x, y]) => x >= L - 0.5 && x <= 680 - R + 0.5 && y >= T - 0.5 && y <= 260 - B + 0.5), JSON.stringify(pts));
  // x strictly increases within each subpath.
  let mono = true;
  for (const sp of subpaths(d)) { for (let i = 1; i < sp.length; i++) { if (sp[i][0] <= sp[i - 1][0]) mono = false; } }
  check('line.geom.x_monotonic', mono);
  // Honest axis: data [1,3] excludes 0 => truncation note, no zero line.
  check('line.axis.note_when_zero_excluded', svg.includes('not zero-based') && !svg.includes('qf-zero'));
  // a11y + disclosure.
  check('line.a11y', svg.includes('role="img"') && /aria-label="[^"]+"/.test(svg) && svg.includes('<title>') && svg.includes('<desc>') && svg.includes('missing'));
}

// --- zero in domain => zero line, no note --------------------------------
{
  const svg = lineChart([{ name: 's', values: [-1, 1] }], { x: ['a', 'b'], ariaLabel: 't' });
  check('line.axis.zero_line_when_included', svg.includes('qf-zero') && !svg.includes('not zero-based'));
}

// --- empty-state: no series / all null / empty x -------------------------
for (const [tag, series, x] of [
  ['empty.no_points', [{ name: 's', values: [] }], []],
  ['empty.all_null', [{ name: 's', values: [null, null] }], ['a', 'b']],
  ['empty.no_x', [{ name: 's', values: [1, 2] }], []],
]) {
  const svg = lineChart(series, { x, ariaLabel: 't' });
  check(tag, svg.includes('qf-chart--empty') && !svg.includes('<path'), svg.slice(0, 60));
}
check('empty.helper', (() => { const s = emptyState('t', {}); return s.includes('qf-chart--empty') && !s.includes('<path') && s.includes('role="img"'); })());
// Empty-state a11y disclosure: <title> plus a <desc> that states there is no
// plottable data and repeats the caller's reason text, escaped (a raw '<'/'&'
// in the reason must never reach the SVG unescaped).
check('empty.desc_disclosure', (() => {
  const s = emptyState('t', { message: 'metrics <blocked> & missing' });
  return s.includes('<title>') && xmlWellFormed(s)
    && s.includes('<desc>no plottable data: metrics &lt;blocked&gt; &amp; missing</desc>')
    && !s.includes('metrics <blocked>');
})());
check('empty.desc_default', (() => { const s = emptyState('t', {}); return s.includes('<desc>no plottable data: no data</desc>'); })());

// --- flat series: one honest midline, labeled, never empty ---------------
{
  const svg = lineChart([{ name: 's', values: [2, 2, 2] }], { x: ['a', 'b', 'c'], ariaLabel: 't' });
  check('flat.one_path', (svg.match(/<path/g) || []).length === 1);
  check('flat.value_labeled', svg.includes('2.0000'));
  check('flat.not_empty', !svg.includes('qf-chart--empty'));
}

// --- bar: null => n/a and NO rect; real 0 => zero-height rect ------------
{
  const svg = barChart([{ label: 'A', value: null }, { label: 'B', value: 0.02 }], { ariaLabel: 'b' });
  check('bar.xml', xmlWellFormed(svg));
  check('bar.null_na', svg.includes('qf-na'));
  check('bar.null_no_rect', (svg.match(/<rect/g) || []).length === 1, 'rects=' + (svg.match(/<rect/g) || []).length);
  check('bar.zero_based', svg.includes('qf-zero') && svg.includes('>0</text>'));
}
{
  const svg = barChart([{ label: 'A', value: 0 }], { ariaLabel: 'b' });
  check('bar.real_zero_has_rect', (svg.match(/<rect/g) || []).length === 1 && !svg.includes('qf-na'));
}
{
  const svg = barChart([{ label: 'A', value: 1 }, { label: 'B', value: 2 }], { ariaLabel: 'b' });
  check('bar.all_positive_zero_line', svg.includes('qf-zero') && svg.includes('>0</text>'));
}

// --- theme + no external references across outputs -----------------------
{
  const line = lineChart([{ name: 's', values: [1, 2, 3] }], { x: ['a', 'b', 'c'], ariaLabel: 't' });
  const bar = barChart([{ label: 'A', value: -1 }, { label: 'B', value: 2 }], { ariaLabel: 'b' });
  const both = line + bar;
  check('theme.series_vars', line.includes('var(--accent)') || line.includes('currentColor'));
  check('theme.structure_vars', both.includes('var(--line)') && both.includes('var(--muted)'));
  check('theme.no_hex', !/#[0-9a-fA-F]{3,6}\b/.test(both));
  check('no_external', !/http|url\(|xlink|<image|<script|<foreignObject/.test(both));
}

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_svg_path_smoke(tmp_path) -> None:
    harness = tmp_path / "charts_smoke.mjs"
    harness.write_text(_SMOKE_HARNESS, encoding="utf-8")
    env = {"QF_CHARTS_URL": CHARTS_PATH.resolve().as_uri()}
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), **env},
        timeout=60,
    )
    # Surface the whole transcript on failure.
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "SMOKE RESULT: 0 failed" in result.stdout
    # Spot-check the load-bearing FP-4 lines are actually present (not skipped).
    for marker in (
        "PASS line.gap.subpaths",
        "PASS line.gap.no_x1_vertex",
        "PASS line.axis.note_when_zero_excluded",
        "PASS line.axis.zero_line_when_included",
        "PASS empty.no_points",
        "PASS empty.desc_disclosure",
        "PASS empty.desc_default",
        "PASS flat.one_path",
        "PASS bar.null_no_rect",
        "PASS bar.real_zero_has_rect",
        "PASS bar.zero_based",
        "PASS no_external",
    ):
        assert marker in result.stdout, result.stdout
