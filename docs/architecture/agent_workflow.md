# Agent Workflow Architecture (Phase B)

Extends `design/agent_develop/06_agent_and_interaction_design.md` (roles,
RunEvent v1, interaction patterns — all still binding). This document adds
the orchestration semantics the corpus left open, informed by the
cross-project adjudication.

## 1. Topology

- **Meta-Orchestrator** is the ONLY writer of run state (one-executor rule
  lifted to run level). Specialist agents write solely inside their own
  artifact namespaces; coordination is mediated (blackboard over
  ArtifactRef + RunEvents), never agent-to-agent chat.
- Specialists (corpus roles + two extensions): Research Guide/Hypothesis,
  Factor Designer, Evaluation Analyst, Backtest Reviewer (adversarial,
  reviews every BacktestResult artifact), Data Risk Reviewer,
  **Portfolio Construction** (emits capability-tagged BacktestRequests
  only), **Execution Simulation** (cost/slippage sweeps, staggered-entry
  robustness; research-grade labels mandatory), Governance Reviewer
  (packets only), Runtime Operator (watchdog: stuck runs, trace gaps,
  cancellation by policy).

## 2. Routing rules (deterministic policy table)

1. NL idea/report → Factor Designer; unresolved operators → draft-review
   branch → run pauses (`approval_required`).
2. New DatasetRef → Data Risk Reviewer BEFORE first evaluation (fail-fast
   on capability gaps; `synthesized_columns` and PIT status are its
   checklist).
3. Every backtest artifact → Backtest Reviewer asynchronously; blocking
   only in Formal Evaluation mode.
4. Gate failure → feedback packet (gate REASONS + evidence refs) to the
   hypothesis lane; bounded retries; dedup signatures prevent re-trying
   equivalents (existing COR-1 machinery).
5. Gate pass → Governance Reviewer assembles packet; run pauses for human.
6. Two consecutive no-progress rounds → early stop with `stopped_reason`.
7. Budget exhaustion (rounds, tokens, wall-clock) → `partial` with
   resumable journal, never silent truncation.

## 3. Memory & state model (three tiers, no shared mutable memory)

| Tier | Store | Mutability |
| --- | --- | --- |
| Durable truth | factor_root, artifact_root(+sha256), operator YAML, trace JSONL | append/versioned |
| Run state | RunEvent JSONL (qf.run.event.v1); optional SQLite index (rebuildable) | append-only |
| Agent working memory | per-run context packets built from artifacts (existing context_builder pattern) | rebuilt on resume; never persisted as free-form memory |

Provenance mandatory on every port response: input refs, data fingerprint,
registry version, request hash (closes the mounted-store trust gap).

## 4. Anti-overfitting mechanics in the loop (FP-6 operationalized)

- Context packets are filtered by `sample_role` — audit-OOS evidence must
  be structurally absent from selection prompts. STATUS: contract-level
  today (closed sample-role vocabulary, selection-only filter default);
  the runtime filter is the orchestrator lane's acceptance test (a trace
  test proving OOS absence from prompts), not an existing fact.
- Dedup: formula fingerprint + result signature + candidate diversity
  (existing), plus value-correlation pruning against the active library
  (RD-Agent-adopted, value-corr not string-similarity).
- Falsification battery (QuantGPT-adopted) runs as an evaluation stage;
  its verdicts enter gates as evidence with statuses, not as scores the
  LLM can argue with.
- Budgets are first-class run parameters recorded in the RunManifest.

## 5. Failure recovery

Run state machine: queued → running → (paused|partial|failed|cancelled) →
completed; `partial → completed` via replay from the journal. Idempotent
request hashes make resume safe. Runtime Operator escalates: stuck-run
detection (no events past timeout), trace-gap detection, cancel as a
first-class RunEvent. LLM outage degrades lanes to local rule generators;
kernel evidence paths never depend on the LLM.

## 6. Knowledge reuse (RD-Agent-adopted, bounded)

A repair-knowledge store (error → successful-fix exemplars) keyed by
validation failure class, consulted by the Factor Designer's bounded repair
loop. Strictly separated from research evidence: repair knowledge may make
the coder faster, never a factor look better.
