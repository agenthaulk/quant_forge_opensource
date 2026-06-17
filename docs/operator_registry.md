# Operator Registry

Quant Forge uses a lightweight Operator Registry to keep formula parsing, MCP
operator catalogs, LLM prompts, RD planning, fingerprints, and draft-operator
review aligned.

The registry is not a new database. It is repo-shipped, read-only YAML plus JSON
Schema under `src/quant_forge/operator_registry/data/`.

## Storage Governance

The project keeps each storage root in its existing physical format:

- `factor_root`: YAML `factor.yaml` definitions.
- `factor_values_root`: Parquet factor values plus metadata JSON.
- `artifact_root`: JSON, JSONL, and Markdown reports/traces.
- `operator_registry`: repo-shipped read-only YAML plus JSON Schema.

What is unified is governance, not the storage engine:

- Every store has a schema/version marker.
- Every record has a canonical key (`factor_id`, score keys, run IDs, or
  operator `name`).
- Metadata must be portable and must not contain local paths or secrets.
- Store/repository/loader responsibilities stay separate from execution logic.
- Writes use atomic or append-only semantics where applicable.
- Read-only roots are not mutated by workbench services.

## Exploration vs Execution

RD and LLM prompts may explore broad ideas, including external DSL names such as
`ts_stddev`. Execution is stricter:

1. Parse the formula with the safe AST grammar.
2. Resolve operators through the registry.
3. Rewrite only explicitly safe canonical aliases, such as `ts_stddev` to
   `stddev`.
4. Validate signatures through the existing parser rules.
5. Execute only canonical implemented operators.

`rolling_std` is intentionally not auto-executed because libraries differ on
alignment and `min_periods`. Unknown operators such as `industry_neutralize`
produce draft review artifacts and are not executable candidates.

## Draft Operators

Draft operator artifacts are metadata only:

- `manifest.json`
- `semantics_request.json`
- `generated_tests.json`
- `audit_status.json`
- `review.md`

The artifact root must not contain executable operator code. Promotion requires
audited source code, tests, and a repo registry update.
