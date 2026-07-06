# Implementation Plan (Phase C task graph)

Derived from `docs/architecture/*.md` (Phase B). Each lane = one Phase C
branch from the accepted Phase B SHA, with file ownership and acceptance
criteria per protocol §五.

| Lane / branch | Scope (owns) | Depends on | Acceptance |
| --- | --- | --- | --- |
| `fable/phase-c-experiment-registry` | `quant_forge/runs/`: RunRef, RunEvent JSONL store, RunManifest persistence, replay/rebuild tool | B3 contracts | events append-only; index rebuild == journal replay; kernel paths run with layer absent |
| `fable/phase-c-agent-orchestrator` | meta-orchestrator over AgentToolPort; routing table; budgets; sample_role context filter; RD loop as executor | experiment-registry | routed run reproduces current RD outcomes; OOS evidence provably absent from selection prompts (trace test) |
| `fable/phase-c-falsification-battery` | `evaluation/falsification.py`: placebo/permutation, noise-injection IC retention, walk-forward IC decay; gate evidence wiring | none (kernel-side) | battery verdicts carry MetricValue statuses; gates block on configured-but-missing battery evidence (FP-2) |
| `fable/phase-c-library-pruning` | value-correlation dedup vs active library; repair-knowledge store (validation-failure exemplars) | none | corr pruning uses factor VALUES; repair store provably isolated from evidence |
| `fable/phase-c-beginner-workflow` | NL→Spec→confirm→run→report flow in web; assumptions screen; status-first rendering | experiment-registry | beginner path never renders a bare scalar for null metrics; confirmation checkpoints enforced |
| `fable/phase-c-expert-workflow` | spec file round-trip UX, run comparison, lineage/replay command | experiment-registry | replay reproduces artifact hashes on demo data |
| `fable/phase-c-strategy-capabilities` | StrategySpec activation; portfolio-construction + execution-sim capability adapters behind BacktestPort | orchestrator | specs with unmet `capabilities_required` fail loudly; adapter results carry research-grade labels |
| `fable/phase-c-server-decomposition` | apps/web/server.py → routing/api/html/jobs modules (characterization tests first) | none (rides M1) | no behavior change; route-by-route parity tests |
| Phase A follow-ups F-1..F-6 | gate evidence extension (retention/turnover/corr), gate-definition unification, `_backtest_metrics` segments, demo fillna, rd.yaml knob, purge-count persistence | none | same test-first protocol as Phase A |

Sequencing: M1 = registry + orchestrator + server-decomposition;
M2 = falsification + pruning + F-register; M3 = strategy capabilities;
M4 = Topology-B adapter swaps (opt-in, contracts frozen).

Assignment guidance (not hardcoded): orchestrator/registry design-heavy →
Opus-class; battery/pruning/flows well-scoped → Sonnet-class; adversarial
review of every lane → Codex; Fable adjudicates and integrates.
