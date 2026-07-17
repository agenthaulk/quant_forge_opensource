# Multi-Factor / Portfolio Backtest — Design Document

**Module:** `synthesis` (multi-factor composite backtest) · **Repo:** `quant_forge_opensource` · **Universe:** `cn_a` cross-sectional equity · **Status:** backtest-only (owner directive; no research-evaluation interval)

> Citations use `(file:NN)` against this repo unless prefixed: `(studio engine.py:NN)` = quant-forge-studio, `(qgpt backtest.py:NN)` = QuantGPT, `(ref: …)` = reference architecture. Line numbers are anchors, not exact after edits.

---

## At a glance

**Mechanism.** Combine ≥2 registered factors by cross-sectional standardize (zscore/rank, per day) → apply explicit ±1 direction → combine (method) → **materialize the composite as a synthetic colon-free `COMPOSITE_<hash>` `precomputed:` factor** → drive the existing `run_factor_backtest` engine by that id. Maximal reuse of the shipped schedule/cost/NAV/metric machinery; the only engine changes are two additive honesty fixes (deterministic tie-break, skip stub) and a shared `rebalance_indices` helper.

**Implementer entry points.** Method catalog `GET /api/synthesis/methods` → §9. Job `POST /api/jobs/multi-factor-backtest` request + response payload → §8. Reuse map (functions/signatures) → §11. Build order → §14. Correctness invariants → §12.

**Non-negotiable constraints.**
- **Backtest-only** module (no research-evaluation interval); the `evaluation` payload slot carries same-window diagnostics only.
- **Method set:** `equal_weight`, `weighted` (a-priori, `is_fitted:false`) + `ic_weighted`, `icir_weighted` (fitted, `is_fitted:true`, point-in-time embargoed).
- **No look-ahead:** signal as-of close `t`, trade at `t+execution_delay_days`; `rebalance_indices` is the single grid source of truth.
- **Disclosed structural limit (RB-1):** the engine's single `holding_days` knob is *both* cadence and lifetime ⇒ non-overlapping cohorts (K=1, N≈len/holding independent periods), emitting `NON_OVERLAPPING_COHORTS` + `PHASE_SENSITIVE_SMALL_SAMPLE`; overlapping tranches deferred to P8.
- **Honesty (FP-4):** coverage `null`→n/a never `0`; `is_fitted` truthful; a-priori `weights_effective` echoed raw; every skip/degeneracy emits an explicit warning code.

## 1. Goal & scope

Build the server side of the **already-shipped** multi-factor frontend (`synthesis.js`). The frontend renders a factor picker with explicit per-factor directions, a method/standardization catalog, a parameter form, and a full composite report — but the two endpoints it calls do not exist yet (confirmed: nothing matches `synthesis/methods` or `multi-factor-backtest` in `src` outside `static/`). This document specifies:

- `GET /api/synthesis/methods` — the method + standardization catalog (`synthesis.js:12`, `renderMethodSelectHtml` at `synthesis.js:139`).
- `POST /api/jobs/multi-factor-backtest` — run a **periodic-rebalance, holding-period** long/short backtest of a **composite** of ≥2 registered factors, combined by a chosen method with explicit ±1 directions and a chosen cross-sectional standardization, over a single backtest window.

**In scope:** cross-sectional composite construction (standardize → direction → combine), point-in-time fitted (IC/ICIR) and a-priori (equal / weighted) methods, a rebalancing engine driven by `holding_days`, transaction costs, and the exact JSON payload `synthesis.js` consumes.

**Out of scope (owner directive, already applied in FE):** this module is **backtest-only**. There is **no research-evaluation date interval**. `parameters` carries only `backtest_start`/`backtest_end`; the `evaluation` block in the payload is retained as a *structural* section the reused `factor.js` renderer expects, but it is populated as **same-window diagnostics** (see §8), not a separate evaluation interval. No new research/OOS split UI.

**Cadence-vs-lifetime caveat (structural, not a default choice — RB-1).** The reused engine exposes a **single knob, `holding_days`**, that is *simultaneously* the rebalance cadence and the position lifetime: the schedule loop steps by `holding` and each cohort exits at `signal_index + delay + holding` `(service.py:141,143,144)`. There is **no separate cadence parameter in the engine signature**, so rebalancing more often than you hold (cadence < lifetime, i.e. overlapping tranches) is **not representable** in the shipped product. The consequence is not cosmetic: every risk metric (turnover, Sharpe, IC, max drawdown) is computed on **non-overlapping** cohorts, so the effective sample is `N ≈ len(dates)/holding` **independent** period returns (e.g. `holding=20` over a ~130-date window ⇒ ~6 points), and the whole result is **start-phase-sensitive** — it shifts if the first signal date lands one bar earlier or later. This is surfaced to the user, not hidden: the run emits `NON_OVERLAPPING_COHORTS` and `PHASE_SENSITIVE_SMALL_SAMPLE` warning codes with the realized period count, and the validity caveats say so in plain language. Overlapping-tranche averaging (1/`holding` of the book rebalanced per bar; Jegadeesh–Titman) is the standard remedy and is a phased extension (§14) via the existing staggered-entry hook.

**Scale target:** ~5,600 instruments × 592 trade dates, long-panel `(instrument, trade_date)` with `close, market_cap, is_st, volume, returns`; factor values in the canonical value store keyed `factor_id=*` (原始因子/合成因子).

---

## 2. Reference architectures (what we adopt, cited)

| Source | Pattern adopted | Where it lands here |
|---|---|---|
| **This repo** | `run_factor_backtest(factor_id, …)` owns the entire long/short schedule, cost, NAV, segment, and typed-metric machinery `(service.py:49)`; `prepare_factor_scores_result` returns a tidy point-in-time, decay-applied, precomputed-aware per-factor score frame `(signal_processing.py:45)`; `FactorValueStore.write_incremental_values` materializes a score frame as cached factor values `(value_store.py:226)`. | **Primary reuse path (§5, §11):** standardize+combine per-factor scores into a composite frame, materialize it as a synthetic `precomputed:` factor, then drive the engine by that synthetic `factor_id`. The engine machinery is reused **unchanged except for two minimal additive honesty fixes** (deterministic tie-break, skip-warning emission) that the §12 invariants require — see §5, §6, RB-2/RB-3. |
| **quant-forge-studio** | Two-phase **signal→execute** loop with a structural 1-bar lag (`pending_execution_date = dates[index+1]`) producing distinct `OrderIntent(signal_date, execution_date)` lineage `(studio engine.py:979,1058)`; **calendar-bucket cadence** keyed off real trade dates `(studio engine.py:407)`; **weights drift, reset only at rebalance** so turnover is honest L1 vs the drifted book `(studio engine.py:916–924)`; as-of loader mediation for PIT + lineage `(studio managed.py:52)`. | Confirms our timing model (§6): our engine already lags via `entry_date = dates[signal_index + delay]` `(service.py:143)`. We adopt the **explicit signal/execution date lineage** in the resolved schedule payload and the **cadence-off-real-trade-dates** discipline. Full weight-drift accounting is a phased extension (§14) — the reused engine marks period returns close-to-close per cohort against the *previous target* book, not a daily-drifted book; the bias direction of this simplification is disclosed (§7, RB-4). |
| **QuantGPT** | `searchsorted(side='left')-1` maps every day to the **previous** rebalance's grouping (T+1 attribution) `(qgpt backtest.py:194)`; **cross-sectional per-date standardize (rank-pct / z-score) BEFORE a-priori weighted sum** `(qgpt composite.py:66–81)`; **rank-IC redundancy matrix** across candidates `(qgpt composite.py:89)`; **turnover-scaled cost drag** `(qgpt backtest.py:224)`. | Composite recipe in §4 is exactly QuantGPT's standardize-then-combine, **extended** with the fitted IC/ICIR methods QuantGPT lacks. We **reject** QuantGPT's auto-direction-flip `(qgpt backtest.py:253)` — directions are explicit ±1, locked at request time, never re-derived (repo notes line 366; `synthesis.js:81`). |
| **reference-architecture** | Discrete-time causal loop: form universe as-of → cross-sectional scores from data ≤ signal → neutralize/standardize/combine → target weights → trade with lag & cost `(ref)`. **Two clocks** (cadence vs holding): if holding > cadence, average `K = holding/cadence` overlapping cohorts (Jegadeesh–Titman; de Prado). **IC/ICIR combination** estimated **point-in-time** (Grinold–Kahn fundamental law `IR≈IC·√breadth`). Cost on **realized turnover vs drifted weight**. | Our cadence == holding_days ⇒ **K=1, non-overlapping** by construction (the engine steps by `holding`, `service.py:141`; see RB-1). We adopt the causal-timing and PIT-IC-estimation invariants literally (§4.4, §6, §12). Full overlapping-cohort (K>1) reuses the existing **staggered-entry** hook (`first_signal_date`, `STAGGERED_COHORT_ROLE`, `service.py:721`) and is a phased extension (§14). |

---

## 3. Data & point-in-time contract

**Panel.** `LocalPanelDataProvider(data_root).load_panel()` returns the long panel `(instrument, trade_date, close, market_cap, is_st, volume, returns)` `(service.py:104)`. Trade dates are the real trading calendar; cadence and lag key off integer index positions into the sorted date list, so holidays/suspensions are handled structurally (studio-style, `studio engine.py:407`).

**Factor values.** Each member factor's per-date/per-instrument score is read via `prepare_factor_scores_result(panel, formula, universe_filters, *, profile, factor_id, factor_name, factor_values_root, factor_values_overlay_root)` → `FactorScoreResult.scores` with columns `[trade_date, instrument, score]` `(signal_processing.py:45)`. This frame is already: (a) restricted to the profile test period `(signal_processing.py:58)`, (b) decay-applied when `decay_days>1` via EWMA over a lookback context panel so the warmup is not look-ahead `(signal_processing.py:71, _apply_ewma_decay:192)`, and (c) precomputed-aware — a `precomputed:factor_id=…` formula reads cached values from the store rather than recomputing `(signal_processing.py:59,94; catalog.is_precomputed_formula)`.

**One pinned universe across members (RB-6).** `SimulationProfile` does **not** carry `universe_filters` — those are a **separate argument** to `prepare_factor_scores_result` `(signal_processing.py:45)`. The workflow therefore resolves **one explicit `universe_filters` set** (default the `cn_a` standard: drop `is_st`, min-listing) and passes the **identical** set to every member's score fetch **and** onto the materialized composite factor. Member `FactorDefinition.universe_filters` are validated to be mutually compatible; if two members declare conflicting universes the request is rejected (`UNIVERSE_MISMATCH`) rather than silently unioned. `is_st`/listing is applied **at formation only** (fetch time); a name that turns ST or delists mid-hold is carried to its last available close (survivorship guard, §6), and this policy is disclosed in the validity caveats. This closes the "trade an ST name a stricter member excluded" hole under `min_factor_coverage < all`.

**Point-in-time contract (the load-bearing invariant).** At a rebalance/signal date `d = dates[i]`:

1. **No look-ahead in scores.** Only rows with `trade_date ≤ d` (minus decay/formula warmup, already handled inside `prepare_factor_scores_result`) may inform the score at `d`.
2. **Execution lag.** The signal at `d` is actionable only at `entry_date = dates[i + delay]`, `delay = profile.execution_delay_days ≥ 1` (default 1) `(service.py:135,143; contracts.py:105,121)`. Returns are close-to-close over `[entry_date, exit_date]` via `_with_period_return` `(service.py:165,702)`. The bar that *produces* the signal never earns the return (ref causality invariant).
3. **Fitted-weight PIT rule.** For IC/ICIR methods, the combination weights at `d` are estimated using **only periods whose forward-return window has fully closed on or before `d`** — see §4.4. This is the embargo that stops IC-weighting from peeking.
4. **Coverage is observed, never fabricated.** A factor missing at `(d, instrument)` reduces that factor's `rows_in_composite`; unobservable ratios emit real `null` (→ `n/a` in `pct()`), never `0` (FP-4; `synthesis.js:334,349`).

**Minimum-history guard (corrected — RF-4 / RB-2).** The backtest gate is **not** the 126-day display floor. `run_factor_backtest` calls `require_minimum_display_trading_days(working_panel, min_trading_days=max(2, holding + execution_delay_days + 1))` `(service.py:106–109)` — so the engine only requires roughly `holding + delay + 1` **in-window** trade dates before it raises `(signal_processing.py:163)`. The `126` figure is two *different*, unrelated constants: `MIN_DISPLAY_TRADING_DAYS = 126` is the *default* of that function used by **other** callers (not the backtest) `(signal_processing.py:19)`, and `MIN_ANNUALIZATION_EXPOSURE_DAYS = 126` `(service.py:33)` only **degrades a metric's `status` to `insufficient_sample`** — it never raises on the backtest path. The `evaluate_factor` call that fills the evaluation slot (§8) *may* still raise via the default, so window sizing must satisfy that separately. Because the real backtest floor is tiny, the synthesis layer adds its **own** window preconditions (below) rather than leaning on a floor that does not exist.

**Synthesis-side window preconditions (RB-2 / RB-8).** Before materializing:
- **Always:** compute the realized non-overlapping period count `N = max(0, ⌊(len(in_window_dates) − 1 − delay − holding) / holding⌋ + 1)` — the count of grid signal indices `s ∈ range(0, len(dates) − delay − 1, holding)` whose forward window closes **inside** the panel (`s + delay + holding < len(dates)`), i.e. the **D3-complete** periods the engine actually realizes. If `N < 2`, reject (`WINDOW_TOO_SHORT`). Emit `N` into provenance and the `NON_OVERLAPPING_COHORTS` warning. *(Corrected from the original `⌊(dates − delay − 1) / holding⌋ + 1` — audit S1: that expression counted the D3-excluded final-partial signal and, when `holding | (dates − delay − 1)`, exceeded even the grid signal count by 1, so a window realizing a single complete period wrongly passed the ≥2 gate.)*
- **Fitted methods only:** a rebalance is *fittable* only once `ic_min_periods` prior periods have closed. The window should admit at least one **fittable** rebalance (`N − ic_min_periods ≥ 1`); this is a **downgrade condition, not a preflight reject**: if **zero** rebalances would be fitted, the run completes with **the method downgraded to `equal_weight` and `is_fitted = false`** plus a prominent `NO_FITTED_PERIODS` code (never advertise `is_fitted=true` on an all-warmup run). If some but few are fitted, keep `is_fitted=true` and expose `fitted_period_fraction` (§4.4, §8).

**Guard rails inherited from `SimulationProfile` `(contracts.py:101)`:** `0 < top_quantile ≤ 0.5`, `nan_policy='drop'` only, `neutralization='none'` only, `truncation=None`, `execution_delay_days ≥ 1`, `decay_days ≥ 0`. The method/standardization catalog must stay inside these.

---

## 4. Signal → composite (methods, standardization, directions, coverage, fitted vs a-priori)

### 4.1 Per-factor scores

For each `factor_ref = {factor_id, direction}` call `prepare_factor_scores_result(...)` once — passing the **single pinned `universe_filters`** (§3, RB-6) — to get `s_f[(trade_date, instrument)]` `(signal_processing.py:45)`. All member factors share one profile (delay, decay, test period) **and** one universe, so their frames are date- and universe-aligned.

**Decay is a member-level transform, applied exactly once (LA-1).** Members are fetched with the shared profile, so when `decay_days > 1` each member is EWMA-decayed **inside this call** `(signal_processing.py:108–109)`. That branch is **unconditional on formula type** — `cache_only`/precomputed does **not** skip it `(signal_processing.py:59,108)`. Therefore the profile handed to the **engine** over the already-combined precomputed composite (§5, §10 step 5) is pinned to **`decay_days = 0`**, so the composite is not decayed a second time. Decay lives in exactly one place: on the members, before combination. (Test `test_double_decay.py`, §13, materializes a two-member composite with `decay_days=10` and asserts the stored composite values are *not* EWMA'd again.)

### 4.2 Cross-sectional standardization (per date)

Applied per `trade_date` over the eligible cross-section, driven by the FE `standardizations` catalog (`synthesis.js:12`). Two standardizers (QuantGPT recipe, `qgpt composite.py:72–77`):

- **`zscore`** — `z = (s − mean_d(s)) / std_d(s)`; if `std_d == 0`, the factor contributes `0` that date and the date is marked degenerate for that factor.
- **`rank`** — `r = s.rank(pct=True)` then mapped to `[-1, 1]` as `2·r − 1` (rank is outlier-robust; ref/qlib default posture). Rank uses a **deterministic tie policy** (`method='first'` over an instrument-sorted frame) so tied inputs get a reproducible order (RB-3).

Standardization is **cross-sectional per date** only — never pooled across dates (that would leak the panel-wide distribution). Winsorization/neutralization are **not** applied (profile pins `neutralization='none'`, `truncation=None`), consistent with the shipped single-factor engine.

**Degenerate cross-section handling (RB-9).** A date is **degenerate** if, after standardize+combine, the composite cross-section is all-NaN **or** zero-variance (all-equal). Both converge on **one explicit behavior**: the composite value for that date is set to `NaN` (so the engine drops the whole date rather than trading tie-noise into an arbitrary long/short split), and the date is recorded under `DEGENERATE_CROSS_SECTION` with a count in provenance. This makes "no real signal today" a single flagged outcome instead of two silent, divergent ones (all-NaN → silent gap; all-equal → arbitrary noise book).

### 4.3 Directions (explicit ±1, never silent)

After standardization, multiply by the declared `direction ∈ {+1, −1}` from `factor_refs` `(synthesis.js:497,537)`. `+1` = use as defined, `−1` = declared inversion. Directions are **locked at request time** and echoed verbatim in `synthesis_provenance.directions` `(synthesis.js:371–408)`. We do **not** port QuantGPT's auto-flip (repo notes line 366).

### 4.4 Combination — a-priori vs fitted

Let `t_{f,d,i}` be the standardized, direction-applied score. Composite:

```
composite_{d,i} = Σ_f  w_{f,d} · t_{f,d,i}      (missing t → excluded from that name's sum; coverage tracked)
```

**A-priori methods (`is_fitted = false`)** — weights are constant, caller-supplied, echoed **RAW/unnormalized**:

- **`equal_weight`** — `w_f = 1/N` (or raw `1` before averaging; echoed as the a-priori claim). No params. `is_fitted=false`.
- **`weighted`** — `w_f` from the `weights` ParamSpec (one per checked factor, validated to cover exactly the checked set — `synthesis.js:510–523`). Echoed raw; normalization (if any) is an internal detail, the **raw** declared value is what `weights_effective` reports.

Because a-priori weights are **date-independent**, the composite value at any date carries the same weight vector, so the grid-fidelity concern (RB-5, below) is moot for these methods and the materialize path ships with no grid coupling.

**Fitted methods (`is_fitted = true`, honestly labeled)** — QuantGPT has none `(qgpt pitfall: "no fitted/IC weighting exists")`; we add:

- **`ic_weighted`** — `w_{f,d} ∝ IC_f(d)`, the point-in-time mean rank-IC of factor `f` estimated on the expanding in-window history ending before `d` (below).
- **`icir_weighted`** — `w_{f,d} ∝ IC_f(d) / std(IC_f over window)` = ICIR (Grinold–Kahn fundamental law, ref). More robust to noisy single-period IC.

**Shared rebalance grid (RB-5, the anti-divergence rule).** The fitted embargo `idx(s)+delay+holding ≤ idx(d)` is only sound if it is measured on the **same index grid the engine actually trades**. Synthesis and the engine must therefore **not** derive schedules independently. We extract one tiny **pure** helper, `rebalance_indices(dates, *, delay, holding, start_signal_index)`, that returns exactly `list(range(start_signal_index, len(dates) - delay - 1, holding))` — the engine's own expression `(service.py:136,141)` — and **both** the engine loop and the synthesis IC fit call it (this small extraction is brought forward from P7; the full `run_backtest_on_scores` extraction stays deferred). Fitted weights are computed **for every rebalance index the helper yields**, so whatever grid the engine realizes is fully covered. The workflow asserts `resolved_schedule.signal_dates == dates[rebalance_indices(...)]` and that every traded signal date has a composite row (RB-7 guards the "no row → silent skip" case).

**Forward returns match the engine (RB-5).** The realized forward return that drives the IC fit is computed with the **same** close-to-close primitive the engine uses to earn returns, `_with_period_return` over `[entry, exit]` `(service.py:702)` — including its last-mark/survivorship fill — so the IC that sets the weights and the return the engine realizes are the same object, not two approximations.

**The point-in-time IC fit (the anti-peek rule).** A period with signal date `s` realizes its forward return over `[s+delay, s+delay+holding]` and is only *observable* at `dates[idx(s)+delay+holding]`. Therefore at rebalance date `d`, the IC estimate may only use periods `s` with:

```
idx(s) + delay + holding ≤ idx(d)          # forward window fully closed on/before d
and  s ≥ backtest_start                     # expanding window from window start
```

Pseudocode (NaN-hardened, RB fix for ICIR):

```python
import numpy as np

def ic_weights_asof(d_idx, dates, per_factor_scores, fwd_returns,
                    rebalance_idx, method, delay, holding, factors,
                    start_idx, min_periods=6):
    # eligible signal periods whose forward window closed on/before d,
    # taken from the SAME grid the engine trades (rebalance_idx = rebalance_indices(...))
    eligible = [s for s in rebalance_idx
                if s + delay + holding <= d_idx and s >= start_idx]
    if len(eligible) < min_periods:
        return None, "WARM_UP_IC_UNFITTED"        # this date runs equal-weight, flagged
    ic_series = {f: [] for f in factors}
    for s_idx in eligible:
        d_s = dates[s_idx]
        y = fwd_returns.at(s_idx)                   # via _with_period_return, matches engine
        for f in factors:
            x = per_factor_scores[f].at(d_s)        # standardized score at signal
            ic = spearman(x, y)                     # cross-sectional rank IC
            if np.isfinite(ic):
                ic_series[f].append(ic)
    raw = {}
    for f in factors:
        arr = np.asarray(ic_series[f], dtype=float)
        if arr.size == 0:
            raw[f] = 0.0
        elif method == "ic_weighted":
            raw[f] = float(np.mean(arr))
        else:  # icir_weighted
            sd = float(np.std(arr))
            raw[f] = float(np.mean(arr) / sd) if sd > 0 else 0.0   # explicit std==0 guard
    # clip negatives / non-finite to 0 with an explicit finite check (NOT bare max)
    w = {f: (v if np.isfinite(v) and v > 0.0 else 0.0) for f, v in raw.items()}
    total = sum(w.values())
    if total <= 0.0:                                # all zero / all non-finite
        eq = 1.0 / len(factors)
        return {f: eq for f in factors}, "IC_DEGENERATE_EQUAL_WEIGHT"
    return {f: w[f] / total for f in factors}, None
```

Because eligible periods use **only fully-realized** forward returns strictly before `d`, no future information enters the weight — this is the ref "IC weights estimated point-in-time" invariant and the purged/embargoed discipline (de Prado) applied to combination weights. The ICIR `std==0` case and any non-finite raw weight now clip to `0` via an **explicit finite check** (not bare `max`, which is order-dependent for NaN), and an all-zero/all-non-finite vector falls back to equal-weight for that date with a flag rather than collapsing the whole cross-section to NaN and silently emptying the rebalance (RB fix).

The first `min_periods` rebalances have no fittable history and degrade to equal-weight, flagged `WARM_UP_IC_UNFITTED`. **If the whole window admits zero fittable rebalances, the method is downgraded to `equal_weight` with `is_fitted=false` and `NO_FITTED_PERIODS`** (§3 precondition, RB-8) — the run never advertises `is_fitted=true` while running entirely unfitted.

> **Materialization consequence (RB fixes on provenance).** Because fitted weights are time-varying, the materialized composite frame carries the **already-combined** `composite_{d,i}` value per `(d,i)` — the per-date weights are folded in before materialization. For **fitted** methods we do **not** populate `weights_effective` (which the FE captions as a-priori raw declared values — see FP-1 below); instead the provenance carries dedicated fitted fields: `fitted_weights_latest` (the **last genuinely-fitted** vector, or `null` if none), the full per-date `fitted_weights_path` diagnostic, `fitted_period_fraction`, and `warmup_period_count`. For **a-priori** methods, `weights_effective` reports the single constant raw vector and no fitted fields are emitted. This keeps the FE caption truthful for both branches.

### 4.5 Coverage

Per factor per role: `rows_scored` (rows where `s_f` is finite over the window), `rows_in_composite` (rows that actually entered a composite value), `coverage_ratio = rows_in_composite / rows_scored` (real `null` when denominator is 0/unobservable — `synthesis.js:349`). A composite row requires a minimum factor presence controlled by `coverage_rule`:

- `coverage_rule = "all_factors"` (default): a name enters the composite at date `d` only if **all** checked factors have a finite score at `d` (strict, matches `rows_full_coverage`).
- `min_factor_coverage = k`: alternatively require ≥ k factors present (records `rows_required`, `rows_full_coverage`). Even under `k < N`, the **pinned universe** (§3, RB-6) still bounds membership, so a permissive member cannot smuggle an out-of-universe (ST/unlisted) name into the book.

The rank-IC redundancy matrix (`qgpt composite.py:89`) is computed as an advisory diagnostic and attached under provenance (crowding check) — advisory only, never gates.

---

## 5. Portfolio construction & weighting (scores → target weights)

We **reuse the shipped construction** by materializing the composite as a synthetic factor and letting `run_factor_backtest` build the book:

- At each signal date the cross-sectional composite is sorted; `count = max(1, int(len(merged) · top_quantile))`; **short = bottom `count`, long = top `count`**, dollar-neutral equal weight: `+1/len(long)` on longs, `−1/len(short)` on shorts `(service.py:171–177, _portfolio_weights:1138)`. `period_return = long_return − short_return`.
- Quantile group returns `Q1..Q_group_count` via `np.array_split` on the composite ordering `(service.py:178, _group_returns:1119)` for the monotonicity/decile diagnostic (ref reporting).

**Deterministic ranking (RB-3, additive engine fix).** Composites make exact ties **common** (rank→`2r−1` collapses ties, `equal_weight` of two rank factors yields equal sums, a zero-variance cross-section is all-equal). The shipped engine sorts with `merged.sort_values("score")` — pandas default quicksort, **non-stable, no tie-break** `(service.py:172)` — so which tied names land long vs short, or in Q1 vs Q5, depends on incoming row order and is **not reproducible**, violating the design's own §12 "deterministic ranking" invariant and the golden-file requirement (§13). Fix, applied in the engine (a minimal additive change): `merged.sort_values(["score", "instrument"], kind="mergesort").reset_index(drop=True)` — stable sort with an explicit deterministic tie-break — and `_group_returns` consumes the same ordering. The same deterministic tie policy is applied inside standardization (§4.2). A tie-heavy unit case locks it (§13).

This gives the classic quantile long-short factor portfolio (ref weighting scheme (a)). The composite's ordering is the only thing that changed vs a single factor; every downstream weighting, NAV, and metric path is identical. Rank/z-score-proportional and optimizer weighting (ref (b),(c)) are **not** in the reused engine and are out of scope (phased extension §14).

**Minimal engine extension.** The engine consumes a `factor_id → formula`, not an arbitrary Series `(service.py:110)`. Rather than refactor the inlined loop, we **materialize** the composite as a synthetic `precomputed:` factor (see §11). The extension is *a thin producer that writes the composite Series into the value store (via the store's own path resolution — RF-3) and registers a `precomputed:factor_id=<composite_id>` synthetic factor in `factor_root` (via `FactorRepository.save` — RF-2)*, after which the engine resolves it by id. The engine machinery itself changes only in the two minimal, additive honesty fixes noted above and in §6 (deterministic tie-break; skip-warning emission) — no logic in the schedule/cost/NAV/metric path is altered. (Alternative: extract `run_backtest_on_scores(scores_df, close_panel, holding, delay, top_quantile, group_count, costs, …)` from `service.py:141–281` and have both call it — deferred to §14 as an optional refactor; the materialize path ships first.)

---

## 6. Rebalancing engine (cadence, latest-value as-of, execution delay, overlap, turnover)

The engine is the existing schedule loop `(service.py:141–281)`, driven by the composite. Precise semantics:

**Cadence from `holding_days` — single knob, cadence == lifetime (RB-1).** The loop is:

```python
for signal_index in rebalance_indices(dates, delay=delay, holding=holding,
                                      start_signal_index=start_signal_index):
    signal_date = dates[signal_index]                 # (service.py:141-142)
    entry_date  = dates[signal_index + delay]          # look-ahead guard (service.py:143)
    scheduled_exit_index = signal_index + delay + holding
```

`holding = holding_days` (REQUIRED for a composite — **no single-factor `horizon_days` fallback**; the composite analog of `_idea_validation_settings` **raises** if `holding_days` is absent rather than defaulting to `factor.horizon_days` as the shipped helper does at `api.py:411,384` — RF-5). The **step is `holding`**, so **rebalance cadence == holding period**: `K = holding/cadence = 1`, **non-overlapping** cohorts. There is no way to make cadence < lifetime in this engine; see the RB-1 caveat in §1. The realized period count `N` and the `NON_OVERLAPPING_COHORTS` / `PHASE_SENSITIVE_SMALL_SAMPLE` codes are emitted so a reader cannot over-read a `~6`-point Sharpe. (Overlapping K>1 cohort averaging (ref Jegadeesh–Titman) is the phased extension via `first_signal_date` staggered cohorts, §14.)

**Latest-factor-value as-of.** At each `signal_index`, the composite used is `scores[scores.trade_date == signal_date]` `(service.py:161)` — the **freshest** composite as-of the signal date. Names with a missing composite are dropped (`.dropna(subset=["score"])`).

**Skipped/thin rebalance is flagged, never silent (RB-7, additive engine fix).** If the cross-section is empty after `dropna`, or too thin (`len(merged) < max(4, group_count)`), the shipped loop `continue`s **before** appending a ledger row, before any `daily_nav`, and before updating `previous_*` state `(service.py:161–169)` — so with a fixed `holding` step the **entire holding-length period silently disappears**: NAV compounds straight across the gap (an implicit costless flat), the next successful rebalance measures turnover against a **stale** previous book, and no warning is emitted. This contradicts the "never silent" posture. Two mutually-reinforcing fixes:
- **Synthesis-side pre-scan (ships first, no engine dependency):** the workflow replicates the shared `rebalance_indices` grid against the composite coverage frame, identifies every rebalance date that would be empty/thin, emits `REBALANCE_SKIPPED_NO_COVERAGE` / `REBALANCE_SKIPPED_THIN` into `warning_codes`, and records `skipped_rebalances` count + dates in provenance.
- **Minimal additive engine change:** on `continue`, the loop appends a **ledger stub** for the skipped period (marked flat, zero return, with the skip warning code) and carries the previous book forward as the reference for the next turnover computation, so the period is explicitly flat-flagged rather than vanished.

**Execution delay.** `entry_date = dates[signal_index + delay]`, `delay ≥ 1` `(service.py:143)`. Signal at close `t` → trade at `t+delay` → returns earned close-to-close from `entry_date` `(service.py:165, _with_period_return:702)`. A name that stops quoting mid-period realizes at its last available close (survivorship guard; `position_lost` counted, `POSITIONS_LOST_BEFORE_EXIT`) `(service.py:181,713)`. A name turning ST or delisting mid-hold is treated the same way (carried to last mark; §3 policy).

**Overlapping holdings / turnover.** With `K=1` there is no cohort overlap; the whole book turns over each rebalance. Turnover is honest L1 change over the **union** of instruments vs the previous book: `_portfolio_turnover(previous_weights, weights)` returns `(turnover = traded_notional/2, traded_notional_rate)` `(service.py:192, 1151)`. First build is `initial_build_turnover`; subsequent are `rebalance_turnover`, averaged into `rebalance_turnover_mean` `(service.py:196–200, 311–313)`. Membership churn is tracked separately as `rebalance_rate` via `_leg_rebalance_rate` `(service.py:187, 1132)`. (This matches studio's union-of-symbols L1 turnover `studio engine.py:924`.) **Bias disclosure (RB-4):** turnover/cost are charged on the L1 change between the previous **target** book (an exact equal-weight reset) and the new target — **not** the drifted book — so real drift-back trades are **uncosted** ⇒ turnover/cost are **understated** in that direction; separately, because `count = int(len(merged)·top_quantile)` recomputes each rebalance, a change in cross-section size shifts `1/len(long)` for **all** held names and can manufacture turnover even when membership is unchanged. Both effects are disclosed in the validity caveats (net cost is neither a clean upper nor lower bound); the drifted-weight refinement is a phased extension (§14).

**Final partial period.** Excluded by default (owner decision D3), flagged `FINAL_PARTIAL_PERIOD_EXCLUDED`; `include_partial_final_period=True` marks it to market instead (`PARTIAL_FINAL_PERIOD`) `(service.py:146–156)`. Never silent.

**No look-ahead, structurally.** Because scores at `signal_date` map to entry at `signal_date+delay` and returns are forward close-to-close, the signal bar's return is never earned (ref). The IC-weight fit adds the second embargo (§4.4), measured on the **shared** `rebalance_indices` grid (RB-5). Segment/purge machinery `_split_rows_with_purge_counts` `(service.py:1266)` still applies if `sample_splits` are passed — but this is backtest-only over one window, so we run a single `external_oos_backtest` role (§8).

---

## 7. Transaction costs & accounting

Reuse `TransactionCostModel(commission_bps, slippage_bps, short_borrow_bps_annual)` (all default `0.0`, `contracts.py:238`) passed straight into `run_factor_backtest`. Per-rebalance cost `(service.py:1160)`:

```
cost_rate = traded_notional_rate · (commission_bps + slippage_bps)/1e4
          + short_borrow_bps_annual/1e4 · trading_days_held/252
net_period_return = period_return − cost_rate                      (service.py:206)
```

Charged on **realized turnover** (`traded_notional_rate` from the L1 book change, `service.py:192`) — ref "cost on realized turnover." **Bias caveat (RB-4):** as noted in §6, cost is charged against the previous *target* book, so drift-back trades are uncosted (understatement), while `count`-recompute churn can add noise-driven turnover (overstatement in thin/tie regimes); the net figure is therefore **not** a clean bound and this is stated in `validity.caveats`. Gross and net NAV compound separately and every metric is reported **gross AND net** `(service.py:214–239)`; a trailing unmarkable (NaN) day does not poison the compounding base (COR-8, `service.py:226`). `cost_reconciliation` sums period costs and terminal equities `(service.py:386)`. FE cost fields (`commission_bps/slippage_bps/short_borrow_bps_annual`) map through the composite settings analog into the model exactly as the single-factor path `(api.py:472; _idea_validation_settings:402)`.

---

## 8. Metrics & report payload (exact JSON matching the FE contract)

The job returns the shape `renderSynthesisReportHtml` consumes `(synthesis.js:445–472)`. It **reuses the single-factor builders verbatim**: `_backtest_payload` `(api.py:1077)`, `_evaluation_payload` `(api.py:1011)`, `_json_safe` `(api.py:1322)` — so `factor.js`'s `renderEvaluationSection / renderInSampleSection / renderOosSection / renderDiagnosticsSection / renderEvidenceSection / renderArtifactsSection` render 1:1.

> **The `backtest` block is `_backtest_payload` output verbatim (FP-3).** The comment below enumerates the highlighted structures, but `_backtest_payload` MUST carry every **top-level scalar tile** the `factor.js` OOS/in-sample renderers read directly `(factor.js:136–176)`: `gross_cumulative_return, net_cumulative_return, completed_periods/periods, exposure_days, gross_annualized_return/annualized_return, net_annualized_return, net_annualized_volatility, net_long_short_sharpe, net_max_drawdown, initial_build_turnover, rebalance_turnover_mean, replacement_rate_mean, holding_days`, and `group_returns[].group/mean_return`. Implement to `_backtest_payload`, not to the illustrative subset.

> **Composite id / formula are colon-free (RF-1, blocking).** `FactorDefinition.__post_init__` enforces `factor_id ~ [A-Za-z][A-Za-z0-9_=-]*` (no colon) `(contracts.py:62)`, and `_PRECOMPUTED_FORMULA_RE` captures the store key with charset `[A-Za-z0-9_.=-]` (no colon) `(catalog.py:25–28)`; `FactorRepository` also uses `factor_id` as a directory name `(repository.py:20–28)`. So the id is `COMPOSITE_<hash>` and the formula `precomputed:factor_id=COMPOSITE_<hash>` — every former `composite:…` occurrence is replaced. `<hash>` is defined in §11 (RB-10).

```json
{
  "factor": { "factor_id": "COMPOSITE_9f3ac21b7e", "formula": "precomputed:factor_id=COMPOSITE_9f3ac21b7e",
              "source": "synthesis", "horizon_days": 10 },
  "parameters": {
    "holding_days": 10, "backtest_start": "…", "backtest_end": "…",
    "top_quantile": 0.3, "decay_days": 10, "execution_delay_days": 1,
    "commission_bps": 0.0, "slippage_bps": 0.0, "short_borrow_bps_annual": 0.0,
    "include_partial_final_period": false,
    "backtest": { "simulation": { "execution_delay_days": 1, "decay_days": 10, "top_quantile": 0.3 },
                  "test_period": { "start": "…", "end": "…" } },
    "transaction_costs": { "commission_bps": 0.0, "slippage_bps": 0.0, "short_borrow_bps_annual": 0.0 }
  },
  "evaluation":        { /* _evaluation_payload shape — SAME-WINDOW diagnostics (backtest-only, see below) */ },
  "in_sample_backtest":{ /* _backtest_payload shape, or null */ },
  "backtest":          { /* _backtest_payload VERBATIM: top-level scalar tiles (see note above),
                            metrics gross+net, group_returns Q1..Q5, daily_nav, rebalance_ledger,
                            resolved_schedule (signal/entry/exit dates), metric_provenance,
                            warning_codes, artifact_path */ },
  "validity": { "message": "研究口径合成回测（非生产交易口径）", "basis": "external_oos_backtest",
                "caveats": [
                  "先验/拟合已如实标注",
                  "调仓周期与持有期为同一参数（holding_days）：K=1 非重叠，指标基于约 N 个独立区间，对起始相位敏感",
                  "样本内评价为同窗诊断，非独立研究样本",
                  "成本以目标簿 L1 换手计，漂移回补交易未计成本（换手/成本偏低估）",
                  "is_st/上市过滤仅在建仓时点应用；持有期内转 ST/退市按最后成交价了结"
                ] },
  "synthesis_provenance": {
    "factors": [ { "factor_id": "…", "direction": 1, "source": "registry" }, … ],
    "directions": { "factor_id_a": 1, "factor_id_b": -1 },
    "method": "ic_weighted", "method_params": { "ic_min_periods": 6 },
    "standardization": "zscore", "standardization_pinned_by_method": false,
    "composite_id": "COMPOSITE_9f3ac21b7e", "is_fitted": true,
    "min_factor_coverage": 2, "coverage_rule": "all_factors",
    "universe_filters": ["drop_is_st", "min_listing_days=…"],
    "period_count": 12, "non_overlapping": true,
    "rows_required": 12345, "rows_full_coverage": 12000,
    "skipped_rebalances": 0, "degenerate_cross_sections": 0,

    "fitted_period_fraction": 0.5, "warmup_period_count": 6,
    "fitted_weights_latest": { "factor_id_a": 0.61, "factor_id_b": 0.39 },
    "fitted_weights_path": [ { "signal_date": "…", "weights": { "factor_id_a": 0.55, "factor_id_b": 0.45 } }, … ],

    "coverage_by_role": {
      "external_oos_backtest": {
        "coverage": [ { "factor_id": "…", "direction": 1, "source": "registry",
                        "rows_scored": 6000, "rows_in_composite": 5800, "coverage_ratio": 0.966 }, … ],
        "rows_required": 12345, "rows_full_coverage": 12000
      }
    }
  }
}
```

> **Fitted vs a-priori provenance fields (FP-1).** For **fitted** runs (as above), `weights_effective` is **absent** and the fitted vector lives in `fitted_weights_latest` (last genuinely-fitted, or `null`) + `fitted_weights_path` + `fitted_period_fraction` + `warmup_period_count`. For **a-priori** runs, the block instead carries `"weights_effective": { … }` (raw declared) and **omits** all fitted fields. This is because `renderProvenanceCardHtml` captions `weights_effective` unconditionally as “权重为先验原始声明值，未做归一化展示” with no `is_fitted` branch `(synthesis.js:386–388)`; routing a time-varying fitted vector through that field would make a factually wrong a-priori claim in the UI. Leaving `weights_effective` undefined makes `weightsLine` render `''` `(synthesis.js:386)`, and the fitted fields carry the honest, qualified story (see Open questions for a proposed FE caption for the fitted fields).

**Same-window evaluation (FP-2).** Because the module is backtest-only, `evaluation` is populated from `evaluate_factor` over the **same** backtest window and is explicitly **same-window diagnostics**, not an independent research sample. The FE `renderEvaluationSection` title (“样本内研究评价”) is hard-coded and cannot be relabeled without an FE edit, so the honesty signal is carried in `validity.caveats` (“样本内评价为同窗诊断，非独立研究样本”) and in `evaluation.meta.basis`. Proposing an FE caption tweak is deferred (Open questions).

**Metric discipline.** Every metric is a typed `MetricValue{value,unit,status,observation_count,minimum_required,method,source_series,sample_role,warning_codes}` `(service.py:441)`. Insufficient sample → a `status`, never a fake `0`; annualization needs ≥126 exposure days (`MIN_ANNUALIZATION_EXPOSURE_DAYS`, `service.py:33`) or the metric `status` degrades (it does **not** raise — see §3). `BACKTEST_HIGHLIGHT_METRICS = annualized_return, net_annualized_return, net_long_short_sharpe, net_max_drawdown, rebalance_turnover_mean` `(workbench/service.py:38)`. Coverage ratios emit real `null` when unobservable (FP-4). Since this is backtest-only, `coverage_by_role` carries the single `external_oos_backtest` role; the FE falls back gracefully if a role is absent `(synthesis.js:337)`.

---

## 9. Method catalog JSON (`GET /api/synthesis/methods`)

Literal response (shape from `synthesis.js:12`, consumed by `renderMethodSelectHtml:139`, `buildRunRequest:493`). `ParamSpec = {name,label,type: float|int|bool|enum|weights, required, default, minimum, maximum, choices, help}`.

```json
{
  "methods": [
    { "name": "equal_weight", "label": "等权合成", "available": true,
      "required_standardization": false, "is_fitted": false, "params": [] },

    { "name": "weighted", "label": "先验加权合成", "available": true,
      "required_standardization": false, "is_fitted": false,
      "params": [
        { "name": "weights", "label": "各因子权重", "type": "weights", "required": true,
          "help": "为每个已选因子提供一个先验权重；原样回显，不归一化展示。" }
      ] },

    { "name": "ic_weighted", "label": "IC 加权合成（拟合）", "available": true,
      "required_standardization": false, "is_fitted": true,
      "params": [
        { "name": "ic_min_periods", "label": "IC 最小拟合期数", "type": "int",
          "required": false, "default": 6, "minimum": 3, "maximum": 60,
          "help": "点位时序拟合的最小已实现期数；不足则该期退化为等权并标注；窗口内无任一可拟合期则整体退化为等权且 is_fitted=false（NO_FITTED_PERIODS）。" }
      ] },

    { "name": "icir_weighted", "label": "ICIR 加权合成（拟合）", "available": true,
      "required_standardization": false, "is_fitted": true,
      "params": [
        { "name": "ic_min_periods", "label": "ICIR 最小拟合期数", "type": "int",
          "required": false, "default": 6, "minimum": 3, "maximum": 60,
          "help": "以 IC 均值/IC 标准差作为权重；窗口内仅用已实现的前向收益，杜绝前视；IC 标准差为 0 或权重非有限时该期退化为等权。" }
      ] }
  ],
  "standardizations": [
    { "name": "zscore", "label": "截面 Z-Score（按日）" },
    { "name": "rank",   "label": "截面排序标准化（按日）" }
  ]
}
```

Notes: `required_standardization:false` on all four ⇒ FE always sends a `standardization` block (`synthesis.js:541`). No method is `available:false` at launch, but the FE renders reserved (`available:false`) methods as disabled 预留 options generically `(synthesis.js:139)` — future methods (e.g. `optimizer`) can ship reserved. `is_fitted` is echoed truthfully into provenance; a-priori methods must never claim `true`, and a fitted method that downgrades on a short window reports `is_fitted:false` at run time (§3, §4.4).

---

## 10. Backend surface & wiring

**New files.**

- `src/quant_forge/synthesis/service.py` — composite core: per-factor score fetch (one pinned universe), standardize, direction, combine (a-priori + PIT IC/ICIR on the shared rebalance grid), coverage accounting, degenerate/skip pre-scan, composite materialization, and the `run_multi_factor_backtest_workflow(...)` orchestrator that mirrors `_validate_factor_workflow` `(api.py:182)`.
- `src/quant_forge/synthesis/methods.py` — the method/standardization catalog constant + validators (server-side re-validation of `buildRunRequest` rules).
- `rebalance_indices(...)` pure helper — added to `backtesting/service.py` (or a small shared module) and consumed by **both** the engine loop and the synthesis IC fit (RB-5).

**Endpoint 1 — `GET /api/synthesis/methods`.** Add a GET branch near the other catalog routes `(routing.py:168, /api/data/catalog)`:

```python
elif path == "/api/synthesis/methods":
    self._require_control_token()
    self._json(_server._synthesis_methods_payload(config))   # returns the §9 dict
```

**Endpoint 2 — `POST /api/jobs/multi-factor-backtest`.** Add a POST job branch next to the existing `/api/jobs/*` routes `(routing.py:315–381)`, same `job_manager.start(name, lambda cancel_event: …, status=202)` pattern:

```python
if path == "/api/jobs/multi-factor-backtest":
    self._json(
        job_manager.start(
            "multi_factor_backtest",
            lambda cancel_event: _server.run_multi_factor_backtest_workflow(
                config,
                factor_refs=_synthesis_factor_refs(payload.get("factor_refs")),   # [{factor_id, direction±1}]
                synthesis=_synthesis_block(payload.get("synthesis")),             # {method, params}
                standardization=_optional_standardization(payload.get("standardization")),
                parameters=_optional_parameters_payload(payload.get("parameters")),
                rd_config=research_config,
                cancel_event=cancel_event,
            ),
        ),
        status=202,
    )
    return
```

**Workflow orchestration** (`run_multi_factor_backtest_workflow`, mirrors `_validate_factor_workflow` `api.py:182`):

1. Re-validate: ≥2 factors, each direction ∈ {+1,−1}, method available, method params present/typed, `weights` covering exactly the checked set, `holding_days` a **required** positive int (raise if absent — no `horizon_days` fallback, RF-5), members resolve to **one compatible universe** (RF/RB-6). All client guards re-asserted server-side (`synthesis.js:495–535`).
2. Build settings from flat FE params via the composite analog of `_idea_validation_settings` `(api.py:402)` → `SimulationProfile(top_quantile, decay_days, execution_delay_days, test_period_start=backtest_start, test_period_end=backtest_end)` + `TransactionCostModel(...)`, plus the pinned `universe_filters`. **`holding_days` required.** Enforce the §3 window preconditions (`WINDOW_TOO_SHORT`; fitted downgrade / `NO_FITTED_PERIODS`).
3. Fetch each member's scores with the **one pinned universe** (`prepare_factor_scores_result`), standardize (deterministic tie policy), apply direction, combine (a-priori, or PIT IC/ICIR on the shared `rebalance_indices` grid with `_with_period_return` forward returns, §4.4), accumulate coverage, and pre-scan for degenerate cross-sections and empty/thin rebalances (record counts + warning codes).
4. Materialize composite as a synthetic `precomputed:` factor (§11): derive `composite_id = COMPOSITE_<hash-of-all-inputs>` (RB-10), write values **through the store's own path resolution** (RF-3), and **`FactorRepository(config.paths.factor_root).save(FactorDefinition(...))`** with `formula="precomputed:factor_id=<composite_id>"`, `horizon_days=holding_days`, `source="synthesis"`, `status="candidate"`, the pinned `universe_filters` (RF-1, RF-2). The definition MUST land in `factor_root` (the engine resolves it via `FactorCatalog(factor_root).get(...)` at `service.py:96–100`, never from the values overlay).
5. Drive the engine: `run_factor_backtest(composite_id, …, sample_role="external_oos_backtest", holding_days=…, transaction_costs=…, factor_values_overlay_root=<overlay>, simulation_profile=<profile with decay_days=0>)`. **Decay is pinned to 0 on this engine-driving profile** (LA-1) because members were already decayed pre-combination. Assert `resolved_schedule.signal_dates == dates[rebalance_indices(...)]` (RB-5/RB-7).
6. Fill the `evaluation` slot from a same-window `evaluate_factor` (FP-2), assemble the §8 payload with `_backtest_payload`/`_evaluation_payload` + `synthesis_provenance`, and clean up the synthetic factor artifacts (both the `factor_root` definition and the overlay values) on failure (mirror `_restore_factor_after_failed_validation` `api.py:246`).

**Job framework reuse.** The `job_manager.start(...)` + `waitForJob` + `cancelJob` lifecycle is exactly what `synthesis.js` already drives (`api.js` imports at `synthesis.js:40`) — no new job infra.

---

## 11. Reuse map (existing functions to call, signatures)

| Function / signature | Ref | Use |
|---|---|---|
| `prepare_factor_scores_result(panel, formula, universe_filters, *, profile, factor_id, factor_name, factor_values_root, factor_values_overlay_root) -> FactorScoreResult` (`.scores` = `[trade_date,instrument,score]`) | `signal_processing.py:45` | Per-member PIT/decay/precomputed-aware score frame to standardize+combine. **`universe_filters` is a separate arg, not on the profile** — pass the one pinned set to every member (RB-6). |
| `FactorValueStore._resolve_factor_paths(factor_id, factor_name, formula) -> paths` then `FactorValueStore.write_incremental_values(paths.write_dir, *, factor_id, factor_name, formula_signature, scores)` | `value_store.py:226,~_resolve_factor_paths` | Materialize composite as cached factor values **using the store's own canonical layout** (RF-3). Do **not** hand-build `overlay_root/composite_id`: the read path resolves dirs via `_factor_dir_candidates → _canonical_factor_dir_name` / `_looks_like_factor_dir` `(catalog.py:397–405)`, which a raw dir with parquet only under `incremental/` does **not** match, so `cache_only` reads would return zero rows and the backtest would produce an empty schedule. |
| `is_precomputed_formula(formula)` / formula `"precomputed:factor_id=<ID>"` | `catalog.py:25` | Make the engine read cached composite values (`cache_only` path) instead of recomputing. `<ID>` must match `[A-Za-z0-9_.=-]` (colon-free, RF-1). Requires `factor_values_overlay_root` or it raises `(signal_processing.py:94)`. |
| `FactorRepository(factor_root).save(definition)` | `repository.py:89–95` | Register the synthetic composite `FactorDefinition` **in `factor_root`** (RF-2). There is **no free `register_factor` symbol** in `src/`; the engine reads the definition via `FactorCatalog(factor_root).get(...)` `(service.py:96–100)`, so it must land in `factor_root`, not the values overlay. |
| `run_factor_backtest(factor_id, *, factor_root, data_root, artifact_root, top_quantile, holding_days, group_count, simulation_profile, transaction_costs, sample_splits, factor_values_root, factor_values_overlay_root, factor_values_manifest_root, sample_role, first_signal_date, include_partial_final_period) -> BacktestResult` | `service.py:49` | The full schedule/cost/NAV/segment/typed-metric engine, driven by the synthetic composite id. Backtest gate = `max(2, holding+delay+1)` in-window dates (RF-4). Reused with two minimal additive fixes (deterministic tie-break RB-3, skip-warning emission RB-7). |
| `rebalance_indices(dates, *, delay, holding, start_signal_index)` (new pure helper wrapping `range(start_signal_index, len(dates)-delay-1, holding)`) | `service.py:136,141` | Single source of truth for the rebalance grid, shared by the engine loop and the synthesis IC fit (RB-5). |
| `_with_period_return(...)` | `service.py:702` | Realized close-to-close `[entry,exit]` return with last-mark/survivorship fill — reused by the synthesis IC fit so weight-driving IC matches engine-realized returns (RB-5). |
| `evaluate_factor(factor_id, *, factor_root, data_root, artifact_root, horizon_days, horizon_days_matrix, sample_splits, simulation_profile, factor_values_root, …) -> EvaluationResult` | `evaluation/service.py:45` | Fill the `evaluation` payload slot over the **same** backtest window (same-window diagnostics, FP-2). May raise via the 126 default `min_trading_days` — size the window accordingly (RF-4). |
| `_validate_factor_workflow(...)` orchestration template | `api.py:182` | Copy the evaluate + backtest + payload-assembly + failure-restore skeleton, swapping the single factor for the materialized composite. |
| `_idea_validation_settings / _default_validation_parameters` | `api.py:402, 379` | Map flat FE params → `SimulationProfile` + `TransactionCostModel`. **Write a composite analog** that requires `holding_days` explicitly and does **not** default it to `factor.horizon_days` (RF-5). |
| `_backtest_payload / _evaluation_payload / _json_safe` | `api.py:1077, 1011, 1322` | Exact JSON field shapes `factor.js` renderers consume (all top-level scalar tiles, FP-3). |
| `_transaction_cost_rate / _portfolio_turnover / _portfolio_weights / _leg_rebalance_rate / _group_returns / _with_period_return` | `service.py:1160,1151,1138,1132,1119,702` | Already invoked inside the loop — documented so the composite path inherits identical accounting. |
| `SimulationProfile` / `TransactionCostModel` / `FactorDefinition` dataclasses | `contracts.py:101, 238, 48` | Construct settings inside guard rails; `FactorDefinition.__post_init__` enforces the colon-free id charset (RF-1). |
| `job_manager.start(name, fn, status=202)` | `routing.py:315` | Register the POST job route. |

**Composite id derivation (RB-10).** `composite_id` is `COMPOSITE_<hash>` where `<hash>` is the first 10–16 hex chars of a stable digest (e.g. SHA-1) over a **canonical JSON of ALL inputs**: ordered `(factor_id, direction)` list, `method`, `method_params`, `standardization`, `backtest_start`, `backtest_end`, `decay_days`, `execution_delay_days`, `top_quantile`, `coverage_rule`, `min_factor_coverage`, and `universe_filters`. This is required because `write_incremental_values` merges existing+new by `(trade_date,instrument)` keeping last but **retains prior-run rows for dates absent from the new run** `(value_store.py:583 _merge_score_updates)`, and reads are filtered by `formula_signature`. If the id hashed only the factor set, a second run with different params but the same id would read a **poisoned blend** (new values where dates overlap, stale prior-run values where they don't) — a silent wrong backtest. Hashing all inputs makes any config change mint a fresh id. **Additionally**, materialization targets a **per-run overlay directory** that is not reused across runs, so even a hash collision cannot surface stale rows. `test_composite_id.py` (§13) asserts changing any single input changes the id.

**Composite materialization sketch (corrected):**

```python
def materialize_composite(composite_scores, *, factor_root, overlay_root,
                          composite_id, name, holding_days, universe_filters):
    # composite_scores: DataFrame [trade_date, instrument, score] (standardized+combined+PIT, decay already on members)
    store = FactorValueStore(overlay_root, write_root=overlay_root)
    formula = f"precomputed:factor_id={composite_id}"          # composite_id is colon-free COMPOSITE_<hash>
    paths = store._resolve_factor_paths(                       # RF-3: use the store's own layout
        factor_id=composite_id, factor_name=name, formula=formula)
    store.write_incremental_values(
        paths.write_dir, factor_id=composite_id, factor_name=name,
        formula_signature=formula, scores=composite_scores)

    FactorRepository(factor_root).save(FactorDefinition(       # RF-2: register in factor_root
        factor_id=composite_id, name=name, source="synthesis",
        formula=formula, status="candidate", horizon_days=holding_days,
        universe_filters=universe_filters))                    # RB-6: the one pinned set, not ()
    return composite_id
```

---

## 12. Correctness invariants (checklist)

- [ ] **Causal signal→execution.** Composite at `signal_date` traded at `entry_date = dates[i+delay]`, `delay≥1`; the signal bar's return is never earned (`service.py:143,165`; ref).
- [ ] **Return attribution from fill.** Period return is close-to-close over `[entry_date, exit_date]`, not from `signal_date` (`service.py:702`; ref).
- [ ] **Latest-value as-of.** Each rebalance uses `scores[trade_date == signal_date]` — freshest composite (`service.py:161`; ref).
- [ ] **Shared rebalance grid.** The synthesis IC fit and the engine trade the **same** `rebalance_indices(...)` grid; `resolved_schedule.signal_dates` asserted equal (RB-5).
- [ ] **PIT IC fit.** Fitted weights at `d` use only periods with `idx(s)+delay+holding ≤ idx(d)`, on the shared grid, with `_with_period_return` forward returns (§4.4; ref/de Prado).
- [ ] **No double decay.** Decay applied once, on members; engine-driving profile pinned `decay_days=0` (LA-1).
- [ ] **No pooled standardization.** zscore/rank computed **per trade_date** only (`qgpt composite.py:66`; §4.2).
- [ ] **Explicit directions.** ±1 locked at request time, echoed raw, never auto-flipped (`synthesis.js:497`; reject `qgpt backtest.py:253`).
- [ ] **holding_days required.** Positive int, validated client + server; the composite settings analog **raises** if absent, no `horizon_days` fallback (RF-5).
- [ ] **One pinned universe.** Same `universe_filters` on every member and the composite; conflicting member universes rejected; `is_st`/listing at formation only, mid-hold ST/delist carried to last mark (RB-6).
- [ ] **Real backtest history gate.** Engine gate is `max(2, holding+delay+1)` in-window dates (RF-4), **not** 126; synthesis adds `WINDOW_TOO_SHORT` and fitted-window preconditions (RB-2/RB-8). `126` only degrades annualization `status` / applies to the `evaluate_factor` default.
- [ ] **Coverage honesty (FP-4).** `coverage_ratio` real `null` when unobservable → `n/a`, never `0` (`synthesis.js:349`).
- [ ] **is_fitted truthful.** a-priori ⇒ `false`; IC/ICIR ⇒ `true` **only if ≥1 rebalance actually fitted**, else downgrade with `NO_FITTED_PERIODS`; warmup periods flagged; `fitted_period_fraction`/`warmup_period_count` exposed (RB-8).
- [ ] **Fitted weights not mislabeled.** Fitted vectors go in `fitted_weights_latest/_path`, never `weights_effective` (FP-1). `weights_effective` is a-priori raw only.
- [ ] **Degenerate cross-section.** All-NaN and all-equal composites converge on skip-with-warning `DEGENERATE_CROSS_SECTION`, not arbitrary tie trading (RB-9).
- [ ] **Skipped rebalance flagged.** Empty/thin rebalances emit `REBALANCE_SKIPPED_NO_COVERAGE`/`_THIN` + ledger stub + provenance count; never a silent NAV gap (RB-7).
- [ ] **Deterministic ranking.** Engine sort is stable `mergesort` on `["score","instrument"]`; standardization uses a deterministic tie policy; quantile membership reproducible (RB-3).
- [ ] **Costs on realized turnover, bias disclosed.** `traded_notional_rate` L1 change; gross+net separate; drift-back-uncosted / `count`-churn bias stated in caveats (RB-4; `service.py:192,206`).
- [ ] **Non-overlap disclosed.** `NON_OVERLAPPING_COHORTS` + `PHASE_SENSITIVE_SMALL_SAMPLE` + `period_count` emitted; cadence==lifetime stated (RB-1).
- [ ] **Final partial period.** Excluded by default, flagged `FINAL_PARTIAL_PERIOD_EXCLUDED`; never silent (`service.py:155`).
- [ ] **Colon-free ids.** `factor_id`/formula key match `[A-Za-z][A-Za-z0-9_=-]*` / `[A-Za-z0-9_.=-]` (RF-1).
- [ ] **Store-resolved materialization path.** Written via `_resolve_factor_paths`, definition saved to `factor_root`; round-trip read asserted (RF-2/RF-3).
- [ ] **Composite id covers all inputs.** `COMPOSITE_<hash-of-all-inputs>` + per-run overlay; no stale-blend reads (RB-10).

---

## 13. Testing plan

**Unit (`tests/synthesis/`)**
- `test_standardize.py` — zscore/rank are per-date, mean≈0 / rank∈[-1,1]; degenerate `std==0` cross-section → zero contribution flagged, not NaN-propagated; deterministic tie policy reproducible.
- `test_direction.py` — `−1` exactly negates the standardized score; `+1` identity; provenance echoes the declared value.
- `test_combine_apriori.py` — `equal_weight` == uniform; `weighted` uses raw declared weights; missing factor at `(d,i)` excluded per `coverage_rule`.
- `test_combine_ic_pit.py` — **the anti-peek test.** Construct a factor whose IC flips sign after `d0`; assert the weight at `d ≤ d0` cannot see post-`d0` forward returns (eligible filter `idx(s)+delay+holding ≤ idx(d)`), computed on the **shared** `rebalance_indices` grid; assert warmup periods degrade to equal-weight with `WARM_UP_IC_UNFITTED`; assert an all-warmup window downgrades to `is_fitted=false` with `NO_FITTED_PERIODS`.
- `test_icir_degenerate.py` — constant/near-constant IC series (`std==0`) and non-finite raw weights clip to 0 via finite check; all-zero vector falls back to equal-weight with `IC_DEGENERATE_EQUAL_WEIGHT`, never emptying the rebalance (RB fix).
- `test_double_decay.py` — a two-member composite run with `decay_days=10` stores composite values that are **not** EWMA'd a second time; engine-driving profile has `decay_days=0` (LA-1).
- `test_grid_fidelity.py` — synthesis `rebalance_indices` == engine realized `resolved_schedule.signal_dates` across delay/holding/start permutations (RB-5).
- `test_degenerate_cross_section.py` — all-NaN and all-equal composite dates both skip-with-`DEGENERATE_CROSS_SECTION`, no arbitrary long/short book (RB-9).
- `test_skipped_rebalance.py` — a stretch of empty/thin coverage emits `REBALANCE_SKIPPED_NO_COVERAGE`/`_THIN`, a ledger stub, and a `skipped_rebalances` count; NAV does not silently compound across the gap (RB-7).
- `test_universe_pinning.py` — heterogeneous member universes rejected (`UNIVERSE_MISMATCH`); under `min_factor_coverage<all`, an ST name covered only by a permissive member does **not** enter the book (RB-6).
- `test_coverage.py` — `rows_scored/rows_in_composite/coverage_ratio`; unobservable ratio emits real `null`; `rows_full_coverage` under `all_factors`.
- `test_composite_id.py` — changing **any** input (factor set/order, direction, method, params, standardization, window, decay, delay, top_quantile, coverage_rule, universe) changes `composite_id`; per-run overlay never reused (RB-10).
- `test_materialize.py` — write via `_resolve_factor_paths` round-trips; the **engine actually reads the rows back** through `cache_only` (`precomputed:` resolves via `factor_values_overlay_root`); definition is found via `FactorCatalog(factor_root)` (RF-2/RF-3).
- `test_ties.py` — tie-heavy composite (all-equal rank sums): quantile membership and long/short split are byte-stable across runs given the stable `mergesort`+instrument tie-break (RB-3).

**Contract (`tests/test_web_synthesis_*`)**
- `test_methods_catalog.py` — `GET /api/synthesis/methods` matches the §9 schema; every method has valid `ParamSpec`s; `weights` type present; `is_fitted` truthful; standardizations = `[zscore, rank]`.
- `test_multi_factor_request.py` — `POST /api/jobs/multi-factor-backtest` rejects <2 factors, non-±1 direction, **missing `holding_days`** (no fallback, RF-5), `weights` not covering the checked set, conflicting universes, and `WINDOW_TOO_SHORT`.
- `test_multi_factor_payload.py` — response has `factor / parameters / evaluation / in_sample_backtest / backtest / validity / synthesis_provenance` with every field `renderSynthesisReportHtml` reads (`synthesis.js:445–472`, `renderProvenanceCardHtml:371`, `renderCoverageByRoleHtml:334`); `backtest` carries all top-level scalar tiles (FP-3); coverage ratios `null` not `0`; typed `MetricValue` status preserved; **fitted runs omit `weights_effective` and carry `fitted_weights_*`** (FP-1); a-priori runs carry `weights_effective` and omit fitted fields.
- Drive `renderSynthesisReportHtml` / `renderProvenanceCardHtml` with the payload fixture (pure renderers, `synthesis.js:2–7`) — asserts contract closure end-to-end without a browser, including that a fitted-run payload does **not** render a wrong a-priori caption over its weights.

**Golden end-to-end on `cn_a`**
- Pick 2–3 registered factors, run the full job over the demo `cn_a` panel (respecting the real `max(2, holding+delay+1)` gate and the synthesis window preconditions). Assert: a stable `composite_id` (colon-free `COMPOSITE_…`), gross+net metrics present with sane `status`, `resolved_schedule` shows `signal_date < entry_date` by `delay` and exit at `entry+holding` and equals the synthesis grid, turnover in [0,2], `period_count` and `NON_OVERLAPPING_COHORTS` present, and a byte-stable artifact JSON (golden file). Add an `ic_weighted` golden and an `equal_weight` golden to lock both branches.

---

## 14. Phased build plan (small landable steps)

1. **P1 — Catalog endpoint.** Ship `GET /api/synthesis/methods` (§9 constant + `_synthesis_methods_payload`) and its route (`routing.py:168`). FE flips from 方法目录不可用 to a live method form. *Landable alone; no engine changes.*
2. **P2 — Engine honesty fixes + grid helper.** Land the two minimal additive engine changes — deterministic `mergesort` tie-break (RB-3) and skip-warning ledger stub (RB-7) — plus the pure `rebalance_indices(...)` helper (RB-5). Unit-tested against the single-factor path so no regression; golden single-factor artifact re-baselined once for the deterministic sort.
3. **P3 — Composite core (a-priori).** `synthesis/service.py`: one-pinned-universe fetch → standardize (deterministic ties) → direction → `equal_weight`/`weighted` combine → coverage → degenerate/skip pre-scan. Pure functions, unit-tested. No endpoint yet.
4. **P4 — Materialize + drive engine.** `composite_id` all-input hash (RB-10) + per-run overlay; `materialize_composite` via `_resolve_factor_paths` (RF-3) + `FactorRepository.save` to `factor_root` (RF-2); engine-driving profile pinned `decay_days=0` (LA-1); call `run_factor_backtest` by id. Golden e2e for `equal_weight`; materialize→read round-trip test.
5. **P5 — Job endpoint + payload.** `POST /api/jobs/multi-factor-backtest` route (`routing.py:315` pattern) + `run_multi_factor_backtest_workflow` mirroring `_validate_factor_workflow` (`api.py:182`); assemble §8 payload with reused `_backtest_payload`/`_evaluation_payload` (all scalar tiles, FP-3) + `synthesis_provenance`; same-window `evaluation` with honesty caveat (FP-2). Full contract tests green; FE renders a real report.
6. **P6 — Fitted methods (PIT IC/ICIR).** Add `ic_weighted`/`icir_weighted` with the §4.4 embargoed expanding fit on the shared grid, `_with_period_return` forward returns, NaN-hardened ICIR; `is_fitted` truthful with `NO_FITTED_PERIODS` downgrade and `fitted_period_fraction`; fitted weights in `fitted_weights_latest/_path` (never `weights_effective`, FP-1); anti-peek + grid-fidelity + ICIR-degenerate unit tests. Add rank-IC redundancy diagnostic (advisory).
7. **P7 — Honesty/degraded polish.** Coverage `null` propagation, a-priori `weights_effective` raw echo, `standardization_pinned_by_method`, `min_factor_coverage`/`coverage_rule` wiring, `NON_OVERLAPPING_COHORTS`/`PHASE_SENSITIVE_SMALL_SAMPLE`/`DEGENERATE_CROSS_SECTION`/skip codes surfaced; verify FP-1/FP-4 across the provenance card.
8. **P8 (optional, deferred) — Engine refactor & overlapping cohorts.** Extract `run_backtest_on_scores(scores_df, close_panel, holding, delay, top_quantile, group_count, costs, …)` from `service.py:141–281` so composite and single-factor share one primitive (removes the materialization round-trip). Then add K>1 **overlapping-cohort** averaging (ref Jegadeesh–Titman / de Prado) via the staggered-entry hook (`first_signal_date`, `STAGGERED_COHORT_ROLE`, `service.py:721`) and drifted-weight turnover/cost accounting (studio `engine.py:916`), which directly changes the numbers users trust. A separate cadence-vs-holding param is a new FE contract — out of current scope.

---

## Review resolutions — traceability changelog

Each adversarial-review finding, its resolution, the section it landed in, and the evidence (`file:line` or the regression test) it was verified against. This table replaces the earlier prose restatement; every code's rationale remains findable here.

| Code | Severity | What changed | Section(s) | Evidence / test |
|---|---|---|---|---|
| **RF-1** | Blocking | Colon ids and `precomputed:factor_id=composite:…` formulas replaced with colon-free `COMPOSITE_<hash>`; otherwise `FactorDefinition` registration raises and the engine never enters `cache_only`. | §5, §8, §11, §12 | `contracts.py:62`; `catalog.py:25–28` |
| **RF-2** | Blocking | `register_factor` does not exist — use `FactorRepository(factor_root).save(FactorDefinition(...))`; the definition must land in `factor_root`. | §10, §11 | `service.py:96–100` (`FactorCatalog(factor_root).get`) |
| **RF-3** | Blocking | Resolve the write dir via `FactorValueStore._resolve_factor_paths`, not a hand-built `overlay_root/composite_id`; else `cache_only` reads zero rows and the schedule is empty. | §11 | `test_materialize.py` (engine reads rows back) |
| **LA-1** | Major | Pin the engine-driving profile to `decay_days=0`; decay is a member-level transform applied once before combination. | §4.1, §5, §10, §12 | `test_double_decay.py` |
| **RB-2 / RF-4** | Major | Backtest gate is `max(2, holding+delay+1)`; `126` is a metric-status floor / `evaluate_factor` default, neither of which raises on the backtest path. Added synthesis `WINDOW_TOO_SHORT` + fitted-window preconditions. | §3, §12 | `service.py:106–109`, `service.py:33` |
| **RB-8** | Major | Fitted → `equal_weight` with `is_fitted=false` + `NO_FITTED_PERIODS` when zero rebalances fit; expose `fitted_period_fraction`/`warmup_period_count`; `weights_effective` never a warmup 1/N vector. | §3, §4.4, §9 | §5 window precondition |
| **RB-1** | Major | Authoritative single-knob caveat: non-overlapping `N≈len/holding` sample + start-phase sensitivity; `NON_OVERLAPPING_COHORTS`/`PHASE_SENSITIVE_SMALL_SAMPLE` + `period_count` emitted. | §1, §6 | `service.py:141,143,144` |
| **RB-3** | Major | Stable `mergesort` on `["score","instrument"]` in the engine + deterministic tie policy in standardization (minimal additive fix); golden-file stability. | §4.2, §5, §12 | `test_ties.py` |
| **RB-7** | Major | Emit `REBALANCE_SKIPPED_NO_COVERAGE`/`_THIN` + a ledger stub + a provenance count (synthesis pre-scan ships first; additive engine stub backs it). | §4.5, §6 | `test_skipped_rebalance.py` |
| **RB-5** | Major | Shared pure `rebalance_indices(...)` used by both engine and IC fit; reuse `_with_period_return`; assert `resolved_schedule.signal_dates == dates[rebalance_indices(...)]`. | §4.4, §6, §10 | `test_grid_fidelity.py` |
| **RB-6** | Major | One explicit `universe_filters` passed to every member and the composite; reject conflicting universes; formation-only `is_st`/listing with mid-hold last-mark. | §3, §4.1, §11 | `test_universe_pinning.py` |
| **RB-10** | Major | `composite_id` = hash of ALL inputs, materialized to a per-run overlay; prevents `_merge_score_updates` retain-old-dates poisoning. | §11 | `test_composite_id.py` (`value_store.py:583`) |
| **FP-1** | Major | Route fitted vectors to `fitted_weights_latest`/`_path` and **omit** `weights_effective` for fitted runs (FE caption is unconditional a-priori); a-priori runs keep `weights_effective`. | §4.4, §8 | `synthesis.js:386–388` |
| **ICIR NaN-hardening** | Minor | Pseudocode replaced bare `max(nan,0)` with finite checks, an explicit `std==0` guard, and an all-zero→equal-weight fallback (`IC_DEGENERATE_EQUAL_WEIGHT`). | §4.4 | pseudocode (`ic_weights_asof`) |
| **RB-4** | Minor | Disclose the drift-back-uncosted turnover/cost understatement + `count`-recompute churn in `validity.caveats`; drifted-weight accounting deferred to P8. | §6, §7 | `service.py:192,206` |
| **RB-9** | Minor | All-NaN and all-equal composites converge on a single flagged skip (`DEGENERATE_CROSS_SECTION`). | §4.2, §5 | §4.2 |
| **FP-2** | Minor | Commit to same-window diagnostics with an explicit caveat + `basis`; the hard-coded FE title noted in Open questions. | §1, §8, §10 | `evaluation.meta.basis` |
| **RF-5** | Minor | Composite settings analog **raises** on missing `holding_days` rather than defaulting to `factor.horizon_days`. | §6, §10, §11, §12 | `api.py:411,384` |
| **FP-3** | Nit | Explicit note that `backtest` is `_backtest_payload` verbatim; enumerate the top-level scalar tiles the FE reads. | §8 | `factor.js:136–176` |
| **weights_effective label** | Nit | Resolved by RB-8/FP-1: `fitted_period_fraction`, `warmup_period_count`, and `fitted_weights_latest` now qualify the label. | §8 | see RB-8 / FP-1 |

---

## Open questions / deferred

- **FE caption for fitted weights (from FP-1/FP-2).** The provenance card hard-codes “权重为先验原始声明值” for `weights_effective` and the evaluation section title “样本内研究评价”, with no `is_fitted` / same-window branch (`synthesis.js:386–388`, `factor.js:117,126`). Shipping keeps honesty in the backend (omit `weights_effective` for fitted; carry caveats). A follow-up FE change should add a dedicated caption for `fitted_weights_latest/_path` (“最新拟合权重（时变，非先验）”) and a same-window label for the evaluation section. Deferred as an FE-contract change.
- **Overlapping tranches / cadence ≠ lifetime (RB-1).** Decoupling rebalance cadence from holding period (Jegadeesh–Titman 1/`holding`-per-bar averaging) materially changes turnover/Sharpe/IC and would give many more independent observations. It requires the staggered-entry hook plus a new FE parameter, so it is deferred to P8 rather than shipped; the current product discloses the constraint instead of hiding it.
- **Drifted-weight cost accounting (RB-4).** Costing against a daily-drifted book (rather than the previous target book) would remove the disclosed turnover/cost understatement; deferred to P8 with the shared-primitive extraction.
- **Full `run_backtest_on_scores` extraction (P8).** Ships as an optional refactor to remove the materialization round-trip entirely; the shared `rebalance_indices` helper (P2) already closes the correctness-critical grid-fidelity gap in the interim.

## CP0 amendments (2026-07-09, Fable — post-adversarial-review)

- **§8 provenance:** `synthesis_provenance.factors[]` carries `formula` — each
  member's formula **pinned at run time** — so downstream consumers (e.g. an
  external-backend translator) never depend on the live registry's current
  state; consumers must refuse on drift (closed code `MEMBER_FORMULA_DRIFT`).
  FE renderers ignore the extra key (additive change).
- **§9 interim state:** until P6 lands, the shipped catalog marks
  `ic_weighted`/`icir_weighted` as `available:false` (reserved 预留) per the
  TASK brief; §9's literal JSON is the **post-P6** end state.
- **§11 RF-3 evidence correction:** `_looks_like_factor_dir` *does* match an
  `incremental/`-only parquet dir (`catalog.py:406-408`); the reason hand-built
  `overlay_root/<id>` dirs still fail is the candidate-name/category resolution
  in `value_store.py:180-198`. RF-3's operative rule is unchanged: materialize
  via `FactorValueStore._resolve_factor_paths` and assert engine read-back.

**Key design commitments recap:** reuse `run_factor_backtest`'s schedule/cost/NAV/metric machinery, adding only two minimal additive honesty fixes (deterministic tie-break, skip-warning stub) and a shared `rebalance_indices` helper; materialize the composite as a colon-free `COMPOSITE_<hash-of-all-inputs>` `precomputed:` factor via the store's own path resolution and `FactorRepository.save` into `factor_root`; pin decay to members only (engine profile `decay_days=0`); pin one universe across members and composite; cadence == `holding_days` ⇒ non-overlapping K=1, disclosed with a period count and phase caveat; fitted IC/ICIR weights estimated on the engine's exact grid with `_with_period_return` forward returns and a strict `idx(s)+delay+holding ≤ idx(d)` embargo, downgrading honestly on short windows; fitted weights never routed through the a-priori-captioned `weights_effective`; every coverage/metric honestly typed with real `null` for the unobservable and explicit warning codes for every skip/degeneracy.