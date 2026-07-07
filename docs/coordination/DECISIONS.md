# Owner Decision Register

Per decision D5, coordination records live in git under `docs/coordination/`.
Owner: project manager (haulk). Recorded by Fable.

## 2026-07-06 — Phase B/C decision set

| # | Decision | Ruling (owner) | Implementation |
| --- | --- | --- | --- |
| D1 | Phase C first wave scope | Approved: parallel lanes, individually stoppable | Wave 1 branch `fable/phase-c-research-platform-wave1`; registry/data-plane/server-decomposition follow |
| D2 | Data & operator investment | Reference existing QF_OS operators/fields; absorb necessary Quant Forge Studio operators | **Finding: absorption already complete** — Studio's 7 ops (cs_rank, scale, delay, delta, ts_rank, rolling_corr, signed_power) all map 1:1 onto existing canonical operators (rank, scale, delay, delta, ts_rank, correlation, signedpower). Actionable remainder: research metadata tags + field expansion (wave 2) |
| D3 | Partial final period | **Exclude** the incomplete final period from backtests by default, and remind the user | Kernel default flips to exclude + explicit warning; `include_partial_final_period=True` remains available (wave 1, lane A) |
| D4 | Agent evaluation artifacts | Allowed, but into a separate subdirectory with source tags | Phase C orchestrator lane acceptance criterion |
| D5 | Coordination docs in git | Yes, under `docs/coordination/`, PR-review exempt | This file; WORKING_STATE migration rides the next merge |
| D6 | Open-source vs commercial split | Build an OPEN-SOURCE interactive platform on the QF_OS kernel, front-end modeled on Quant Forge Studio and surveyed projects. Commercial differentiation lives at the AGENT layer and PORTFOLIO REBALANCING layer | Open-source scope: full research workbench (factor research, evaluation, backtest, RD loop, research history, benchmarking, web UI). Commercial-reserved: advanced agent orchestration, multi-factor portfolio construction/rebalancing adapters. BacktestPort capability tags mark the boundary |

## Standing inputs adopted

- `docs/research_platform_optimization_from_vibe_quantgpt.md` (Codex memo)
  adopted as the wave-1/wave-2 feature source: research-goal artifacts,
  artifact lineage index, run index + `qf runs`, research memory,
  deterministic ResearchStrategySelector, secondary anti-overfit
  diagnostics (advisory first), factor bench. Its constraints match the
  FP axioms and remain binding.
- Reference projects (read-only): Quant Forge Studio branch, QuantGPT,
  RD-Agent, Vibe-Trading (research components only; trading stack out of
  scope per the memo).
