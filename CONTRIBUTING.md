# Contributing to Quant Forge OpenSource

Thank you for helping improve Quant Forge. This repository accepts community
pull requests, but every contribution must preserve the clean public boundary,
local-first runtime model, and BUSL-1.1 to Apache-2.0 licensing path.

## License And CLA

This repository is source-available under BUSL-1.1 until the Change Date
listed in `LICENSE`. On that date, or earlier if the maintainers choose to
make an early release, the licensed work changes to Apache-2.0.

To keep that path possible, pull requests require agreement to `CLA.md`.
Opening a pull request means you agree that your contribution may be included
in:

- the current BUSL-1.1 source-available release;
- the future Apache-2.0 open-source release;
- commercial or dual-licensed Quant Forge releases.

## What We Accept

- Small, readable bug fixes with tests.
- Documentation improvements that do not expose private paths, names, keys, or
  non-public provider details.
- Local-first factor, evaluation, backtesting, RD, and UI improvements that
  keep public code independent from private infrastructure.
- New operators or fields only when they are documented, tested, and backed by
  public data contracts.

## What We Do Not Accept

- API keys, credentials, tokens, private data, or local absolute paths.
- Proprietary market data samples or unlicensed factor formulas.
- Hosted-service, live-trading, order-placement, account-system, or private
  provider code in the public tree.
- Silent fallbacks that hide missing data, schema, or configuration problems.
- Broad rewrites without a focused issue or design note.

## Pull Request Checklist

Before opening a PR:

```bash
python3 scripts/release_safety_scan.py
PYTHONPATH=src pytest
git diff --check
```

For behavior changes, add or update targeted tests. For documentation changes,
check local links and keep the bilingual public-facing docs consistent when the
change affects users.

## 中文说明

欢迎提交社区 PR。为了保证项目可以先以 BUSL-1.1 发布，并在 `LICENSE` 指定的
Change Date 自动转为 Apache-2.0，所有 PR 都需要同意 `CLA.md`。

请不要提交 API key、token、私有路径、私有数据、未授权公式、私有供应商代码或隐藏
错误的兜底逻辑。涉及用户行为、配置、因子计算、评价、回测或 RD 流程的改动，需要同步
补充测试和文档。
