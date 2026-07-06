# Phase B Adversarial Review Resolution (Step B6)

Reviews: Opus B4 (architecture, read-only, ran full suite 453 green) →
`docs/reviews/phase_b_opus_review.md`; Codex B5 (GPT-5-family, high
reasoning, read-only; sandbox had no writable tmp so pytest could not run —
inspection-only) → `docs/reviews/phase_b_codex_review.md`. Both verdicts:
**REVISE-FIRST**. Convergence was high; adjudication below.

## Consensus (adopted without conflict — revision lane R1..R6)

| Item | Opus | Codex | Ruling |
| --- | --- | --- | --- |
| `sample_role` free string, fail-open default (FP-6 hinge) | F2 major | #3 major | Closed vocabulary mirroring kernel literals; manifest role explicit-only; filter default stays selection-only (safe direction) |
| RunManifest accepts empty data/registry provenance; test pins it | F1 major | #4 major | Non-empty required; typed `UNVERIFIED_PROVENANCE` sentinel; test flipped |
| AgentTaskSpec denylist polarity + overclaiming docstring | F5 major | #2 major | Allowlist against declared `KNOWN_AGENT_TOOLS` catalog; docstring corrected |
| ValidationGate silently ignores universe_filters / capabilities; PIT claims overstated | F3 major | #1 major | Capabilities fail-loud vs `KNOWN_CAPABILITIES` (empty today); filters validated against executor's allowlist forms; explicit `unchecked` field; doc claims re-scoped |
| NL fallback fabricates interpretation silently | F6 major | — | Fallback-parse warning mandatory |
| Architecture prose states Phase C obligations in present tense | (implied) | #5 answer | Docs edited: enforcement claims marked as Phase C acceptance criteria, not current facts |

## Codex-only increments (adopted)

1. **RunManifest FactorSpec-only vs doc claims (C#5):** adopted —
   `manifest_for` generalized to accept FactorSpec or StrategySpec (spec
   kind recorded); docs otherwise had to say "factor-only".
2. **Fingerprint canonicalization (C#6):** adopted in part — non-finite
   payload values now REJECTED (consistent with the Phase A A7 JSON rule);
   unicode normalized NFC before hashing. Int-vs-float representation is
   documented as caller responsibility (JSON semantics), not silently
   coerced.
3. **Run recovery is prose (C#8):** adopted — minimal typed `RunEvent`
   contract (qf.run.event.v1 from the design corpus) with a legal-transition
   table lands in the spec layer so the Phase C registry lane starts from a
   contract, not prose. Side-effect idempotency and replay policy remain
   the lane's acceptance criteria (documented as such).
4. **Concurrent-writer temp collision in `_atomic_write` (C#7):** recorded
   to the F-register (kernel file, out of Phase B scope; single-writer is
   today's documented reality).
5. Test-count claim corrected: 24 test functions (27 passed = pytest
   parametrization), noted for the record.

## Conflicts and Fable rulings

| Conflict | Opus position | Codex position | Fable ruling (evidence) |
| --- | --- | --- | --- |
| Existence of the spec layer | Justified: FactorDefinition is the library-lifecycle object; sim/costs/capabilities/metadata don't belong in it; delegation-by-construction keeps drift contained | Alternative: make core.contracts canonical, add serialization around it; defer AgentTaskSpec/StrategySpec until enforcing adapters exist | **Keep the spec layer** (Opus). Pushing UI/agent concerns into kernel dataclasses inverts the layering both reviews endorse elsewhere; delegation-by-construction inherits kernel invariants automatically. Codex's drift concern is answered by the R-lane closures, not by deleting the layer |
| StrategySpec now vs later | Defensible schema reservation (weakest pull, keep) | Defer until adapters | **Keep**, with R4 making unmet capabilities fail-loud — a spec written today cannot silently run on an engine that ignores its requirements; that is the point of reserving the schema |
| AgentTaskSpec now vs later | Fix polarity, keep | Defer or rename | **Keep with allowlist**: the catalog constant IS the Phase C AgentToolPort contract (1:1), which makes the port's surface reviewable before code exists |
| Beginner semantic-mismatch severity | Major (F6): fabricated interpretation | (not raised) | Adopted as major; fallback warning now; the clarify-loop remains the Phase C beginner-lane acceptance bar |

## Rejected proposals (with reasons)

- Codex alternative "core.contracts as the only model": rejected (above).
- Widening the exec denylist instead of allowlist: rejected — wrong
  polarity cannot be fixed by enumeration (both reviews agree in substance).
- Making the falsification battery advisory if demo data is underpowered:
  rejected pre-emptively (Opus F7 scenario) — the M2 lane instead gets a
  demo-data power policy; INSUFFICIENT verdicts must stay blocking-grade
  evidence (FP-2), with sample floors set so demo runs can pass them.

## Residual risks accepted knowingly

- Validation-witness pattern (gate token consumed by execution ports) is a
  named Phase C requirement, not implemented in B — the advisory gap
  (Opus F4) stays open until the orchestrator lane lands; mitigated by the
  `precomputed:` path being blocked at the gate and by no autonomous
  execution existing yet.
- Data-plane lane added to the roadmap; until it delivers, research
  breadth (7 fields / 20 operators) remains demo-grade — accepted and
  disclosed.
- Prompt-injection threat model recorded in the LLM-boundary doc as a
  Phase C review item for the orchestrator lane.
