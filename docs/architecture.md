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
| `quant_forge.agent_workspace` | Safe agent tool facade. Proposals are returned as data and are not persisted directly. |
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
   the read-base store.
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

## Signal Preparation

Evaluation, backtesting, web workflows, and RD all call the same lightweight
signal preparation entrypoint. First-version responsibilities are deliberately
small:

- validate the effective `SimulationProfile`;
- apply the configured `test_period` to local panel rows;
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
CLI `eval-factor`, local web idea parsing, and RD runs all use the same
configured horizon matrix and split definitions when an RD config is supplied.

## Research Loop Semantics

The public RD loop is a local research triage tool. It is not a production
worker, not a trading scheduler, and not an automatic `active` promotion system.

RD has two separate local workflows:

- `research`: optimize or propose factors through bounded research ideas plus
  optional hyper-parameter/profile search.
- `factor_synthesis`: combine multiple existing factors through the Campaign
  workflow. Campaign is not part of ordinary idea optimization.

The two workflows intentionally share scoring, gates, trace writing, and report
formatting, but they do not share generation semantics. Research accepts one
seed factor and produces idea/profile variants. Campaign accepts multiple seed
factors and produces composite/synthetic candidates.

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
未指定 for the public clean branch.

If research proposes a formula that needs an unknown operator, the run is marked
`requires_operator_draft_review`. The workbench writes draft artifacts under
`artifact_root/operator_drafts/<draft_id>/`, including an operator stub,
manifest, example formula, generated test requirements, and audit status. Draft
operators are never imported or executed until Codex/developer review promotes
them into the formal public operator set.

RD weights, gate thresholds, cost assumptions, allowed schedule intervals, and
bounded parameter-search profile variants are loaded from `configs/rd.yaml` by
default in the release docs, or from a user-supplied `--rd-config` file at
runtime. Gates can remain permissive for smoke demos or be tightened against
OOS net return, rebalance rate, turnover rate, net/gross retention, and OOS
decay. The public parameter search supports `full_grid` and
`successive_halving`; successive halving is a two-stage budget strategy, not
reinforcement learning.
