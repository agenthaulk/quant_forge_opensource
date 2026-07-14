# Agent Sidecar Frontend — Design Document (V1, revision R3.1)

**Module:** web workbench sidecar (`apps/web` + `static/`) · **Repo:** `quant_forge_opensource` · **Status:** APPROVED design, pre-implementation · **Decisions:** D10–D12 in `docs/coordination/DECISIONS.md`

> Citations use `(file:NN)` against this repo. Line numbers are anchors, not
> exact after edits. This document is the authoritative spec for the
> P0–P3 build; the execution plan is
> `docs/design/WORKORDER_frontend_sidecar_p0_p3.md`. Do not re-litigate the
> settled contracts it builds on: FP-4/FP-5 (honest metrics, single
> renderers), D6 (open-source/commercial boundary), D7/D7a (declarative
> extensions, no dynamic loading, `agent.workflow` reserved), D8 (no-build
> static ES modules, zero external resources), D9 (design-parity not
> stack-parity).

---

## At a glance

**Goal.** One kernel, three on-ramps. A beginner types one sentence and gets
an evidence-backed report with confirmations only at real decision points; an
expert takes over parameters and formulas at any moment; ambiguity that would
change the hypothesis triggers a bounded clarify interview. The LLM sidecar
narrates, asks, and orchestrates — it never computes, never renders a number,
never crosses an irreversible gate.

**Mechanism.** A user-visible **pipeline card** backed by a **server-owned
pipeline aggregate** drives two independent pipeline kinds:
`factor_study` (A: parse → confirm → compute → report, report is TERMINAL) and
`rd_optimize` (B: RD-confirm → run → leaderboard), with **no automatic bridge**
from A to B. All numbers flow kernel → JSON payload + artifact refs →
canonical renderers (`metric.js`, `views/charts.js`, `views/dsl.js`,
`views/tags.js`). All provenance, attempt lineage, and edit attribution are
**server-derived**; client and agent claims are never trusted.

**Provenance.** Converged over three adversarial rounds: R1 (Codex, default
xhigh) attacked the architecture thesis; an owner deep interview locked four
product decisions; R2 (Codex, `gpt-5.6-sol`) attacked this frontend design
with 16 grounded findings (all adopted; verdicts: every component MODIFY,
none DROP); R3/R3.1 are owner directives (dual pipeline split;
anti-bloat governance; RD interval inheritance). Round summaries: Appendix A.

**Non-negotiable iron laws (FE-L1..L5).**
- **FE-L1 Thin shell, no rewrite.** The sidecar and its cards are a thin
  layer over the existing six-tab workbench; cards embed output from the
  existing canonical renderers. No second canvas, no parallel navigation.
- **FE-L2 Renderers are the only place numbers become pixels.** The sidecar
  never draws a metric, chart, tag, or formula, and chat text is never the
  only carrier of a numeric claim (extension of FP-4/FP-5 into the agent
  era; `static/metric.js`, `docs/frontend_contributing.md`).
- **FE-L3 The server decides truth.** Pipeline state, per-value provenance
  badges, attempt counters, and `edited_by` attribution are derived
  server-side from artifacts and fingerprints. Today the validation API
  echoes client-supplied parser metadata `(apps/web/api.py:1263, :1529)` —
  that path must not be trusted for badges.
- **FE-L4 Checkpoint defense-in-depth.** Irreversible actions are refused at
  the contract/tool layer first (`GateDecision` raises on auto-promotion
  `(research_loop/contracts.py)`; external submit is dry-run by default with
  an explicit confirm flag `(apps/cli/main.py)`; draft operators are
  metadata-only `(research_loop/operator_drafts.py)`). The UI presents
  gates; it is never their only enforcement.
- **FE-L5 One in, one out.** Every new construct names what it replaces;
  each build phase ships its own deletions (§8); `apps/web/api.py`
  (~2,986 lines) is frozen — new endpoints land in new modules and api.py
  may only shrink.

---

## 1. Personas and journeys

| Persona | Journey | Gates crossed |
|---|---|---|
| Beginner (简洁模式, default) | one sentence → clarify (≤3 hypothesis-level questions) → confirm card → auto compute → report (terminal) → **user chooses** whether to start RD | G1, optionally G2 |
| Expert (专家工作台, one toggle) | edit params/formula → fast pre-validation → confirm (grid density) → new immutable run → side-by-side compare | G1, G2; G3/G4 stay outside the UI |
| Ambiguous input | hypothesis-changing ambiguity → blocking question with default options; parameter-level ambiguity → profile default + disclosure on the confirm card | — |

The beginner mode is a constrained **view** of the expert mode over one
kernel, never a separate engine (`docs/architecture/beginner_expert_workflows.md`).

## 2. Dual pipeline model (R3)

### 2.1 Kinds

- `factor_study` (pipeline A): 解析 → 假设确认 → 计算（评测＋回测）→ 报告.
  **The report is the terminal stage.** The report offers an explicit
  entry button to pipeline B; nothing auto-continues.
- `rd_optimize` (pipeline B): RD 确认 → RD 运行 → 候选排行榜. Seeded from a
  completed report (or a registry factor). The RD confirm card carries the
  user-chosen iteration count (1..`MAX_RD_ITERATIONS`, server-enforced
  `(apps/web/api.py:97, :1749)`), candidates per round, and objective.
- **R3.1 (owner ruling):** RD inherits the factor-evaluation interval and
  sample contract. There is **no independent RD interval parameter** of any
  kind, and the legacy auto-cycle scheduler UI is deleted (§8).

### 2.2 Honest stage granularity

Stages map 1:1 onto real execution units. Pipeline A's compute stage is ONE
job with one failure surface, because the shipped validation workflow saves,
evaluates, and runs both backtests inside a single job
`(apps/web/api.py:225)`. Pipeline B's N rounds run inside ONE job
`(apps/web/api.py:1749)`. The UI must not fake finer progress (no per-round
lights) until the backend emits real stage/round events (open question §12).

### 2.3 Pipeline aggregate (server-owned; net-new)

The existing `_WebJob` has only running/cancel-requested/terminal states and
starts its worker immediately `(apps/web/jobs.py:35)` — "pause at a
checkpoint awaiting a human" is NEW server state, not a UI trick. A durable
record (JSON under `artifact_root/pipelines/`, append-only transition
journal + snapshot, no worker thread parked on a human) with:

```text
schema_version, pipeline_id, kind, created_at, expires_at
status: draft | awaiting_confirm | running | paused_failure |
        completed | aborted | expired
stages: [{stage_id, status, child_job_id?, started_at?, ended_at?}]
input_hash            # canonical hash of formula + params + seed + config
confirm: {nonce, version, confirmed_at?}   # idempotent confirm
attempt: {number, parent_run_id?}          # server-authoritative lineage
artifact_refs: [...]                       # parse/eval/backtest/report refs
failure: {stage_id, reason_code}?          # pause-in-place record
```

Rules:
- **Idempotent confirm.** Confirm carries `(pipeline_id, nonce, version)`.
  A double click, a second tab, or a retried request returns the SAME run;
  attempt numbers reflect research history, not browser behavior.
- **Rejoin.** On page load the frontend queries active pipelines and
  re-attaches; refresh, server restart, and Back never silently strand a
  running computation (today job ids live only in module memory
  `(static/app.js:68)`).
- **Freeze semantics.** After confirm the card freezes. Edits made while a
  run is live are labeled 「仅用于下次尝试」 and preserved — never silently
  attached to the in-flight run (today inputs stay editable during a run and
  the completed run repopulates them from the old snapshot
  `(static/app.js:563)`).
- **Failure transitions.** `paused_failure` offers exactly three exits:
  edit (forks the frozen inputs into a new draft), fall back to rule parse
  (new parse provenance), abort (terminal). Retry declares which completed
  stages are reused.
- **Snapshot isolation.** Runs read a factor-definition snapshot; the
  current overwrite-then-restore-on-failure behavior
  `(apps/web/api.py:236)` must not clobber concurrent edits or invalidate a
  parallel reader's provenance.

## 3. Human gates (G1–G4)

| Gate | Where | Enforcement |
|---|---|---|
| G1 assumption confirm | pipeline stage 假设确认 (both kinds) | server `awaiting_confirm` state; idempotent nonce; card freeze |
| G2 RD beyond one shot | pipeline B's own confirm card (rounds, candidates, objective, cost preview) | B is a separate pipeline the user explicitly creates; no A→B bridge |
| G3 candidate → active | outside the sidecar, human-initiated | `GateDecision` raises on auto-promotion; CLI `recommend-active` only recommends |
| G4 external submit | CLI only, dry-run first | `factor submit` requires the explicit confirm flag; sidecar/tool registry structurally lacks submit |

## 4. Structural prohibitions (FE-X1..X4)

| # | The sidecar must never | Enforced by |
|---|---|---|
| X1 | compute a number | tools return typed payloads + artifact refs only; no numeric tools exist |
| X2 | be the only carrier of a number | narration schema forbids numeric args; eval asserts every numeric claim has an artifact ref rendered by a canonical renderer |
| X3 | promote / release operators / submit | G3/G4 hard stops; tool registry has no such tools |
| X4 | use external-OOS as selection evidence | selection surfaces render selection-only evidence; narration refers by ref; no autonomous RD selection until sample-role filtering is runtime-tested (`specs/agent_task.py` vocabulary exists; enforcement is net-new) |

## 5. Component contracts

### 5.1 Confirm card (two densities, one payload)

One hashable params object (never N hidden calls) rendered at two densities:
beginner = grouped plain-language lines; expert = the full 11-parameter grid.
Every VALUE carries a server-derived provenance badge; mixed-origin grouped
lines carry multiple badges (badges are per value, not per row).

Provenance vocabulary (7, server-derived from the immutable parse artifact +
value fingerprints):

```text
user_explicit | user_answer | profile_default | fixed_policy |
data_resolved | agent_inferred | human_override
```

Each entry: `{field, value, source, parent_value?, evidence_ref?,
superseded_by?}`.

**Lossless negative-evidence projection:** blocking warnings,
`INSUFFICIENT_*` statuses, synthesized fields (e.g. `is_st`), and withheld
metrics remain visible at EVERY density. Density changes layout, never bad
news.

### 5.2 Clarify (deep interview)

- Ask ONLY hypothesis-level ambiguity — the answer would change the
  hypothesis itself (market-cap basis, holding-horizon semantics, hard vs
  soft exclusions). Parameter-level ambiguity is never asked: profile
  default + per-line disclosure on the confirm card.
- Tiering: **blocking** (execution-critical; unanswered ⇒ do not run) ranks
  above semantic (has a safe default; skippable). Cap ≤3 questions total,
  each with a default option; skip = accept default, recorded.
- Superseded answers: a later answer that invalidates an earlier one keeps
  BOTH in provenance with a `superseded_by` link. The report can show
  "what you clarified".

### 5.3 Editable formula card (expert)

- A `<textarea>` is the single source of truth with an `aria-hidden`
  highlight overlay driven by the canonical highlighter
  (`static/views/dsl.js` — structural only, it does not judge validity
  `(static/views/dsl.js:19)`). `contenteditable` is rejected (CN IME
  composition, undo, cursor, paste hazards).
- **Net-new pre-validation endpoint:** canonicalize + ValidationGate checks
  (fields resolvable, operators in the read-only registry, data capability)
  WITHOUT persisting, evaluating, or backtesting — today's revalidate path
  runs the whole evaluation chain `(apps/web/api.py:225)`. Returns a
  canonical fingerprint or an unknown-operator draft review packet ref
  (existing `operator_drafts` path; never hot-executed).
- `edited_by=human` is derived server-side by fingerprint comparison.

### 5.4 Compare loop

Every edit = a NEW immutable run. `attempt_number`, `parent_run`,
formula/spec/profile/data fingerprints, failed/cancelled counting policy,
and dedup dispositions (`executed | reused | skipped`) are
server-authoritative. Diffs are drawn side-by-side by canonical renderers;
narration links by ref and never restates numbers; reports disclose attempt
counts (multiple-comparison honesty). Status-word metrics never enter
numeric comparisons.

### 5.5 Typed narration AST (net-new)

Narration is structured data, not free prose:

```text
NarrationNode:
  kind: status | question | ref | action_suggestion
  message_key: stable i18n code (CN label resolved client-side)
  args: non-numeric tokens only (statuses, labels, ids)
  ref?: {component_id, artifact_ref}   # must resolve to a currently
                                       # rendered, status-aware component
  options?: [{id, label, is_default}]  # question nodes
```

The sidecar never receives full metric/formula payloads — to "say" a result
it emits a `ref` and the canonical renderer shows it. Presentation: narration
attaches to the pipeline card as an event stream (desktop may add a bounded
side column; ≤375px uses a drawer/inline). There is NO standalone chat
column in V1.

### 5.6 Mode and degradation

- Simple mode is the DEFAULT landing (idea box + example seeds + one status
  line); the expert workbench (current six tabs) is one toggle away; the
  choice is remembered.
- Precedence: recognized expert deep link > saved mode preference > default
  simple. A deep link wins that navigation without rewriting the saved
  preference (hash routing exists in `static/views/lab.js:191`).
- LLM readiness is tri-state `unknown | unavailable | ready` (token-redacted
  boot must not pre-judge). No key ⇒ the simple landing degrades in place to
  the seeded guided form (spec fields ARE the form) + rule parser; the
  expert tabs are untouched; discovering a provider later upgrades
  NON-destructively (drafts survive). A local OpenAI-compatible endpoint
  with `require_api_key=false` may power the sidecar without breaking
  local-first (docs/USER_MANUAL.md §6).
- One shared draft object spans both modes and both densities; mode switches
  never destroy state. Both modes expose visible cancel/error surfaces.

### 5.7 In-process typed tool adapter (net-new; D3-locked)

- A server-side allowlisted tool registry over EXISTING workflows
  (`run_idea_parse_workflow`, validation, research-once in
  `apps/web/api.py`), signatures MCP-schema-shaped so a later MCP export is
  a transport change, not a redesign. **No MCP server in V1.**
- v1 catalog (closed): read — `list_factors, get_factor, search_runs,
  get_run, get_data_summary, search_docs`; action — `parse_idea,
  validate_draft_formula, create_pipeline, confirm_pipeline,
  cancel_pipeline`. Nothing else; no shell/fs/provider transports; no
  promote/submit; no auto multi-round RD.
- Server-side authority independent of model prose: per-run authorization
  (tools scoped to a pipeline), schema-validated results, rate/concurrency
  budgets, and **control-token required for action-class tools even on
  loopback** (today loopback binds skip token checks
  `(apps/web/api.py:2155)`; the agent surface must not inherit that). The
  bearer never enters model context. Idea text and factor-catalog text are
  untrusted DATA, never instructions (prompt-injection posture; RD prompts
  already consume catalog free text `(research_loop/context_builder.py)`).

## 6. Net-new server modules (five; api.py frozen)

| # | Module | Content |
|---|---|---|
| 1 | `apps/web/pipeline.py` (+ `specs/pipeline.py` types) | pipeline aggregate: record, transitions, idempotent confirm, rejoin, expiry; pre-validation endpoint route |
| 2 | `apps/web/provenance.py` | per-value provenance derivation from parse artifacts + fingerprints (7-value vocabulary) |
| 3 | `apps/web/narration.py` (+ `specs/narration.py`) | NarrationNode schema, ref validation, payload minimization |
| 4 | `apps/web/tools.py` | allowlisted tool registry, per-run authorization, budgets, token gating |
| 5 | (frontend) `static/views/pipeline.js`, `static/views/provenance.js`, `static/views/narration.js` | pipeline state machine + cards; THE badge renderer (fifth single-renderer seat); THE narration renderer |

## 7. Frontend modularity rules

- One surface = one static module, registered in `EXPECTED_STATIC_MODULES`
  (`tests/test_web_static_frontend.py`), pure render functions first and a
  `[controller]` section last (docs/frontend_contributing.md).
- `provenance.js` joins the single-renderer discipline: THE badge renderer;
  a provenance badge rendered anywhere else fails the sweep. Numbers still
  come only from `metric.js` / `views/charts.js` / `views/dsl.js`.
- `static/app.js` returns to "read config + assemble + route"; job wiring
  converges into `pipeline.js` (P2 deletion item).
- D8 holds: no build step, no external resources, CN-first UI text, both
  themes from tokens.

## 8. Deletion ledger (R3; each executes in the phase that ships its replacement)

| Existing element | Disposition | Phase | Rationale |
|---|---|---|---|
| `.lab-stepper` five-step strip `(apps/web/html.py:535)` | DELETE | P1 | semantically duplicated by the pipeline card |
| resident 11-param grid `#validation-controls` `(apps/web/html.py:838)` | ABSORB then DELETE | P1 | parameters live on the confirm card (expert density); control rail slims to idea entry + runtime strip |
| `rd-interval` auto-cycle select + 开启/停止 loop controls | DELETE (owner-ruled R3.1) | P3 | RD inherits the evaluation interval, no independent parameter; explicit pipeline B replaces implicit timed RD; CLI `research run-once` kept |
| `#staggered-run` resident side button `(apps/web/html.py:853)` | MIGRATE | P3 | becomes a report follow-up action next to the RD entry |
| `apps/web/api.py` growth | FREEZE | all | new endpoints in new modules; api.py may only shrink (migration of existing endpoints is a later chore) |
| scattered parse/validate wiring in `static/app.js` | CONVERGE | P2 | into `pipeline.js`'s state machine |

Removals update string-contract pins in the same commit — tests lock both
presence AND absence.

## 9. i18n / accessibility / responsive requirements

- Stable message keys (enums) distinct from displayed Chinese labels; LLM
  narration is never the translation catalog.
- Live regions: pipeline status + failure announcements via throttled
  `aria-live="polite"`; steps carry `aria-current`; focus moves to the
  revealed card on stage transitions and returns predictably on dismiss;
  clarify questions use `fieldset/legend`.
- True-375px zero horizontal overflow for every new card; 44px touch
  targets; 200% zoom usable; `prefers-reduced-motion` respected (no scroll
  animation, no dot pulsing).

## 10. No-LLM-key degradation matrix

| Runtime | Sidecar | Evidence plane |
|---|---|---|
| provider ready | full: semantic parse + interview + narration | unchanged |
| no provider / no key | simple landing becomes the seeded guided form + rule parser; pipeline runs unchanged (parse stage = rule); narration = static hints | **untouched** |
| local endpoint, `require_api_key=false` | full sidecar, still local-first | unchanged |

LLM absence degrades the sidecar, never the evidence plane. Direct
form/button operation is always first-class.

## 11. Ship gates (pre-launch requirements)

1. **Journal + replay.** Every sidecar action journals `{tool, objective,
   input refs, request hash, produced artifact refs, navigation target}`
   under `artifact_root`; a replay reproduces the same rendered cards; a
   test asserts chat text is never the sole carrier of a number.
2. **Pipeline state machine tests.** Never advances without confirm;
   failure always pauses in place; cancel/rejoin behave; double-confirm
   returns the same run.
3. **Provenance tests.** Every confirm-card line carries a server-asserted
   badge; missing badge = fail; negative evidence visible at both densities.
4. **Security plane.** Action tools token-gated on loopback; injection
   corpus (idea text + factor descriptions containing instructions) cannot
   escalate past the allowlist; budgets enforced.
5. Baseline: `python3 -m pytest`, CLI `--help`, `git diff --check`,
   `scripts/release_safety_scan.py` (AGENTS.md).

## 12. Open questions (non-blocking)

1. Splitting pipeline A's compute stage / pipeline B's rounds into finer
   stages — requires backend stage/round events first.
2. A free-form "追问" input (narration-only, read-only tools): P2 or cut.
3. RD confirm card field details beyond rounds/candidates/objective.
4. Pipeline record expiry/GC policy under `artifact_root/pipelines/`.
5. Migration pace of existing api.py endpoints into the new modules (chore).

---

## Appendix A — Review provenance (summaries)

- **R1 (Codex, default xhigh, architecture round):** 7 findings, all
  adopted. Sharpest: "chat is a dangerous second renderer" (→ FE-L2);
  UI-only checkpoints are insufficient (→ FE-L4); "conversational canvas"
  demoted to a thin sidecar; CLI remains the expert primary surface
  (it has `submit`/`backends`/`goal` the web lacks).
- **Owner deep interview (4 locks):** simple mode default landing;
  hypothesis-level-only clarify (≤3, defaults); in-process typed adapter
  (MCP-ready signatures, no server); blueprint before code.
- **R2 (Codex, `gpt-5.6-sol`, frontend round):** 16 attacks + 16 gaps;
  verdicts all-MODIFY. Net cost corrections adopted: pipeline aggregate is
  net-new (jobs are terminal-only); stage count must match execution
  truth; client-suppliable badges are forgeable; confirm needs idempotency;
  narration needs a typed AST; `contenteditable` rejected; readiness needs
  a tri-state; a standalone chat column fails at 375px. Two adjudication
  refinements: stages collapsed to truth-mapped counts (A=4, B=3); narration
  attaches to cards instead of a chat column.
- **R3/R3.1 (owner):** dual-pipeline split with user-chosen RD rounds;
  anti-bloat governance (FE-L5, deletion ledger, api.py freeze,
  one-surface-one-module); RD interval inherits evaluation settings — no
  independent parameter; auto-cycle controls deleted.

## Appendix B — Locked decision register (see DECISIONS.md D10–D12)

D10 sidecar adoption + iron laws FE-L1..L5 + gates G1..G4 + prohibitions
FE-X1..X4 · D11 dual pipelines + honest granularity + R3.1 interval
inheritance · D12 anti-bloat governance (deletion ledger, api.py freeze,
modularity rules).
