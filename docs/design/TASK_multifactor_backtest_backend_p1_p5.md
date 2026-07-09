# TASK — Multi-Factor / Portfolio Backtest backend (P1–P5)

> Hand-off development brief for Fable. Self-contained: an executor can pick this
> up cold. The **authoritative spec** is
> [`docs/design/multi_factor_portfolio_backtest.md`](multi_factor_portfolio_backtest.md)
> — this brief only scopes, sequences, and gates the work.

## One-line goal

Build the missing server side of the already-shipped multi-factor frontend so
that selecting ≥2 factors and clicking **合成并回测** runs a real
periodic-rebalancing composite backtest end-to-end on `cn_a` data and renders the
report — resolving the "点击回测无反应" defect (root cause: the two endpoints the
FE calls do not exist; CP10 landed frontend-only).

## Scope: P1–P5 only (a-priori methods, runnable end-to-end)

Deliver exactly phases **P1–P5** from the design doc §14. **Out of scope this
task:** P6 (fitted IC/ICIR methods), P7 (honesty polish beyond what P1–P5 need),
P8 (engine refactor / overlapping cohorts). The method catalog therefore ships
`equal_weight` + `weighted` (both `is_fitted:false`); `ic_weighted` /
`icir_weighted` render as **reserved (`available:false`) 预留 options** so the FE
shows them without offering to run them until P6.

| Phase | Deliverable | Touches |
| --- | --- | --- |
| P1 | `GET /api/synthesis/methods` catalog (design §9 JSON, minus fitted = available:false) + route | `apps/web/routing.py`, `apps/web/api.py` |
| P2 | Two minimal **additive** engine fixes — deterministic `mergesort` tie-break (RB-3) + skip-warning ledger stub (RB-7) — and the pure `rebalance_indices(...)` helper (RB-5) | `backtesting/service.py` (+ re-baseline single-factor golden once) |
| P3 | `synthesis/service.py` a-priori core: one-pinned-universe fetch → standardize (zscore/rank, deterministic ties) → ±1 direction → `equal_weight`/`weighted` combine → coverage → degenerate/skip pre-scan. Pure, unit-tested. | new `src/quant_forge/synthesis/` |
| P4 | Materialize composite as a colon-free `COMPOSITE_<hash-of-all-inputs>` `precomputed:` factor via `FactorValueStore._resolve_factor_paths` + `FactorRepository.save` into `factor_root`; engine-driving profile pinned `decay_days=0`; drive `run_factor_backtest` by id | `synthesis/service.py` (+ reuse only) |
| P5 | `POST /api/jobs/multi-factor-backtest` route + `run_multi_factor_backtest_workflow` (mirror `_validate_factor_workflow`); assemble the design §8 payload (`factor`, `synthesis_provenance`, `backtest`, same-window `evaluation` diagnostics, `validity`) | `apps/web/routing.py`, `apps/web/api.py`, `synthesis/service.py` |

## Fixed decisions (do not re-litigate)

- **Backtest-only module** (owner directive) — no research-evaluation date
  interval. Already applied in the FE (`synth-param-evaluation-*` inputs removed;
  `BACKTEST_DATE_FIELDS` now only `backtest_start`/`backtest_end`). Keep the
  `evaluation` payload slot as **same-window diagnostics** (design §8, FP-2), not
  a separate interval.
- **Fuller method set is the end state** (owner chose it), but fitted methods
  land in **P6**, not here. Ship them reserved in the catalog now.
- **Honesty discipline (FP-4)** is non-negotiable: coverage ratios `null`→n/a
  never `0`; `is_fitted` truthful; a-priori `weights_effective` echoed raw; every
  skip/degeneracy emits an explicit warning code (design §12 checklist).
- **No look-ahead:** signal as-of close `t`, trade at `t+execution_delay_days`;
  `rebalance_indices` is the single source of truth for the grid.

## The 3 blocking traps already found (design review) — honor the fixes

1. Composite ids must be **colon-free** (`FactorDefinition.__post_init__` /
   `_PRECOMPUTED_FORMULA_RE` reject `:`). Use `COMPOSITE_<hash>`.
2. There is **no `register_factor` symbol** — use
   `FactorRepository(factor_root).save(FactorDefinition(...))`.
3. Materialize via the store's own `_resolve_factor_paths`, not a hand-built
   `overlay_root/composite_id` dir, or `cache_only` reads 0 rows → empty
   schedule. See design §11 + Review-resolutions RF-1/RF-2/RF-3.

## Contract, boundaries, verification (from `AGENTS.md` / `docs/agent_entrypoint.md`)

- **Read order first:** `AGENTS.md` → `docs/agent_entrypoint.md` →
  `docs/architecture.md` → `docs/integration_workflow.md` →
  `docs/full_integration_test_prompt.md` → `docs/WORKING_STATE.md`. State a read
  receipt before editing.
- **Boundaries:** local-first, config-driven, public-safe. No non-public
  providers/paths/secrets in tracked files. Factor defs under `factor_root`; data
  under `data_root`; artifacts under `artifact_root`. Call typed workbench
  services; do not write source-of-truth paths directly.
- **Architecture note:** `architecture.md:170` says synthesis must be a *separate
  workflow* — this design complies; **update `architecture.md`** to document the
  synthesis module + endpoints as part of P5.
- **Verification gate (every phase):**
  ```bash
  python3 -m pytest
  PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
  git diff --check
  ```
  Web/data/factor-cache change ⇒ also drive the live server per
  `docs/full_integration_test_prompt.md`. Per-phase tests named in design §13
  (`test_ties.py`, `test_double_decay.py`, `test_materialize.py`,
  `test_composite_id.py`, `test_grid_fidelity.py`, contract tests).

## Coordination / ownership

`docs/WORKING_STATE.md` (stale, phase-A era) assigns `backtesting/**` and web
files to Codex. **P2 edits `backtesting/service.py`** — additive only (new
mergesort tie-break, skip-stub, `rebalance_indices` helper), no behavior change
to the single-factor path beyond the deterministic sort re-baseline. Confirm the
write lane / coordinate before landing P2; P1/P3/P4/P5 are new files + web
surface and don't contend.

## Acceptance criteria

1. On the live server against mounted `cn_a` data, selecting ≥2 registered
   factors + a method + standardization enables **合成并回测**; clicking it POSTs,
   a job runs, and the report renders (composite hero, provenance card with
   coverage-by-role, backtest section, validity banner). No console errors.
2. `equal_weight` and `weighted` both produce a coherent backtest; directions ±1
   respected; coverage honest (`null` where unobservable).
3. Full `pytest` green (with the new tests); CLI `--help` ok; `git diff --check`
   clean; release scan green.
4. `is_fitted:false` everywhere (no fitted method runnable yet); reserved fitted
   methods visible-but-disabled in the FE.

## Suggested execution

Per the owner's standing preference, drive each phase with the **Workflow tool**
(ultracode): plan → implement → adversarially verify against real `file:line` →
gate. Land P1 first (no engine changes; immediately un-breaks the FE method
form), then P2→P5. Keep the design doc's §12 invariant checklist as the
verification target.

## Git policy — OWNER TO DECIDE at sequencing time

Not fixed yet (owner is bundling this with other requirements). Options on the
table: new branch `fable/phase-d-synthesis-backend` + atomic per-phase commits
(Phase-C pattern) · current branch + atomic commits · working-tree only, no
commits. Default recommendation: **new branch, atomic per-phase commits, not
pushed** (merge needs owner approval), consistent with prior phases.
