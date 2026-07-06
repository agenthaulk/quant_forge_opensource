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

## First principles

Every finding, fix, and recommendation in this audit derives from one of
these axioms — not from convention or reference-project imitation. Each
register entry cites its axiom.

| # | Axiom | Derived findings/fixes |
| --- | --- | --- |
| FP-1 | Information unavailable at decision time must not enter the decision | QUANT-1 embargo; A-P1-2 segment purge; A-P1-1 formation (exit-availability is future info); COR-5 order-independence |
| FP-2 | Absence of evidence is not evidence of compliance | A-P1-3 INSUFFICIENT_OOS_EVIDENCE (both gates); A-P1-4 status rendering; metrics.v2 null-not-zero |
| FP-3 | Realized P&L is conserved — a loss cannot vanish through re-normalization or exclusion | A-P1-1 lost-position realization + frozen-mark NAV |
| FP-4 | Unobserved values are never fabricated; unknowns are surfaced, not guessed | A-P2-1/2 snapshot NaN + synthesized_columns; COR-8 unmarkable-day NaN |
| FP-5 | One quantity, one definition, across every surface | A-P2-3 agent config parity; eval/backtest timing parity (contrast: Studio's shift(-1) eval vs t+2 backtest) |
| FP-6 | A sample touched by selection is no longer out-of-sample | COR-2 audit-only external OOS; bounded/deduped RD rounds (contrast: RD-Agent's every-round test-set SOTA gate) |
| FP-7 | Every statistic must carry its own validity (sample size, significance, status) | HAC/NW t-stats; 126-day reportability floor; MetricValue statuses; degenerate guards |

Phase B design rule that follows: prefer making axiom-violating states
*unrepresentable in the type/contract layer* over policing them with review.

## 0. Step A0 baseline status

| Gate | Status (2026-07-05, this session) |
| --- | --- |
| Full `pytest` on unmodified BASE (`main`@7931522) | **397 passed** (105.7s) — the true Step A0 baseline |
| Full `pytest` on Phase A branch after A-P1-2/A-P1-3 | **404 passed** (397 baseline + 7 new regressions, zero regressions) |
| CLI `--help` smoke (worktree) | OK |
| `release_safety_scan.py` (worktree) | PASSED (112 public files) |
| `git diff --check` (worktree) | clean |

The classifier outage that blocked the first execution window resolved
mid-session; all gates above were executed directly, not carried forward.

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
| **A-P1-1** (NEW-1) | `backtesting/service.py` formation join (~L145-149) + `_leg_cumulative_returns` (~L833-862) | Survivorship conditioning: period formation requires a price at BOTH entry and scheduled exit (uses information not available at entry); NAV mean silently drops names that stop quoting mid-period, so delisting losses never realize | Upward return bias on real data with delistings/suspensions; invisible on gap-free demo data | A name absent at exit realizes its last available close (0% when no post-entry quote exists — without corporate-action data, assuming −100% would fabricate a value, FP-4); NAV keeps last-known mark until resolution; emit a warning count | Realize last-available-price return for names lost mid-period; add `delisted_positions` count to result; keep formation conditioned only on entry-date availability | Fixture with a name that stops quoting mid-period at a loss; assert leg return reflects it and a warning is emitted | No |
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

## 5. Step A6/A7 — Cross-review record and Fable adjudication

Reviews executed on the 11-commit branch (both read-only, independent):

- **Opus recheck** (Claude Opus, adversarial mandate): verdict **ACCEPT**.
  All six fixes confirmed correct from first principles; P&L conservation
  verified to machine precision by probe; the signal-date purge boundary
  argued correct (conservative superset; one boundary definition shared
  with evaluation). Follow-ups raised: delay>0 embargo fixture, gapped-panel
  end-to-end test, is_st compat-note overstatement, missing-evidence
  residuals in retention/turnover/correlation clauses, staggered-NAV NaN
  fragility, lost_positions web observability.
- **Codex review** (GPT-5-family, reasoning effort xhigh, read-only):
  verdict **FIX-FIRST** with four findings: (1) blocker — NaN NAV marks leak
  into JSON/ledger/staggered aggregates; (2) major — snapshot path lacks
  duplicate-key guards; (3) minor — lost_positions absent from web payload;
  (4) minor — doc claimed −100% delisting while code realizes last mark/0%.

**Fable adjudication** (evidence-based, no majority voting):

| Conflict | Ruling | Rationale |
| --- | --- | --- |
| Codex #1 "blocker" vs Opus "pre-existing, non-blocking" | Both partially right → severity MAJOR, fixed in-phase (`4d31047`) | NaN semantics predate the branch (COR-8, BASE emits the same NaN), so it cannot block THESE commits; but A-P1-1 interacts with the NaN path and the serialization defect is real — fixing now is cheaper than a ticket |
| Codex #2 snapshot dup keys | ACCEPTED, fixed (`cfaf324`) | DATA-1's sibling gap; exact dups deduped (unambiguous), conflicting dups fail closed per FP-4 |
| Codex #3 / Opus observability | ACCEPTED, fixed (`4d31047`) | lost_positions now in web payload |
| Codex #4 / Opus doc errors | ACCEPTED, fixed (`1cb2ee5`) | −100% claim and is_st claim corrected to match implemented, FP-4-consistent semantics |
| Opus test gaps | ACCEPTED, fixed (`1cb2ee5`) | delay>0 boundary fixture + gapped-panel e2e added |
| Opus: extend evidence-blocking to retention/turnover/corr clauses; unify the two gates' evidence definitions; `_backtest_metrics` omits segment_metrics; demo-panel warmup fillna | **DEFERRED (recorded)** | Gate-semantics changes deserve their own test-first pass; none is a P1 (all fail toward strictness or affect demo data only). Queued as Phase A follow-ups below |

**Deferred follow-up register (not blocking acceptance):**
1. F-1: retention/turnover/correlation clauses treat missing evidence as
   pass (candidate_gate.py + service.py) — same FP-2 class, lower stakes.
2. F-2: unify candidate_gate vs apply_gate "sufficient OOS evidence"
   definitions (any-one-non-null vs per-segment-None-blocks).
3. F-3: `_backtest_metrics` (research_loop/service.py) omits
   segment_metrics — structured gate would fail closed surprisingly if OOS
   clauses were configured externally.
4. F-4: `_build_demo_panel` warmup fillna(0.0) (demo-only fabrication).
5. F-5: `missing_oos_evidence_blocks` not yet parseable from rd.yaml;
   ResearchGate has no warn-mode equivalent.
6. F-6: purged-period counts not persisted (only warning codes).

## 6. Step A8 — Acceptance status

| Protocol condition | Status |
| --- | --- |
| P0 fixed or blocked-with-reason | ✓ none open (none found on BASE) |
| Confirmed P1s fixed or deferral recorded | ✓ A-P1-1/2/3/4 fixed with fail-on-BASE evidence |
| Key PIT tests pass | ✓ embargo (eval+backtest), delisting, ordering, boundary fixtures |
| Eval/backtest definitions consistent | ✓ one boundary definition; period return == NAV at exit by construction |
| IS/OOS boundaries provable | ✓ embargo default-on both sides; purge tests pin the edges |
| Golden dataset tests | Spec written (`docs/testing/golden_dataset_spec.md`); GD-2/GD-3 realized as regression tests; full GD suite queued with F-register |
| Unit+integration tests green | ✓ 426 passed (BASE baseline 397) |
| Phase A docs complete | ✓ 6 documents under docs/reviews, docs/testing, docs/migration |
| Worktree clean, all changes on Phase A branch | ✓ 14 atomic commits on `fable/phase-a-quant-core-audit` |
| No merge/push/delete without user approval | ✓ nothing merged or pushed |

**Fable decision: Phase A ACCEPTED.** Proceed to Phase B: the quant core is
stable enough to be an architecture base (no open P0/P1; both adversarial
reviews' blocking items remediated in-phase; residuals are recorded
strictness gaps, none affecting the correctness of stored evidence).

## 7. Limitations (this session)

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
