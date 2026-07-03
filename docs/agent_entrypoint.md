# Unified Agent Entrypoint

This document is the shared boot protocol for AI coding agents working on
Quant Forge OpenSource. It exists to keep Codex, Claude Code, and other agents
aligned on the same project rules before they analyze, edit, test, or review
the repository.

Do not treat tool-specific files as independent project contracts. If a tool
uses its own entry shim, that shim should only point back to `AGENTS.md` and
this document.

## Per-Tool Entry Files

Different tools auto-load different filenames. All of them converge here:

- `AGENTS.md` — the canonical contract. Codex and most OpenAI-style agents load
  it automatically.
- `CLAUDE.md` — a thin shim for Claude Code / Claude that points to `AGENTS.md`
  and this document. It defines no independent policy.

Because both files redirect to the same contract, it is expected and safe for
any agent to read either file. Reading the "other" tool's entry file causes no
conflict: `CLAUDE.md` only points back, and `AGENTS.md` is the shared source of
truth for every agent, Claude included. Never fork project policy across
per-tool files.

## Authority

`AGENTS.md` is the canonical project contract. This file is a coordination
layer that explains how multiple agents should read and apply that contract.

Instruction priority:

1. The latest explicit user instruction for the current task.
2. `AGENTS.md`.
3. `docs/agent_entrypoint.md`.
4. Task state files, such as `docs/WORKING_STATE.md`, if present.
5. Architecture and workflow docs, including `docs/architecture.md`,
   `docs/integration_workflow.md`, and `docs/full_integration_test_prompt.md`.
6. Tool-specific shims or local agent configuration files, only as pointers.

If two instructions conflict, stop before editing and report the conflict with
the exact file names and quoted instruction summaries. Do not silently choose a
convenient interpretation.

## Required Read Order

Before analysis or edits, every agent must read these files in order:

1. `AGENTS.md`
2. `docs/agent_entrypoint.md`
3. `docs/architecture.md`
4. `docs/integration_workflow.md`
5. `docs/full_integration_test_prompt.md`
6. `docs/WORKING_STATE.md`, if it exists

For focused tasks, agents may read additional prompt, ledger, or acceptance
files after this base sequence. Those files must not override `AGENTS.md`.

## Read Receipt

Before modifying files, an agent must state a compact read receipt:

- Current branch and commit hash.
- The base documents actually read.
- The current task goal.
- Files or directories the agent intends to modify.
- Verification gates it expects to run.
- Any conflicts or ambiguity found in the instructions.

If an agent cannot read one of the required documents, it must state the missing
path and continue only if the missing file is optional.

## Multi-Agent Rules

Use one executor per work loop. Other agents should be read-only reviewers,
architects, debuggers, or verifiers unless the user explicitly assigns separate
write scopes.

When more than one agent is involved:

- Assign file ownership before edits.
- Do not let two agents modify the same file at the same time.
- Keep public code free of private providers, private data, secrets, and local
  absolute paths.
- Preserve the local-first architecture and typed workbench service boundaries.
- Record handoff state in `docs/WORKING_STATE.md` when the task spans multiple
  sessions or multiple tools.

If a reviewer suggests changes, the executor owns the final patch and
verification. Reviewers should not rewrite the implementation plan unless they
were asked to act as the architect.

## Handoff State

When a task is paused, resumed, or shared between tools, update or create
`docs/WORKING_STATE.md` with:

- Active branch.
- Current objective.
- Last known passing and failing gates.
- Completed fixes.
- Remaining risks.
- Files currently in scope.
- Explicit non-goals.
- Commands already run and their outcomes.

Do not include API keys, tokens, private data samples, private mounted-disk
paths, or other secrets in the handoff state.

## Verification Baseline

Before claiming completion, follow `AGENTS.md` and run the verification that is
appropriate for the change size. The default baseline is:

```bash
python3 -m pytest
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
git diff --check
```

For integration, Docker, Web, RD, LLM, data, or factor-cache changes, also use
`docs/full_integration_test_prompt.md` and `docs/integration_workflow.md`.

## Tool-Specific Shim Policy

Tool-specific shims (such as `CLAUDE.md`) are allowed only as thin pointers.
They must not define a second project policy, alternate read order, alternate
safety boundary, or alternate verification standard.

A valid shim may say:

```text
This repository uses AGENTS.md and docs/agent_entrypoint.md as the shared agent
contract. Read those files first, then follow their required read order.
```

The shim must not duplicate or reinterpret the rules in this file. When project
rules change, edit `AGENTS.md` and this document only; leave the shims untouched.
