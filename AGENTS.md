# Quant Forge OpenSource AGENTS.md

This branch is the clean public Quant Forge workbench. Keep it local-first,
config-driven, and safe to publish.

## Boundaries

- Public code must not import non-public platform, non-public provider, deployment,
  credential, or hosted-service modules.
- Factor definitions live under `factor_root`.
- Market data lives under `data_root`.
- Evaluation and backtest outputs live under `artifact_root`.
- Agents may call typed workbench services but must not write source-of-truth
  paths directly.

## Coding Rules

- Keep modules small, typed, and explicit.
- Avoid hidden global state and silent fallback behavior.
- Do not guess provider fields, PIT semantics, licenses, paths, or credentials.
- Put local paths in config files or CLI arguments.
- Never commit private data, local absolute paths, API keys, or provider secrets.

## Verification

Before claiming completion, run:

```bash
python3 -m pytest
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
git diff --check
```
