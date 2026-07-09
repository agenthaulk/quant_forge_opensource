# CP6 Frontend Plan — Fable framework review (decision D8)

Status: APPROVED as the CP6 execution plan (Fable, 2026-07-07, under the
owner's general-manager delegation). Prerequisite named by
ENGINEERING_PROGRESS.md CP6 ("Fable framework review first") — this
document is that review.

## First-principles derivation

1. The platform's differentiator is honest quant research surfaced
   faithfully — MetricValue statuses, evidence chains, falsification
   surfaces — not UI gloss (FP-4/FP-7 extended to the presentation
   layer).
2. Local-first is a hard constraint: users run this against private
   data. Zero external resources at runtime (no CDN, no fonts, no
   telemetry) and minimal supply-chain surface are therefore part of the
   security posture, same rank as the no-exec rule.
3. Community contributability favors the lowest toolchain barrier that
   still supports the Studio-style multi-view UX.
4. Modern browsers natively support ES modules; multi-view navigation,
   componentization, and client-side state need NO build step.

## Decision D8 (recorded in DECISIONS.md)

The CP6 frontend is a **static ES-module application served by the
existing stdlib web server — no build step, no npm, no runtime
dependencies**. Views are plain JS modules under
`src/quant_forge/apps/web/static/` served by a containment-checked
static-file handler in `routing.py`. The Studio React app is the UX
reference, not the implementation template. Escape hatch (documented,
not exercised): if a future capability genuinely requires a build-step
frontend, it lives in a separate opt-in directory and never becomes a
prerequisite for the kernel or the default UI.

## Sub-phases

### CP6-1 — Frontend architecture skeleton (no behavior change)
- Extract the ~1,600-line inline template/JS from `html.py` into static
  ES modules: `app.js` (view router), `api.js` (fetch client, control
  token handling), `metric.js` (THE shared MetricValue renderer — one
  definition; every metric cell in every view must go through it),
  `views/*.js` (current panels re-hosted 1:1).
- `routing.py` gains a static-file handler: whitelisted directory,
  resolve+is_relative_to containment, correct MIME types, no directory
  listing.
- Acceptance: characterization tests FIRST (current panel HTML/JS
  behavior pinned), route parity, all existing web tests green
  unmodified; release scan passes; zero external resource references
  (test greps built pages for http(s):// URLs).

### CP6-2 — Lab / research workbench view
- Multi-tab flow over existing APIs: idea → parse → validate → factor
  report → RD loop; tab state client-side; report sections componentized.
- Acceptance: no new backend endpoints without spec; MetricValue renderer
  reused everywhere; keyboard/anchor navigation basic parity with Studio
  Lab reference.

### CP6-3 — Data console + Registry views
- Data console over the CP5 DataCatalogPort surface: fields, coverage,
  research-metadata tags, quality/validation gate results.
- Registry over the factor catalog + lineage run index: factor list,
  definition detail, evidence chain (runs referencing the factor).
- Acceptance: statuses never bare scalars; paths always redacted
  server-side (existing `_web_public_json` discipline); read-only.

### CP6-4 — Docs view + Extensions browse panel (D7/D7a)
- Docs: server-rendered read-only rendering of repo `docs/` markdown
  (stdlib rendering; no external renderer).
- Extensions: declarative registry backend (manifest schema, Pydantic-
  free stdlib validation consistent with repo style, 5 MVP contribution
  points + reserved stubs; `executable` rejected unconditionally per D7;
  data-interface points implementable per D7a) + GET-only endpoints +
  browse panel. CUTTABLE per D7 if the wave runs tight.

## Design reference and optimization (owner directive 2026-07-07)

- Visual/UX design for CP6-2..4 references the Quant Forge Studio design
  language (read-only branch: navigation structure, layout patterns,
  information hierarchy of Lab/Data/Registry views).
- Each view sub-phase includes a design-optimization pass: an Opus
  design lane applying Claude design guidance (visual hierarchy, spacing
  and typography discipline, light/dark theming, accessible chart and
  status-color conventions from the dataviz guidance) BEFORE the Codex
  review stage. Design output must respect D8 (no build step, no
  external fonts/assets — system font stack + inline SVG only) and FP-4
  (status rendering is part of the design system, not decoration).

## Execution notes

- Each sub-phase runs as a Workflow wave: disjoint-scope dev lanes
  (model per task complexity), pipelined Codex (high/xhigh) review,
  Fable barrier gate + landing. CP6-1 must land before CP6-2/3/4 (they
  build on the skeleton); CP6-3 depends on CP5 (data plane) being landed.
- The commercial boundary stays out of scope entirely (D6/D7a): no agent
  orchestration UI beyond the existing basic research-run controls, no
  portfolio construction/rebalancing views.
