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

## 2026-07-07 — Extensions decision (owner delegated the ruling to Fable)

| # | Decision | Ruling (Fable, under owner delegation) | Implementation |
| --- | --- | --- | --- |
| D7 | Adopt Studio's Extensions system in the open-source platform? | **Adopt, declarative-only, as a cuttable CP6 sub-item.** Studio's system is a read-only control plane over pure-manifest metadata (~600 LOC; 5 MVP contribution points: data.snapshot_source, data.canonical_mapping, data.quality_rule, agent.context_pack, docs.pack; 5 reserved stubs) — verified: no dynamic code loading. Binding constraints for QF_OS: (1) STRICTER no-exec than Studio — `executable` contributions rejected unconditionally, no built-in exemption; (2) D6 boundary — agent.context_pack stays a declarative knowledge pack; agent.workflow remains a reserved stub in open source; (3) one-truth integration — data.* contribution points must feed the CP5 DataCatalogPort / field-expansion path, never a parallel catalog (FP-5); (4) manifests pass release-safety rules — no absolute paths or secrets; provider endpoints via env-var indirection only; (5) web surface read-only (GET-only endpoints + browse panel, control-token protected). | Optional sub-item of CP6, cuttable without replanning; defers to a post-CP8 enhancement wave if CP6 runs tight. Port the contract (manifest schema, registry, 5 MVP points), not the Studio code wholesale. |
| D7a | Owner refinements to D7 (2026-07-07, same day) | **(1) Data-interface extensions are IN SCOPE for open source**: beyond the three declarative data.* MVP points, the reserved data-interface points (`data.provider_adapter`, `data.pit_resolver`) MAY be implemented in the open-source platform when the data plane needs them — under the same discipline: manifest-declared, adapter code ships in-repo through normal review, NO dynamic loading of third-party code, endpoints via env-var indirection. **(2) Agent surface: open source keeps only the Studio-level basic agent** (the existing research loop / hypothesis generation); agent optimization and role subdivision are reserved for the commercial version — `agent.workflow` stays a reserved stub, `agent.context_pack` stays declarative. All other aspects follow Fable's D7 ruling unchanged. | CP5 data plane may target the provider-adapter/PIT-resolver contract when field expansion requires it; CP6 extension registry scope updated accordingly (owner ruling, recorded by Fable) |

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
