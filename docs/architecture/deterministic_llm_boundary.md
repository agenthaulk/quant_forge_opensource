# Deterministic / LLM / Workflow / Human Boundary (Phase B)

The single most important boundary in the platform. Derived from FP-1..FP-7
and the cross-project evidence (RD-Agent's LLM-judged test-set gate and
Studio's exec()-based codegen are recorded counterexamples).

## Responsibility matrix

| Concern | LLM | Deterministic workflow | Deterministic engine | Human |
| --- | --- | --- | --- | --- |
| Intent understanding, clarification questions | ✓ owns | routes | — | answers |
| FactorSpec/StrategySpec DRAFTING | ✓ proposes fields | validates schema | — | confirms assumptions |
| Operator/field resolution | suggests candidates | ✓ owns (registry lookup) | — | reviews new-operator drafts |
| Formula execution | ✗ forbidden | — | ✓ safe AST, canonical ops only | — |
| Returns, weights, NAV, risk metrics | ✗ forbidden (never a calculator) | — | ✓ owns | — |
| Split/embargo/sample-role assignment | ✗ | ✓ owns (config-derived) | ✓ enforces | can widen embargo, never narrow below delay+horizon |
| Gate evaluation | ✗ | ✓ owns (typed thresholds, evidence checks) | — | sets thresholds |
| Interpreting results, drafting narrative | ✓ (from typed evidence only, with refs) | supplies evidence bundle | — | reads; may reject narrative |
| Next-hypothesis generation | ✓ (bounded, deduped, budgeted) | enforces budget/dedup/diversity | — | can steer with hints |
| Promotion to active / registry release | ✗ | assembles packet | — | ✓ owns |
| Code generation | DSL formulas ONLY; general Python codegen is out of scope for the public platform (no exec sandbox) | — | — | — |

## Hard rules (contract-enforceable)

1. **LLM output is always a PROPOSAL in a typed schema.** Free text never
   flows into execution paths; JSON-schema-validated task payloads only
   (existing rd task_type validation is the pattern).
2. **No numeric field of any result may originate from an LLM.** Results
   are constructed exclusively by kernel services; the agent plane can only
   reference them by ArtifactRef.
3. **LLM never sees audit-OOS evidence during selection.** The orchestrator
   filters context packets by sample_role before prompting (FP-6). External
   OOS appears only in post-selection audit sections and human reports.
4. **Reproducibility mechanism:** trace-replay is the guarantee — all
   prompts and responses traced, request hashes make reruns idempotent.
   Temperature 0.0 on hypothesis lanes reduces variance but providers do
   not guarantee bitwise determinism; never claim it.
5. **LLM unavailability degrades, never blocks evidence:** parsing falls
   back to structured forms; RD falls back to local rule generators
   (existing pattern); evaluation/backtest paths have zero LLM dependency.
6. **No exec() of generated code.** The only executable surface generated
   from NL is the restricted formula DSL through the hardened AST path.
   (Studio's in-process exec of agent code is explicitly rejected for the
   public repo.)

## Phase C obligations (named requirements, not current facts)

- **Validation witness (B4 F4):** the gate must issue a typed witness
  (validated spec + registry/catalog provenance) and execution ports must
  accept ONLY the witness — closing the advisory-gate gap and the
  `precomputed:` resolver short-circuit path. Until then, no autonomous
  spec→execution routing exists by policy.
- **Prompt-injection threat model (B4 F12):** context packets interpolate
  artifact-derived text (factor names, theses, objectives) into prompts;
  the orchestrator lane must review data→LLM flows, and LLM narrative may
  never be the sole carrier of a numeric claim — every number in narrative
  must be checkable against typed evidence refs (UI renders the typed
  value, not the prose).
- **AgentTaskSpec.allowed_tools is a declared catalog contract**: it
  constrains construction today; runtime enforcement arrives with
  AgentToolPort, which must implement the catalog 1:1 (B5 #2 honesty gap).

## Human confirmation points (minimum set)

- Before first execution of a new spec (assumptions screen).
- Before starting a bounded RD loop (budget shown).
- Operator draft registry release.
- Candidate → active promotion.
- Any action that deletes/overwrites artifacts (otherwise append-only).
