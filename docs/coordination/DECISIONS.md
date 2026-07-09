# Owner Decision Register

Per decision D5, coordination records live in git under `docs/coordination/`.
Owner: the project owner. Recorded by Fable.

## 2026-07-06 — Phase B/C decision set

| # | Decision | Ruling (owner) | Implementation |
| --- | --- | --- | --- |
| D1 | Phase C first wave scope | Approved: parallel lanes, individually stoppable | Wave 1 branch `fable/phase-c-research-platform-wave1`; registry/data-plane/server-decomposition follow |
| D2 | Data & operator investment | Reference existing QF_OS operators/fields; absorb necessary Quant Forge Studio operators | **Finding: absorption already complete** — Studio's 7 ops (cs_rank, scale, delay, delta, ts_rank, rolling_corr, signed_power) all map 1:1 onto existing canonical operators (rank, scale, delay, delta, ts_rank, correlation, signedpower). Actionable remainder: research metadata tags + field expansion (wave 2) |
| D3 | Partial final period | **Exclude** the incomplete final period from backtests by default, and remind the user | Kernel default flips to exclude + explicit warning; `include_partial_final_period=True` remains available (wave 1, lane A) |
| D4 | Agent evaluation artifacts | Allowed, but into a separate subdirectory with source tags | Phase C orchestrator lane acceptance criterion |
| D5 | Coordination docs in git | Yes, under `docs/coordination/`, PR-review exempt | This file; WORKING_STATE migration rides the next merge |
| D6 | Open-source vs commercial split | Build an OPEN-SOURCE interactive platform on the QF_OS kernel, front-end modeled on Quant Forge Studio and surveyed projects. Commercial differentiation lives at the AGENT layer and PORTFOLIO REBALANCING layer | Open-source scope: full research workbench (factor research, evaluation, backtest, RD loop, research history, benchmarking, web UI). Commercial-reserved: advanced agent orchestration, multi-factor portfolio construction/rebalancing adapters. BacktestPort capability tags mark the boundary |

## 2026-07-07 — Extensions decision (owner delegated the ruling to Fable)

| # | Decision | Ruling (Fable, under owner delegation) | Implementation |
| --- | --- | --- | --- |
| D7 | Adopt Studio's Extensions system in the open-source platform? | **Adopt, declarative-only, as a cuttable CP6 sub-item.** Studio's system is a read-only control plane over pure-manifest metadata (~600 LOC; 5 MVP contribution points: data.snapshot_source, data.canonical_mapping, data.quality_rule, agent.context_pack, docs.pack; 5 reserved stubs) — verified: no dynamic code loading. Binding constraints for QF_OS: (1) STRICTER no-exec than Studio — `executable` contributions rejected unconditionally, no built-in exemption; (2) D6 boundary — agent.context_pack stays a declarative knowledge pack; agent.workflow remains a reserved stub in open source; (3) one-truth integration — data.* contribution points must feed the CP5 DataCatalogPort / field-expansion path, never a parallel catalog (FP-5); (4) manifests pass release-safety rules — no absolute paths or secrets; provider endpoints via env-var indirection only; (5) web surface read-only (GET-only endpoints + browse panel, control-token protected). | Optional sub-item of CP6, cuttable without replanning; defers to a post-CP8 enhancement wave if CP6 runs tight. Port the contract (manifest schema, registry, 5 MVP points), not the Studio code wholesale. |
| D7a | Owner refinements to D7 (2026-07-07, same day) | **(1) Data-interface extensions are IN SCOPE for open source**: beyond the three declarative data.* MVP points, the reserved data-interface points (`data.provider_adapter`, `data.pit_resolver`) MAY be implemented in the open-source platform when the data plane needs them — under the same discipline: manifest-declared, adapter code ships in-repo through normal review, NO dynamic loading of third-party code, endpoints via env-var indirection. **(2) Agent surface: open source keeps only the Studio-level basic agent** (the existing research loop / hypothesis generation); agent optimization and role subdivision are reserved for the commercial version — `agent.workflow` stays a reserved stub, `agent.context_pack` stays declarative. All other aspects follow Fable's D7 ruling unchanged. | CP5 data plane may target the provider-adapter/PIT-resolver contract when field expansion requires it; CP6 extension registry scope updated accordingly (owner ruling, recorded by Fable) |

## 2026-07-07 — CP6 frontend architecture (Fable, under general-manager delegation)

| # | Decision | Ruling | Implementation |
| --- | --- | --- | --- |
| D8 | CP6 frontend technology | **Static ES-module app served by the existing stdlib server — no build step, no npm, zero runtime deps, zero external resources.** First-principles grounds: honest-metric rendering is the product, local-first security posture ranks toolchain surface with the no-exec rule, native ES modules make a build chain unnecessary for Studio-style multi-view UX. Studio's React app is UX reference only. Escape hatch documented (opt-in separate dir, never a kernel prerequisite). | Plan + sub-phases CP6-1..4 in docs/coordination/CP6_FRONTEND_PLAN.md; CP6-1 skeleton (extract html.py inline JS into modules + containment-checked static handler, characterization tests first) precedes all other sub-phases |

## 2026-07-09 — Phase D frontend design-parity + multi-factor synthesis (recorded by Fable)

| # | Decision | Ruling | Implementation |
| --- | --- | --- | --- |
| D9 | Open-source frontend stack after a Studio-vs-OSS comparison, and the shape of the CP10 multi-factor module | **Design-parity, not stack-parity: D8 upheld.** Adopt Studio's DESIGN and IA — time-series charts, the module information architecture, and the visual polish. Decline Studio's STACK — React + Vite + npm + Monaco + ECharts. The open-source frontend stays a no-build static ES-module app served by the stdlib server; source is exactly what ships. | Charts CP9-1 `src/quant_forge/apps/web/static/views/charts.js`; IA CP9-2 `.../static/views/lab.js` + `.../apps/web/html.py`; synthesis backend `src/quant_forge/synthesis/`; synthesis frontend `.../static/views/synthesis.js`. Auditability enforced by `scripts/release_safety_scan.py` + `tests/test_web_static_frontend.py` |

**Rationale (the load-bearing axioms).** (1) Local-first with a minimal
dependency surface: a build chain is toolchain surface the no-exec, local-first
posture does not want. (2) Source == shipped auditability: the release scan and
the static-frontend characterization test cover exactly the bytes served, with
no bundler step in between (`scripts/release_safety_scan.py`,
`tests/test_web_static_frontend.py`). (3) Contributor fit for the target
audience — quant/Python practitioners, not frontend specialists (axiom A3): a
contributor edits a served ES module and reloads, with no npm/bundler to learn.
(4) The commercial boundary: Studio's heaviest surfaces — agent orchestration,
governance workflow, and data-plane operations — are commercial (D6/D7a) and
out of scope here, so the stack that carries them is not needed. (5) Studio's
Lab is a Monaco IDE that executes user code, which is incompatible with the
open-source no-exec posture. The only genuine capability gap was time-series
charts, and it was closed D8-compliantly in CP9-1 with an inline-SVG module
(Studio itself only draws line charts).

**Escape hatch (documented, unexercised).** Reaffirms D8: if a build-step
frontend is ever truly required, it lives in a separate opt-in directory and is
never a prerequisite for the kernel or the default UI. This branch does not
exercise it.

**IA (CP9-2).** The primary view is 「LLM 因子工作台」 — it occupies Studio's
primary-surface slot but is never named "Agent" (boundary integrity, D6). It
hosts two modules: 单因子研究 and 多因子策略回测. The former RD 循环 and
Benchmark top-level tabs fold into 单因子研究 sections; legacy hashes migrate,
so no deep link dead-ends (`lab.js` `TAB_IDS` / `LEGACY_HASH_ALIASES`).

**CP10 multi-factor synthesis rulings.** The module is open-source SIMPLE
synthesis — it produces a composite SIGNAL, not an optimized portfolio.
(1) Three a-priori methods: `equal_weight`, `custom_weight` (raw declared
weights, used as declared — the downstream ranking is scale-invariant),
`rank_average` (pins `cross_sectional_rank` standardization). (2) `ic_weighted`
is reserved as a non-runnable schema stub (`available=False`): its weights are
data-driven from past-only IC history and deferred; the open-source build ships
no implementation. (3) PER-ROLE composite computation (FP-5): each per-factor
score is byte-identical to the single-factor path for that role — a union
window would shift decay/warmup scores. (4) Complete-case coverage (FP-4):
missing values are never imputed to 0; a `(trade_date, instrument)` row enters
the composite only when every declared factor is present. (5) Weights are
a-priori declared (`is_fitted=false`, enforced), surfaced through a validity
banner. (6) Schema-driven param validation is the single enforcement source:
`validate_params_against_schema` runs before any method's own
`validate_params`, and the same `ParamSpec` schema drives both backend
validation and the frontend dynamic form — a new method registers one
implementation, with zero pipeline or form changes. (7) The commercial boundary
holds: no optimizer, no covariance, no risk model (D6).

## Standing inputs adopted

- `docs/research_platform_optimization_from_vibe_quantgpt.md` (Codex memo)
  adopted as the wave-1/wave-2 feature source: research-goal artifacts,
  artifact lineage index, run index + `qf runs`, research memory,
  deterministic ResearchStrategySelector, secondary anti-overfit
  diagnostics (advisory first), factor bench. Its constraints match the
  FP axioms and remain binding.
- Reference projects (read-only): Quant Forge Studio branch, QuantGPT,
  RD-Agent, Vibe-Trading (research components only; trading stack out of
  scope per the memo).
