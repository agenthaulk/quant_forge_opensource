# Migration Strategy (Phase B → C)

Principle: additive adapters over an audited kernel; no big-bang; every
step leaves `main` releasable and Topology-A functional.

## Reuse decisions (Step B0 baseline)

| Component | Verdict | Reason |
| --- | --- | --- |
| evaluation/backtesting/factor_engine/operator_registry/factor_library | REUSE as-is | Phase A audited; semantics are the platform's core value |
| research_loop (RD harness) | REUSE | becomes the deterministic loop the agent plane drives |
| agent_workspace / mcp read models | REUSE, extend to AgentToolPort | thin, safe |
| apps/web/server.py | REUSE short-term, DECOMPOSE during Studio-plane work (routing/API/HTML/jobs) | god file, known hazard (three historical P1s) |
| research_loop/service.py | REUSE, decompose opportunistically (trial exec / assessment / chaining) | god file |
| Studio-repo code | DO NOT port code; port CONTRACT IDEAS only (manifest, visible_at PIT, basis split, preflight taxonomy, registration governance) | different stack (DB/React/exec), public-boundary conflicts |
| QuantGPT/RD-Agent code | no code reuse; mechanics reimplemented against our contracts | license/stack/quality boundaries |

## Phased path

- **M0 (Phase B, this branch):** spec + validation + manifest + task-schema
  contracts with tests (B3 slice). Zero behavior change to existing flows.
- **M1 (Phase C):** runs/events layer + orchestrator MVP driving the
  existing RD loop; web renders run timeline; god-file decomposition of
  server.py rides along (routing extraction first).
- **M2:** falsification battery stage + value-corr library pruning +
  repair-knowledge store; gates consume new evidence classes.
- **M3:** portfolio/execution-sim capability adapters behind BacktestPort;
  StrategySpec fields activate; `capabilities_required` enforcement.
- **M4 (Topology B, opt-in):** shared artifact store + metadata DB + auth as
  adapter swaps; contracts unchanged (proof: kernel tests run unmodified).

## Compatibility commitments

- `qf.metrics.v2`, existing artifact JSON keys, CLI commands: unchanged.
- New spec/manifest files are additive; absence = legacy mode everywhere.
- Every M-step ships with: full suite green, release scan green, and a
  WORKING_STATE handoff entry.

## Migration risks

| Risk | Guard |
| --- | --- |
| Spec/kernel drift (FP-5) | specs import kernel types; round-trip tests |
| Event layer becomes required for old flows | CLI/kernel paths tested with runs-layer absent |
| server.py decomposition regressions | characterization tests before extraction; route-by-route moves |
| Scope pull toward full Studio | phase gates: each M-step has its own acceptance list; UI ambitions parked behind accepted ports |
