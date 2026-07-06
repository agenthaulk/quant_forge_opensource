# Beginner & Expert Workflows (Phase B — Step B1)

Two user modes over ONE kernel and ONE spec layer. The beginner mode is a
constrained view of the expert mode, never a separate engine (FP-5).

## 1. Beginner journey (小白模式)

Natural-language in, evidence-with-caveats out. The LLM never computes a
number (see `deterministic_llm_boundary.md`).

```text
NL idea ─▶ Intent parse (LLM) ─▶ clarify loop (missing horizon? universe? risk appetite?)
      ─▶ FactorSpec DRAFT (typed, validated fields only)
      ─▶ ValidationGate: fields resolvable? operators canonical? data capability ready?
             │ fail ⇒ human-readable unresolved list; draft stays non-executable
      ─▶ Assumptions screen (delay=1, costs, holding, splits — plain language, MUST confirm)
      ─▶ Evaluation (embargoed splits, metrics.v2) ─▶ Backtest (audited kernel)
      ─▶ Report: returns WITH statuses, warnings first-class, overfitting note
             (IS vs OOS shown side-by-side; INSUFFICIENT_* rendered as words, not 0.00)
      ─▶ optional: bounded RD improvement loop (budgeted rounds, dedup)
      ─▶ Candidate? ⇒ promotion REQUEST only; human decides (governance)
```

Journey guarantees:
1. Every number the beginner sees came from the kernel with a MetricValue
   status; the UI is forbidden to render a bare scalar for a null metric.
2. Confirmation checkpoints: before first run; before any long RD loop;
   before promotion request. Nothing irreversible without a click.
3. The system states what it did NOT check (e.g., "no fundamental PIT data
   loaded; is_st synthesized") from `synthesized_columns`/capability packs.
4. Overfitting disclosure is mandatory report content, not an easter egg:
   number of candidates tried, dedup hits, OOS decay, evidence statuses.

Failure paths: unresolvable field/operator → draft parked with unresolved
list; data capability not ready → blocked with the exact capability gap;
LLM unavailable → beginner mode degrades to template-guided form entry
(the spec fields ARE the form).

## 2. Expert journey (专家模式)

Experts operate on the same artifacts with full visibility:

- Edit FactorSpec/StrategySpec as files (or via web forms bound to the same
  schema); diff view = git diff of spec files.
- Direct port access: evaluate/backtest with explicit profiles, splits,
  cost models, horizon matrices; run falsification battery on demand.
- Experiment branches: spec + config changes on git branches; RunRefs tie
  results to (spec version, data fingerprint, registry version).
- Custom operators: draft → JSON review packet → human registry release
  (never hot-executed).
- Walk-forward / OOS: expert can request additional split schemes; the
  sample_role tagging still marks which windows selection has touched —
  the platform records, it does not police experts.
- Full lineage: every artifact carries RunManifest (inputs, fingerprints,
  code/registry versions); "reproduce this row" is a one-command replay.
- Agent collaboration: experts can hand a bounded objective to the agent
  plane ("improve turnover without losing ICIR, 10 rounds max") and get a
  trace-backed diff of what was tried.

## 3. Factor lifecycle (shared by both modes)

idea → FactorSpec(draft) → validation-gated runs → evidence bundle
(evaluation + selection backtest + audit-only OOS + falsification results)
→ candidate (gates passed, INSUFFICIENT_* clean) → human approval → active
→ inactive → archived. Auto-promotion to active is structurally impossible
(GateDecision raises on should_promote_active=True — existing contract).

## 4. Strategy journey (NL → executable definition)

Phase B scope: StrategySpec = typed parameterization of kernel capabilities
(universe filters, ranking factor(s), top_quantile, holding, delay, costs,
benchmark). NL → StrategySpec follows the same parse→clarify→validate→
confirm pipeline. Multi-factor weighting / risk overlays arrive in Phase C
as BacktestPort capability adapters; the spec reserves the fields now
(`capabilities_required`) so specs written today fail loudly, not silently,
on engines that cannot honor them.

## 5. Human-machine co-optimization loop

The loop the platform optimizes for, in both modes:

hypothesis (human or agent) → deterministic evidence → gate verdict with
explicit reasons → human reads REASONS (not just scores) → next hypothesis.
The unit of collaboration is the gate reason string + evidence refs; agents
consume exactly what humans read (one feedback definition, FP-5).
