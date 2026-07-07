# Phase A Compatibility Notes

Behavior changes that Phase A fixes will introduce, for downstream users and
for the Phase B baseline. No public function signatures are removed; changes
are numeric/semantic and additive-field only.

| Fix | Behavior change | Who sees it | Compat action |
| --- | --- | --- | --- |
| A-P1-2 backtest segment embargo | IS `segment_metrics` shrink (straddling periods excluded); IS cumulative/annualized figures change on every run with splits | RD gates (OOS-decay clauses), web segment display | Numbers change BY DESIGN (same rationale as QUANT-1). Flag in release notes; RD thresholds may need re-tuning on real data |
| A-P1-1 delisting realization | Net/gross returns drop on panels with mid-period disappearances; new lost-position counter field | real-data users; demo data unaffected (gap-free) | Additive field on BacktestResult; document that pre-fix results on gapped data were biased upward |
| A-P1-3 missing-OOS gate evidence | Candidates without OOS observations now block (or warn — pending default decision) instead of passing | RD auto-candidate flows | If default=block, some previously "passed" candidates re-grade; trace records reason code `INSUFFICIENT_OOS_EVIDENCE` |
| A-P1-4 metrics-map consumers | Web/report render status badges instead of 0.00 for null metrics | web UI, MD reports | Display-only; JSON adds fields, removes none |
| A-P2-1/2 snapshot fallbacks | Snapshot-derived panels: missing `market_cap` stays NaN (was fabricated 1.0); warmup derived fields NaN not 0.0 → those rows drop under nan_policy=drop. `is_st` REMAINS a schema-stability False (the snapshot schema has no ST field) but the synthesis is now disclosed via `DataValidationResult.synthesized_columns` | snapshot-based workflows | Coverage counts may drop slightly; validation result gains availability fields; capless snapshots now yield zero usable cap observations instead of degenerate all-tied ranks |
| A-P2-3 agent matrix/splits wiring | Agent-surface evaluations now match CLI/Web numbers for the same factor | MCP/agent users | Convergence fix; agent-side historical numbers were the divergent ones |
| D3 partial-final-period default flip (Phase C) | `run_factor_backtest(include_partial_final_period=...)` default flips True → False: the incomplete tail period is excluded from periods/NAV/segment metrics, `request.final_period_policy` reports `exclude_partial_final`, and the result carries `FINAL_PARTIAL_PERIOD_EXCLUDED` plus a human-readable warning telling the user how to opt back in. Passing `True` explicitly restores the legacy mark-to-market tail (still flagged `PARTIAL_FINAL_PERIOD`) | every `run_factor_backtest` caller (CLI `run-backtest`, workbench service, web idea-validation, RD loop selection/external-OOS backtests, staggered-entry cohorts) — none passed the flag explicitly, so all inherit exclusion | Resolves the COR-7 deferred decision by owner decision D3. Headline cumulative/annualized figures can shrink versus pre-D3 artifacts when a tail existed; comparisons against stored pre-D3 artifacts must re-run with `include_partial_final_period=True` to be like-for-like |

Deferred decisions (Fable → user; recorded, not silently changed):

- COR-7 `include_partial_final_period` default stays True in Phase A
  (flagged via `PARTIAL_FINAL_PERIOD` warning). Flip requires a product
  decision. (Resolved in Phase C by owner decision D3 — default now excludes
  the incomplete tail; see the D3 row above.)
- A-P1-3 default block-vs-warn for missing OOS evidence: implementation will
  ship configurable with a conservative default proposal (block when the
  OOS threshold is explicitly configured); flagged for user sign-off in the
  Phase A report.

Re-baselining guidance: any stored artifacts produced before Phase A fixes
remain valid AS ARTIFACTS (they carry their own request snapshots), but
cross-comparing pre/post Phase A segment metrics or gapped-panel backtests
is apples-to-oranges; compare within a schema generation only.
