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

### CP0 amendments (same day, after the mandated Opus adversarial round)

The adversarial review confirmed 7 rulings and required 3 revisions; adjudicated:

1. **D-i ignore durability (accepted).** The `worldquant/` ignore rule existed
   only as an *uncommitted* working-tree edit in the main checkout — one
   `git checkout --`/`reset --hard` away from exposing third-party-derived
   capture to a stray `git add -A`. Now: `worldquant/` and
   `.claude/skills/worldquant-brain/` are **committed** ignore rules on this
   branch and mirrored in the repo-local `.git/info/exclude` for immediate
   effect across every worktree. D-i's "zero `.gitignore` edits" note is
   corrected to "one committed ignore commit".
2. **D-viii member-formula pinning (accepted).** `synthesis_provenance.factors[]`
   additionally carries each member's **`formula` pinned at run time**. The
   workflow-B translator consumes the **full report artifact** (so
   `parameters.decay_days` is visible too), never resolves member formulas from
   the live registry, and refuses on registry drift with closed code
   `MEMBER_FORMULA_DRIFT`. This closes the hole where editing a member factor
   after synthesis would let a submit target an expression that was never
   backtested (FP-G/FP-I).
3. **Merge order + coordination-doc ownership (accepted).** Branch A merges
   before branch B. Branch B does **not** modify `docs/coordination/*` or
   `docs/design/*`; B's decision/progress records are carried in this register
   (branch A) and appended on B only at PR-rebase time after A lands.
4. **D-iv anti-squatting opt-in (accepted, folded into CP1).** A backend
   resolves only when (a) present in the reviewed static table, (b) explicitly
   enabled via `QF_ENABLE_BACKEND_<ID>=1`, and (c) importable — new closed code
   `BACKEND_NOT_ENABLED`. An unpublished module name alone can no longer be
   activated by an unrelated installed package.
5. **D9-supersession hygiene.** Of the D9-recorded CP10 rulings: **superseded**
   — the `custom_weight`/`rank_average` method naming, "`ic_weighted` ships no
   implementation" (P6 ships it), and the per-role composite byte-match;
   **still binding** — FP-4 coverage honesty, truthful `is_fitted`,
   schema-driven single-source param validation, and no optimizer / covariance
   / risk model (D6).
6. **Review-substitution scope (clarified).** The owner's waiver of pre-PR
   Codex passes is a standing decision for this cycle (Codex auto-reviews the
   PRs); the quota embargo (until 2026-07-10 ~06:00Z) independently forbids
   Codex plugin calls. CP4-class strict review = Opus adversarial lenses
   in-cycle + Codex on the PRs.
7. **Evidence hygiene (folded into lane prompts).** RF-3's citation corrected
   in the design doc (conclusion unchanged); P1's catalog source is the TASK
   brief interim (fitted `available:false` until P6), not design §9 verbatim;
   new `docs/design/` files require `git add -f` (blanket `design/` ignore
   rule) or they silently escape both git and the release scan; public gate
   tests use synthetic `SubmissionGateSpec` values only.

## 2026-07-13 — Agent-sidecar frontend design V1/R3.1 (owner directives; recorded by Fable)

Authoritative spec: `docs/design/agent_sidecar_frontend.md`. Execution plan:
`docs/design/WORKORDER_frontend_sidecar_p0_p3.md`. Converged over three
adversarial rounds (Codex xhigh architecture; owner deep-interview locks;
Codex `gpt-5.6-sol` frontend — 16 findings, all adopted) plus owner
directives R3/R3.1.

| # | Decision | Ruling | Implementation |
| --- | --- | --- | --- |
| D10 | Adopt the agent-sidecar frontend design (simple mode default + pipeline cards + sidecar) | **Adopted with iron laws FE-L1..L5** (thin shell over existing renderers; renderers are the only place numbers become pixels; the SERVER decides truth — pipeline state, per-value provenance badges, attempt lineage, `edited_by` all server-derived, client/agent claims untrusted; checkpoint defense-in-depth at the contract layer; one-in-one-out). Human gates G1–G4 (assumption confirm; explicit RD start; candidate→active stays human via `GateDecision`; submit stays CLI dry-run+confirm). Structural prohibitions FE-X1..X4 (sidecar never computes, is never a number's sole carrier, never promotes/releases/submits, never uses external-OOS as selection evidence). Owner deep-interview locks: simple mode is the DEFAULT landing (no-key → seeded form, non-destructive); clarify asks hypothesis-level ambiguity only (≤3 questions with defaults, blocking tier must be answered); tool layer = in-process typed allowlisted adapter with MCP-shaped signatures, **no MCP server in v1**. | Spec §§1–5; net-new modules `apps/web/{pipeline,provenance,narration,tools}.py` + `static/views/{pipeline,provenance,narration}.js` (spec §6); `provenance.js` joins the single-renderer discipline as THE badge renderer |
| D11 | Pipeline topology and RD interval (owner R3 + R3.1) | **Dual pipelines, honest granularity.** `factor_study` (parse→confirm→compute→report; the report is TERMINAL) split from `rd_optimize` (RD-confirm→run→leaderboard); **no automatic A→B bridge** — the report only offers an entry button; RD rounds are user-chosen 1..`MAX_RD_ITERATIONS` on B's own confirm card. Stages map 1:1 to real execution units (A's compute and B's N rounds are each ONE job; no fake per-stage/per-round progress until the backend emits events). **R3.1:** RD inherits the factor-evaluation interval and sample contract — NO independent RD interval parameter exists anywhere; the legacy `rd-interval` auto-cycle controls (开启/停止 timed loop) are DELETED (CLI `research run-once` kept). | Spec §2, §8; WORKORDER P1 (pipeline A) + P3 (pipeline B + auto-cycle deletion) |
| D12 | Anti-bloat governance for the frontend track | **One in, one out (FE-L5).** Deletion ledger ships with the design and executes with each phase: delete `.lab-stepper` (P1, superseded by the pipeline card); absorb-then-delete the resident `#validation-controls` grid (P1); converge `app.js` job wiring into `pipeline.js` (P2); delete `rd-interval` auto-cycle controls (P3, per D11); migrate `#staggered-run` to a report action (P3); **freeze `apps/web/api.py`** — new endpoints land only in new modules, api.py may only shrink. One surface = one static module registered in `EXPECTED_STATIC_MODULES`; removals update string-contract pins in the same commit (tests lock absence as well as presence). | Spec §7–§8; WORKORDER §5 per-phase 减法 items and pins |

## 2026-07-13 — Self-evolution engine CP0 (SE-i..SE-x; owner interview rulings R1–R6; recorded by the coordination steward)

Design input: `docs/design/DESIGN_self_evolution_engine.md` (this register is
authoritative over its §4/§5/§8/§9 where they differ). Review provenance: two
adversarial rounds on `gpt-5.6-sol` (S1 self-evo deep: 21 findings, §5+§9
REJECT → v2; S2 cross-doc/plan: 15 findings), model pins verified in rollout
transcripts; owner deep interview ×2 (2026-07-13).

| # | Decision | Ruling | Implementation |
| --- | --- | --- | --- |
| SE-i | Seam-1 layer + dual-domain stores (owner R1) | Neutral `ResearchOutcome` contract + pure mapper live in `research_loop/` (public kernel). **Dual domain:** external-plugin outcomes (e.g. BRAIN) go to the PLUGIN'S OWN store instance (same `ResearchMemoryStore` class, plugin-local gitignored root) and steer only that plugin's work; the MAIN store ingests LOCAL outcomes only. No main-store external ingress; the previously proposed `RUN_KINDS` extension is CANCELLED (dissolves the web enum-pin ripple and the SE-P2→FE-P2 cross-track ordering). | `research_loop/outcomes.py` (new); store-root parameterization; BRAIN producer stays under `worldquant/` |
| SE-ii | Closed sets + evidence identity | Four-axis split `stage / verdict / reason_codes (sorted, closed) / lifecycle_status` (lifecycle never enters scientific denominators). Self/factor-correlation is a FIRST-CLASS reason family + allowlisted metric keys (`redundancy`, `self_correlation`) (owner R5-②). `evidence_strength` closed tier `submitted_live > platform_simulated > local_backtest > prescreen` weights steering + priors (owner R5-③). Logical evidence unit = hash(local factor fingerprint × canonical typed window × stage); promotion counts ≤1 observation per (signature, evidence-run) — re-simulation and UI-retry gaming both die. Kernel-minted `outcome_id`, producer-side `observed_at`. 10-field canonical signature payload (generalization-by-unification, owner R5-①); typed `{status,start,end}` window — unknown never counts; external same-window evidence capped at finding tier. | `outcomes.py`; property tests incl. smuggling corpus |
| SE-iii | Rule governance = review surface (owner R3) | New Web review tab: `apps/web/memory_review.py` + `static/views/memory.js` + html.py tab mount (lands AFTER FE-P1; html.py under sequential ownership; api.py untouched). Rules MUST pass review: append-only `activate/deactivate/retire` events (actor, rationale, supersedes); promoted rows never mutated; read paths exclude `needs_human_review`. Findings/failures stay automatic, retirable from the review surface. CLI parity command kept. No signatures, no popups. | SE-P4 |
| SE-iv | Single steering point | Proposed lesson-aware `ExperimentPlanner` REJECTED. Pre-generation `ResearchContext` composition is the ONLY steering owner; planner, strategy selector, objective scoring, `candidate_gate`, `GateDecision` stay memory- and external-blind (regression-pinned). Bounded `active_rules` channel: cap 5, exact-scope before global, cross-tier dedup, closed template registry in `llm.py` (template re-authentication would silently drop foreign statements — S1-F11). Channel default-on; per-rule human activation IS the opt-in. Windows consumed for steering are recorded in trace as training/adaptive for descendants, never confirmatory OOS. | SE-P4/P5; `context_builder.py`, `llm.py` |
| SE-v | Priors basis | Computed, non-persisted view over deduplicated ELIGIBLE OBSERVATIONS (never over promoted findings); `passed/blocked/unknown/not_applicable` counted separately; below-floor cells = null + `insufficient_sample`; `evidence_strength` weighting; output carries `as_of` revision for snapshot fingerprints. | `research_loop/priors.py` (SE-P5) |
| SE-vi | Defers (explicit, not silent) | Cross-seed priors + goals wiring deferred (as proposed). P6 register: playbook-style skills (owner R6: V1 skills = approved rules, closed template + scope), regime-conditioned diagnostics, `parameter_search_overlay` selector output, memory compaction/archive design (V1 ships guards only: pruner never age-prunes live memory or non-terminal pipelines; terminal-pipeline TTL default off). | recorded here |
| SE-vii | Open boundary (owner R2) | Mechanism code public (BUSL source-available); learned memories/skills stay structured and LOCAL under artifact/plugin roots — never published. Strict field allowlists; tracked-tree leak tests + runtime-artifact canary sweep (paths / alpha-id shapes / synthetic secrets) at CP-INT. Promotion-semantics divergence from the inspiration memo (two same-window runs → active finding) RETAINED deliberately; external finding-tier ceiling compensates. | SE-P1 tests; Step F sweep |
| SE-viii | Submission margins (owner R4) | Sharpe ≥ 1.28, Fitness ≥ 1.05, Turnover ≤ 60% — INCLUSIVE semantics; instance lives ONLY in the local adapter config (gitignored), kernel zero hardcode; tuning changes logged with provenance. | `worldquant/adapter/` config (SE-P3) |
| SE-ix | Cross-track contract (A×B) | `planning_influence_snapshot` captured at pipeline confirm (cutoff revision, eligible activated-rule ids, priors query fingerprints, overall hash); hash joins pipeline `input_hash` (same-nonce ⇒ same search policy); disclosure persisted in trace/result/report/web payload and rendered ONLY by canonical components (FE-P3). Sidecar v1 gets NO general memory-read tool — only the run-local snapshot projection. Memory-store appends and promote/activate critical sections adopt the lineage advisory-lock pattern. | SE-P5 freeze before FE-P3 review |
| SE-x | Transformation sequencing | Step 0: phase-f closure + owner merge precedes every `service.py`/`api.py`-touching phase; Step 0b: D12-compliance chore (extract fe23744's api.py additions into a module; api.py net-shrinks). SE-P4 after FE-P1 (html.py). Temp integration branch runs the full CP-INT (incl. two-run activation/steering determinism fixture) before final merges; integration/architecture/workflow docs update FIRST (they currently contradict D11's auto-cycle deletion). One coordination-doc steward (DECISIONS/WORKING_STATE/EP appends serialized). Public synthetic-producer fixture (no provider vocabulary) drives the REAL ingress chain in CI; SE-P3 completion claim requires public CI proof + local BRAIN offline suite both green. | master sequence; Step F |

**Amendment (2026-07-13, owner): two-workflow / two-PR topology.** The two
tracks develop as two workflows and land as exactly TWO pull requests:
Workflow A = frontend sidecar (one branch accumulating P0→P3) and Workflow B =
self-evolution engine v2 (continues `feature/self-evolution-engine`: design +
CP0 + SE-P1..P5 code; the SE-P3 plugin producer is gitignored local-only and
contributes no tracked diff). Phase discipline inside each track is unchanged:
per-phase dev → adversarial review → fixes, sequential within a track. The
SE-x temp-integration-branch CP-INT runs over the union of both PR heads
before either merges; owner approval gates both merges.

**Amendment (2026-07-15): CP-INT executed and closed on the local union
branch.** The SE-x temp-integration-branch requirement is SATISFIED:
`cp-int-union` = merge of the two PR heads; exactly two content conflicts
(routing.py route families; EXPECTED_STATIC_MODULES), both resolved as
pure unions; first united suite 1971 passed, 1975 at union tip. The SE-ix
real capture is wired ON THE UNION ONLY: both confirm sites persist the
snapshot and fold its hash into `input_hash`; the frozen contract and
golden vector are byte-untouched; the two-run determinism fixture proves
identical inputs → identical hashes, governance events → different hashes
with strictly increasing `review_events_revision`, and priors invariance
under events. Full integration test per the canonical prompt: verdict
ACCEPT (real desktop Chrome, zero blocking/major findings; residuals
registered as BUG_LIST U-1..U-3 plus two doc fixes landed in place).
Runtime-artifact canary and tracked-tree sweeps clean. Register
adjudications: the §8 app.js→pipeline.js convergence row is ruled
SATISFIED in aggregate (the resident validate control routes through the
pipeline aggregate; the parse job is the pipeline's designed entry; the
remaining app.js state is a deliberate ownership decision); the
publish-CAS residual window and the plugin-domain read-only pane remain
deferred with their recorded boundaries; the RD kernel scoring
legacy-alias consumption is queued as an owner question. Landing shape:
the two PRs stay independent; the union-only wiring + roving-cycle
commits land as a small follow-up PR after both merge; `cp-int-union`
itself is not pushed.
