# Design — Self-Evolution Engine (Quant Forge main body)

> **What this is.** A design + work order for the Quant Forge *self-evolution
> engine*: the capability that turns the outcomes of past factor research into
> durable, human-governed priors that improve future research. It is written to
> be picked up by **another agent for review and development**.
>
> **The one-line thesis (read before anything else).** The engine is **~75%
> already built** in `src/quant_forge/research_loop/` (durable cross-run memory,
> deterministic promotion, human governance, de-identification, feed-forward into
> hypothesis generation). This work **completes and opens the existing loop**; it
> **must not** stand up a parallel system (FP-F). The net-new surface is three
> small seams, defined in §4.
>
> **Authority.** This document is a **design input/proposal**, below `AGENTS.md`
> in instruction priority (`docs/agent_entrypoint.md` §Authority). Its open
> decisions (§8, `SE-i..SE-viii`) are **not yet rulings** — the executor
> adjudicates them into `docs/coordination/DECISIONS.md` at CP0 before coding,
> and that register (not this file) becomes authoritative. If this doc and
> `AGENTS.md`/DECISIONS ever disagree, they win.
>
> **CP0 STATUS (2026-07-13): ADJUDICATED.** `SE-i..SE-viii` are now ruled in
> `docs/coordination/DECISIONS.md` §"2026-07-13 — Self-evolution engine CP0"
> (plus new `SE-ix`/`SE-x`), incorporating two `gpt-5.6-sol` adversarial
> rounds (S1: 21 findings, §5+§9 REJECT → v2 contract; S2: 15 cross-doc
> findings) and owner rulings R1–R6. THAT REGISTER OVERRIDES §4/§5/§8/§9 of
> this file wherever they differ — notably: dual-domain stores (external
> outcomes never enter the main store; no `RUN_KINDS` extension),
> `ResearchOutcome` v2 (kernel `outcome_id` + `observed_at`, four-axis codes,
> `evidence_strength`, typed window, logical evidence-run dedup, 10-field
> signature), review-surface governance (Web tab; rules must pass review),
> single steering point (planner stays blind), and the corrected phase order.
> A full v2 revision of this document is queued with SE-P1.

---

## 0. How to use this document (receiving agent)

1. Follow the **required read order** in `docs/agent_entrypoint.md` §Required
   Read Order first (`AGENTS.md` → `agent_entrypoint.md` → `architecture.md` →
   `integration_workflow.md` → `full_integration_test_prompt.md` →
   `WORKING_STATE.md`). This doc is read **after** that base sequence and does
   not override it.
2. State a **read receipt** (branch+commit, base docs read, task goal, files to
   modify, verification gates, conflicts) before editing anything.
3. Treat §3 (current-state map) as ground truth to **verify, then reuse** — do
   not rebuild what it lists as EXISTS. Re-run the greps yourself; if any
   `file:line` here has drifted, correct it in your read receipt.
4. Produce the **CP0 output first**: adjudicate `SE-i..SE-viii` (§8) into a new
   dated `DECISIONS.md` section, plus branch/model-routing/phasing — *then*
   implement. Do not start P1 before CP0 is recorded and owner-approved.
5. `docs/design/` is **blanket-gitignored**, so NEW files there need
   `git add -f` (CP0 amendment 7, 2026-07-09). This file itself is ALREADY
   TRACKED (committed at `c1a20c2`); ordinary edits need no force-add.

---

## 1. Required reads (this task, after the base sequence)

**Contract & coordination:** `AGENTS.md`; `docs/agent_entrypoint.md`;
`docs/coordination/DECISIONS.md` (esp. the 2026-07-09 CP0 set `D-i..D-ix` +
amendments, and standing `D6/D7/D7a/D8`); `docs/coordination/ENGINEERING_PROGRESS.md`;
`docs/WORKING_STATE.md`.

**Engine (reuse targets — the whole point):**
`src/quant_forge/research_loop/{service.py,memory.py,contracts.py,context_builder.py,
feedback_builder.py,strategy_selector.py,experiment_planner.py,candidate_gate.py,
llm.py,trace_store.py,scheduler.py,goals.py}`;
`src/quant_forge/lineage/store.py`; `src/quant_forge/evaluation/falsification.py`.

**External-backend seam (the outcome producer side):**
`src/quant_forge/integrations/{contracts.py,registry.py,gate.py,dry_run.py}`.

**Local-only (gitignored; inputs, never distribution):**
`worldquant/adapter/` (the BRAIN adapter — one *producer* of external outcomes),
`worldquant/reference/brain_api_and_submission.md`.

---

## 2. First principles (derive the design from these; do not copy a spec)

These reuse the project's FP axioms (WORKORDER §2 / DECISIONS) and add the
self-evolution corollaries. Every design choice must trace to one.

- **FP-F one-truth (the governing axiom here).** Reuse the existing research
  memory, lineage, gate, and contracts. Adding a *parallel* learning store,
  promotion policy, or run loop is out of scope and will be rejected. The
  net-new code is a thin adapter + one contract + one review path, not an engine.
- **FP-A provider-neutral core.** The engine learns from **provider-neutral
  outcomes** only. It imports no provider, credential, network, or hosted-service
  module (`AGENTS.md` §Boundaries). A default install with no backend still has a
  fully functional self-evolution loop over local outcomes.
- **De-identification boundary (reuse, don't reinvent).** Anything that becomes a
  memory row passes `quant_forge.lineage.store.redact_free_text`; evidence refs
  are `run_id`s or `artifact_root`-relative paths, never absolute
  (`memory.py:32`, `:324-336`). External (BRAIN) outcomes cross into memory only
  after this redaction — no alpha ids, no PnL series, no credentials.
- **Deterministic promotion; no LLM decides.** Promotion stays the PURE function
  `memory.promote` (`memory.py:121`). LLM prose never mints or activates a
  knowledge row.
- **Human governance (FP-2).** Rules never auto-activate: a promoted rule is
  always `needs_human_review` (`memory.py:101-104`). Activation is an explicit
  human step. Gates never auto-promote a factor to active
  (`contracts.py:164`; `GateDecision`).
- **Honest degradation (FP-4).** Null ≠ 0; missing evidence is a labeled unknown,
  not a zero. Missing OOS evidence **blocks by default** in the candidate gate
  (`candidate_gate.py:53`, `INSUFFICIENT_OOS_EVIDENCE`). Coverage/priors that
  cannot be computed report `n/a`, never a fabricated rate.
- **No lookahead / no ex-post selection (FP-G/FP-6).** A lesson must not be
  learned from in-sample-only evidence or from any selection that touched the OOS
  window (`strategy_selector.py:16-19`; `integrations/gate.py:28-34`;
  `falsification` is advisory and sample-floored, `falsification.py:4-5`).
- **Open engine / private fuel.** The engine *mechanism* is committed public
  code. The *learned memory* lives under `artifact_root/research_memory/` — an
  output tree, already outside tracked source — so accumulated research edge is
  not published by default. External-derived lessons stay local (D-v).

---

## 3. Current-state map — EXISTS vs MISSING (verify, then reuse)

Verified by a read-only sweep (2026-07-13). Re-verify before relying on any line.

### EXISTS (reuse; do not rebuild)

| Capability | Where | Notes |
| --- | --- | --- |
| Loop driver | `research_loop/service.py:559` `ResearchLoopService.run_once` | hypothesis→plan→evaluate→gate→feedback→trace→memory |
| Durable cross-run memory | `memory.py:181` `ResearchMemoryStore` | append-only JSONL under `artifact_root/research_memory/`: `rules/findings/failures/observations.jsonl` |
| Deterministic promotion | `memory.py:121` `promote()` | ≥2 obs/≥2 `run_id`→finding(/failure); ≥3 obs/≥2 windows/≥2 runs→rule **candidate**; PURE, no LLM |
| Human governance | `memory.py:101-104` | rules always `needs_human_review`, never auto-active |
| De-identification | `memory.py:32,324-336` | `redact_free_text` + relative-refs-only (reuses `lineage.store`) |
| **Feed-forward (local)** | `context_builder.py:89` `_memory_items` → `ResearchContext.recent_successes/failures` → `llm.py:225-226` | findings/failures **already** steer next hypotheses, with template re-authentication (`llm.py:330-364`) |
| Cross-run trace priors | `trace_store.py:87`; `feedback_builder.py:21-32` | all-runs recent successes/failures + fixed-vocabulary hints |
| Honesty gate | `candidate_gate.py:253` `evaluate_candidate` | OOS return/decay/retention/corr/turnover; missing-evidence-blocks-by-default (`:53`) |
| Falsification (advisory) | `evaluation/falsification.py:78` `run_falsification` | placebo/half-life/block-consistency; status-carrying `MetricValue`, below-floor→`None` |
| Lineage/run provenance | `lineage/store.py:217` `LineageStore`, `:287` `RunIndex` | `search(factor_id=, kind=)`; shares `canonical_fingerprint`/`redact_free_text` with memory |
| External backend seam | `integrations/contracts.py:359` `FactorBackendPort` | 4 capabilities `{translate,prescreen,simulate,submit}`, closed `WARNING_CODES` (`:58`) |
| Local submission gate | `integrations/gate.py:164` `evaluate_submission_gate(spec, report)` | pure math, parameterized by `SubmissionGateSpec` (`:90`); FP-G ex-post ban on its surface (`:28-34`) |

### MISSING (the net-new surface — this task)

1. **External outcomes never re-enter the loop.** Zero import coupling between
   `integrations/` and `research_loop/` (confirmed both directions). No path from
   `PrescreenReport`/`SimulationResult`/`SubmitReceipt` into `record_observation`.
   `lineage.store.RUN_KINDS` (`:36`) has **no** external/prescreen/submit kind.
2. **Rule-tier lessons are dead-ended.** `rules.jsonl` candidates are minted but
   `context_builder` reads only `finding`/`failure` (`context_builder.py:66-67`).
   There is no review→activate→consume path, so an approved rule cannot influence
   planning or prompting.
3. **Planning is not lesson-aware.** `ExperimentPlanner.plan`
   (`experiment_planner.py:158-166`) uses `ResearchContext` only for
   field/operator resolution, not memory; and there is **no quantitative priors
   view** (success-rate by family/settings) over `findings.jsonl`.

*(Deferred, not in the core scope: cross-seed priors into `select_strategy`
(today same-seed-chain scoped, `service.py:612-617`); wiring `ResearchGoalStore`
(CLI-only) into the loop. See `SE-vi`.)*

---

## 4. Target design — three seams + the plugin split

```
   LOCAL outcomes                                         EXTERNAL outcomes (BRAIN, plugin)
   FactorExperimentResult / GateDecision                 PrescreenReport / SimulationResult / SubmitReceipt
            │                                                        │
            │ (already wired: service._record_memory_observations)   │  SEAM 1 (new): outcome→observation adapter
            ▼                                                        ▼  (neutral, de-identified via redact_free_text)
   ┌──────────────────────────  ResearchMemoryStore  (memory.py, UNCHANGED)  ─────────────────────────┐
   │  observations.jsonl ──promote() [PURE]──►  findings.jsonl   failures.jsonl   rules.jsonl          │
   └───────────────┬───────────────────────────────────────────────────────┬───────────────────────────┘
                   │ read_recent(finding|failure)  (already wired)          │ SEAM 2 (new): rule review→activate→consume
                   ▼                                                        ▼
        ResearchContext.recent_successes/failures ──► LLM prompt      activated rules ──► context + SEAM 3
                   │                                                        │
                   └───────────────► SEAM 3 (new): lesson-aware planning + quantitative priors view ◄──┘
```

- **Seam 1 — external-outcome ingress (neutral).** A provider-neutral
  `ResearchOutcome` record (§5) and an adapter that maps the existing external
  typed results into one or more `MemoryObservation`s with **closed-set
  signatures**, redacted, then `record_observation` + `promote_pending`. The
  **engine defines the neutral contract**; the **BRAIN plugin is one producer**
  of `ResearchOutcome`s (P3). The engine never imports the plugin.
- **Seam 2 — rule activation loop.** A review surface (CLI) that lists
  `needs_human_review` rules, lets a human **activate** one (recording an
  activation row, still append-only), and a consume path so **activated** rules
  join `finding`/`failure` in `context_builder` feed-forward. Preserves FP-2:
  activation is human, never automatic.
- **Seam 3 — lesson-aware planning + priors view.** (a) A pure, deterministic
  **quantitative priors view** over `findings.jsonl` (e.g., pass-rate by factor
  family / by settings bucket) with FP-4 `n/a` for thin cells; (b) make
  `ExperimentPlanner` consult activated rules + priors as **soft constraints /
  ordering hints**, never as hard overrides, and never sourced from OOS-touching
  evidence.

**Plugin split (answers the owner's placement question, consistent with D-iii).**
The main-body engine stays provider-neutral. The BRAIN plugin's job is only to
(a) **help a factor pass the WorldQuant gate** and (b) **emit a de-identified
`ResearchOutcome`** back to the engine. The submission thresholds
**Sharpe > 1.28, Fitness > 1.05, Turnover < 60%** are a **`SubmissionGateSpec`
instance in `worldquant/adapter/`** (margined above BRAIN's hard gate
1.25/1.0/70%) — exactly the D-iii pattern (pure gate math in kernel; provider
threshold instances in the adapter). No engine change encodes these numbers.

---

## 5. Contracts (new + extended)

Keep the project's discipline: frozen dataclasses, closed-set vocabularies,
labels-not-scores, `to_dict`/schema-versioned, `redact_free_text` before disk.

### 5.1 `ResearchOutcome` (new, neutral) — proposed home `research_loop/outcomes.py`
A provider-neutral record any producer (local evaluator or an external backend
adapter) emits, which the Seam-1 adapter turns into `MemoryObservation`(s).

- `factor_id: str` — links to lineage/`RunIndex.search(factor_id=)`.
- `run_id: str` — the producing run (satisfies the ≥2-run promotion rule across runs).
- `source: Literal[...]` — closed set, e.g. `local_gate`, `external_prescreen`,
  `external_simulate`, `external_submit`. (Adjudicate exact set in `SE-ii`.)
- `outcome_code: str` — **closed set** (e.g. `GATE_PASSED`, `GATE_BLOCKED`,
  `TURNOVER_TOO_HIGH`, `SHARPE_BELOW_GATE`, `FITNESS_BELOW_GATE`,
  `SELF_CORRELATION_HIGH`, `REGION_MISMATCH`, `SUBMIT_ACTIVE`,
  `SUBMIT_NOT_CONFIRMED`). No free-form reasons.
- `metric_snapshot: Mapping[str, float | None]` — neutral metrics only
  (`sharpe`, `returns`, `turnover`, `drawdown`, `max_weight`, `sub_universe_sharpe`);
  **`None`, never 0, for absent** (FP-4). Note: `fitness` is a BRAIN composite —
  keep it out of the neutral snapshot; the plugin owns it.
- `data_window: str` — for cross-window promotion; empty = unknown (never counts).
- `evidence_ref: str` — a `run_id` or `artifact_root`-relative path only.
- `scope: str = "global"`; `schema_version: str`.

### 5.2 Outcome → observation mapping (Seam-1 adapter, pure)
`outcome_to_observations(outcome) -> tuple[MemoryObservation, ...]`, pure and
deterministic: builds a **signature** from `(source, outcome_code, family?)` and
a **statement** from a closed template vocabulary (mirroring
`feedback_builder.NEXT_HYPOTHESIS_HINT_TEMPLATES` discipline), redacts, and
returns observations. It never writes disk itself — the caller uses the existing
`record_observation`/`promote_pending`. **No change to `memory.promote`.**

### 5.3 `RUN_KINDS` extension (lineage)
Add external kind(s) to `lineage/store.RUN_KINDS` (`:36`) so an external outcome
can be recorded as a `RunIndex` row keyed by `factor_id` (`SE-ii` fixes the
names). This is a reviewed closed-set extension, not an open string.

### 5.4 Rule activation record (Seam-2)
An append-only activation row (new `activations.jsonl`, or a superseding row in
`rules.jsonl` with status `active` **only** via the human path) — adjudicate
shape in `SE-iii`. Consumption: extend `context_builder` to also read activated
rules. Must preserve: promotion still cannot mint an `active` rule; only the
human activation path can.

---

## 6. Reuse map (need → existing seam)

| Need | Reuse (do NOT rebuild) |
| --- | --- |
| Durable store, append-only, superseding | `ResearchMemoryStore` (`memory.py`) |
| Promotion policy | `memory.promote` — unchanged |
| De-identification | `lineage.store.redact_free_text` + relative-ref check |
| Stable content hashing / ids | `lineage.store.canonical_fingerprint` |
| Run/factor provenance keys | `lineage.store.RunIndex.search(factor_id=, kind=)` |
| Feed-forward into prompts | `context_builder._memory_items` + `llm.py:225-226` |
| Honesty gate the engine respects | `candidate_gate.evaluate_candidate`; `falsification` (advisory) |
| External result types (producer side) | `integrations/contracts.py` result dataclasses |
| Local gate math + spec pattern | `integrations/gate.py` `evaluate_submission_gate` / `SubmissionGateSpec` |

---

## 7. Invariants the implementation must preserve (checklist)

- [ ] Promotion stays PURE and deterministic; no LLM prose mints/activates a row.
- [ ] Rules never auto-activate; activation is an explicit human step (FP-2).
- [ ] Every memory-bound string passes `redact_free_text`; every evidence ref is a
      `run_id` or relative path (no absolute paths, no `..`, no drive letters).
- [ ] External outcomes carry **no** alpha ids, PnL series, or credentials into
      memory — only neutral metric snapshots + closed codes.
- [ ] `None` ≠ 0 for absent metrics/coverage/priors (FP-4); thin priors cells = `n/a`.
- [ ] No lesson learned from IS-only or OOS-touching selection (FP-G/FP-6).
- [ ] Missing OOS evidence blocks by default (`candidate_gate`).
- [ ] `research_loop/` imports no provider/credential/network module; Seam-1 stays
      a neutral contract, with the BRAIN producer in `worldquant/adapter/`.
- [ ] Engine mechanism committed; learned memory stays under `artifact_root/`.
- [ ] BRAIN gate numbers (1.28/1.05/60%) live only in the adapter's
      `SubmissionGateSpec` instance, never in the kernel.

---

## 8. Decisions to adjudicate at CP0 (`SE-i..SE-viii` → DECISIONS.md)

The executor rules on these (record as a new dated CP0 section; owner-reviewable
items marked). Recommendations given, but the executor owns the ruling.

- **SE-i — Seam-1 layer.** Neutral `ResearchOutcome` contract + `outcome_to_observations`
  adapter live in `research_loop/` (public kernel); the BRAIN *producer* lives in
  `worldquant/adapter/`. *Recommend: yes* (mirrors D-i/D-iii; keeps kernel neutral).
- **SE-ii — closed sets.** Exact `ResearchOutcome.source`/`outcome_code` sets and
  the `RUN_KINDS` external additions. *Recommend: minimal closed set above; extend
  by reviewed PR only.*
- **SE-iii — rule activation mechanism.** CLI (`qf memory rules {review,activate}`)
  vs config; activation record shape (`activations.jsonl` vs superseding row);
  how activated rules are consumed by `context_builder`. **owner-reviewable.**
- **SE-iv — lesson-aware planning.** Whether `ExperimentPlanner` consumes
  rules/priors now, and as soft-hints vs constraints. *Recommend: soft ordering
  hints only, behind a flag, to avoid overfitting the search.*
- **SE-v — quantitative priors view.** Computed read-only view over `findings.jsonl`
  vs a new persisted record kind; FP-4 null handling. *Recommend: computed view,
  no new persisted kind.*
- **SE-vi — cross-seed priors + goals wiring.** In scope now or deferred?
  *Recommend: defer to a later phase; keep this task to the three seams.*
- **SE-vii — open-source boundary.** Confirm engine is committed, learned memory
  is gitignored under `artifact_root/`, and no BRAIN-derived prose enters
  committed lessons (D-v). **owner-reviewable.**
- **SE-viii — plugin gate margins.** Where the 1.28/1.05/60% `SubmissionGateSpec`
  instance lives and how the owner tunes it (config in `worldquant/adapter/`).
  **owner-reviewable.**

---

## 9. Phase plan (checkpointed; file-scope disjoint; update ENGINEERING_PROGRESS per phase)

**CP0 (executor, in person):** adjudicate `SE-i..SE-viii`; branch (suggest
`phase-self-evolution` off `main`); model routing (assign by difficulty —
hard/contract lanes to Opus/qf-opus-reviewer, mechanical lanes to Sonnet/qf-sonnet-dev,
never a single lane); one adversarial review pass on this design. Owner approval before P1.

| Phase | Scope (file ownership) | Deliverable |
| --- | --- | --- |
| **P1** | `research_loop/outcomes.py` (new), tests | Neutral `ResearchOutcome` + pure `outcome_to_observations`; closed-set validation; redaction; **no network, no provider import**. Unit tests incl. redaction + FP-4 null. |
| **P2** | `lineage/store.py` (additive: `RUN_KINDS`), `research_loop/service.py` (additive ingress hook), tests | Record external outcomes as `RunIndex` rows; wire `outcome_to_observations`→`record_observation`→`promote_pending` for outcomes the engine already holds locally. Cross-run promotion test. |
| **P3** | `worldquant/adapter/` (local-only), tests | BRAIN **producer**: map `PrescreenReport`/`SimulationResult`/`SubmitReceipt` → `ResearchOutcome` (de-identified); the 1.28/1.05/60% `SubmissionGateSpec` instance. Fixture tests; never distributed. |
| **P4** | `research_loop/{memory.py additive read, context_builder.py, cli}` , tests | Seam-2: rule review→activate→consume; activated rules join feed-forward. FP-2 preserved (no auto-activate). |
| **P5** | `research_loop/{priors.py new, experiment_planner.py}` , tests | Seam-3: pure quantitative priors view (FP-4 `n/a`); optional lesson-aware planning behind a flag (`SE-iv`). |
| **P6 (deferred)** | `strategy_selector.py`, `goals.py` | Cross-seed priors + goals wiring — only if `SE-vi` opts in. |
| **Review** | — | Opus adversarial + Codex xhigh (fresh thread) on P1–P5; full gate. |

---

## 10. Verification gates

**Baseline (every phase, `AGENTS.md` §Verification):**
```bash
python3 -m pytest
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
git diff --check
python3 scripts/release_safety_scan.py   # if present in this branch
```
**Task-specific:**
- **Neutrality boundary test:** assert `research_loop/` imports nothing from
  `integrations/` or any provider/credential module (grep/AST test), and that
  `outcomes.py` does no network I/O.
- **Determinism:** `outcome_to_observations` and the priors view are pure —
  identical inputs → byte-identical outputs.
- **De-identification:** property test that any `ResearchOutcome` (incl. one
  carrying absolute paths / alpha-id-shaped strings) is redacted / rejected
  before disk; no absolute path or `..` ever reaches a JSONL row.
- **FP-4:** absent metrics serialize as `null`, never `0`; thin priors cells = `n/a`.
- **FP-2:** promotion cannot emit an `active` rule; only the human activation path
  can; regression test.
- **FP-G/FP-6:** ex-post-selection ban regression (reuse the `integrations/gate.py`
  pattern) — no lesson from IS-only/OOS-touching evidence.
- **Plugin producer:** fixture test that a sample BRAIN `PrescreenReport` maps to
  the expected neutral `ResearchOutcome` with **no** alpha id / PnL leakage.
- **Release/leak sweep** at review: no BRAIN prose, no credentials, no private
  paths in tracked files (D-v).

---

## 11. Hard boundaries (cross → stop and report)

- **No parallel engine (FP-F).** Reuse `ResearchMemoryStore`/`promote`/lineage/gate.
  A second store or promotion policy is a design rejection.
- **Kernel neutrality.** `research_loop/` and `integrations/` public code import no
  provider, credential, network, or hosted-service module. The BRAIN producer is
  local-only under `worldquant/adapter/`.
- **De-identify before disk.** No alpha ids, PnL, tokens, or absolute paths in
  memory rows — `redact_free_text` + relative-refs only.
- **Human governance.** Rules never auto-activate; gates never auto-promote active.
- **Honesty.** Null ≠ 0; missing evidence blocks; no ex-post/lookahead learning.
- **Open engine / private fuel.** Commit mechanism; keep learned memory under
  `artifact_root/`; no BRAIN-derived prose in tracked files (D-v).
- **Git/ownership.** No push/merge/delete without owner approval; claim file
  ownership in `WORKING_STATE.md`; one executor per lane; `docs/design/` files
  need `git add -f`.

---

## 12. Expected outputs

1. `DECISIONS.md` — new dated CP0 section ruling `SE-i..SE-viii` (extends D6/D7/D-i/D-iii).
2. This design doc — revised per CP0 rulings (`git add -f`).
3. P1–P5 code + tests (engine seams neutral & pure; BRAIN producer local-only).
4. Green gates per phase (§10) with fail-on-BASE evidence + atomic commits.
5. `ENGINEERING_PROGRESS.md` / `WORKING_STATE.md` updates; handoff state kept current.

---

## 13. Open risks / for the reviewing agent (adversarial checklist)

- **Signature design (Seam-1).** If `outcome_code`/`signature` are too coarse,
  unrelated failures collapse into one row and promote spuriously; too fine, and
  nothing ever reaches ≥2 observations. Adversarially test both tails.
- **Cross-run counting via external outcomes.** Confirm external outcomes get a
  real `run_id` and `data_window` so promotion's ≥2-run / ≥2-window rules stay
  meaningful and can't be gamed by resubmitting one alpha.
- **Overfitting the search (Seam-3).** Lesson-aware planning must not turn priors
  into a self-reinforcing loop that narrows the search to what already passed —
  verify diversity/dedup still holds and priors are advisory.
- **`fitness` leakage.** Ensure the BRAIN composite `fitness` never enters the
  neutral snapshot or any kernel objective (provider-vocabulary trap).
- **Redaction completeness.** Try to smuggle an absolute path / alpha id / PnL
  through every `ResearchOutcome` string field; all must be caught.
- **Determinism under promotion supersession.** Confirm re-runs don't rewrite
  statements and `entry_id` hashing stays stable.

---

## 14. Traceable change log (fill during review)

| # | Reviewer finding | Severity | Resolution | Commit |
| --- | --- | --- | --- | --- |
| _(P0 — to be filled by the review lane before implementation starts)_ | | | | |
