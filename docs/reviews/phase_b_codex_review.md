# Phase B Codex Review (Step B5) — Record

Reviewer: Codex (GPT-5-family; reported effort: high; requested xhigh —
actual capability reported honestly per protocol). Read-only inspection;
pytest could not run in its sandbox (no writable tmp) — code claims were
verified by inspection, suite state carried from the branch record (453).
Verdict: **REVISE-FIRST**.

Findings (severity, anchor):
1. major: ValidationGate overclaims PIT/data readiness vs
   quant_forge_target_architecture.md:121 and workflow doc promises;
   actual checks = operators + static field names only.
2. major: no-exec not structurally enforced — exact-string denylist accepts
   `bash`, `python`, `tool.exec`, `mcp__server__shell`; AgentWorkspaceTools
   does not consume AgentTaskSpec at all (honesty gap).
3. major: sample-role anti-leakage documented as structural but represented
   as free strings ("research_evaluation,external_oos_backtest" and "*"
   accepted).
4. major: RunManifest defaults data_fingerprint/registry_version to "" —
   Phase A cache-trust gap survives wearing a manifest.
5. major: manifest_for is FactorSpec-only while docs claim lineage across
   strategy/agent workflows → two-definition drift risk.
6. minor: fingerprint canonicalization — 1 vs 1.0, −0.0, NFC/NFD unicode
   produce different hashes; NaN/Infinity accepted into non-standard JSON.
7. minor: `_atomic_write` fixed temp name is not concurrent-writer safe.
8. major: failure recovery is prose — no RunEvent schema/transition
   table/idempotency/replay contract in the diff; promotion-then-crash
   scenario unresolvable.
Also: test-function count is 24 (27 = parametrized executions); several
tests shallow; highest-value missing test = negative enforcement proof
that shell-equivalent aliases cannot execute; later, the trace test proving
OOS absence from selection prompts.

Alternative architecture proposed (#9): make core.contracts the canonical
spec model with versioned serialization; defer AgentTaskSpec/StrategySpec
until enforcing adapters exist. Disposition (rejected with reasons) in
`adversarial_review_resolution.md`.

Kernel-boundary positives on record: safe-AST and resolver fail closed;
existing RD selection provably ignores external OOS (tests cited);
`_apply_universe_filters` allowlist sound.
