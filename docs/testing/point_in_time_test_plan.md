# Point-in-Time Test Plan — Phase A

Purpose: make every time-semantics guarantee in
`docs/reviews/lookahead_and_leakage_audit.md` executable and regression-proof.
Convention: every test below is test-first — it must FAIL on the unfixed tree
(for open findings) or pass as a characterization (for verified-safe areas).

## 1. New regression tests (open findings)

### PIT-1 Backtest segment embargo (A-P1-2, NEW-2)
- Fixture: synthetic panel, ~90 trading days, deterministic monotone factor;
  splits IS/OOS1 with a boundary date D; holding=5, delay=1.
- Assert: no period whose signal date < D realizes any daily return ≥ D
  while being attributed to IS `segment_metrics`; count of IS periods shrinks
  by exactly the straddling periods after the fix.
- File: `tests/test_backtesting_semantics.py` (extend).

### PIT-2 Delisting realization (A-P1-1, NEW-1)
- Fixture: 3 instruments; instrument C quotes until mid-period then stops
  (last close −40% from entry); A/B survive.
- Assert (post-fix): long-leg period return includes C's −40% realization;
  result carries a nonzero lost-position/delisting counter; NAV never
  silently re-normalizes C away.
- Assert (pre-fix, characterization to be flipped): C excluded from
  formation when exit-date price missing ⇒ demonstrates survivorship
  conditioning.
- File: new `tests/test_backtesting_delisting.py`.

### PIT-3 Missing-OOS-evidence gate (A-P1-3, NEW-3)
- Fixture: `FactorExperimentResult` with `min_oos_net_annualized_return`
  configured and (a) no `segment_metrics` at all, (b) OOS segments present
  with `net_annualized_return=None`.
- Assert: gate decision is blocked (or carries `INSUFFICIENT_OOS_EVIDENCE`
  warning per chosen default), NOT a clean pass.
- File: `tests/test_candidate_gate.py` (extend).

### PIT-4 Metrics-map consumers (A-P1-4, COR-4 consumer half)
- Fixture: evaluation result whose `rank_icir` MetricValue has
  status=`insufficient_sample`, value=None; flat legacy scalar present.
- Assert: web JSON payload / report renderer emit the status marker and no
  `0.00` for that metric.
- File: `tests/test_web_server.py` / report tests (extend).

### PIT-5 Snapshot fallback surfacing (A-P2-1/2, NEW-4/5)
- Fixture: snapshot rows lacking `is_st` and `market_cap`; panel with <5
  warmup days.
- Assert: validation result reports `is_st` unavailable and missing-cap
  ratio; derived warmup rows are NaN (not 0.0) and drop under
  nan_policy=drop.
- File: `tests/test_data_local.py` (extend).

## 2. Characterization tests to keep green (already passing at 394)

- Evaluation embargo boundary (QUANT-1) — last IS label strictly inside IS.
- Executor order-independence (COR-5) — shuffled panel ⇒ identical scores.
- HAC t-stat degenerate guards (COR-9).
- RD dedup signature round-trip (COR-1).
- External OOS audit-only isolation (COR-2).
- Catalog strict resolution & dup detection (CAT-1/2).
- Panel quality rejection (DATA-1).

## 3. Boundary matrix (parameterize where cheap)

| Axis | Values |
| --- | --- |
| execution_delay_days | 1 (default), 2 — delay=0 rejected at contract level (`core/contracts.py:114`), covered by a ValueError assertion |
| horizon/holding | 1, 5, 21 |
| split edge | signal at D−1, D, D+1 (D = split boundary) |
| history length | < 126d (non-reportable), ≥ 126d |
| panel gaps | none / mid-period gap / trailing gap (delisting) |

## 4. Execution

Runner: `PYTHONPATH=src python3 -m pytest -q` in the Phase A worktree.
Every fix commit pairs with its test in the same atomic commit; the failing
state is demonstrated in the commit message (protocol Step A4).
