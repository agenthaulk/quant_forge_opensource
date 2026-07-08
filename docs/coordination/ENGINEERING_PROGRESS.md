# Engineering Progress — Phase C Platform Buildout

**This file is the session-recovery anchor.** Protocol (owner directive
2026-07-06): work is split into checkpointed phases; a phase is marked DONE
only after its gate ran green AND its commits landed. On a session-limit
interruption: read this file first; phases marked DONE need no re-audit
beyond confirming their commits exist (`git log --oneline <range>`); resume
at the FIRST phase not marked DONE by running its Verify command, then its
remaining steps. Do not re-derive completed phases from transcripts.

Branch: `fable/phase-c-research-platform-wave1` (base = Phase B tip
`562a52b`). Gates definition: `PYTHONPATH=src python3 -m pytest -q` +
`python3 scripts/release_safety_scan.py` + CLI `--help` + `git diff --check`.

---

## CP0 — Foundations (Phases A/B) — ✅ DONE
- Phase A quant-core audit: merged approval given, **PR #13** submitted
  (branch `fable/phase-a-quant-core-audit` @ bf19c73, 426 passed).
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

## CP4 — Server decomposition + Web research panels — ⬜ TODO
- Order is binding (B4 F11): extract `apps/web/server.py` into
  routing/api/html/jobs modules with characterization tests FIRST; only then
  add Web "Research History" (run index reader) and "Benchmark" panels.
- Acceptance: no behavior change in extraction step (route parity tests);
  panels render statuses, never bare scalars.

## CP5 — Data plane — ⬜ TODO
- DataCatalogPort backed by the actually-loaded catalog (replaces the static
  7-field dict); operator/factor research metadata tags (OUR schema — see
  memo corrections in WAVE1_REVIEW_RESOLUTION.md); field expansion path;
  ValidationGate wired to real capabilities. (Owner decision D2.)

## CP6 — Interactive platform frontend (D6) — ⬜ TODO
- Open-source Studio-style UI over the QF_OS kernel (research workbench,
  run timeline, goal/criteria view, bench tables). Commercial boundary:
  agent orchestration depth + portfolio rebalancing stay out (capability
  tags mark the line). Will be split into its own sub-phases when opened.

## CP7 — Residual register — ⬜ TODO
- Phase A F-1..F-6 (gate evidence extension to retention/turnover/corr,
  gate-definition unification, `_backtest_metrics` segments, demo fillna,
  rd.yaml knob for missing_oos_evidence_blocks, purge-count persistence);
  `_atomic_write` concurrent-writer safety; prompt-injection review item.

## CP8 — Integration acceptance + merge prep — ⬜ TODO
- Full-chain integration test (Docker/web/RD per docs/integration_workflow.md
  if environment allows); final cross-review; PR(s) for Phase B + Phase C
  branches after owner approval; WORKING_STATE migration into
  docs/coordination/.
