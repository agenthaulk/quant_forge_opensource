# Phase C Wave 1 — Review Resolution & Source Verification

## Adversarial review (Opus, on the uncommitted wave-1 tree): fix-first ×10 — ALL RESOLVED

| # | Finding | Resolution |
| --- | --- | --- |
| F1 major | strategy_selector_enabled was dead config (loaded, never passed at CLI/web construction) | Flag now passed at both entry points; disable-path test via real entry point |
| F2 major | Staggered aggregate hardcoded warning_codes=[] while cohorts drop D3 tails | Aggregate = sorted union of distinct cohort codes; cohort rows carry their codes |
| F3 major | Strategy context pooled trace rows across ALL runs (round_index over every run) | Context scoped to the current run (+ same-seed fingerprints only, for dedup); cross-run leakage test |
| F4 minor | D3 opt-in unreachable from every surface | include_partial_final_period passthrough: workbench + CLI flag + web parameter (default False) |
| F5 minor | redact_free_text bypasses (quoted values, key=value, arbitrary absolute paths) | Patterns hardened; each listed bypass pinned in tests |
| F6 minor | New trace rows shrank context/dedup reader windows | trace_store.read_recent_entries gained phase filtering BEFORE the limit; both readers use it |
| F7 minor | Duplicated evaluation_data_window helper | research_loop imports the workbench helper (acyclic verified); local copy deleted |
| F8 minor | duplicate_rate denominator excluded blocked plans | Denominator = ready + blocked (all attempted) |
| F9 nit | Falsification report carried codes in the prose warnings field | Report gains warning_codes; warnings now human-readable prose |
| F10 nit | new_run_id labeled naive datetimes with Z | Naive datetimes raise ValueError |

Also: 1 pre-existing test encoding the old D3 default updated (RD assessment
bundle test) and 3 web-test fakes gained the new backtest kwarg.

Gate after resolution: full pytest **555 passed** (wave-1 entry baseline 487),
release scan 141 files green, CLI smoke OK, `git diff --check` clean.

Note: the serial fix agent was interrupted by a session limit after
completing F1-F5/F8 and the failing-test fix; Fable verified the partial
state (compile + marker audit + full suite) and completed F6/F7/F9/F10
directly. No work was assumed done without on-tree evidence.

## Source verification (memo vs primary sources) — corrections adopted

The verify agent confirmed 10 memo claims against Vibe-Trading/QuantGPT
sources (file:line cited) and corrected 5; corrections that bind wave 2+:

1. Alpha-Zoo metadata: VT's real schema is id/nickname/theme(11)/formula_latex/
   columns_required/extras_required/requires_sector/universe/frequency/
   decay_horizon/min_warmup_bars/notes — the memo's field list
   (expected_direction, pit_safety, known_failure_modes) does not exist in VT;
   ours must be designed, not "ported".
2. VT does NOT redact filesystem paths in lineage (path redaction is our
   extension — keep it, it exceeds the reference).
3. VT's goal ledger is a SQLite session DB, not portable artifacts — our
   artifact-file adaptation is a deliberate deviation, correctly so.
4. QuantGPT has exactly 4 meta strategies; parameter_search is our extension
   (window tuning is its MutationEngine default branch).
5. VT goal-record fields (seed factor, dataset window, objective weights,
   risk boundary) are memo inventions — fine as OUR schema, not a port.

Gap additions surfaced by the verify agent are queued for wave-2 triage.

## CP3 adjudication — Codex wave-1 (C1-C6) + Opus wave-2 (O1-O10)

Fable adjudication: all 16 findings judged against source, tests, and the
FP axioms; **15 accepted fix-first, 1 deferred**. No finding was rejected.

| Item | Verdict | Landed in |
|------|---------|-----------|
| C1 selector reads last-traced candidate, not round winner | accepted (FP-5: the "round evidence" quantity must have one definition — the winner) | 4d4a4a3 |
| C2 global 200-row window truncates before seed filtering | accepted (busy stores silently erase a seed's history — evidence loss) | 4d4a4a3 |
| C3 segment vol/Sharpe treat partial tail as full period | accepted (FP-5 violation vs top-level metrics; FP-7 observation_count honesty) | 627bc34 |
| C4 redaction misses UNC + file:// | accepted (release policy: no location leaks) | 445d4c7 |
| C5 run-index read-then-append race | accepted (advisory flock; no-fcntl fallback documented, appends stay line-atomic) | 445d4c7 |
| C6/O7 --kind choices missing rd/falsification | accepted (derived from RUN_KINDS so it cannot drift again) | 4d4a4a3 |
| O1 vacuous goal completion (all-optional criteria) | accepted (FP-2: absence of required evidence must not read as success) | 4d4a4a3 |
| O2 research memory inert end-to-end | accepted (blocker-class: the feature existed but nothing fed or read it) | 4d4a4a3 |
| O3 retries inflate observation_count | accepted (FP-7) | 4d4a4a3 |
| O4 rules mintable from a single run | accepted (>=2 distinct runs; single-run patterns are findings at best) | 4d4a4a3 |
| O5 failure-classed rule minting drops failure row | accepted (rule candidacy must not hide the underlying failure) | 4d4a4a3 |
| O6 complete_goal bypasses transition table | accepted (one legal-transition authority; complete only from active/insufficient_evidence) | 4d4a4a3 |
| O8 falsification assembly duplication | **deferred to CP7** — refactor-only, no behavior divergence today; an element-wise IC-series parity test (delay=2 + truncation, 1e-9) now guards the seam until the dedup lands | guard test in 4d4a4a3 |
| O9 naive timestamps / unredacted memory fields | accepted (tz-aware enforced; data_window + failure_class pass redact_free_text) | 4d4a4a3 |
| O10 raw CLI tracebacks; symlink escape of artifact_root | accepted (resolve()-based containment) | 4d4a4a3 |

Session note: the CP3 serial fix agent was interrupted by a session limit
at its final full-suite verification step, with all 17 file edits already
on tree. Fable re-verified from scratch (compileall, per-item marker
audit, full suite, scan, CLI, diff-check) before landing the commits —
same protocol as the wave-1 interruption; no work assumed done without
on-tree evidence.

Gate after CP3: full pytest **613 passed** (CP2 baseline 588, +25 new
tests), release scan 148 files green, CLI smoke OK, `git diff --check`
clean.
