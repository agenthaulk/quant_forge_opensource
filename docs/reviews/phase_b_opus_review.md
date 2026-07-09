# Phase B Opus Review (Step B4) — Record

Reviewer: Claude Opus, adversarial read-only mandate; ran the full suite
(453 passed) and empirical probes against the prototype. Verdict:
**REVISE-FIRST** (architecture docs strong; prototype encoded three
fail-open conventions contradicting the design's own meta-rule).

Findings (severity, anchor):
- F1 major: RunManifest fail-open on data_fingerprint/registry_version
  (run_manifest.py:76-77); test pinned the permissive behavior.
- F2 major (borderline blocker): sample_role free string, fail-open default
  toward the selection-eligible role; FP-6 hangs on an unvalidated string.
- F3 major: ValidationGate ignores universe_filters and
  capabilities_required while docs promise "fail loudly"; FactorSpec lacked
  unsupported_capabilities.
- F4 major: gate observance by convention; `precomputed:` short-circuit in
  resolver is a kernel bypass if a caller skips the advisory gate →
  validation-witness pattern named as Phase C requirement.
- F5 major: exec-guard was a two-name denylist oversold by its docstring
  ("bash"/"subprocess" constructed fine).
- F6 major: NL fallback (`rank(close)`) fabricates an interpretation with
  zero warning; static 7-field catalog makes "data capability ready"
  nominal; `rank(is_st)` passed the gate.
- F7 major: no data-plane lane in Phase C plan; falsification battery
  underpowered on demo data risks FP-2 erosion.
- F8 minor: SpecValidationResult carries no provenance (registry/catalog
  versions).
- F9 minor: spec fingerprint over-covers volatile fields (metadata/thesis)
  → dedup-evasion channel and provenance illusion over unconsumed fields.
- F10 minor: kernel-friction leaks (spec-side formula check; dummy-formula
  id probe; _set_tuple ×4).
- F11 minor: M1 overloaded (decompose server.py while adding timeline
  features) → sequence extraction strictly first.
- F12 minor: no data→prompt injection threat model; LLM narrative numeric
  claims unchecked.
- F13 nit: exception-type inconsistencies; "temperature 0.0" determinism
  overstated (trace-replay is the real mechanism).

Positive verdicts worth keeping on record: option analysis honest; layering
right; StrategyDSL rejection and exec-sandbox rejection correct and
evidenced; delegation-by-construction called "genuinely good design";
kernel PIT end strong; expert mode adds no new restrictions.

Minimal revision list (8 items) adopted into Step B7 — disposition in
`adversarial_review_resolution.md`.
