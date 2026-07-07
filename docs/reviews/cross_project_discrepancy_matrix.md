# Cross-Project Discrepancy Matrix — Phase A

Projects: Quant Forge OpenSource (QF_OS, primary, audited), Quant Forge
Studio (local branch `quant-forge-studio` @ `fef3832`, read via `git show`
only — never checked out or modified), QuantGPT (Miasyster/QuantGPT),
RD-Agent (microsoft/RD-Agent) — both shallow-cloned read-only.

## Access status (2026-07-05)

| Project | Route | Status |
| --- | --- | --- |
| QF_OS | Phase A worktree | FULL (primary audit) |
| Quant Forge Studio | `git show quant-forge-studio:<path>` | **FULL (read-only scan complete)** |
| QuantGPT | shallow clone in scratchpad | **FULL (module comparison complete)** |
| RD-Agent | shallow clone in scratchpad | **FULL (module comparison complete)** |

Reference projects are NOT assumed correct (protocol §A.4); every row is
classified: naming / implementation / positioning / genuine quant conflict /
bug / needs-verification.

## Matrix (QF_OS vs Studio; QuantGPT/RD-Agent columns pending agent report)

| Dimension | QF_OS (this branch) | Quant Forge Studio (fef3832) |
| --- | --- | --- |
| Factor representation | formula DSL over canonical operator registry, safe AST executor | Python modules (`FACTOR = qf.factor.define(...)` + `compute(ctx)`), tiny pandas ops lib; executed via in-process `exec()` with AST preflight only |
| Factor generation | NL idea → LLM parse → validated draft; bounded RD loop with dedup | agent graph writes factor/strategy Python files; objective recipes + tool whitelists |
| Factor evaluation | daily Rank IC (Spearman), HAC/NW t-stat, ICIR, IS/OOS1/OOS2 with embargo=delay+horizon, horizon matrix, null-not-zero metrics | daily RankIC via `shift(-1)` (same-close-entry label), ICIR = mean/std·√252 unconditionally; **no t-stats/HAC/p-values anywhere**; costs not deducted in eval |
| Splits / leakage control | chronological splits + purge/embargo (both eval and, post-A-P1-2, backtest segments) | **no IS/OOS split, no purge/embargo in code** (own roadmap admits it) |
| Backtest | non-overlapping H-day close-to-close L/S quantile legs, daily NAV, borrow-cost model, lost-position realization (post-A-P1-1) | weight×return constant-mix simulator, monthly/weekly rebalance, next-day-close entry, long-only, costs = turnover×(comm+slip)bps, no borrow/short, no drift between rebalances |
| Delisting/suspension | (post-A-P1-1) missing-at-exit names realize last mark + counted + warned | suspended/delisted holdings earn exactly 0% and are always sellable; `delist_date` never consulted |
| Strategy research | out of scope by design | first-class: strategy Python via SDK, benchmark, contribution diagnostics |
| Agent workflow | read-only catalogs + bounded RD loop, external OOS audit-only | clarify→write→preflight→backtest→evaluate→repair graph; milestones from succeeded tool calls; **no OOS isolation from the agent loop** |
| Experiment feedback/storage | trace JSONL + artifacts under artifact_root | RunManifest with snapshot fingerprint, sha256 artifacts, dependency logs, Postgres metadata (alembic) |
| Overfitting defenses | embargoed splits, external-OOS audit-only, dedup signatures, bounded rounds, INSUFFICIENT_OOS_EVIDENCE gates (post-A-P1-3) | none in code; keyword-gated "analysis" milestone is trivially satisfiable |
| Human-in-the-loop | candidate→active promotion human-gated | factor registration proposals gated on validation runs + human review; immutable versions, deprecate-only |
| PIT data plane | local panel, PIT by construction; snapshot loader (post-A-P2-1/2 honest fallbacks) | `visible_at`-grained finance PIT tables, `latest_visible` resolvers, three-way price-basis split (raw/display/PIT-adjusted), preflight blocking codes |

## §A.4 classification — Studio vs QF_OS

### Genuine quant-logic conflicts (Studio side is wrong or weaker)
1. **Eval/backtest timing inconsistency inside Studio**: IC labels assume
   same-close entry (`shift(-1)`), backtest earns new-weight returns from
   t+2 — factor evidence one day more optimistic than its own backtest.
   QF_OS aligns eval and backtest at delay=1 by contract.
2. **√252-annualized ICIR regardless of as-of frequency**; no significance
   stats at all (QF_OS: HAC t-stat with Bartlett weights, frequency-aware).
3. **Overlapping labels** for horizon>1 diagnostics with no correction
   (QF_OS: non-overlapping periods).
4. **No IS/OOS/embargo** (QF_OS: default-on purge both sides post-Phase A).
5. **Monthly project-factor "IC" is a 1-day-horizon IC** (monthly as-of
   filter but next-day return label).
6. Sharpe = compound annual return ÷ pstdev·√252, no risk-free, population
   std (QF_OS: period-return mean/std with reportability floor).

### Confirmed Studio bugs / risky shortcuts (evidence in scan report)
- Suspended/delisted = 0% + always sellable (engine.py:912, :345).
- Percent-vs-decimal magnitude heuristics can divide a genuine >50% move
  by 100 (engine.py:256-259; factor_matrix per-value rescale).
- `ic_win_rate` counts |IC|>0.02, so strong NEGATIVE-IC days count as wins.
- Comparable-price missing adjustment factors silently coalesce to 1.0.
- `exec()` of agent-written code in-process; AST preflight only.
- Dead engine paths (`windows`/`factor_ids` hardcoded empty).

### Implementation differences (legitimate positioning, not bugs)
- Weight×return simulator + long-only monthly constant-mix vs QF_OS
  single-factor L/S quantile engine — different product scopes.
- Python-module factors + SDK data access vs QF_OS restricted DSL —
  expressiveness vs safety tradeoff (Phase B must reconcile).

### Studio capabilities QF_OS lacks (Phase B adoption candidates)
1. RunManifest: snapshot fingerprint + sha256 artifacts + dependency logs.
2. Market-data-basis contract (PnL basis / execution price / as-of-anchored
   comparable price / display price) enforced by SDK + preflight + report.
3. `visible_at`-grained finance PIT + `latest_visible` resolvers; drop rows
   lacking announcement dates instead of guessing.
4. Structured preflight blocking-code taxonomy + zero-trade quality gate
   feeding a bounded repair loop.
5. Registration governance: validation-run-gated proposals, immutable
   versions with executable snapshots, deprecate-only lifecycle.
6. Report reliability section (passed / passed_with_warnings / blocked).
7. Multi-factor diagnostics (correlation matrix, quantile spreads, top-N
   membership turnover).

### Needs verification later
- Studio factor cache invalidation on snapshot change under concurrent
  writes; remote_compute paths (skipped in scan).

## RD-Agent (microsoft, qlib factor scenario) vs QF_OS

| Dimension | RD-Agent | vs QF_OS classification |
| --- | --- | --- |
| Factor representation | NL spec (name/desc/LaTeX/vars) + LLM-generated Python `factor.py` → HDF5 | positioning difference (codegen vs restricted DSL) |
| Generation | hypothesis loop + CoSTEER evolving coder with cross-run success/error knowledge base; PDF-report factor extraction | capability QF_OS lacks (knowledge-reuse repair; report mining) |
| Evaluation | full Qlib pipeline (IC/ICIR/RankIC/RankICIR, ARR/IR/MDD); fixed 2008-14/15-16/17-20 splits; **no t-stats/HAC**; no embargo | implementation gap on statistics; split hygiene weaker than QF_OS |
| Screening | LLM yes/no SOTA gate ("any small improvement counts") on TEST-period metrics, reused EVERY round; per-date corr ≥0.99 dedup | **genuine quant conflict: iterative test-set mining** — exactly the leak QF_OS's audit-only external OOS is designed to prevent |
| Backtest | Qlib TopkDropout, T+1 label `Ref($close,-2)/Ref($close,-1)-1`, limit_threshold 0.095 | timing consistent with QF_OS delay=1; limit handling delegated to qlib (QF_OS has none — documented scope gap) |
| Overfit defenses | fixed split only; no multiple-testing control | weaker than QF_OS post-Phase A |
| Notable bugs found | bandit metric-name typo (trailing space) silently zeroing ARR reward; bare `except:` leaving `acc_rate` unbound; duplicated LLM call | reference-project bugs, recorded not fixed (out of mandate) |

## QuantGPT (Miasyster) vs QF_OS

| Dimension | QuantGPT | vs QF_OS classification |
| --- | --- | --- |
| Factor representation | Alpha101-style string DSL (~60 ops), Python + Rust evaluators | naming/implementation kin to QF_OS DSL |
| Generation | LLM expression generation; trajectory meta-controller (EXPLOIT/EXPLORE/RECOMBINE/SIMPLIFY); diagnosis-driven directed mutation | capability candidates |
| Evaluation | daily Pearson+Spearman IC, ICIR, monotonicity, turnover, composite 0-100 score; **no t-stats** | statistics weaker than QF_OS |
| Screening | numeric A/B/C/D grades + threshold gates + expression-similarity dedup (string, not value corr) | implementation difference; value-corr dedup (QF_OS/RD-Agent style) is stronger |
| Backtest | own long-only quantile engine; **overnight-return look-ahead** (T-close→T+1-close credited while claiming T+1-open entry); **ex-post direction auto-flip**; ex-post top-group selection; no delisting handling | **genuine quant conflicts/bugs** — QF_OS semantics are correct here |
| PIT | industry classification fetched today applied to all history; cap proxy = dollar volume | **genuine PIT violations** |
| Overfit defenses | **best-in-class battery: 4 anti-overfit tests (yearly IC stability, regime consistency, permutation placebo, IC half-life) + 4 adversarial tests (label permutation, block shuffle, universe subsample, noise injection) + walk-forward IC decay + external WQ BRAIN validation; wired into scoring (15%)** | **capability QF_OS lacks — top Phase B adoption candidate** |

## Fable adjudication — what QF_OS adopts (first-principles filtered)

Adoption is justified only where a mechanism serves a first principle, not
because a reference project has it:

1. **Falsification battery (from QuantGPT)** — placebo/permutation and
   noise-injection tests operationalize "a signal must beat its own null
   distribution"; walk-forward IC decay operationalizes non-stationarity
   honesty. ADOPT in Phase B evaluation plane.
2. **Audit-only OOS discipline (QF_OS keeps its own)** — RD-Agent's
   every-round test-set SOTA gate is the canonical counterexample; QF_OS
   must NOT import its selection loop shape. KEEP + strengthen.
3. **Knowledge-reuse repair loop (from RD-Agent CoSTEER)** — error/success
   retrieval reduces wasted LLM attempts; orthogonal to statistics. ADOPT
   (bounded) in Phase B agent plane.
4. **Value-correlation dedup vs active library (RD-Agent)** — string
   similarity (QuantGPT) misses numerically identical factors; QF_OS
   already has correlation gates — extend to a persistent SOTA library
   concept with correlation pruning.
5. **RunManifest/provenance + visible_at PIT + basis contract (Studio)** —
   see Studio section; ADOPT in Phase B data/artifact plane.
6. Reference-project defects (Studio 0%-delisting, QuantGPT overnight
   look-ahead & ex-post flips, RD-Agent test-set mining) are recorded as
   *negative* design constraints for Phase B: the target architecture must
   make these states unrepresentable or loudly flagged.
