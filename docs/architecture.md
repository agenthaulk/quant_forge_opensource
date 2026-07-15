# Quant Forge OpenSource Architecture

Quant Forge OpenSource is a local factor workbench with one public kernel and
thin orchestration surfaces.

```text
CLI / Local Web / Agent Tools / MCP
              |
   Workbench / Research Loop
              |
   +----------+-----------+
   |          |           |
Factor    Evaluation   Backtesting
Library       |           |
   |          +-----+-----+
   |                |
Factor Engine <----+
   |
Local Data Provider
```

## Modules

| Module | Responsibility |
| --- | --- |
| `quant_forge.config` | Load config and resolve user-supplied paths. |
| `quant_forge.data` | Local panel data contract, demo data generation, validation. |
| `quant_forge.factor_library` | `factor_root` source of truth, mounted precomputed factor discovery, and factor-value store normalization. |
| `quant_forge.factor_engine` | Safe formula execution, mounted daily factor-value reuse, and shared score preparation over local panels. |
| `quant_forge.evaluation` | Deterministic single-factor metrics. |
| `quant_forge.backtesting` | Lightweight next-day factor backtest. |
| `quant_forge.research_loop` | Local RD loop: hypotheses, candidate scoring, smoke gates, Markdown reports, and in-process web scheduling. |
| `quant_forge.workbench` | Use-case orchestration. |
| `quant_forge.mcp` | Read-only catalogs for agents and LLM tooling. |
| `quant_forge.agent_workspace` | Safe agent tool facade. Factor proposals are returned as data (not persisted) and promotion is refused; `evaluate_factor` writes a standard evaluation artifact under `artifact_root`, identical to the CLI/Web path. Artifact growth is bounded by the retention policy (`scripts/prune_artifacts.py`). |
| `quant_forge.apps` | CLI and local web adapters only. |

## Public Boundary

There are no non-public platform modules in this branch. Public code must not
reference non-public providers, hosted deployments, credentials, PostgreSQL, or
local machine paths.

## Data Flow

1. `qf init` writes demo parquet data and demo factor definitions.
2. `qf idea-to-factor` creates a draft factor under `factor_root`.
3. Evaluation and backtesting load factor definitions through `FactorCatalog`,
   which merges local `factor_root` entries with mounted `factor_values_root`
   manifests.
4. The factor engine reuses complete daily factor values from
   `factor_values_root/{原始因子,合成因子}/factor_id=<FACTOR_ID>`. If
   `factor_values_overlay_root` is configured, missing local formula dates are
   written to overlay incremental sidecars under the same category instead of
   the read-base store. Without an overlay, missing dates are computed for the
   current run but are not written back to `factor_values_root`.
5. The factor engine compiles formulas only when cached values are incomplete or
   unavailable; shared signal preparation applies the effective simulation
   profile.
6. Artifacts are written under `artifact_root`.
7. `qf research run-once` can generate bounded hypotheses from a seed factor,
   evaluate/backtest candidates, score them, and move only smoke-gate-passing
   factors to `candidate`.
8. The same run writes a local Markdown research report under
   `artifact_root/research_reports`.

## Backtest Semantics

Signals are generated on `signal_date`. The earliest executable date is the
configured `execution_delay_days`, defaulting to the next trading day recorded
as `entry_date`. The lightweight backtest uses the factor definition's
`horizon_days` as the default non-overlapping holding period. Period returns
are computed from `entry_date` close to `exit_date` close, where `exit_date` is
`holding_days` trading days after entry.
Precomputed multi-day forward returns are not reused as daily compounded
returns.

The public backtest also emits FactorLab-style research diagnostics:

- quintile group returns, with Q1 as the lowest factor scores and Q5 as the
  highest factor scores;
- gross and net long-short metrics from non-overlapping period returns;
- rebalance rate from long/short membership changes per rebalance;
- turnover rate from portfolio weight changes;
- IS/OOS1/OOS2 backtest segments for return, Sharpe, and drawdown;
- configurable research cost assumptions for commission, slippage, and short
  borrow cost.

Rebalance rate is not true traded turnover. Turnover rate and net
returns are still local research estimates, not production execution results.
The main return series remains non-overlapping H-day close-to-close period
returns. Backtest artifacts also include a daily mark-to-market NAV that holds
the entry-date long/short baskets through each completed period. Reportable
max drawdown comes from this daily NAV instead of period endpoints.

Annualized return, volatility, and Sharpe are reportable only when exposure
history is sufficient. Short-window mechanical annualization is retained as an
`extrapolated_annualization` diagnostic, while primary annualized fields are
`null` with `INSUFFICIENT_ANNUALIZATION_HISTORY` when the sample is too short.
Initial build turnover is recorded separately from rebalance turnover: the
initial build still incurs transaction costs, but it is not included in ongoing
rebalance turnover averages.

## Signal Preparation

Evaluation, backtesting, web workflows, and RD all call the same lightweight
signal preparation entrypoint. First-version responsibilities are deliberately
small:

- validate the effective `SimulationProfile`;
- apply the configured `test_period` to local panel rows;
- require at least 126 daily trading dates for displayable evaluation metrics;
- allow shorter holding-period backtest holdouts when at least one entry/exit
  path is possible, while surfacing short-window warnings;
- execute the factor formula and factor-owned universe filters;
- apply EWMA score decay when `decay_days > 1`;
- support only `nan_policy: drop`, `neutralization: none`, and `truncation:
  null`.

Neutralization and truncation are intentionally not implemented in this public
first version. Unsupported values fail fast instead of silently changing scores.

## Evaluation Semantics

Evaluation keeps the original whole-sample Rank IC, ICIR, and coverage fields
and adds a configurable FactorLab-style evidence matrix:

- default horizons: 5, 10, 21, and 63 trading days;
- default chronological sample splits: IS/OOS1/OOS2 at 50%/30%/20%;
- split metrics carry independent score weights, defaulting to 0.5/0.3/0.2.

Near-zero IC standard deviation is treated as zero ICIR to avoid falsely large
scores from numerical noise.
Metric artifacts use the `qf.metrics.v2` contract for availability-aware
outputs. A metric with insufficient evidence is serialized as `null` with a
stable status such as `insufficient_sample` or `not_applicable`; it is not
serialized as `0.0`. Legacy top-level numeric fields remain as compatibility
aliases, but Web, RD, and reports should prefer the `metrics` map when deciding
whether a value is reportable.

For overlapping forward-return horizons such as H21, the legacy independent
sample statistic is retained as `rank_ic_t_stat_naive`. The primary
`rank_ic_t_stat` uses a Bartlett/Newey-West HAC standard error with default lag
`horizon_days - 1`. Artifacts persist the full daily IC series, HAC lag,
method, coverage lineage, and boundary diagnostics so the statistic can be
independently replayed. If the IC series is constant or numerically
near-constant, HAC t-stat is not reportable and the artifact emits
`DEGENERATE_IC_SERIES` instead of converting a near-zero standard error into an
extreme t-stat.
CLI `eval-factor`, local web idea parsing, and RD runs all use the same
configured horizon matrix and split definitions when an RD config is supplied.
The RD config may also define role-specific profiles:
`evaluation.simulation` controls factor testing and IC/ICIR evidence, while
`backtest.simulation` controls the holding-period backtest. If those sections
are omitted, both roles fall back to the legacy `simulation` profile. Runtime
RD trials consume an already-resolved effective trial config: parameter-search
overlays carry only explicitly configured search fields, so disabled or partial
search grids do not overwrite role-specific profiles.

## Research Loop Semantics

The public RD loop is a local research triage tool. It is not a production
worker, not a trading scheduler, and not an automatic `active` promotion system.

RD currently exposes the research workflow in the public workbench:

- `research`: optimize or propose factors through bounded research ideas plus
  optional hyper-parameter/profile search.

Factor synthesis is a separate workflow (`quant_forge.synthesis`), not part of
ordinary idea optimization. The multi-factor composite backtest combines two or
more registered factors — per-date cross-sectional standardization (`zscore` or
`rank`), explicit `+1`/`-1` directions locked at request time, an a-priori
combination method with raw declared weights — then materializes the composite
as a synthetic `precomputed:` factor (`COMPOSITE_<hash-of-all-inputs>`, values
written into a per-run overlay through the value store's own path resolution)
and drives the existing holding-period backtest engine by that factor id with
`decay_days` pinned to 0 (members are decayed exactly once, before
combination). The web adapter exposes it through two endpoints:

- `GET /api/synthesis/methods`: the method + standardization catalog as
  schema-driven `ParamSpec` JSON; fitted methods ship reserved
  (`available: false`) until implemented, with `is_fitted` always truthful.
- `POST /api/jobs/multi-factor-backtest`: a background job running
  `run_multi_factor_backtest_workflow`. Every client guard is re-validated
  server-side before the job starts (at least 2 factors, `±1` directions,
  runnable method, weights covering exactly the selected set, REQUIRED
  `holding_days` with no `horizon_days` fallback, one pinned universe across
  members, minimum-window precondition), so bad requests are rejected as 4xx
  JSON instead of failed jobs. The report payload reuses the single-factor
  evaluation/backtest builders and adds `synthesis_provenance` (member
  formulas pinned at run time, coverage by role with real-null ratios) plus a
  `validity` block that states the structural caveats: same-window evaluation
  diagnostics only, non-overlapping cohorts (rebalance cadence equals holding
  period) with the realized period count, target-book cost accounting bias,
  and formation-time-only universe filtering.

One `research run-once` cycle:

1. Loads the seed factor from `factor_root`.
2. Generates a bounded set of readable hypotheses through a generator interface.
3. Parses each hypothesis through the public factor parser.
4. Saves each candidate as a draft under `factor_root`.
5. Builds formula/profile trials from generated candidates and configured
   simulation profile variants.
6. If successive halving is enabled, runs a quick screening stage and keeps
   only the top survivor trials for full evaluation.
7. Runs full evaluation and lightweight backtest with the candidate horizon and
   effective simulation profile.
8. Scores the candidate with explicit objective weights from RD config,
   including weighted split ICIR and net-of-cost backtest metrics by default.
9. Generates a bounded self-review summary with strengths, risks, and next
   hypotheses through the configured review adapter. The default public RD
   config is local; ignored local configs may enable LLM hypothesis/review.
   LLM review payloads use the versioned `qf.rd.llm.v1` JSON contract. If a
   provider omits metadata such as `summary`, local normalization records a
   warning and supplies deterministic text rather than failing the run.
10. Promotes only full-stage smoke-gate-passing candidates to `candidate`.
11. Writes a Markdown report with overview, search trace, comparison table,
   iteration trace, evidence tables, conclusion notes, and risk notes. Numeric
   evidence and promotion decisions remain local; review prose may be
   LLM-generated when RD review mode is `llm`.

`active` promotion remains a separate user decision through `qf factor promote`.
The local web scheduler is an in-process convenience scheduler for one local
server. Long-running distributed workers and non-public platform rebinding remain
unspecified for the public clean branch.

If research proposes a formula that needs an unknown operator, the run is marked
`requires_operator_draft_review`. The workbench writes draft artifacts under
`artifact_root/operator_drafts/<draft_id>/`, including JSON/Markdown-only
metadata: manifest, semantics request, generated test requirements, audit
status, and a review note. Draft operators are never imported or executed until
Codex/developer review promotes them into audited source code and the formal
public operator set.

RD weights, gate thresholds, cost assumptions, allowed schedule intervals, and
bounded parameter-search profile variants are loaded from `configs/rd.yaml` by
default in the release docs, or from a user-supplied `--rd-config` file at
runtime. Gates can remain permissive for smoke demos or be tightened against
OOS net return, rebalance rate, turnover rate, net/gross retention, and OOS
decay. The public parameter search supports `full_grid` and
`successive_halving`; successive halving is a two-stage budget strategy, not
reinforcement learning.

## Multi-Factor Synthesis Memory

The multi-factor composite backtest is the most memory-intensive local
workflow. Peak memory scales with panel rows times member count: it holds one
standardized member matrix over the in-window panel, and the engine drive that
follows is the next large allocator. `run_multi_factor_backtest_workflow` builds
that matrix once — standardization, directions, and the per-period rank IC
sweep are each computed a single time and shared between the fitted weight fit
and the advisory redundancy diagnostic — and releases the per-member tidy score
frames as soon as the matrix exists, before it drives the backtest engine, so
the standardized matrix and the engine working set are not both at full size at
the same instant.

A Python-level `MemoryError` fails the job honestly: the background job runner
catches `Exception` (`apps/web/jobs.py`) and records a failed job with a client
error. A container cgroup out-of-memory kill is not the same event — the kernel
delivers `SIGKILL`, which no in-process handler can catch, so the job vanishes
with exit code 137 instead of a JSON error. Size container memory for the
largest full-panel fitted run you intend to serve. Reference observation from
integration testing: a ~3.19M-row `cn_a` panel fitted (`ic_weighted`) run
completed in ~230 s on an unconstrained container; on a memory-tight container
an earlier build that rebuilt the standardized matrix and the forward-return
sweep once per consumer was OOM-killed (exit 137) before it could finish.

## Agent Sidecar and Research Pipelines

The local web workbench runs its research flow through server-owned pipeline
aggregates with an agent sidecar. The design contract lives in the design
docs; the load-bearing invariants are:

- **Two explicit pipeline kinds.** `factor_study` (pipeline A:
  parse → confirm → compute → report) and `rd_optimize` (pipeline B:
  RD confirm → one research job → leaderboard). There is no automatic A→B
  bridge — the report exposes an explicit entry action, seeded from the
  server's published factor id, never from the working row that completion
  cleans up. The legacy auto-cycle RD controls are deleted; RD inherits the
  evaluation period contract and adds no interval parameter.
- **Server-owned aggregates.** Stage state, attempt lineage, input
  fingerprints, and failed/cancelled-attempt disclosure are computed
  server-side and survive restarts through attempt-scoped durable completion
  artifacts. Completion publishes the canonical factor by compare-and-set
  under an advisory publisher lock, records `publish_state` honestly
  (`published` / `declined_promoted` / `conflict`), and cleans working
  artifacts; a cancel that races a completion re-reconciles and never
  overwrites a won completion.
- **Provenance.** The baseline provenance of a pipeline is immutable from
  confirm time; current provenance (including `edited_by=human_override`)
  is derived server-side by fingerprint comparison — never client-asserted.
- **Typed narration.** Sidecar chat renders a typed narration AST; numeric
  values reach the UI only through the canonical renderers via refs — chat
  text is never the sole carrier of a number.
- **Closed tool registry.** The sidecar invokes a closed catalog of read and
  action tools behind the control token plus a per-pipeline capability
  grant; a grant for one pipeline cannot act on another (cross-pipeline
  arguments are rejected server-side before the token gate).
- **Read-only pre-validation.** Formula edits pre-validate through the
  canonical fingerprint and the validation gate with zero side effects; an
  unknown operator yields a deterministic operator-draft review reference
  and never executes. A validated edit the user runs branches a NEW
  immutable run.
- **Availability-honest rendering.** Status-carrying metrics render as
  `n/a` plus status and never enter numeric ranking; the dedup disposition
  discloses duplicates as detected after execution.
- **`api.py` freeze.** New endpoints live in `routing.py` handlers
  delegating to sidecar modules; `api.py` only shrank (dependency direction
  stays routing → api).
- **Planning-influence disclosure seam.** The pipeline record reserves a
  `planning_influence_hash` slot that participates in `input_hash`
  (empty-stable and value-sensitive); the real capture wires up together
  with the self-evolution engine's frozen snapshot contract once both
  tracks are merged.
