# Look-ahead & Leakage Audit — Phase A

- **Tree:** `main` @ `7931522` (= audited `fa8f310` content, squash-merged)
- **Method:** full-file source read of the quant kernel
  (`evaluation/service.py`, `backtesting/service.py`,
  `factor_engine/{executor,signal_processing,formula_parser,value_store}.py`,
  `data/local.py`, `research_loop/candidate_gate.py`), plus targeted greps.
  Executable verification (pytest) pending — see baseline table in
  `quantitative_core_audit.md`.

## 1. Time semantics (single source of truth)

| Event | Time | Code anchor |
| --- | --- | --- |
| Signal formation | close(T) scores, per-day cross-section | `signal_processing.prepare_factor_scores_result` |
| Entry | close(T + execution_delay) | eval: `_with_forward_return` shift(−delay); backtest: `dates[signal_index + delay]` |
| Exit / label | close(T + execution_delay + horizon) | eval: shift(−(delay+horizon)); backtest: period end index |
| Mark-to-market | daily closes inside period | `_leg_cumulative_returns` |

## 2. Verified-safe (evidence-backed, re-checked on this tree)

| Concern | Verdict | Evidence |
| --- | --- | --- |
| Forward-return alignment | SAFE | per-instrument `groupby(...).shift(-k)` on a panel sorted in `load_panel`; no cross-instrument bleed |
| Signal-day fill (same-close execution) | STRUCTURALLY IMPOSSIBLE | `SimulationProfile.__post_init__` raises on `execution_delay_days < 1` (`core/contracts.py:114`); delay=0 cannot be constructed — verified this tree |
| IS/OOS1/OOS2 split leakage (evaluation) | SAFE | `_split_dates(..., embargo)` drops the trailing `embargo = delay + horizon` dates from every non-final split; boundary walk-through: last IS signal's label lands strictly before first OOS1 date |
| Rolling operators causality | SAFE | all `ts_*` ops backward-looking with `min_periods=window`; `decay_linear` recent-weighted; correlation/cov pairwise windows trailing |
| Rolling operator order-dependence | SAFE (COR-5 fixed) | `execute_factor_formula` stable-sorts (instrument, trade_date), restores order after |
| Cross-sectional ops | SAFE | `rank`/`zscore` computed per trade_date group only — no full-sample normalization anywhere in executor |
| EWMA decay (profile decay_days) | SAFE | per-instrument `ewm(span=..., adjust=False)` — strictly causal; raw-NaN positions re-masked after transform (`signal_processing._apply_ewma_decay`) |
| Test-period context | SAFE | `_profile_context_panel` extends lookback BEFORE test_period_start only for formula warm-up; truncates at `test_period_end`; scores restricted back to the working panel afterwards |
| Feature set | SAFE | panel fields are trailing quantities (`return_1d` = close pct_change, `volatility_5d` = trailing std); forward return exists only inside evaluation, never as a formula field |
| Train/selection vs audit OOS (RD) | SAFE (COR-2 fixed) | in-sample selection uses `evaluation_profile`; external OOS isolated to audit-only in gates & reporting |
| Candidate dedup / retry shopping | SAFE (COR-1 fixed) | `_result_signature_values` round-trips through trace; identical candidates not re-tried |
| Prompt/LLM determinism | SAFE-ish (COR-11) | hypothesis temperature default 0.0; repair/review lanes 0.1 (documented) |
| Duplicate (date,instrument) rows | GUARDED (DATA-1 fixed) | `_panel_quality_problems` rejects dup keys, NaN in required cols, dtype drift |

## 3. Open leakage-class findings

| ID | Class | Severity | Detail |
| --- | --- | --- | --- |
| A-P1-1 (NEW-1) | Survivorship / implicit look-ahead | P1 | Backtest formation inner-joins on price availability at entry AND scheduled exit. Exit-availability is future information at entry time: names that will delist/suspend are excluded from formation, and names lost mid-period drop out of the NAV mean (loss never realized). Effect is systematically anti-conservative on real data. |
| A-P1-2 (NEW-2) | Split leakage (backtest segments) | P1 | `_split_rows_by_signal_date` assigns periods to IS/OOS by signal date with no purge; an IS-attributed period realizes returns up to `delay+holding` days into OOS1. Evaluation side was fixed (QUANT-1); backtest side was not. RD's OOS-decay gates read these segment metrics. |
| A-P1-3 (NEW-3) | Evidence integrity (not leakage per se) | P1 | OOS gates silently pass when OOS values are `None` — absence of out-of-sample evidence is treated as compliance. |
| INT-2 (carried) | Degenerate audit window | Low | Single-window external OOS can duplicate the in-sample window when history is short; flagged in the prior review's integration test; warning-level. |
| Cache provenance | Hypothesis / design gap | P2→Phase B | Mounted `factor_values_root` scores are trusted by fingerprint; nothing proves an external store's values were computed causally. Mitigation belongs to the Phase B ArtifactRef/provenance layer; Phase A only documents the trust boundary. |

## 4. Explicit non-findings (checked, clean)

- No full-sample winsorization/truncation path exists (truncation
  unsupported by profile validation, fails fast).
- No neutralization path exists (neutralization='none' enforced) — so no
  future-beta/industry leakage possible today.
- No use of OOS metrics in objective scoring (objective reads selection
  sample only).
- No `eval`/`exec`/`import` reachable from formulas (hardened AST, canonical
  operator allowlist, SEC-1..4 verified).
- Annualization uses exposure days with a 126-day reportability floor;
  extrapolated figures carry explicit status (COR-6/COR-9 family verified).

## 5. Required regression coverage (feeds `docs/testing/point_in_time_test_plan.md`)

1. Split-boundary purge test for backtest segments (mirror of the evaluation
   embargo test) — currently ABSENT.
2. Delisting fixture (name stops quoting mid-period at a loss) — currently
   ABSENT.
3. Missing-OOS-evidence gate test — currently ABSENT.
4. Metrics-map consumer test (insufficient sample renders status, not 0.00)
   — currently ABSENT.
5. Existing embargo/ordering/dedup tests: keep as-is (they passed at 394 on
   this tree per the recorded gate).
