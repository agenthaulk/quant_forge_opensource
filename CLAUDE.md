# CLAUDE.md

This file is a thin shim for Claude Code / Claude. It defines no independent
project policy.

This repository uses `AGENTS.md` and `docs/agent_entrypoint.md` as the shared
agent contract. Read those files first, then follow the required read order they
define.

## Read first

1. `AGENTS.md` — the canonical project contract.
2. `docs/agent_entrypoint.md` — the shared boot protocol and full read order.

Everything that governs how to work in this repo (authority, read order, read
receipts, boundaries, verification baseline) lives in those two files, and they
apply to Claude exactly as they apply to Codex and every other agent. There is
no Claude-specific policy here by design: if `AGENTS.md` and this shim ever seem
to disagree, `AGENTS.md` wins.

Do not add project rules to this file. When project policy changes, edit
`AGENTS.md` and `docs/agent_entrypoint.md` only.
