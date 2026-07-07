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

## CP2 — Wave 2: memory / goals / falsification surfaces — 🔄 IN FLIGHT
- Scope: research memory (append-only rules/findings/failures + deterministic
  promotion, rules never auto-activate); research-goal artifacts (completion
  gated on per-criterion audit rows citing on-disk evidence); workbench
  falsification surface (frame parity with evaluate_factor, artifact +
  run-index + lineage).
- Runner: Workflow `wf_4ddf4796-5f8` (3 lanes → gate → Opus review).
- Remaining steps at this checkpoint: workflow returns → Fable adjudicates
  review → fixes → per-lane atomic commits → gate re-run → mark DONE here.
- Verify on resume: `git status --porcelain` in the worktree (uncommitted
  lane files = workflow output not yet adjudicated); full suite tail.

## CP3 — Cross-review adjudication (Codex wave-1 + Opus wave-2) — 🔄 IN FLIGHT
- Scope: Codex xhigh review of committed wave-1 diff (`562a52b..87dbff9`)
  is running as external Codex task `task-mrafznd5-ls7oz2` (state file under
  ~/.claude/plugins/data/codex-inline/state/.../jobs/). Fable adjudicates
  findings from BOTH reviews, fixes accepted items, commits, updates
  `WAVE1_REVIEW_RESOLUTION.md` (or a WAVE2 twin).
- Verify on resume: read the Codex job JSON status; if done, its .log tail
  holds the findings; check resolution doc for an adjudication section.

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
