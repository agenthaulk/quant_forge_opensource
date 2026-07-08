# Untrusted-text dataflow review

> CP7 review item (ENGINEERING_PROGRESS.md CP7). Produced 2026-07-07 by a
> read-only Opus review agent under Fable orchestration; adjudicated by
> Fable: findings F1–F6 accepted, hardening proposals P1–P5 accepted and
> scheduled as the CP7-H hardening batch (after the CP5/D1 landing).
> Trust levels: *operator* (local human at CLI/web UI), *model* (LLM
> output), *provider-channel* (non-model bytes from the provider
> endpoint), *disk* (JSONL/YAML under artifact_root/factor_root —
> trusted-at-write, unverified-at-read).

## Review scope and method

Tree reviewed: branch `fable/phase-c-platform-buildout` (citations are
repo-root-relative `file:line` at review time). Manual source read of the
four scoped sinks plus every producer feeding them: `research_loop/llm.py`,
`research_loop/memory.py`, `research_loop/context_builder.py`,
`research_loop/feedback_builder.py`, `research_loop/trace_store.py`,
`research_loop/candidate_gate.py`, `research_loop/experiment_planner.py`,
`research_loop/service.py` (targeted sections),
`research_loop/operator_drafts.py`, `lineage/store.py`,
`llm_factor_parser.py`, `llm_client.py`, `factor_library/repository.py`,
`factor_engine/formula_parser.py`, `factor_engine/executor.py`,
`operator_registry/resolver.py`, `core/contracts.py`, `apps/web/api.py`,
`apps/web/routing.py`. Grep sweeps: `eval(`/`exec(`/`compile(`/
`__import__` across `src` (only `re.compile` hits); `redact_free_text`
call sites; `universe_filters` consumers. Nothing modified. Test coverage
cited by test-name inspection, not execution.

## Data flows

| # | Flow | Source trust | Verified safeguards | Residual gap |
|---|------|--------------|--------------------|--------------|
| A1 | Research-memory statements → hypothesis prompt (`context_builder.py:84-99` → `llm.py:192-193, 224-225`) | disk (written by service templates at `service.py:999-1022`) | `redact_free_text` at write time (`memory.py:212-219, 284`); prompt forwards only `statement`+`observation_count`, max 5/tier, `source=="research_memory"` filter (`llm.py:315-338`); deterministic promotion, rules never auto-activate (`memory.py:101-104, 121-178`; `tests/test_research_memory.py:148, 249, 337, 453`) | Read path trusts disk: statements re-read verbatim, never re-validated, unbounded length (`memory.py:245-258, 260-275, 303`; `llm.py:327`). Redaction strips paths/secrets only, not imperative text (`lineage/store.py:70-82`) |
| A2 | Trace summaries → hypothesis prompt | disk | Trace dicts enter `ResearchContext` (`context_builder.py:78-79`) but prompt assembly forwards only research-memory rows and `next_focus_hints` (`llm.py:188-193`); hints are producer-side fixed templates (`feedback_builder.py:15, 22, 29, 46-60`) | Hint strings are read back from `trace.jsonl` without checking they belong to the template set (`context_builder.py:126-134`) |
| B1 | Web factor-idea text → LLM parser (`routing.py` POST → `api.py:66-118, 145-164` → `llm_factor_parser.py:28-51`) | operator (loopback-only bind default, `routing.py:52-58`; bearer token required only for `0.0.0.0`, `api.py:1222-1231`) — effectively also any same-host process | Idea text goes verbatim into the LLM user message (`llm_factor_parser.py:80`) by design; model output gated: name slugged (`llm_factor_parser.py:122-124`), formula through registry+AST gate (`llm_factor_parser.py:88-92`), digest factor_id (`llm_factor_parser.py:99-101`) | `description` (`llm_factor_parser.py:93`) and `universe_filters` (`llm_factor_parser.py:98`) kept as free text and persisted (see C2) |
| B2 | Web "edited draft factor" JSON → persistence (`api.py:121-142, 614-631, 180-182`) | operator / same-host | `factor_id` regex contract (`core/contracts.py:58-59`); `horizon_days` positive int (`api.py:628`); invalid formulas rejected at evaluation time by the registry gate | Arbitrary `name`/`description`/`formula`/`status` strings persist to `factor_root`; `status:"active"` immediately qualifies the row for prompt recycling (`context_builder.py:50-52`) |
| C1 | Gate reasons → durable memory statements → prompts | model-influenced (identifiers) + provider-channel | Gate reasons are metric/threshold templates (`candidate_gate.py:52, 62, 67, 73, 76, 126, 134, 138, 184-187`); signature uses value-free family reduction (`service.py:2738-2751`); LLM-chosen operator/field names constrained to Python identifiers by AST parse (`experiment_planner.py:71, 74`; `resolver.py:88-96`) | Statement text keeps full joined reasons (`service.py:1016-1017`); reasons can include `str(exc)` from repair failures (`service.py:2288-2290`), and `llm_client.py:280` embeds up to 500 chars of provider HTTP error body in exception text |
| C2 | Factor-catalog strings → `effective_ideas`/seed summary in prompts (`context_builder.py:50-52, 102-114`; `llm.py:188, 218, 222, 409-418`) | disk (yaml written by B1/B2/RD loop) | Precomputed formulas masked (`llm.py:421-424`, `context_builder.py:107-109`; pinned by `tests/test_research_loop_structure.py:438-458`); RD-created names slugged (`service.py:2447-2448`), descriptions from LLM rationale (`service.py:1922`) | Factor `name`, `universe_filters` (effective ideas) and `description` (seed summary) are unvalidated free text included verbatim in prompts |
| C3 | Run-index rows → later contexts | disk | Structural validation at append: kind enum, sha256 fingerprint, ISO timestamp, relative paths, metric-status whitelist (`lineage/store.py:307-321, 360-397`); web reads re-redacted via `_redact_web_text` (`api.py:1418-1434, 1494, 1517`) | None into LLM prompts (RunIndex is not referenced by `llm.py`/`context_builder.py`). `data_window` date *format* only presence-checked (`store.py:378-387`) — display-level only |
| C4 | Data-window notes → memory rows | derived from evaluation results | `start:end` template, empty when unavailable (`service.py:2729-2735`); tz-aware ISO enforced on memory timestamps (`memory.py:339-347`) | None found |
| D1 | Model output → file name/path (`repository.py:89-96`; `operator_drafts.py:35-36`) | model | factor_id digest-based (`llm_factor_parser.py:99-101`, `service.py:1916-1918`) + regex `[A-Za-z][A-Za-z0-9_=-]*` in the dataclass contract (`contracts.py:58-59`); `get`/`delete` pre-validate ids (`repository.py:20-28`; `tests/test_repository_security.py:19-31`); draft dirs slugged+capped (`operator_drafts.py:144-148`) | None found (charset excludes `/`, `\`, `.`) |
| D2 | Model output → DSL program | model | AST-only parse, node whitelist, no `eval`/`exec`/`compile` anywhere in `src` (grep-verified); interpreter over whitelisted ops (`formula_parser.py:129-158`; `executor.py:66-149`); fields must be existing numeric columns (`executor.py:255-261`); filters reduced to literal `is_st == false` or rejected (`experiment_planner.py:197-211`, `service.py:2451-2457`, `executor.py:264-270`) | No upper bound on window arguments or formula size (`executor.py:242-245` floors to ≥1 but never caps; `formula_parser.py` has no length limit) |
| D3 | Model output → config values | model | `horizon_days` int-coerced, ≥1 (`llm_factor_parser.py:94, 111-119`; `contracts.py:60-61`); `expected_direction`/`source` enum-coerced (`llm.py:579-590`); web `objective` must match a configured weight profile before generation (`research_loop/config.py:298-305`, called at `api.py:705`) | `horizon_days` has no upper bound (robustness, not injection) |

## Findings ranked by priority

**F1 (highest). Prompt context trusts free text read back from
`artifact_root`; redaction is not instruction-neutralization.**
`redact_free_text` removes paths and `KEY=value` secrets only
(`lineage/store.py:70-82`). Memory statements are redacted at write
(`memory.py:212-219`) but re-enter prompts verbatim from disk with no
read-time shape/length check (`memory.py:245-258, 303`;
`context_builder.py:95`; `llm.py:327`, then `llm.py:224-225`). Concrete
failure scenario: a row appended to
`artifact_root/research_memory/failures.jsonl` by any other same-host
process, sync tool, or hand edit — with a `statement` containing
imperative instructions — is included verbatim, up to 5 items per tier,
in every subsequent hypothesis prompt and can steer generation for as
long as the row stays live. Damage is bounded by the DSL gate and
candidate gates (a steered hypothesis still cannot execute arbitrary code
or touch arbitrary paths), but research direction, compute waste, and
result quality are exposed.

**F2. Provider-channel text can enter durable memory statements.**
`llm_client.py:275-280` raises `RuntimeError(f"LLM request failed with
HTTP {exc.code}: {body[:500]}")` — 500 chars of raw response body from
the configured endpoint. `_repair_failed_plan` folds `str(exc)` into plan
blocking reasons (`service.py:2288-2290`); blocking reasons become gate
reasons (`candidate_gate.py:42`) and are joined verbatim into the
persisted memory statement (`service.py:1016-1017`). Concrete failure
scenario: a misbehaving or compromised OpenAI-compatible endpoint returns
HTTP 4xx with an instruction-bearing body; that text is persisted into
`failures.jsonl` and, after promotion, replays into future hypothesis
prompts via F1. The signature-side family reduction
(`service.py:2738-2751`) protects the *signature* only, not the
statement.

**F3. Web-persisted factor text recycles into prompts without shape
limits.** `_factor_from_request` accepts arbitrary `name`, `description`,
`formula`, `universe_filters`, and `status` (`api.py:614-631`);
`repo.save` persists them (`api.py:180-182`). A factor saved with
`status:"active"` immediately joins `effective_ideas`
(`context_builder.py:50-52`), placing its free-text `name`, `formula`,
and `universe_filters` into every hypothesis prompt (`llm.py:222`); if
later used as a seed, its `description` also enters the prompt
(`llm.py:409-418`). Trust level is "local operator" (loopback bind
default, `routing.py:52-58`), so this is a robustness issue rather than a
remote one — but it is the widest free-text channel into prompt assembly,
and the loopback bind carries no token (`api.py:1222-1224`), so any
same-host process can use it.

**F4. `next_focus_hints` template set is enforced only at the producer.**
Hints are generated exclusively from a fixed template set
(`feedback_builder.py:15, 22, 29, 46-60`) but read back from
`trace.jsonl` with no membership check (`context_builder.py:126-134`)
before entering the prompt (`llm.py:223`). Same disk-trust scenario as
F1, smaller surface.

**F5 (robustness, not injection). DSL numeric arguments and formula size
are unbounded.** A model-supplied `ts_mean(close, 999999999)` passes
validation (`formula_parser.py:274-278` only checks "is a number";
`executor.py:242-245` floors but never caps) and drives
`formula_lookback_rows`/rolling windows; arbitrarily long formula strings
are parsed without a length cap. Concrete failure scenario: one hostile
or degenerate hypothesis stalls an RD run with memory/CPU blowup.
`horizon_days` similarly has no upper bound (`contracts.py:60-61`).

**F6 (minor inconsistency). Lineage timestamps accept naive datetimes.**
`memory.py:339-347` and `new_run_id` (`store.py:183-184`) require
tz-aware timestamps, but the lineage/run-index `_require_iso_timestamp`
(`store.py:400-404`) accepts naive ISO strings. Not exploitable; noted
for contract symmetry.

## Minimal hardening proposals (all ACCEPTED by Fable → CP7-H batch)

**P1 — Read-time statement gate in `_memory_items_for_prompt` (closes F1,
and F2's replay half).** The service writes statements with exactly two
prefixes: `"accepted candidate formula family "` and
`"gate blocked candidate formula family "` (`service.py:1005-1007,
1015-1018`). In `_memory_items_for_prompt` (`llm.py:315-338`), drop any
item whose statement does not start with a known prefix, collapse it to a
single line, and cap it (e.g. 300 chars). ~6 lines at the single choke
point every memory statement must pass to reach a prompt; no schema
change, no migration; turns the "hostile JSONL under artifact_root" class
into silently-skipped rows. A one-line counterpart in `_next_focus_hints`
(`context_builder.py:126-134`) — keep only hints found in the enumerable
`feedback_builder` template set — closes F4 the same way.

**P2 — Family-reduce the memory statement, not just the signature (closes
F2 at the write side).** In `_record_memory_observations`, build the
blocked-candidate statement from
`_gate_reason_families(result.gate_reasons)` (already computed at
`service.py:1012`) instead of `"; ".join(result.gate_reasons)`
(`service.py:1016-1017`). The statement becomes e.g. `"gate blocked
candidate formula family ab12cd34ef56: score, turnover_rate"` — which is
what the docstring at `service.py:990-993` already claims. Provider error
bodies and repair-exception text then never reach durable memory. Full
reasons remain in trace and report artifacts, so no operator information
is lost.

**P3 — Truncate the provider error body before it enters exception text
(defense-in-depth for F2).** In `llm_client.py:280`, replace `body[:500]`
with a summary keeping only the HTTP status plus a short single-line
length-capped extract (e.g. 120 chars, newlines stripped), or log the
body and raise with the status only. One-line change; keeps operator
debuggability while shrinking the provider-channel free-text window
everywhere `str(exc)` propagates (`service.py:2289`,
`apps/cli/main.py:544`).

**P4 — Length/charset caps on persisted factor free text (closes F3's
worst edge).** In `_factor_from_llm_json` (`llm_factor_parser.py:93, 98`)
and `_factor_from_request` (`api.py:614-631`), cap `description` (e.g.
500 chars), `name` (already slugged on the parser path — apply the same
`_slug` on the web path), and each `universe_filters` entry (e.g. 120
chars), and strip control characters/newlines. Optionally reject `status`
values other than `draft` on the idea-validation endpoint, since
promotion has its own audited path (`repository.py:109-124`). Small,
local validators; no new framework.

**P5 — Cap DSL window arguments and formula length (closes F5).** In
`_validate_operator_signature`/`_positive_int_arg`
(`formula_parser.py:235-278`), reject window arguments above a
config-visible bound (e.g. 750 rows ≈ 3 trading years) and formulas
longer than e.g. 2000 chars in `parse_formula_node`
(`formula_parser.py:46-57`). Add the same upper bound for `horizon_days`
in `contracts.py:60-61`. Pure validation constants at the existing gate;
every legitimate formula in the repo is far below them.

## Non-findings (flows checked and confirmed clean)

- **No dynamic code execution on any model output.** Grep for
  `eval(`/`exec(`/`compile(`/`__import__` across `src` returned only
  `re.compile` uses; formulas are parsed via `ast.parse(mode="eval")`
  into a validated node tree (`formula_parser.py:46-57, 129-158`) and
  *interpreted* (`executor.py:66-149`), never compiled or executed as
  Python.
- **LLM `universe_constraints` cannot alter program behavior.** RD
  planning reduces them to the literal `"is_st == false"` or a blocking
  reason (`experiment_planner.py:197-211`; `service.py:2451-2457`);
  execution accepts only three literal filter forms and raises otherwise
  (`executor.py:264-270`).
- **Model output cannot name a file or path.** Factor ids are
  digest-based and contract-constrained to `[A-Za-z][A-Za-z0-9_=-]*`
  (`contracts.py:58-59`); `get`/`delete` pre-validate ids
  (`repository.py:20-28`; `tests/test_repository_security.py:19-31`);
  operator-draft directories use `_safe_identifier`
  (`operator_drafts.py:144-148`). Charset excludes `/`, `\`, `.`.
- **Run-index rows do not reach LLM prompts.** `RunIndex` is referenced
  by `api.py`/`service.py` only; no reference in `research_loop/llm.py`
  or `context_builder.py`. Writes structurally validated
  (`store.py:307-321, 360-397`); web reads pass `_redact_web_text`.
- **Web `objective` cannot inject free text into the prompt.**
  `weights_for_objective` raises for unconfigured objectives
  (`research_loop/config.py:298-305`, called at `api.py:705`).
- **LLM prose cannot mint active rules.** Promotion is a pure function of
  observations (`memory.py:121-178`); promoted rules always carry
  `needs_human_review` (`memory.py:101-104`); pinned by
  `tests/test_research_memory.py:148, 249`.
- **Precomputed seed references are masked in prompts** (`llm.py:421-424`;
  `context_builder.py:107-109`), pinned by
  `tests/test_research_loop_structure.py:438-458`.
- **tz/ISO enforcement** verified for memory observations and goal-store
  timestamps (`memory.py:339-347`) and run ids (`store.py:183-186`);
  exception noted as F6.
- **Trace entries carrying full LLM hypothesis text** (`service.py:1300,
  1325`) do not currently reach prompts — prompt assembly consumes only
  research-memory rows and template hints (`llm.py:188-193`). Caveat:
  those dicts *are* inside `ResearchContext.recent_successes/failures`
  (`context_builder.py:78-79`), so P1's filter is also the guard that
  keeps future prompt-assembly changes honest.
- **Unknown/unverified:** whether every `apps/web/html.py` render site
  escapes factor text (sampled sites use `html.escape`, `html.py:5,
  69-72`; full coverage not audited — HTML rendering was outside the four
  scoped sinks); the full behavior of `backtest.warnings` producers
  feeding `_review_messages` (`llm.py:390`) — spot-checked as metric
  templates, not exhaustively traced.
