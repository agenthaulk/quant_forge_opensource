# Quant Forge Target Architecture (Phase B)

- **Branch:** `fable/phase-b-agent-platform-architecture`, base `PHASE_A_SHA = bf19c73`
  (accepted Phase A tip; quant kernel audited and remediated).
- **Inputs:** Phase A first-principles axioms (FP-1..FP-7,
  `docs/reviews/quantitative_core_audit.md`), cross-project adjudication
  (`docs/reviews/cross_project_discrepancy_matrix.md`), the decided design
  corpus (`design/agent_develop/04..10`: 17 versioned ports, ADR-001..012,
  agent roles, topology A/B/C). This document composes those inputs into the
  Phase B target and records the B2 option analysis. It does NOT re-open
  decided ADRs.

## 1. Governing rule (derived, not asserted)

Every architectural decision below follows one meta-rule derived from Phase A:

> **Make axiom-violating states unrepresentable in the contract layer;
> where impossible, make them loudly visible; only then rely on review.**

Applications: `execution_delay_days >= 1` is a constructor invariant, not a
lint (FP-1); `MetricValue.status` makes "0.00 that is actually null"
unrepresentable (FP-2/FP-7); `sample_role` tags make selection-touching of
OOS evidence detectable (FP-6); `synthesized_columns` makes fabricated data
visible (FP-4); `INSUFFICIENT_*_EVIDENCE` reasons make silent-pass gates
impossible (FP-2).

## 2. Layered target (Core / Studio / Agents)

```text
┌───────────────────────── STUDIO PLANE (application/control) ─────────────────────────┐
│ UI/BFF (local web first)                                                             │
│ Experiment Manager (RunRegistry/RunRef)   Strategy Lifecycle Mgr (draft→…→archived)  │
│ Governance Console (ApprovalPort/GovernancePort — human decisions only)              │
│ Run Event Store (append-only JSONL, qf.run.event.v1)   Artifact Store (ArtifactRef+  │
│ sha256 + RunManifest provenance)                                                     │
└──────────────▲───────────────────────────────▲──────────────────────────────────────┘
               │ versioned ports only (qf.*.v1/v2)│
┌──────────────┴───────────── AGENT PLANE ──────┴──────────────────────────────────────┐
│ Meta-Orchestrator (routing, budgets, single writer of run state)                     │
│ Research/Hypothesis · Factor Designer · Evaluation Analyst · Backtest Reviewer ·     │
│ Data Risk Reviewer · Portfolio Construction (adapter-scoped) · Execution Simulation  │
│ (research-grade) · Governance Reviewer (packets only) · Runtime Operator (watchdog)  │
│ All tool access via AgentToolPort → typed ports. No raw FS/DB/provider transport.    │
└──────────────▲───────────────────────────────────────────────────────────────────────┘
               │ EvaluationPort / BacktestPort / FactorScorePort / FactorDefinitionPort │
               │ OperatorCatalogPort / DataCatalogPort / SnapshotPort / FactorCompilePort│
┌──────────────┴────────────── CORE (deterministic research kernel) ────────────────────┐
│ operator_registry → parser/executor (safe AST) → signal prep → evaluation/backtest    │
│ factor_root · factor_values(+overlay) · artifact_root — file source-of-truth          │
│ No LLM inside. No UI/DB/queue imports. Public-boundary safe.                          │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

Invariants (from ADRs, restated as enforceable checks):
- Deleting the Studio and Agent planes leaves the Core fully functional
  (Topology A guarantee — CI can enforce by running kernel tests with the
  app modules absent from the import graph).
- Agents never call kernel functions directly; only ports (ADR-007).
- Files remain source-of-truth; any DB is a rebuildable index (ADR-004).

## 3. Step B2 — Options considered

### Option R (RECOMMENDED): In-repo Studio plane over versioned ports, file-backed, local-first

Shape: `quant_forge.specs` (new spec contracts) + `quant_forge.runs`
(run/event/manifest) + existing web adapter evolving into the Studio BFF;
agent plane as thin orchestrator over AgentToolPort; SQLite only as an
optional derived index.

### Option L (lighter): Status quo + spec files only

No run/event layer; FactorSpec/StrategySpec added as documentation-grade
schemas; agents keep using today's `agent_workspace` tools directly.

### Option F (fuller): Sibling-service Studio (Studio-repo style)

Separate repo/process with Postgres metadata plane, React UI, remote
workers (Topology B immediately), gRPC/HTTP port implementations.

### Comparison (criteria per protocol §B.6/B2)

| Criterion | R (in-repo, file-backed) | L (specs only) | F (service now) |
| --- | --- | --- | --- |
| Correctness | ports make sample-role/PIT tags mandatory at boundaries | unchanged (gaps stay implicit) | equal to R but more moving parts to get wrong |
| Implementation cost | M (contracts + thin stores) | S | XL (auth, DB, deploy, two repos) |
| Maintenance cost | M — one repo, one test suite | S but grows tech debt | H — schema migrations, service ops |
| Extensibility | ports designed for adapter swap (B→C topology) | poor — nothing to attach agents to safely | high but paid up-front |
| Migration cost | incremental, additive modules | none | big-bang; violates local-first ADR-006 |
| Performance | local files, adequate at single-user scale | same | network overhead without need |
| Security/公开边界 | no new secrets/deployment surface | same | credential+deployment surface in public repo — conflicts with AGENTS.md boundary |
| 小白 friendliness | good — run timeline + statuses feasible on local web | poor | best UI ceiling, worst time-to-value |
| Expert openness | full — specs editable as files, SDK = ports | partial | full but behind service API |

**Fable decision: Option R.** L fails the mandate (no agent platform, no
experiment integrity layer); F contradicts ADR-006 (local-first), the
public-repo boundary, and buys nothing Phase B needs. R is the only option
where every Studio capability is an adapter over contracts that already
exist or are prototyped in this phase. F remains the Topology-B evolution
path of R, not a competitor.

## 4. Module boundaries (target, delta over current tree)

| Boundary | Owner module (target) | Status |
| --- | --- | --- |
| PIT data layer | `data/` + DataCatalogPort/SnapshotPort; adopt Studio-style `visible_at` resolvers when fundamentals arrive | exists (panel); PIT-fundamentals deferred to Phase C |
| Data catalog & schema | `mcp/read_models` → DataCatalogPort | exists (read models) |
| Factor DSL | operator_registry + formula parser (canonical-only) | exists, audited |
| Strategy DSL | **deliberately NOT a new language**: StrategySpec = typed parameters over kernel capabilities (universe, top_quantile, holding, costs, splits) | new spec (B3) |
| Operator Registry | operator_registry (git-tracked YAML, draft-review flow) | exists |
| Factor execution engine | factor_engine (safe AST, value store) | exists, audited |
| Strategy execution engine | BacktestPort capability adapters; portfolio construction beyond single-factor L/S is a capability-tagged adapter, NOT kernel growth | contract only in B |
| Backtest kernel | backtesting/service (audited semantics: purge, lost-position realization, cost model) | exists, audited |
| Portfolio & risk module | Phase C adapter behind BacktestPort capabilities | deferred |
| Factor evaluation | evaluation/service (embargoed splits, HAC, metrics.v2) + QuantGPT-adopted falsification battery (placebo/permutation/noise/walk-forward decay) as a new `evaluation/falsification.py` stage | battery = Phase C, contract slot reserved in EvaluationRequest |
| Experiment management | `quant_forge.runs`: RunRef + RunEvent JSONL + RunManifest | B3 prototype (manifest-lite) |
| Artifact Registry | artifact_root + ArtifactRef{uri,sha256,schema_version} | B3 prototype |
| Model & factor registry | factor_root lifecycle (draft→candidate→active→inactive→archived) + validation-gated promotion packets (Studio-adopted governance) | exists + governance packet in Phase C |
| Agent Orchestrator | meta-orchestrator over AgentToolPort; RunEvent timeline; bounded budgets; dedup | Phase C; task schema in B3 |
| Research Workflow | existing RD loop = the deterministic harness the agent plane drives | exists, audited |
| Code Generation Sandbox | **rejected for public repo**: no exec() of generated code (Studio's exec+AST-preflight is a recorded negative constraint); generation targets the restricted DSL only | decided |
| Validation Gate | ValidationGate contract: data capability + operator resolution + PIT preconditions before any run | B3 prototype |
| Review Gate | gates (candidate_gate/apply_gate) + INSUFFICIENT_*_EVIDENCE discipline + human approval | exists, audited |
| Beginner API | NL idea → FactorSpec draft → validated run plan → report (never raw kernel calls) | B1 journey; flow stub in B3 |
| Expert SDK/API | the ports themselves + spec files on disk | by construction |
| CLI / Web | existing adapters, extended to render RunRefs/statuses | exists |
| Permissions / audit / observability | local-first: control token (exists), RunEvent audit trail, trace JSONL; multi-user auth deferred to Topology B | partial |
| Failure recovery | run state machine (queued/running/paused/partial/failed/cancelled), resumable from journal; idempotent request hashes | contract in B3 |

## 5. Storage & source of truth (unchanged from corpus, restated)

factor_root (definitions), operator YAML, artifact_root (+ArtifactRef/sha256),
run-event JSONL = truth. SQLite/Postgres/object store = derived, optional,
rebuildable, topology-gated. The mounted factor-value store stays
read-base+overlay; RunManifest fingerprints close the provenance gap noted
in Phase A ("cached values trusted as-is").

## 6. Key risks (delta to corpus register R001-R014)

| Risk | Mitigation |
| --- | --- |
| Spec layer drifts from kernel contracts (two definitions, FP-5) | specs are thin typed views over `core.contracts`; kernel dataclasses stay canonical; round-trip tests in B3 |
| Agent plane re-introduces test-set mining (FP-6; RD-Agent counterexample) | external-OOS stays audit-only at the PORT level: EvaluationPort/BacktestPort responses carry sample_role; the orchestrator refuses to route OOS-tagged evidence into selection prompts |
| Run/event layer becomes a second truth | events reference artifacts by hash; replay test: delete index, rebuild from JSONL |
| Scope creep toward Studio parity in one phase | B3 is contracts + one vertical slice only; UI/DB/workers are Phase C+ behind accepted ports |
