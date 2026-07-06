# Cross-Project Discrepancy Matrix — Phase A

Projects in scope: Quant Forge OpenSource (QF_OS, primary), Quant Forge
Studio (local branch `quant-forge-studio` @ `fef3832`, read-only),
QuantGPT (github.com/Miasyster/QuantGPT), RD-Agent
(github.com/microsoft/RD-Agent).

## Access status (2026-07-05) — RECORDED LIMITATION, NOT GUESSED

| Project | Access route | Status |
| --- | --- | --- |
| QF_OS | local worktree | FULL (audited) |
| Quant Forge Studio | local branch `fef3832`; needs `git show`/worktree (Bash) | **BLOCKED this session** — Bash classifier outage. Secondary evidence exists in `design/agent_develop/0*.md` (a prior Studio-informed port/owner analysis); used ONLY where labeled, pending primary re-read. |
| QuantGPT | GitHub via WebFetch | **BLOCKED this session** — WebFetch backend model outage. No rows fabricated. |
| RD-Agent | GitHub via WebFetch | **BLOCKED this session** — same. No rows fabricated. |

Per protocol §A.4: reference projects are NOT assumed correct; every future
row must be classified as naming / implementation / positioning / genuine
quant conflict / bug / needs-verification.

## Matrix (QF_OS column filled from primary audit; others pending access)

| Dimension | QF_OS (verified) | Studio | QuantGPT | RD-Agent |
| --- | --- | --- | --- | --- |
| Factor representation | formula DSL over canonical operator registry; safe AST executor; FactorDefinition dataclass (id/formula/horizon/status) | TBD | TBD | TBD |
| Factor generation | NL idea → LLM parse → validated draft; bounded RD candidate loop with dedup signatures | TBD | TBD | TBD |
| Factor evaluation | daily rank IC, HAC/NW t-stat, ICIR, IS/OOS1/OOS2 with purge/embargo, horizon matrix, `qf.metrics.v2` null-not-zero | TBD | TBD | TBD |
| Factor screening | candidate gates (score/coverage/IC/ICIR/return/sharpe/corr/turnover/OOS decay); NEW-3 gap: missing OOS evidence passes silently | TBD | TBD | TBD |
| Backtest | non-overlapping H-day close-to-close L/S quantile legs, daily NAV, cost model, segment metrics (NEW-2 gap: no segment embargo) | TBD (Studio has strategy-backend ambitions per design corpus — needs primary verification) | TBD | TBD |
| Strategy research | out of scope by design (single-factor workbench) | TBD | TBD | TBD |
| Agent workflow | read-only MCP catalogs + agent_workspace tools; bounded RD loop is the only autonomous loop | TBD | TBD | TBD |
| Experiment feedback | trace JSONL + report MD; gate feedback into next RD round | TBD | TBD | TBD |
| Result storage | file-first: factor_root / artifact_root / values overlay | TBD | TBD | TBD |
| Overfitting defenses | chronological splits + embargo, external OOS audit-only, dedup, bounded rounds, temp 0.0 | TBD | TBD | TBD |
| Human-in-the-loop | candidate→active promotion is human-gated | TBD | TBD | TBD |

## Fill plan

1. When Bash recovers: `git show quant-forge-studio:<path>` sweep (read-only;
   the branch itself is never checked out or modified, honoring
   WORKING_STATE non-goal "quant-forge-studio is read-only").
2. When WebFetch recovers (or `gh`/network via Bash): pull QuantGPT and
   RD-Agent module trees + key quant files; fill columns with file-level
   citations, then classify each discrepancy per protocol §A.4.
3. Comparison priorities: (a) RD-Agent's research-loop feedback vs QF_OS RD
   gates (overfitting defenses), (b) Studio strategy-backtest semantics vs
   QF_OS BacktestPort contract (they must not silently disagree on return
   definitions), (c) QuantGPT factor-expression semantics vs operator
   registry canon.
