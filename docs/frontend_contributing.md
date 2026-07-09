# Frontend Contributing Guide / 前端贡献指南

This guide is for contributors who want to change the Quant Forge web
workbench. The target audience is quant/Python practitioners, not frontend
specialists, so the toolchain barrier is deliberately low (axiom A3): there is
no npm, no bundler, and no build step. You edit a file and reload the page.

Read `AGENTS.md` and `docs/agent_entrypoint.md` first — they are the project
contract. This document only covers the frontend.

## The no-build model / 无构建模型

The entire UI is a set of static ES modules under
`src/quant_forge/apps/web/static/`, served directly by the Python stdlib web
server. There is no framework, no CDN, and no compile step: the bytes on disk
are the bytes the browser runs.

- **Edit + reload.** Change a `.js` file under `static/`, refresh the page.
  That is the whole loop.
- **No npm / bundler / transpiler.** Modules use native `import` / `export`.
  The browser loads them as ES modules.
- **Run it locally.** Create a demo workspace and start the adapter:

  ```bash
  python3 -m quant_forge.apps.cli.main init --workspace ./qf-demo
  python3 -m quant_forge.apps.cli.main web --workspace ./qf-demo
  # then open the printed loopback URL (default http://127.0.0.1:8765/)
  ```

- **D8 — zero external resources.** The served page and every served module
  must reference nothing off-host: no CDN scripts, no remote fonts, no external
  images. The release-safety sweep greps every served page and module for
  `http://` and `https://` and fails if either appears
  (`tests/test_web_static_frontend.py::test_served_frontend_contains_no_external_resource_references`).
  Keep all CSS, JS, and SVG inline or local. (This doc, served as Markdown
  through `/api/docs`, may name the scheme in prose — the sweep runs on the
  served *page* and the static modules, not on rendered docs.)

## Architecture / 架构

Three layers, each with a single job:

- **`apps/web/html.py`** server-renders the page shell and the CSS design
  tokens. Colors, spacing, and typography live as CSS custom properties in a
  `:root` block plus a `@media (prefers-color-scheme: dark)` block, so every
  view reads correctly in light and dark. `html.py` also emits the two — and
  only two — `<script>` tags: a JSON `#qf-page-config` block (server-computed
  values like the control-token flag and LLM provider options) and the module
  entry tag `<script type="module" src="/static/app.js">`. There is no inline
  application script.
- **`static/app.js`** is the router and control wiring. It reads
  `#qf-page-config`, imports the view modules, binds the control-rail buttons
  and forms, and drives the job lifecycle. Nothing executable lives outside the
  static modules.
- **`static/views/*.js`** are the per-view modules — one file per surface
  (`factor.js`, `research.js`, `history.js`, `bench.js`, `data.js`,
  `registry.js`, `docs.js`, `extensions.js`, `synthesis.js`), plus the shared
  chrome (`lab.js`) and shared renderers.

### The fetch/render split / 取数与渲染分离

Every view module keeps **pure render functions first, the controller last**.
A render function is `payload -> HTML string`: no `fetch`, no DOM access, no
module state, so tests and design passes can drive it with fixtures. Only the
controller section (marked `[controller]`) touches `fetch`, the DOM, and
events.

`static/views/synthesis.js` is the exemplar. Its header pins the rule, every
`render*` / `build*` function above the `[controller]` divider is pure, and the
fetch/DOM/wiring (`refreshSynthesisPanel`, `initSynthesisModule`) sits below it.
Follow that shape in any new view.

## Single-renderer disciplines / 单一渲染器纪律

FP-4 and FP-5 (honest metrics, one source of truth) apply in the presentation
layer as *single definition sites*. Four modules each own one rendering
concern, and nothing may render that concern anywhere else:

- **`static/metric.js`** is THE `MetricValue` renderer. A null value renders
  `n/a`, never `0`; a withheld status renders its status label, never a bare
  scalar. `test_web_static_frontend.py` asserts each metric helper is defined
  exactly once, and only in `metric.js`.
- **`static/views/tags.js`** is THE research-tag chip renderer. A null tag set
  and an observably-empty one render differently and are never collapsed.
- **`static/views/charts.js`** is THE chart module (inline SVG). A missing
  point is a GAP in the path, never plotted as `0`; a fully-absent series
  renders an explicit empty-state box, never a flat line at `0`; bars are
  always zero-based.
- **`static/views/dsl.js`** is THE formula highlighter. It tokenizes
  structurally and emits every input character exactly once (escaped), so the
  rendered text always round-trips to the input.

If you need a metric cell, a research-tag chip, a chart, or a highlighted
formula, import it from these modules. Do not hand-roll a second one.

## How to add a view / 新增一个视图

A new top-level tab is a small, five-touch change:

1. **`static/views/lab.js`** — add the tab id to `TAB_IDS`. `lab.js` is a pure
   client-side tab/hash controller (no fetch), so it only needs the id.
2. **`apps/web/html.py`** — add the tab button, the `lab-panel-<name>` panel,
   and the view's mount element (e.g. `<div id="<name>-result">`) with its
   empty state.
3. **`static/views/<name>.js`** — pure renderers first, then the `[controller]`
   section with a `refresh<Name>Panel()` that fetches and writes into the mount.
4. **`static/app.js`** — import `refresh<Name>Panel` and wire it to the tab's
   `onActivate` hook so the panel lazy-loads when the tab is opened.
5. **Tests** — add the module filename to `EXPECTED_STATIC_MODULES` in
   `tests/test_web_static_frontend.py` (so the no-external-resources and
   single-renderer sweeps cover it), and add string-contract assertions for the
   new markers to the relevant `test_web_*.py`.

## How to add a synthesis method / 新增一个合成方法

The multi-factor module is schema-driven end to end. A new a-priori method is
**one `register_method(...)` call** — the orchestrator, the endpoints, and the
frontend dynamic form all pick it up with no further edits, because the same
`ParamSpec` schema drives both backend validation and the rendered form.

```python
# src/quant_forge/synthesis/methods.py (sketch)
class MyBlendMethod:
    name = "my_blend"
    label = "My declared blend"
    required_standardization = None  # or "cross_sectional_rank" to pin one
    available = True

    def param_schema(self):
        return (ParamSpec(name="alpha", label="Alpha", type="float",
                          required=True, minimum=0.0, maximum=1.0),)

    def validate_params(self, params, factor_ids):
        # Only method-specific cross-field logic here; the shared
        # validate_params_against_schema has already enforced the ParamSpec
        # (unknown names, required, type, min/max) BEFORE this runs.
        return dict(params)

    def combine(self, std_matrix, params):
        return std_matrix.mean(axis=1)  # your a-priori combination

# register once at import time (src/quant_forge/synthesis/registry.py)
register_method(MyBlendMethod())
```

Boundary reminder (D6): open-source methods are a-priori declared blends only —
no optimizer, no covariance, no risk model, and `is_fitted` stays `False`. A
data-driven method (like the reserved `ic_weighted` stub) must derive weights
from past-only history gated at the composite date, never from the evaluation
window.

## Testing / 测试

Web tests come in two layers, and neither needs a browser:

- **String-contract pins.** Python tests read the served page and the served
  modules and assert on their source — required markers, the single-renderer
  rule, the fetch/render purity split, esc() discipline, and the absence of any
  external reference. These hold with no runtime.
- **Stdlib Node smoke.** For behavior that needs execution, a test writes a
  small `.mjs` harness that `import()`s the *real* module and drives it with
  FP-4 fixtures, then runs it with `subprocess.run(["node", ...])`. The test is
  `@pytest.mark.skipif(shutil.which("node") is None, ...)`, so it skips cleanly
  where Node is absent — there is still no npm and no browser.

`tests/test_web_charts.py` (the chart module) and
`tests/test_web_synthesis_view.py` (the synthesis module) are the reference
examples of both layers. To run the web suite:

```bash
python3 -m pytest -q tests/test_web_*.py
```

Before you claim a change is done, also run the project baseline from
`AGENTS.md` (`python3 -m pytest`, the CLI `--help`, `git diff --check`) and, for
any served-file change, `python3 scripts/release_safety_scan.py`.
