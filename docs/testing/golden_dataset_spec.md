# Golden Dataset Spec — Phase A

Purpose: tiny deterministic panels with hand-derivable expected outputs that
pin the quant semantics forever. Golden tests are the last line of defense
against silent semantic drift (alignment, costs, splits, NAV).

Storage convention: NO binary fixtures. Each golden panel is built by a pure
in-code builder under `tests/golden_builders.py` (deterministic, no RNG, no
dates derived from "now"), and expectations are hand-derived constants with
the derivation written next to the assertion.

## GD-1 Alignment canary (look-ahead detector)

- Panel: 8 instruments × 60 trading days, close prices constructed so that
  the factor at day T is BY CONSTRUCTION the realized forward return over
  (T+delay, T+delay+horizon), delay=1, horizon=5.
- Expectation: daily rank IC = 1.0 exactly on every evaluable day under
  correct alignment. ANY off-by-one in entry/exit shifting drags IC below 1
  detectably (assert mean rank IC == 1.0 within 1e-12).
- Also run mirrored: factor = NEGATIVE forward return ⇒ IC = −1.0.
- Covers: `_with_forward_return` shifts, score/panel joins, sort stability.

## GD-2 Delisting realization (A-P1-1 regression)

- Panel: 3 instruments, 3 periods (holding=5). Instrument C quotes through
  period 2 day 3, last close = entry × 0.6, then disappears.
- Hand-derived expectations (post-fix): long-leg period-2 return includes
  C at −40%; `delisted/lost position` counter = 1; NAV path dated and
  monotone-consistent with the daily closes.
- Pre-fix behavior (characterization to flip): C excluded from formation
  (exit close missing) OR silently dropped from NAV mean — assert exposes it.

## GD-3 Split boundary attribution (A-P1-2 regression)

- Panel: 1 factor, 90 days, split boundary D at day 45, delay=1, holding=5.
- Signals at days 40–44 straddle D. Hand-derive: with embargo, IS contains
  only periods fully realized before D; each straddler is excluded from IS
  `segment_metrics` (and NOT double-counted into OOS).
- Assert exact period counts per segment and exact IS cumulative return.

## GD-4 Cost reconciliation

- Panel: 4 instruments, 2 periods, equal weights, known turnover:
  initial build = full notional, rebalance replaces exactly half the leg.
- Costs: commission 10 bps, slippage 15 bps, borrow 365 bps annual,
  holding 5 days ⇒ hand-computed: net_period = gross_period −
  (traded_notional × 25 bps) − (short_leg × 365/252 × 5 bps-annual-pro-rata).
- Assert `cost_reconciliation` dict equals the hand-derived decomposition
  and net == gross − total_costs within 1e-12.

## GD-5 Degenerate statistics

- Constant positive IC series (all days identical): HAC t-stat must carry
  degenerate guard status, ICIR MetricValue must NOT render as a huge
  number without its warning code; naive t-stat may be large but must carry
  `rank_ic_t_stat_naive` labeling only.
- Empty/short series (< minimum_required): every affected MetricValue has
  status `insufficient_sample`, value None — never 0.0.

## GD-6 Annualization boundary

- Exposure exactly 125 days vs 126 days: `reportable_annualization` flips
  from status-gated to available; extrapolated figure always labeled.

## Acceptance

Golden tests live in `tests/test_golden_semantics.py`, run in the default
suite, and are required green for Phase A acceptance (protocol Step A8).
