# Quantitative Core Audit — Phase A

- **Phase:** A (quant core review, fix, optimize)
- **Branch:** `worktree-fable+phase-a-quant-core-audit` (protocol name
  `fable/phase-a-quant-core-audit`; rename pending — see Limitations)
- **BASE_BRANCH:** `main`
- **BASE_SHA:** `793152289c0613537fa9c727e90fb36d381298d3` (`7931522`,
  "Rebuild backtest/RD evaluation and remediate first-principles review (#12)")
- **Date:** 2026-07-05
- **Author:** Fable (orchestrator). Method: direct full-file source audit.
  Subagent fan-out (Claude/Codex read-only reviewers) was attempted repeatedly
  and blocked by a platform-wide tool-safety-classifier outage; see Limitations.
- **Prior evidence:** this audit builds on
  `docs/first_principles_review_20260703.md` (~34 findings) and an independent
  re-verification of every finding against the `fa8f310` tree, which was
  squash-merged to `main` as `7931522` (verified via reflog: `03e33c8` →
  ff-pull → `7931522`; the audited tree IS the base tree).

## 0. Step A0 baseline status

| Gate | Status |
| --- | --- |
| Full `pytest` | **BLOCKED — not run this session** (Bash classifier outage). Last recorded at this exact tree: 394 passed (`docs/first_principles_review_20260703.md` §10, WORKING_STATE handoff log 2026-07-03). CI (`.github/workflows/ci.yml`) enforces pytest + release scan + `git diff --check` on push/PR for py3.11/3.12. |
| CLI `--help` smoke | BLOCKED — same. Last recorded: ok. |
| `release_safety_scan.py` | BLOCKED — same. Last recorded: green. |
| `git diff --check` | BLOCKED — same. Last recorded: clean. |

Per protocol, **no large-scale code modification happens before the baseline
is re-run in this worktree.** Documentation-only commits are exempt. The
moment Bash recovers, Step A0 runs first and this table is updated.

## 1. Step A1 — Review map

### 1.1 Module map (verified layering; no upward imports found)

| Layer | Module | Role | Risk rating |
| --- | --- | --- | --- |
| Adapters | `apps/cli/main.py` | CLI entry | Low |
| Adapters | `apps/web/server.py` (~3.4k LOC) | Web UI + JSON API + job manager | **High (god file; COR-4 consumer gap lives here)** |
| Adapters | `mcp/read_models.py`, `agent_workspace/` | read-only agent surfaces | Medium (DIV-1 wiring gap) |
| Orchestration | `workbench/service.py` | use-case facade | Low |
| Orchestration | `research_loop/` (13 modules; `service.py` ~2.4k LOC) | bounded RD loop | **High (god file; historical COR-1/2/3 lived here)** |
| Quant kernel | `evaluation/service.py` | IC/ICIR/HAC, splits, embargo | Medium (correct today; highest blast radius) |
| Quant kernel | `backtesting/service.py` (~1.2k LOC) | period backtest, NAV, costs, segments | **High (NEW-1/NEW-2 open)** |
| Quant kernel | `factor_engine/{formula_parser,executor,signal_processing,value_store}` | safe AST, score prep, cache | Medium |
| Registry | `factor_library/{repository,catalog,classification}` | factor_root CRUD + mounted catalog | Low (CAT-1/2/3 fixed) |
| Registry | `operator_registry/` | canonical operator YAML + resolver | Low |
| Data | `data/local.py` | panel provider, validation, demo/snapshot | Medium (NEW-4/NEW-5 open) |
| Contracts | `core/contracts.py` | DTOs, MetricValue, SimulationProfile | Low |

### 1.2 Factor evaluation path

`panel (data/local)` → `prepare_factor_scores_result` (signal_processing;
EWMA decay, test-period truncation, value-store reuse) →
`evaluation/service.py`: forward return via per-instrument
`shift(-execution_delay)` / `shift(-(execution_delay+horizon))` → daily
Spearman rank IC → `_ic_summary` (HAC/Newey-West t-stat, degenerate guards)
→ IS/OOS1/OOS2 chronological splits (50/30/20) with **embargo =
execution_delay + horizon** dropped from every non-final split tail
(QUANT-1 fix, verified).

### 1.3 Factor backtest path

Same score prep → `backtesting/service.py`: signal at close(T), entry
close(T+delay), exit close(T+delay+H), non-overlapping periods; long/short
quantile legs; daily mark-to-market NAV for drawdown; costs =
traded_notional × (commission+slippage) bps + annualized borrow pro-rata;
initial-build vs rebalance turnover separated; annualization reportable only
at ≥126 exposure days; partial final period included by default but flagged
(`PARTIAL_FINAL_PERIOD`, COR-7 deferred decision).

### 1.4 Strategy/portfolio backtest path

**Does not exist by design** (single-factor L/S only; no order book, no
matching, no cash management, no leverage). Scope boundary confirmed in
`docs/architecture.md`. Anything portfolio-level is Phase B+ design surface,
not a Phase A defect.

### 1.5 Key state & caches

- `factor_values_root` (read base) + overlay (write) — fingerprint-keyed
  score cache; reuse governed by formula/universe/profile suffix hash
  (`simulation_profile_suffix`).
- `artifact_root` — evaluation/backtest/RD artifacts + trace JSONL.
- RD dedup signatures (`_result_signature_values`) — prevents re-trying
  identical candidates (COR-1 fix).
- Web job manager (in-process) — run/cancel state for long RD jobs.

### 1.6 High-risk modules (ranked)

1. `backtesting/service.py` — NEW-1 (delisting), NEW-2 (segment embargo).
2. `apps/web/server.py` — COR-4 consumer half + god-file coupling.
3. `research_loop/candidate_gate.py` — NEW-3 (silent pass on missing OOS).
4. `data/local.py` — NEW-4/NEW-5 silent fallbacks.
5. `research_loop/service.py` — size/coupling (no open correctness finding).

## 2. Step A2 — Review evidence base

Full re-verification of the prior register against this tree (summary; per
finding detail is in `docs/first_principles_review_20260703.md` §4–§5,
§10):

- **Confirmed FIXED (26):** SEC-1..5, COR-1/2/3/5/6/8/9/10/11/12, CAT-1/2/3,
  DATA-1, QUANT-1, HARDEN-1, CFG-1(raise half), DIV-1(service half),
  OSS-1/2/3.
- **Confirmed OPEN:** COR-4 (consumer half), DIV-1 (agent wiring half),
  COR-7 (deferred product decision), AGENT-1 / OSS-5 (owner decisions),
  CFG-1 (logging half, unverified), COR-13 (deferred).
- **New findings this audit:** NEW-1..7 below.

## 3. Step A3 — Graded findings register

No P0 (result-invalidating / hard look-ahead) findings are open on
BASE_SHA. Evaluation-side look-ahead, split leakage, and rolling-op
nondeterminism were all fixed and re-verified.

### P1 — material logic errors / backtest bias

| ID | Location | Finding | Impact | Correct behavior | Fix plan | Tests needed | Product decision? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **A-P1-1** (NEW-1) | `backtesting/service.py` formation join (~L145-149) + `_leg_cumulative_returns` (~L833-862) | Survivorship conditioning: period formation requires a price at BOTH entry and scheduled exit (uses information not available at entry); NAV mean silently drops names that stop quoting mid-period, so delisting losses never realize | Upward return bias on real data with delistings/suspensions; invisible on gap-free demo data | A name absent at exit realizes its last available close (or −100% on true delisting); NAV keeps last-known mark until resolution; emit a warning count | Realize last-available-price return for names lost mid-period; add `delisted_positions` count to result; keep formation conditioned only on entry-date availability | Fixture with a name that stops quoting mid-period at a loss; assert leg return reflects it and a warning is emitted | No |
| **A-P1-2** (NEW-2) | `backtesting/service.py` `_split_rows_by_signal_date` (~L1152-1169) | Backtest segment (IS/OOS) splits have **no embargo/purge**, asymmetric with the evaluation-side QUANT-1 fix; IS periods realize returns inside OOS1's calendar | IS segment metrics mildly OOS-contaminated; RD OOS-decay gates consume these `segment_metrics` | Same purge as evaluation: drop signals whose realization window crosses the split boundary (embargo = delay + holding) | Reuse `_split_dates(embargo=...)` logic or exclude straddling periods from the earlier segment | Boundary fixture: signal near split edge must not count in IS | No |
| **A-P1-3** (NEW-3) | `research_loop/candidate_gate.py:59-72,160-178` | OOS gate clauses skip silently when segment values are `None` — a candidate with NO OOS observations passes `min_oos_net_annualized_return` and decay gates without warning | Promotion-quality evidence overstated; compounds INT-5 (high-IC/negative-return winners) | Configured OOS gate + no OOS evidence ⇒ explicit blocking reason or at minimum a warning (`INSUFFICIENT_OOS_EVIDENCE`) | Add evidence-presence check when the corresponding gate threshold is configured | Gate test: OOS thresholds set + segments absent/None ⇒ blocked (or warned per config) | Mild (block vs warn default) |
| **A-P1-4** (COR-4 consumer) | `apps/web/server.py` (~L1401, ~L2822 et al.), report renderer | UI/report read flat scalar `rank_icir` etc. instead of the `qf.metrics.v2` map; `insufficient_sample`/null metrics can render as `0.00` | Users see fabricated-looking zeros; erodes the null-not-zero contract (risk R006) | Prefer `metrics` map; render status badges for non-`available` values | Point all consumers at the MetricValue map with status-aware formatting | Web/report test: insufficient-sample eval renders status, not 0.00 | No |

### P2 — architecture / stability / maintainability

| ID | Location | Finding | Fix plan |
| --- | --- | --- | --- |
| A-P2-1 (NEW-4) | `data/local.py:224-225` | Snapshot loader hardcodes `is_st=False`, `market_cap` fillna(1.0) — silent fallbacks make ST filters no-ops and distort cap ranks | Surface `is_st_unavailable` / missing-cap ratio through `DataValidationResult`; warn, don't invent |
| A-P2-2 (NEW-5) | `data/local.py:228-236` | Derived `return_1d/5d`, `volatility_5d` fillna(0.0) — warmup zeros enter cross-sectional ranks as real data | Leave NaN (nan_policy=drop already handles them) |
| A-P2-3 (DIV-1 half) | `agent_workspace/tools.py` | Agent evaluations don't pass matrix/splits config → agent results diverge from CLI/Web for the same factor | Thread rd-config matrix/splits through the agent tool |
| A-P2-4 (COR-7) | `backtesting/service.py` (`include_partial_final_period=True`) | Partial final period on by default; flagged but included in cumulative | Deferred; revisit default with user (product decision) |
| A-P2-5 | `apps/web/server.py` (~3.4k LOC), `research_loop/service.py` (~2.4k LOC) | God files; the three worst historical bugs lived here | Decompose behind existing seams — Phase A only if time permits after P1s; otherwise Phase B |

### P3 — low-risk improvements

| ID | Location | Finding |
| --- | --- | --- |
| A-P3-1 (NEW-6) | `evaluation/service.py:335-343` | Naive (non-HAC) t-stat can still explode for near-constant IC with tiny mean; primary HAC stat is guarded — diagnostic only |
| ~~A-P3-2 (NEW-7)~~ | `core/contracts.py:116` | **CLEARED 2026-07-05:** `SimulationProfile.__post_init__` enforces `0 < top_quantile <= 0.5`; leg overlap impossible. Also confirmed `execution_delay_days >= 1` enforced at contract level (L114) — same-close execution is structurally impossible. Non-issue. |
| A-P3-3 (CFG-1 half) | `config.py` | Unknown-key logging half of CFG-1 unverified |

## 4. Fix order (Step A4 queue, test-first)

1. A-P1-2 segment embargo (small, isolated, high leverage for RD gates).
2. A-P1-3 missing-OOS gate evidence (small, pure logic).
3. A-P1-1 delisting realization (medium; touches formation + NAV; needs a
   careful fixture).
4. A-P1-4 metrics-map consumers (medium; web + report surface).
5. A-P2-1/A-P2-2 data fallbacks (small).
6. A-P2-3 agent wiring (small).

Each lands as its own atomic commit with a failing-then-passing regression
test, per protocol.

## 5. Limitations (this session)

- **Platform outage:** the tool-safety classifier (`glm-5.1[1m]`) is down;
  `Bash` and `Agent` (subagent spawn) calls fail; `WebFetch`'s internal model
  (`glm-5-turbo`) is also down. Consequences: (1) Step A0 baseline not yet
  re-run — carried state documented above; (2) multi-reviewer read-only
  fan-out (Step A2 protocol) executed as a single-reviewer Fable audit; the
  independent Claude/Codex cross-reviews (Step A6) remain MANDATORY before
  Phase A acceptance; (3) QuantGPT / RD-Agent GitHub repos unreachable —
  cross-project matrix records this limitation instead of guessing; (4) the
  local `quant-forge-studio` branch (`fef3832`) needs git plumbing (Bash) to
  read — comparison deferred, not fabricated.
- **Branch naming:** worktree tooling forced branch name
  `worktree-fable+phase-a-quant-core-audit`; will be renamed to
  `fable/phase-a-quant-core-audit` when git commands are available. Base is
  exactly BASE_SHA (verified via `.git/refs/heads/...`).
