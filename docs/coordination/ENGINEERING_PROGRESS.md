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

## CP6 — Interactive platform frontend (D6/D8) — 🔶 CP6-1/2/3 DONE
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
- CP6-4 ⬜ TODO (Docs view + declarative-only Extensions registry per
  D7/D7a — proceeding, not cut; schedule permits). Commercial boundary
  (agent orchestration depth + portfolio rebalancing) stays out.
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

## CP8 — Integration acceptance + merge prep — ⬜ TODO
- Full-chain integration test (Docker/web/RD per docs/integration_workflow.md
  if environment allows); final cross-review; PR(s) for Phase B + Phase C
  branches after owner approval; WORKING_STATE migration into
  docs/coordination/.
