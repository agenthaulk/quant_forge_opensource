# Engineering Progress — Phase C Platform Buildout

**This file is the session-recovery anchor.** Protocol (owner directive
2026-07-06): work is split into checkpointed phases; a phase is marked DONE
only after its gate ran green AND its commits landed. On a session-limit
interruption: read this file first; phases marked DONE need no re-audit
beyond confirming their commits exist (`git log --oneline <range>`); resume
at the FIRST phase not marked DONE by running its Verify command, then its
remaining steps. Do not re-derive completed phases from transcripts.

Branch: `fable/phase-c-platform-buildout` since CP4-1 landed (created from
`fable/phase-c-research-platform-wave1` tip `8dc2731`, then merged
`origin/main` @ `7e85b76` = PR #13 Phase A merge, zero tree delta — owner
approved sync 2026-07-07; the wave1 branch is preserved at `8dc2731`).
Earlier CPs landed on `fable/phase-c-research-platform-wave1` (base =
Phase B tip `562a52b`). Gates definition: `PYTHONPATH=src python3 -m
pytest -q` + `python3 scripts/release_safety_scan.py` + CLI `--help` +
`git diff --check`.

---

## CP0 — Foundations (Phases A/B) — ✅ DONE
- Phase A quant-core audit: **PR #13 MERGED** 2026-07-07 (true merge,
  commit `7e85b76`; branch `fable/phase-a-quant-core-audit` @ bf19c73,
  426 passed at submission). Local main fast-forwarded 7931522 → 7e85b76.
- Phase B architecture + specs contracts: accepted @ `562a52b` (487 passed).
- Verify (if ever needed): `git log --oneline bf19c73 562a52b` exist; PR #13 open/merged.

## CP1 — Wave 1: kernel decisions + research platform core — ✅ DONE
- Scope: owner decisions D1-D6 recorded; D3 partial-period default flip
  (+opt-in on CLI/web/workbench); falsification diagnostics module
  (advisory); lineage store + run index + `qf runs` + `qf factor bench`;
  deterministic 5-strategy RD selector wired (run-scoped context, disable
  flag); Opus review ×10 findings all resolved; session-limit interruption
  audited and completed.
- Commits: `af19710..87dbff9` (7). Gate: **555 passed**, scan 141 files,
  CLI OK, diff clean. Docs: `WAVE1_REVIEW_RESOLUTION.md`, `DECISIONS.md`.
- Verify: `git log --oneline af19710..87dbff9 | wc -l` == 6; suite green.

## CP2 — Wave 2: memory / goals / falsification surfaces — ✅ DONE
- Landed: research memory (`ea849f8`), goal artifacts (`b3960e2`),
  falsification surface (`7aede17`). Gate at landing: **588 passed**
  (555→588), scan 148 files, CLI OK, diff clean.
- Note: the wave-2 Opus review inside the workflow was blocked by a
  safety-filter false positive on its wording; a neutral-worded
  verification agent was relaunched and its findings fold into CP3.
- Verify: `git log --oneline f0b2c84..7aede17 | wc -l` == 3; suite green.

## CP3 — Cross-review adjudication + hardening — ✅ DONE
- Codex wave-1 review (gpt-5.4 high, task-mrafznd5-ls7oz2) returned
  **fix-first ×6, ALL ACCEPTED by Fable**:
  C1 blocker: selector context reads OOS decay/blocking reasons from the
  LAST-evaluated candidate, not the round winner (service.py:840,2763);
  C2 major: same-seed chain history truncated by the global 200-row window
  BEFORE seed filtering (service.py:583);
  C3 major: `_segment_metrics` treats a partial tail as a full holding
  period while top-level metrics use actual exposure; `_return_summary`
  observation_count overstates when vol uses the complete-only subset;
  C4 major: redaction misses UNC `\\\\server\\share` and `file://host/...`;
  C5 major: lineage dedup read-then-append races under concurrent CLIs
  (fix: advisory flock around read+append, no-lock fallback documented);
  C6 minor: `qf runs search --kind` argparse lacks rd/falsification.
- Opus wave-2 verification RETURNED: fix-first ×2 major (O1 vacuous
  goal completion when all criteria optional; O2 research memory inert —
  service.py:561 builds the context builder without memory_store, nothing
  records observations, and llm.py prompt assembly ignores the fed fields)
  + O3/O4/O5 promotion-count honesty, O6 transition-table bypass, O7=C6,
  O9 naive timestamps + unredacted fields, O10 CLI errors/symlink
  containment, O8 assembly duplication (DEFERRED to CP7, parity test added
  now). Core invariants verified sound (no rule auto-activation,
  append-only, evidence gating, frame parity 1e-12).
- Resolution: serial fix agent completed all 17 file edits, then was
  interrupted by a session limit at its final full-suite step; Fable
  re-verified from scratch (compileall, per-item marker audit, full suite,
  scan, CLI, diff-check) and landed the commits — no work assumed done
  without on-tree evidence.
- Commits: `627bc34` (backtest C3), `445d4c7` (lineage C4/C5),
  `4d4a4a3` (rd/CLI C1/C2/C6 + O1-O6/O9/O10 + parity guard). O8 deferred
  to CP7 with an IC-series parity test (1e-9) guarding the seam.
- Gate: **613 passed** (588→613, +25 tests), scan 148 files, CLI OK,
  diff clean. Adjudication table: WAVE1_REVIEW_RESOLUTION.md §CP3.
- Verify: `git log --oneline 627bc34 445d4c7 4d4a4a3` exist; suite green.

## CP4 — Server decomposition + Web research panels — ✅ DONE
- Step 1 (extraction) ✅ DONE. Order was binding (B4 F11): characterization
  tests FIRST (`d682f20`, 23 tests, verified green against the pre-split
  server via stash), then pure-move extraction (`8dc2731`): server.py
  3451→167-line composition root re-exporting all 144 names; jobs.py 210 /
  api.py 1391 / routing.py 353 / html.py 1594; 9 monkeypatch seams
  late-bound at 24 call sites (in-function `import server as _server`);
  `_index_html` byte-identical; logger channel pinned;
  test_web_workbench.py untouched. Gate: **636 passed** (613→636), scan
  153 files, compileall, CLI, diff-check all OK — Fable independent re-run.
- Codex (GPT-5.5) strict review task-mrafznd5→task-mrbgqnk3: missed-seams /
  early-binding / import-cycle / semantic-drift / re-export-completeness
  ALL CLEAN (AST+grep audits). One minor ACCEPTED-DEFERRED to step 2: the
  new test file behaviorally exercises only workflow-level seams (deep
  seams are hasattr-only there; behavioral protection lives in
  test_web_workbench.py within the same gate). `_web_public_json`
  recursion now binds to the real implementation inside api.py (old code
  resolved via module globals); adjudicated no-impact — the only existing
  patch of that name is a whole-function replacement, not a delegating
  wrapper.
- Step 2 ✅ DONE (`1f66648` seam/error-mapping tests — verified green
  against pre-CP4-2 sources via stash, 30 passed; `d392e52` panels):
  GET /api/research/history (lineage RunIndex.read_rows, FP-5 no parallel
  parser) + GET /api/bench (kind=bench + qf.bench.v1 loader) + 研究历史 /
  Benchmark panels. MetricValue {value,unit,status,observation_count}
  end-to-end, null-not-zero (JS strict null checks). Codex (GPT-5.5)
  review of the step-2 diff: 3 majors ALL ACCEPTED and fixed in place —
  F1 bench artifact validates schema_version+run_id vs the referencing
  row; F2 O_NOFOLLOW fd open closes the final-component symlink race
  (residual intermediate-dir race documented, local-only threat model);
  F3 ValueError→400 reflection scoped to the two new endpoints,
  pre-existing GET error mapping restored byte-identical to HEAD.
  Clean per Codex: MetricValue honesty, seam late-binding (10 new
  X-as-X re-exports), test quality, param validation.
- Gate: **655 passed** (613→636→655), scan 154 files, compileall, CLI,
  diff-check OK — Fable independent re-runs at each step.
- Verify: `git log --oneline d682f20 8dc2731 fe0fcdc 1f66648 d392e52`
  exist; suite green.

## CP5 — Data plane — ✅ DONE
- Landed in `8f95f0a` (D1 lane): DataCatalogPort backed by the loaded
  PANEL_FIELD_CATALOG (mcp/read_models.py advertises real fields, payload
  byte-compatible); research metadata tags (factor_library/research_tags.py,
  OUR schema per WAVE1 memo correction #1; D7/D7a-ready plain data);
  documented+tested field expansion path; ValidationGate consults the real
  catalog. (Owner decision D2.) Codex xhigh review majors fixed in-commit.
- Verify: `git log --oneline 8f95f0a` exists; `tests/test_data_catalog_*.py`
  present; suite green.

## CP6 — Interactive platform frontend (D6/D8) — ✅ ALL DONE (CP6-1..4)
- Framework review = decision D8 (`a574674`, CP6_FRONTEND_PLAN.md): static
  ES-module app on the stdlib server, no build step / npm / external
  resources; sub-phases CP6-1 skeleton → CP6-2 Lab → CP6-3 Data+Registry
  → CP6-4 Docs+Extensions. Design references Studio + an Opus design pass
  from CP6-2 on (D-clause `633945c`).
- **CP6-1 ✅ DONE** (`82fc631`): html.py inline JS (1759→674) extracted to
  static/{app,api,metric,views/*}.js served by a containment-checked
  static handler; metric.js is the single MetricValue renderer (FP-4
  null-not-zero preserved verbatim); pyproject ships static/*.js. Codex
  xhigh review (fresh thread, after the first job hung) + Fable spot-check:
  all clean. Gate: combined barrier **748 passed**, scan 172, CLI, diff.
- **CP6-2 ✅ DONE** (`d83f28e`): Lab workbench view — flow stepper
  (想法→解析→验证→因子报告→RD 循环) + four tab panels re-hosting the
  existing mounts; views/lab.js pure tab controller (zero fetch by
  pinned contract), views/spark.js inline-SVG sparkline (null marks
  skipped, never 0); factor/research componentized into section
  renderers with byte-preserved markup; token-gated panels lazy-refresh
  on activation + token storage. Opus design pass: computed WCAG audit,
  4 contrast fixes (--accent-ink cut-out for dark), bench metric cells
  adopt metricCellHtml. Codex xhigh fresh-thread review (fingerprint
  verified): 3 major + 2 minor, all adjudicated accepted and fixed
  (panel refresh wiring, FP-4 null-to-zero sweep, esc() interpolation
  sweep, report-anchor deep links, 4 regression tests). Gate: **763
  passed**, scan 175, CLI, diff-check clean.
- **CP6-3 ✅ DONE** (`ad0889d`): Data console + Registry views. Four
  GET-only endpoints (/api/data/catalog, /api/data/status,
  /api/registry/factors, /api/registry/factors/{id}) with CP4-2
  endpoint discipline; /api/data/status built field-by-field so
  DataValidationResult path fields never enter the payload; factor-id
  segment decode-once-then-validate. views/data.js + views/registry.js
  + views/tags.js (single research-tag chip renderer; null-vs-empty
  visible in UI). Codex xhigh fresh-thread review: 1 major
  (percent-encoded legal ids 404ed) + 1 minor (id-class coverage) both
  fixed. Gate: **795 passed**, scan 180, CLI, diff clean.
  Residual (non-blocking, batch with integration findings): KeyError
  404 bodies carry repr quotes (shared /api/jobs/ pattern);
  fetchPanelJson errors lack HTTP status so 404/400 render unified;
  history.js/bench.js duplicate status label for non-available metrics
  (pre-existing CP4-2 quirk, fixed only in registry view).
- **CP6-4 ✅ DONE** (`02fb31e`): Docs view + declarative Extensions
  registry (D7/D7a executed in full, not cut). Stdlib markdown renderer
  (escape-first, tag whitelist, external URLs never anchors);
  /api/docs{,/relpath} with the doc-name rule defined once server-side
  and mirrored by the frontend hash routers; extensions manifest
  schema + validation (executable contribution types rejected
  unconditionally, no dynamic loading, case-insensitive external-URL
  rejection) + reference manifest + browse panel. Codex xhigh review
  (fresh thread after a hung first launch — orphaned-job countermeasure
  applied): 1 major + 3 minors, resolved by narrowing the doc-name
  contract to a single server-side rule. Gate: **880 passed**, scan
  190, CLI, diff clean. Lab strip: eight tabs (工作台/RD/历史/Bench/
  数据/注册表/文档/扩展).
- Optional cuttable sub-item per decision D7: declarative-only Extensions
  registry (manifest schema + 5 MVP contribution points + read-only
  browse panel; executable contributions rejected unconditionally; data.*
  points feed the CP5 DataCatalogPort, no parallel catalog). Defers to a
  post-CP8 enhancement wave if CP6 runs tight.

## CP7 — Residual register — ✅ DONE
- Mechanical residuals (D2 lane): `f89bc72` _atomic_write concurrent-writer
  safety, `b6e0b48` F-6 purge-count persistence (MetricValue into
  artifacts + report), `dbe8381` O8 single IC-assembly path (1e-9 parity
  guard untouched). Codex-reviewed, 1 major + 1 minor fixed pre-landing.
- Gate-cluster residuals (D1 lane, in `8f95f0a`): F-1 retention/turnover/
  correlation evidence fail-closed; F-2 single gate-definition authority;
  F-3 _backtest_metrics segment_metrics; F-4 demo fillna removed; F-5
  rd.yaml missing_oos_evidence_blocks knob. Codex xhigh review majors
  fixed in-commit.
- Free-text data-handling review (input-validation robustness item):
  read-only dataflow analysis landed as
  docs/reviews/free_text_dataflow_review.md (`e274012`, reworded to
  neutral framing in a later commit); findings F1-F6, proposals P1-P5
  accepted.
- CP7-H hardening batch (`7b10667`): P1 read-time statement/hint template
  gates (whole-statement match to the exact service templates), P2
  family-reduced memory statements, P3 provider error-body cap, P4 factor
  free-text caps + web-path slug + draft-only status, P5 DSL window/
  formula-length caps + horizon bound, F6 tz-aware lineage timestamps.
  Codex xhigh review found 2 gaps (prefix-only match; wq_min/wq_max window
  skip) — both fixed by an Opus lane. Test fixtures use benign structural
  markers, not free-form values.
- Gate: combined barrier **748 passed**, scan 172, CLI, diff-check.
- Verify: `git log --oneline f89bc72 b6e0b48 dbe8381 e274012 7b10667`
  exist; suite green.

## CP8 — Integration acceptance + merge prep — 🔶 INTEGRATION ACCEPTED
- **Full integration test PASSED end-to-end** (owner directive
  2026-07-08; per docs/full_integration_test_prompt.md, adapted to test
  the Phase C branch pre-merge): fresh `git archive` of the branch tip
  into a clean python:3.12-slim container (constraints pins), real
  DeepSeek LLM, real desktop Chrome (Playwright-CDP Level 2 fallback —
  Computer-Use tooling unavailable that session) over the whole flow:
  3 seeds parse → 11-param adjust → validate/evaluate → RD ×1/×2 →
  all eight tabs → deep-link reloads → zero console errors →
  true-375px (CDP device metrics). Zero blocking issues. Evidence:
  37 screenshots + findings register (session scratchpad).
- **Findings batch-fixed and re-verified against the running system**:
  F-001..F-010 landed in `7983795` (4 disjoint lanes + Codex xhigh
  review; its 1 major — invalidate-during-in-flight refresh race — and
  1 minor fixed with a 14-check node state-machine smoke) and
  F-011/F-012 in `4906072` (job-failure reasons surfaced in all five
  handlers; .github/ ships in the image so the in-container pytest
  gate passes). Re-verification round: all findings VERIFIED-FIXED in
  a fresh container built from the new reference Dockerfile.
  Gate: **894 passed**, release scan 194 files (roots now include
  Dockerfile/extensions/constraints.txt/CLAUDE.md), CLI, diff clean,
  docker build OK.
- Known cosmetic residuals (deliberately deferred): failed RD
  schedule-start leaves its placeholder; KeyError 404 bodies carry
  repr quotes; fetchPanelJson errors lack HTTP status; history/bench
  duplicate status label on non-available metrics.
- Remaining for CP8 closure: Phase C PR (stacked on PR #14) after the
  sensitive-info leak sweep; WORKING_STATE migration into
  docs/coordination/; final cross-review at merge time.

## Phase D — frontend design-parity + multi-factor synthesis — 🔶 CONVERGED (pre-PR)
- Framing = decision D9 (docs/coordination/DECISIONS.md): design-parity, not
  stack-parity — adopt Studio's charts / module IA / polish, keep D8 (no build
  step, static ES modules, zero external resources). Sub-lanes CP9-1 charts,
  CP9-2 IA, CP10 backend + frontend, then convergence. Branch
  `fable/phase-d-converged` carries all of Phase D on the Phase C tip
  (`81ed4cf`, PR #15 merge).
- **CP9-1 charts ✅** (`1035eb8`): honest inline-SVG charting module
  `static/views/charts.js` (line/area/bar; "spark.js grown up" — pure
  string-returning functions, no DOM builder, no state). FP-4 is the whole
  point: a missing/null/non-finite point is a GAP in the path, never plotted
  as 0 (each contiguous finite run is its own subpath); a fully-absent series
  renders an explicit empty-state box, never a flat line at 0; bars are always
  zero-based. Colors come only from existing CSS vars (both themes); each chart
  is a role="img" SVG with a11y disclosure of the missing-data count. Eight
  charts wired over the EXISTING payloads (factor/research/bench), no new
  endpoints. Gate: **912 passed**.
- **CP9-2 IA ✅** (`a5671a5`): consolidated 8→6 top tabs into the
  「LLM 因子工作台」 primary view (`static/views/lab.js` + `html.py`). The
  workbench tab keeps id `lab-tab-factor`; the former RD 循环 and Benchmark
  tabs fold into its 单因子研究 module as the `#workbench-rd` /
  `#report-comparison` sections; 多因子策略回测 is the reserved CP10 module
  slot. Legacy `#lab-tab-rd` / `#lab-tab-bench` hashes migrate through
  `LEGACY_HASH_ALIASES` (no deep-link dead-ends); structural DSL formula
  highlighting added (`static/views/dsl.js`). Gate: **915 passed**. Its
  adversarial-Opus review fixes (tab hover contrast MAJOR; single-dot priority
  error>running>done; hash fidelity so reload/copy-link carry the canonical
  fragment) landed folded into the CP10-FE commit (see below).
- **CP10 backend ✅** (`da07e69`): synthesis package `src/quant_forge/synthesis/`
  (contracts, methods, registry, standardizers, alignment, orchestrator). SIMPLE
  a-priori synthesis — a composite SIGNAL, not an optimized portfolio: three
  runnable methods (`equal_weight`, `custom_weight`, `rank_average`),
  `ic_weighted` reserved as a non-runnable schema stub. PER-ROLE composite
  keeps every per-factor score byte-identical to the single-factor path (FP-5);
  complete-case coverage (FP-4: missing never imputed to 0);
  `is_fitted` pinned False and enforced; deterministic `MFC_` composite id.
  Schema-driven validation (`validate_params_against_schema`) is the single
  enforcement source for both the backend and the frontend form. Three
  endpoints: GET /api/synthesis/methods, POST /api/multi-factor-backtest
  (sync), POST /api/jobs/multi-factor-backtest (async). No optimizer/
  covariance/risk model (D6). Gate: **1010 passed**.
- **CP10 frontend ✅** (`4dee08a`): 多因子策略回测 module `static/views/synthesis.js`
  filling the reserved `#multi-result` slot — schema-driven dynamic params form
  (rendered PURELY from the chosen method's `ParamSpec` list, zero per-method
  hardcoding, so a new method needs no frontend edit), honest degraded state
  when the methods catalog is absent, and a provenance / validity / coverage
  report (raw a-priori weights echoed unnormalized, coverage null→n/a never 0,
  is_fitted surfaced only as the 先验声明 label). Landed together with the
  CP9-2 IA review fixes at **931 passed**.
- **Convergence 🔶** (merge `1999b53`, parents `4dee08a` frontend-parity line +
  `da07e69` CP10 backend): the CP10 backend was merged into the CP9-1→CP9-2→
  CP10-FE frontend-parity line on `fable/phase-d-converged`. Combined barrier
  **1037 passed**, release scan 216 files, CLI OK, diff-check clean. The FIRST
  real end-to-end multi-factor backtest was verified live: a composite `MFC_`
  id, raw declared weights 0.6/0.4 echoed unnormalized, per-role coverage
  reported for both engine roles, a-priori validity banner (is_fitted=false),
  and FP-4 coverage caveats surfaced rather than papered over.
- Review posture (recorded honestly): the CP9-2 and CP10-FE strict reviews ran
  on Opus (adversarial, fresh-context) during a Codex quota embargo. A Codex
  confirmation pass is deferred to post-embargo / pre-PR; it is a known
  remaining gate, not a skipped one.
- Verify: `git log --oneline 1035eb8 a5671a5 da07e69 4dee08a 1999b53` exist;
  suite green; `python3 scripts/release_safety_scan.py` passes.

## Phase D (revised 2026-07-09) — synthesis backend per the authoritative design (workorder CP0)

- **Supersession (CP0 / D-0):** the `da07e69` in-memory synthesis backend and
  the `fable/phase-d-converged` line are **superseded** by
  `docs/design/multi_factor_portfolio_backtest.md` (materialize the composite as
  a colon-free `COMPOSITE_<hash>` `precomputed:` factor; drive the unchanged
  `run_factor_backtest` by id; two additive engine honesty fixes RB-3/RB-7 plus
  the shared `rebalance_indices` helper RB-5). Full adjudication set D-0,
  D-i..D-ix in `docs/coordination/DECISIONS.md` (CP0 section).
- **New Phase D PR candidate:** `fable/phase-d-synthesis-backend`
  (fork `4dee08a` → `8eabc05` D-ix backtest-only FE patch → cherry-picked docs
  `575ede4`/`ab950d0` → CP0 docs → P1..P6 atomic commits, this section updated
  per phase).
- **Plan (design §14):** P1 catalog endpoint (fitted rows reserved) → P2
  additive engine fixes (deterministic mergesort tie-break, skip-ledger stub,
  `rebalance_indices`) → P3 a-priori composite core → P4 materialization +
  engine drive (`decay_days=0` pin, per-run overlay, all-input hash id) → P5
  job endpoint + §8 payload (same-window evaluation diagnostics, FP-2) → P6
  fitted PIT IC/ICIR (embargo `idx(s)+delay+holding ≤ idx(d)`, honest
  downgrades). Workflow B (`fable/phase-e-external-backends` from `main`):
  CP1 public port/manifest/registry seam → CP2 WorldQuant adapter (local-only
  `worldquant/adapter/`, D-i) + public gate evaluator → CP3 CLI wiring
  (`qf backends list`, `qf factor submit --target`) → CP4 adversarial reviews.
  Then CP-INT per `docs/full_integration_test_prompt.md` over a local merge of
  both branches.

### Implementation wave landed (2026-07-09, same day)

- **Workflow A (`fable/phase-d-synthesis-backend`):** P1 `485e988` catalog
  endpoint (fitted reserved) → P2 `2261850` additive engine fixes
  (RB-3 stable mergesort tie-break, RB-7 skip-ledger stubs excluded from
  metric series, RB-5 `rebalance_indices`; zero existing assertions
  re-baselined, BASE artifacts byte-identical on no-tie runs) → P3 `02c2d6a`
  a-priori core → P4 `533d462` materialization + engine drive (RF-1/2/3,
  LA-1, RB-10 all-input hash, failure cleanup) → P5 `cd48bcf` job endpoint +
  §8 payload (eager preflight = clean 4xx, degraded evaluation slot instead
  of a literal-"undefined" tile, orchestrator placed in `apps/web/api.py`
  to avoid a core→web import inversion, `architecture.md` updated, Node
  renderer drive over a real wire payload) → P6 `aa13ccd` fitted PIT IC/ICIR
  (shared-grid embargo, engine forward returns, noise-floored ICIR guard,
  honest downgrades, FP-1 fitted-field split) → `4119237` verify-pass nit
  cleanup. **Gate: 1108 passed, scan 226 files, CLI OK, diff clean.**
- **Workflow A verification:** P4 targeted adversarial verify PASS (5 minors
  carried to final review); P6 anti-peek verify PASS — independent reference
  implementation matched to 1e-12 across 6 geometries, embargo boundary
  teeth-proofs (strict-< and ≤d+1 both distinguishable and absent),
  survivorship-fill inheritance proven, downgrade honesty probed, ICIR
  noise-floor misclassification unreachable below n≈18k names/cross-section.
- **Workflow B (`fable/phase-e-external-backends`):** CP1 `aeb368c` port/
  contracts/static registry + extensions vocabulary → CP2 `84205f4`
  provider-neutral gate evaluator (public) + local-only WorldQuant adapter
  under gitignored `worldquant/adapter/` (87 offline tests; D-i) → CP3
  `97225e1` `qf backends list` + gated `qf factor submit --target` + agent
  facade without submit (FP-D) → `aebca45` review-fix batch
  (TARGET_REGION_UNSUPPORTED + BACKEND_ERROR closed codes, degraded-
  simulation block, per-row violation containment, gate-var uniqueness).
  **Gate: 991 passed, scan 203 files.** Opus adversarial review: PASS,
  0 blocking/major; all 4 public minors/nits fixed in `aebca45`; adapter
  advisories fixed in the local package (honest region-refusal report,
  BACKEND_ERROR receipts, refuse-first on truncated provenance, absolute-URL
  rejection for cookie scoping).
- **CP-INT integration line:** local-only `fable/phase-de-integration`
  (`232fdd1` = merge of both tips; NOT a PR branch). Combined gate:
  **1195 passed, scan 234 files, CLI OK, diff clean**; merge had zero
  conflicts (file scopes disjoint by construction).
- **Model-routing incident (recorded honestly):** every implementation lane
  this day ran on the session model despite explicit per-lane model opts — a
  host-config issue (subagent model env override + gateway alias remaps),
  fixed owner-approved in host settings afterwards. Verification quality was
  unaffected (all lanes ≥ session-model tier); Codex xhigh cross-reviews of
  both branches were dispatched once the Codex CLI was repaired (broken
  platform binary reinstalled).

### Codex xhigh cross-reviews + fix closure (2026-07-09, post-embargo)

- **Branch A review** (fingerprint `WFA-P1P6-4119237`): FIX_FIRST — 1 blocking
  (empty-universe pin let ST names into the book), 2 major (all-tied rank
  ladder unflagged; `composite_id` digest missing `holding_days`), 1 minor
  (fitted runs wore the hard-coded a-priori banner). All closed at `9d932bd`
  with regressions + a one-time golden-id re-pin (digest input list changed
  by design); the caller-supplied-overlay reuse loophole from the earlier
  targeted verify was hardened in the same commit (fresh-directory refusal).
  Engine additive-only proof, FE payload closure, and release safety were
  confirmed clean by the same review. **Post-fix gate: 1110 passed, scan 226.**
- **Branch B review** (fingerprint `WFB-CP1FIX-aebca45`): FIX_FIRST — 2
  blocking (rejected/errored receipts exited 0; plain-factor dry runs lost
  pinned parameters so decay>1 bypassed the adapter refusal), 3 major
  (evaluation artifacts eclipsed backtest artifacts; new closed codes not
  re-exported; nested adapter ImportError masqueraded as not-installed).
  All closed at `fd677f6` with regressions; adapter advisories (platform-
  error containment tests, truncated-provenance refusal tests) landed in the
  local package (**92 offline tests**). **Post-fix gate: 997 passed, scan 203.**
- **CP-INT (Fable-driven, per docs/full_integration_test_prompt.md):** fresh
  `python:3.12-slim` container from the local integration merge, real Chrome
  via the spec's L2 ladder (Playwright `channel:"chrome"`; L1 Computer-Use
  path was blocked by a host-side classifier outage), real DeepSeek key via
  `--env-file` reference only. Verified live: token gate via the native
  prompt dialog; all 6 IA tabs; two Appendix-C seeds parsed by the real LLM
  into sensible DSL and validated into the registry; multi-factor module
  end-to-end for `equal_weight`, `weighted` (raw 0.6/0.4 echoed), and
  `ic_weighted` — resolving the workorder's founding defect (合成并回测 dead
  button); §8 payload field-audit over the job API (fitted run: genuine fit
  with `fitted_period_fraction=0.875`, `weights_effective` absent, pinned
  member formulas present, single `external_oos_backtest` role, six warning
  codes surfaced, `same_window_diagnostics` evaluation); deep-link reload,
  dark scheme, true-375px zero-overflow; zero console errors across the
  synthesis stages. Post-fix re-verify on the rebuilt image confirmed the
  fitted banner branch (拟合权重（时变）) live. CLI degradation ladder
  verified in-container (`not_enabled` → `not_installed`, dry-run default)
  and the full-capability adapter path verified host-side (install + enable
  gate + translate boundary + cn_a REGION_MISMATCH prescreen; no live
  platform call anywhere).
- **Integration line:** `fable/phase-de-integration` (local-only) carries
  both fix batches; combined image `qf-de:r2`.
## Phase F — CP-INT bug-fix batch (BUG_LIST #001–#005) — 2026-07-10/11

Branch `fable/phase-f-cpint-bugfixes` (base `06a4bf2`). Source: the owner-
requested post-merge deep integration test registered five defects in
`docs/coordination/BUG_LIST.md` (the canonical bug registry, first tracked
in this phase). Process per the standing owner rules: Fable adjudicated
every fix design before dispatch; implementation lanes (sonnet/opus) built
to binding specs; gpt-5.6-terra served as the strict reviewer across four
adversarial rounds.

- **#001 (MAJOR, OOM):** single-build seam — `build_directed_matrix`, one
  shared per-period rank-IC sweep (`compute_period_ic_sweep` →
  `PeriodICSweep`), redundancy from the same sweep; member frames released
  early with explicit `gc.collect()` (web jobs disable gc); wide-matrix
  working set dropped before the engine drive. Numerics bit-identical
  (identity + single-sweep regressions; goldens unchanged).
  `docs/architecture.md` records memory behavior + container sizing.
- **Reuse-seam integrity (terra rounds 2–4):** the sweep is provenance-
  bound (dates/grid/params/columns/row-count/edge keys), content-bound
  (whole-frame hashes of the sorted matrix and validated close), payload-
  self-certifying (`ics_content_hash` + proxy-frozen payload), and consumed
  via validated private snapshots on BOTH the weight-driving and advisory
  surfaces (check-then-use closed). Final verdict T2-FINAL3: PASS.
- **#003 (MINOR, dangling composites):** tri-state
  `precomputed_values_present` on registry rows (FP-4 null-not-guessed,
  probe through the scoring path's own store roots, I/O failure propagates
  — False only on readable confirmed absence); early `_prepare` refusal +
  distinct fetch-loop backstop; picker disables 值不可用 rows; registry
  badges. Value retention/GC stays a recorded open decision.
- **#005 (NIT):** `_concat_score_frames` empty-frame exclusion (pandas
  remediation class; original warning not locally reproducible — recorded
  as such in the register).
- **#002/#004 (local adapter, never ships):** recursive derive expansion
  (parenthesized, cycle-guarded, depth-capped, per-occurrence
  `applied_derives` audit trail) + presence-branched mapping resolution
  (blank env var errors honestly; repo-layout fallback; README section).
  121 offline tests.
- **Commits:** `fe87642` → `332d0c7` (T-1..T-4) → `6fdfa9b` (content
  hashes) → `0d77bea` (payload self-integrity) → `0a9d8ac` (snapshot
  consumption). Gates at tip: 1239 passed / release scan 237 files / CLI
  OK / leak sweeps clean. In-container (fresh image off the tip archive):
  1235 passed, 4 skipped.
- **CP-INT (this phase):** fresh `qf-f:r1` image from `git archive` of the
  tip; demo-workspace new-user mode (external cn_a volume not mounted this
  run — recorded); real desktop Chrome via the Claude-in-Chrome extension
  (real-Chrome control; embedded-browser shortcut NOT used); §3 preflights
  all green incl. a real DeepSeek `llm-smoke` parse. Results recorded in
  the phase-F CP-INT report block below when the walk completes.

### Phase F closure — #006/#007 + failure-path hardening (2026-07-11 → 07-14)

- **#006 (rd/research_loop, `fe23744`):** the demo-workspace RD wholesale
  failure's true root cause was the SEED self-assessment lacking exception
  handling when a required metric is unavailable on short histories
  (candidate scoring was already weight-gated). Fix: typed
  `metric_unavailable:<name>` reasons, seed failure → `seed_assessment=None`
  + `seed_unscorable` trace + None-safe consumers; zero-weight components
  never fetch; runs complete with the existing honest outcome fields.
- **#007 (web/lineage, `fe23744`):** pure-Web runs never wrote RunIndex.
  Fix: the workbench recording block extracted byte-identically into
  `lineage/recording.record_run` (CLI path unchanged); web validate records
  1 evaluate + 2 backtests, staggered 1 backtest, multi-factor 1 under the
  COMPOSITE_ id inside the cleanup envelope.
- **CP-INT (fresh-container, real Chrome):** all core acceptance passed on
  the rebuilt image, incl. the #003 end-to-end refusal path and honest n/a
  annualization; both fixes live-verified (evidence chain 0→3 rows; the
  previously-fatal balanced RD run completes with structured honest
  outcomes).
- **Review (sol-high, replacing the discontinued terra lane):** round
  `qf-phasef-solR5-20260714` FAIL with PF-F1..F4 → `bfce31f` (narrowed
  seed-recovery catch so non-metric exceptions fail loudly again; report
  renders n/a instead of synthetic zeros; composite recording moved after
  payload construction; late-cancel no longer relabels completed runs) →
  re-verify `qf-phasef-reverify-20260714b` (F1–F3 CLOSED, one residual
  window) → `e2aa2f4` uniform last-look checkpoint before every recording
  site + a regression reproducing the reviewer's probe. Adjudicated CLOSED.
- **Batch state at closure:** all seven BUG_LIST rows ✅ DONE (register
  synced into this branch); merged with main (`3b107c5`, docs-only); gates
  1252 passed / CLI / diff-check / release scan 241 files. Known follow-up
  (recorded, not in this batch): fe23744 adds net +203 lines to
  apps/web/api.py, predating the D12 freeze — the D12 compliance chore
  extracts them into a module right after this branch merges.
- **Continuity note:** the repo migrated Desktop→Dropbox mid-phase
  (2026-07-12, `mv` with .git intact); this closure resumed from the
  migrated branch after forensic verification that nothing was lost.
- **Dark/375px residual (closed 2026-07-14):** L2 ladder per the integration
  prompt — Playwright `channel:"chrome"` (real Chrome), colorScheme dark +
  true 375×812 viewport (innerWidth==375 asserted): all six tabs render the
  dark surface token with zero horizontal overflow.

## 2026-07-13 — Workflow B / SE-P1: ResearchOutcome v2 contract (CLOSED)

Branch `fable/self-evolution-design` @ `4a42972` (chain: design `c1a20c2` →
CP0 rulings `6bbef79`/`e12b894` → contract `9cb0967` → battery `d01073d` →
review rework `3fb734d` → extended battery `80f7ab4` → coherence matrix
`4a42972`). Deliverable: `research_loop/outcomes.py` (neutral four-axis
contract, logical evidence-run identity, derived strength, structural
de-identification, sample_role + to_record envelope, sig.v2.1) +
`memory.promote` evidence-unit cap + 167-test battery incl. golden frozen
vectors.

Review trail: S1 sol-xhigh design round (21 findings, §5/§9 REJECT → v2) →
implementation → sol-high REWORK (R-F1..R-F9) ∥ opus verify REWORK-scoped
(F1..F5, probe-executed) → rework batch → sol-high re-verify
(13/14 CLOSED, 1 HIGH residual: submit verdict×lifecycle contradictions) →
coherence-matrix fix + exhaustive 4×4 regression. All 14 findings closed;
model pins verified per dispatch (sonnet lanes ×2, sol ×3, opus ×1,
gpt-5.5 ×1 on the FE side).

Gates at close: 1370 passed / CLI OK / diff-check clean / release scan 241
files. Next SE phase (P2 ingress sink) is gated on the phase-f branch merge
per SE-x.

## 2026-07-13 — Workflow A / FE-P0: mode shell (CLOSED)

Branch `fable/fe-p0-mode-shell` @ `3fe4307` (chain `774023f` → rework
`b47837e` → residuals `3fe4307`, forked from main `3d98230`). Deliverable:
simple/expert mode shell per WORKORDER §5 P0 — persistent `#mode-header`
toggle (hoisted after the FE0 trap finding), idea box + seeds + runtime
strip, 11-param grid folded into 高级 details, term tooltips
(column-aware positioning after the 375px left-clip finding), deep-link >
saved-preference > default-simple precedence with non-persistent
simple-run handoff, sticky-element offset audit (3 pinned), CN-first
strings, 44px targets. Tests: mode-shell suite incl. a stdlib Node
DOM-stub harness importing the REAL app.js (vacuity self-checked) + an
html.parser containment check.

Review trail: gpt-5.5 high round 1 REWORK (FE0 BLOCKING toggle trap +
FE1 preference overwrite + FE2/FE3/FE4/FE5) → rework `b47837e` →
gpt-5.5 re-verify (5/6 closed; FE3 left-edge residual + sticky collision)
→ `3fe4307` with two-edge numeric evidence for all five tooltips and
four-scroll-position rail/tab measurements. Adjudicated CLOSED by the
steward (both residual fixes implement the reviewer's own proposals,
machine-verified). Lane model verified claude-sonnet-5 per dispatch.

Gates at close: 1228 passed / CLI OK / diff clean / release scan 237
files (all in the FE worktree). FE-P1 is gated on the phase-f merge +
D12 chore per SE-x.

## 2026-07-14 — Workflow B / SE-P3: external-plugin producer (CLOSED, local-only)

Deliverable lives entirely under the gitignored plugin tree (dual-domain,
SE-i): result→ResearchOutcome v2 mappers with stage derived from the call
site (SE-ix), closed reason-code mapping with case-normalized negation-aware
classification of free-text rejection detail, provider composites excluded
from metric snapshots, sample_role stamped from platform semantics
(in_sample) with kernel-vocabulary mapping for local reports; plugin-rooted
store instance with forced child-suffix + hostile-root rejection (main-store
isolation), transactional ingest (observations first, ledger completion
marker last) with restart-idempotence and trailing-corruption quarantine;
owner-ruled submission margins config (inclusive semantics, boundary-pinned).
Historical mission ledger backfill: 6/10 entries converted, 4 honestly
skipped; converted statements verified template-only.

Review trail: gpt-5.5 high (fingerprint qf-se-p3-review-20260714) →
ACCEPT-WITH-FIXES, 4 findings (1 High transactional order, 2 Medium root
isolation + classifier, 1 Low corruption tolerance) → all fixed with
regressions. Offline suite at close: 209 passed (121 pre-existing + 88
SE-P3). Zero tracked-file changes throughout (proven per round). No public
CI coverage by design; the public synthetic-producer fixture (SE-P1 battery)
covers the kernel seam.

## 2026-07-14 — Workflow B / SE-P4a: rule-activation governance (CLOSED)

Trunk chain `b72f9b1` (initial: activations.jsonl review events, store-wide
advisory lock, active_rules bounded context channel, llm closed-template
authentication, CLI qf memory rules) → `9cd3145` (14-item dual-review
rework: pre-activation silencing of rule-tier signatures from passive
feeds; event row-binding; append-order recency + supersedes
auto-population; scope grammar + statement/scope equality;
auth-before-cap with full-set dedup; verdict/reason coherence parser;
locked readers + trailing quarantine; atomic resolve_validate_append +
unretire; Dropbox conflicted-copy detection; repair-prompt retention;
global-slot reservation; public advisory_file_lock) → `d451548` (R2:
single-lock effective_active_rules/rule_review_snapshot kills the
split-snapshot TOCTOU; event-id fingerprint readback rejection; strict
schema; activation_seq ranking; stage/strength coherence; uniform tail
tolerance; traceability fields; four-state CLI + real two-process race)
→ `955d98b` (R3: append-safe torn-tail repair under the lock with
heal-in-place for newline-only tears; honest dangling-vs-lapsed labels).

Review trail (all model pins verified): sol-high REWORK 12 ∥ opus verify
probe-executed 5 → rv2 8/14 closed → rv3 all prior closed + 2 residuals
→ R3 → adjudicated CLOSED (reviewer probe sequences are regressions).
Threat-model ruling recorded: in-process context forgery is out of scope
(equivalent trust to patching promote()); artifact-root write access is
the trusted boundary; read-side validation defends files. Drop-stats
surfacing deferred to SE-P5 planning_influences by design.

Gates at close: 1494 passed / CLI OK / diff clean / release scan 245
files. Governance battery: 111 review-event + memory tests.
