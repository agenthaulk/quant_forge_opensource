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

## 2026-07-09 — CP0: multi-factor backtest backend × external factor backends (workorder adjudications, Fable)

Context: the owner workorder (local `docs/design/WORKORDER_fable_multifactor_and_external_backends.md`)
merges two file-scope-disjoint workflows: **(A)** the server side of the
already-shipped multi-factor frontend, per the authoritative
`docs/design/multi_factor_portfolio_backtest.md`; **(B)** a pluggable external
factor-backend extension family with a WorldQuant BRAIN first adapter. These
rulings extend D6/D7/D7a/D8; items with commercial/licensing weight are marked
**owner-reviewable**.

| # | Decision | Ruling (Fable) | Implementation |
| --- | --- | --- | --- |
| D-0 | Two backend architectures exist for the same shipped FE: the in-memory per-role composite landed at `da07e69` (`fable/phase-d-multifactor-synthesis`, merged into `fable/phase-d-converged`), and the adversarially-reviewed materialize-as-`COMPOSITE_<hash>`-precomputed-factor design | **The design doc is authoritative; the `da07e69` backend is superseded.** Grounds: (1) owner designation of the spec as authoritative; (2) strongest FP-F reuse — the composite drives `run_factor_backtest` **by id**, engine unchanged except two additive honesty fixes, so the composite inherits artifacts/history/registry/benchmark for free; (3) a materialized composite is a first-class registered factor — the natural submittable object for workflow B (D-viii); (4) the backtest-only ruling (D-ix) dissolves the two-window per-role divergence that motivated the in-memory design; (5) the design review's RB-3 (non-deterministic tie-break) and RB-7 (silent skipped rebalance) are engine-level honesty gaps that affected the superseded path too. The earlier CP10 backend rulings (per-role composite, `MFC_` ids, score-seam extraction) are superseded where they conflict; the seam extraction is re-deferred to design §14 P8. Salvage inputs (not contracts): ParamSpec schema validation, per-date standardizers, method-registry shape, FP-4 test patterns. | `fable/phase-d-converged` and `fable/phase-d-multifactor-synthesis` retained unmerged as archive; deletion needs owner approval. New Phase D PR candidate = `fable/phase-d-synthesis-backend`. |
| D-i | Where does the external-backend adapter live (D6 commercial/agent layer vs public optional extra)? **owner-reviewable** | **Split by capability.** Public core (this repo): provider-neutral typed `FactorBackendPort` contracts, the `integration.factor_backend` contribution point, registry surfacing, CLI degradation paths, and the provider-neutral submission-gate evaluator (pure local math). The WorldQuant adapter (formula translator + BRAIN gate spec + REST client with auth/simulate/submit) is a **local-only package under the already-gitignored `worldquant/adapter/`** (installable via `pip install -e`), absent from the distribution: FP-E places credentialed outward submitters at the commercial/agent layer; D-v counsels caution while any third-party-derived surface is involved; and the split is reversible (publishing later is one decision — unpublishing is not). | Public: `src/quant_forge/integrations/` + extensions vocabulary. Local: `worldquant/adapter/` (covered by the existing ignore rule; zero `.gitignore` edits). |
| D-ii | Port granularity | **One typed port, four declared capabilities.** `FactorBackendPort.describe() -> BackendDescriptor{backend_id, label, regions, capabilities ⊆ {translate, prescreen, simulate, submit}}` plus `translate/prescreen/simulate/submit` methods over typed request/result dataclasses; calling an undeclared capability raises `CapabilityNotSupported`. Closed-set warning codes: `BACKEND_NOT_INSTALLED`, `BACKEND_NOT_CONFIGURED`, `REGION_MISMATCH`, `NOT_TRANSLATABLE`, `SUBMIT_NOT_CONFIRMED`, `PRESCREEN_LOCAL_PROXY_ONLY`. | CP1 contracts module + tests. |
| D-iii | Does the provider-neutral pre-screen enter the public kernel? | **Yes.** The gate evaluator (dollar-neutral weighting, Sharpe / fitness / turnover-band / sub-window-Sharpe / concentration / returns-floor arithmetic over **local** backtest outputs, parameterized by a `SubmissionGateSpec`) is pure math — no credentials, no network, no provider imports. Provider-specific spec **instances** (threshold values) ship with the adapter. FP-G honesty: when local data region ≠ target platform region, prescreen emits `REGION_MISMATCH` and must not claim a predicted pass-rate. | CP2 public half (`integrations/gate.py`) + regression tests incl. the ex-post-selection ban. |
| D-iv | Pluggability mechanism | **Declarative manifest metadata + a static reviewed import table. No entry-point scanning, no path-based loading, no runtime exec (D7 unchanged).** The extensions vocabulary gains `integration.factor_backend` as a *declarative* contribution point (metadata only; executable contributions remain rejected unconditionally). Capability **binding** is a code-reviewed constant `{backend_id → fixed module name}` resolved by a literal `import` inside `try/except ImportError` — the D7a in-repo-adapter precedent extended to optional-install: package absent ⇒ capability does not exist ⇒ honest `BACKEND_NOT_INSTALLED`. Entry-point discovery was considered and **rejected**: it imports whatever installed distribution claims the group — a wider trust surface than a reviewed one-line table. | CP1 registry extension; adding a backend = one reviewed public PR line + an installable package. |
| D-v | Copyright / captured-content hygiene | Captured BRAIN documentation **prose never enters tracked files or the distribution**. Only facts — operator names/signatures, field ids, numeric submission thresholds, endpoint shapes — may inform code, expressed originally. The local adapter package is kept prose-clean too; `worldquant/mapping/*.yaml` stay local-only inputs. | Release scan + adversarial review lane checks; CP-INT leak sweep. |
| D-vi | Workflow A git strategy | Branch **`fable/phase-d-synthesis-backend`** forked from `4dee08a` (the shipped FE contract), worktree `.claude/worktrees/fable+phase-d-synth-backend`. First commit = the D-ix backtest-only FE patch; Phase D docs commits `575ede4`/`ab950d0` cherry-picked so the Phase D story travels with the PR. Atomic per-phase commits; no push/merge/delete without owner approval. | In effect (this commit). |
| D-vii | Method-set phase boundary | **Confirmed: P1–P5 first** (a-priori `equal_weight` + `weighted` runnable end-to-end — resolves the dead 合成并回测 button), with `ic_weighted`/`icir_weighted` shipped `available:false` (reserved) in the catalog until **P6** flips them within this same workorder execution. End state = design §9. | P1 catalog ships reserved fitted rows; P6 enables them. |
| D-viii | Composite factors as submittable objects (A→B seam) | **Yes, with an honesty boundary.** Materialized `COMPOSITE_<hash>` factors are first-class registry entries (`source="synthesis"`) and valid workflow-B targets. Translation reconstructs the symbolic expression from `synthesis_provenance` (member formulas × standardization × direction × a-priori weights) — possible only when **every member is formula-backed and the method is a-priori**. Fitted (time-varying-weight) composites are refused as `NOT_TRANSLATABLE`: a static expression would misrepresent the backtested strategy (FP-G/FP-I). Submission provenance carries `composite_id` + full synthesis provenance end-to-end. | CP2/CP3 translator honors the boundary; fixture-tested against the §8 provenance shape. |
| D-ix | Uncommitted FE WIP (eval-interval removal) in the concurrent session's checkout | **Adopted by patch, not by committing in the other session's tree.** The 6-line backtest-only diff (`html.py` / `synthesis.js` / `test_web_synthesis_view.py`) was captured via `git diff` and committed as the first commit on the workflow-A branch; the concurrent session's working tree was left untouched. FE contract baseline = `4dee08a` + this patch. | Commit `8eabc05` on `fable/phase-d-synthesis-backend`. |

Also recorded: during the owner's Codex quota embargo (until 2026-07-10 ~06:00Z)
all lanes run fable/opus/sonnet; strict review = Opus adversarial now + the
Codex auto-review at PR time (owner waived a separate pre-PR Codex pass).
Workflow B branch: `fable/phase-e-external-backends`, forked from `main@81ed4cf`
(PR-independent; file scopes disjoint from A; CP-INT merges both locally for the
integration container only). Deferred question (recorded, not blocking):
retention/GC policy for successful `COMPOSITE_*` registry entries.
