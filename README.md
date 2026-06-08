# Quant Forge OpenSource

Quant Forge OpenSource is a clean, local-first factor research workbench. It
turns a natural-language idea or report text into a validated factor draft,
evaluates the factor on local panel data, runs a lightweight research backtest,
and can run a bounded research-development loop to compare improved candidates.
This public repository is source-available under BUSL-1.1 until 2027-12-31,
then automatically changes to Apache-2.0.

Quant Forge OpenSource 是一个干净、面向本地运行的因子研究工作台。它可以把自然语言观点
或研报文本解析成经过校验的因子草稿，在本地面板数据上完成因子评价、轻量回测，并通过
可控的 RD 研究循环比较候选因子的改进效果。本公开仓库在 2027-12-31 前采用 BUSL-1.1
source-available 许可证，之后自动转为 Apache-2.0。

## What This Project Does / 项目能力

- Natural-language idea to factor draft / 自然语言观点转因子草稿
- Local Parquet panel validation / 本地 Parquet 面板数据校验
- Safe formula execution with a small public operator set / 安全执行公开算子集合
- Rank IC, ICIR, coverage, IS/OOS split metrics, horizon matrix / 因子评价指标
- Lightweight next-trading-day factor backtest / 次交易日执行语义的轻量回测
- LLM-driven RD loop with smoke gates, objective weights, and optional
  successive halving /
  默认由 LLM 生成研究假设、并带门槛、权重和可选 successive halving 参数搜索的 RD 循环
- Mounted-disk factor database for portable daily factor values /
  可随移动硬盘迁移的日频因子值数据库
- Local Web UI and CLI / 本地 Web 与命令行
- Markdown research report output / 本地 Markdown 研究报告

This edition intentionally does **not** include hosted services, production
trading, order placement, non-public data providers, account systems, or
database-backed platform features.

本版本刻意不包含托管服务、实盘交易、下单、非公开数据供应商、账户体系或数据库平台功能。

## Install / 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

If you do not install the package, run commands with `PYTHONPATH=src`:

如果不安装包，可以用 `PYTHONPATH=src` 运行：

```bash
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
```

## Five-Minute Demo / 五分钟演示

```bash
qf init --workspace ./qf-demo
qf doctor --workspace ./qf-demo
qf data validate --workspace ./qf-demo
qf factor list --workspace ./qf-demo
qf idea-to-factor --text "small non-st stocks perform better" --workspace ./qf-demo
qf eval-factor FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
qf run-backtest FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
qf research run-once FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
```

`configs/rd.yaml` is local-first. Ordinary RD focuses on research ideas plus
optional hyper-parameter/profile search; enable `llm.hypothesis_mode` and
`llm.review_mode` only in an ignored local RD config when you want LLM-backed
idea generation or review. Campaign mode is reserved for factor synthesis.

`configs/rd.yaml` 默认 local-first。普通 RD 只聚焦研究 idea 和可选的超参数/profile
搜索；如需大模型生成 idea 或复盘，只在被忽略的本地 RD 配置中打开
`llm.hypothesis_mode` 和 `llm.review_mode`。Campaign 模式只用于因子合成。
运行 RD 前请先配置当前 `llm.provider` 的 key；如果只是离线 smoke，可复制
`configs/rd.draft.yaml` 为被忽略的本地 RD 配置，并保持 `llm.hypothesis_mode`
和 `llm.review_mode` 为 `local`。

The RD command prints a `report_path`. The Markdown report is written under
the workspace artifact root, usually `./qf-demo/artifacts/research_reports/`.

RD 命令会输出 `report_path`。Markdown 研究报告默认写入工作区的
`./qf-demo/artifacts/research_reports/`。

## Local Web Workbench / 本地 Web 工作台

```bash
qf init --workspace ./qf-demo
qf doctor --workspace ./qf-demo
qf web --workspace ./qf-demo --rd-config configs/rd.yaml
```

Open the printed local URL in your browser. The web adapter is local-only.
It exposes two explicit parser modes: local rule parsing for the built-in
small-cap/momentum/low-volatility/volume patterns, and LLM semantic parsing for
configured providers. When LLM parsing is selected, missing keys or failed LLM
requests are returned to the user first; the browser asks before falling back
to local rule parsing.

在浏览器打开命令行打印的本地地址。本项目 Web 适配器只面向本地运行。
界面会明确区分两种解析方式：本地规则解析只覆盖内置的小市值、动量、低波动、
成交量等有限模式；LLM 语义解析会调用已配置 provider。选择 LLM 解析时，
如果缺少 key 或 LLM 请求失败，系统先展示失败原因，并询问是否改用本地规则解析。

## LLM Provider Setup / 大模型配置

Configuration files store provider metadata and environment variable names
only. For the local Web workbench, real API keys should stay in an ignored
local env file declared by `runtime.env_files`.

配置文件只保存供应商元信息和环境变量名。对于本地 Web 工作台，真实 API key
应放在 `runtime.env_files` 显式声明、且被 git 忽略的本地 env 文件中。

Example:

```bash
cp configs/default.draft.yaml configs/default.local.yaml
printf 'DEEPSEEK_API_KEY=<your-api-key>\n' > configs/default.local.env
chmod 600 configs/default.local.env
# edit configs/default.local.yaml paths.* and runtime.env_files as needed
qf doctor --config configs/default.local.yaml --rd-config configs/rd.yaml
qf web --config configs/default.local.yaml --rd-config configs/rd.yaml
```

For cloud providers, if a selected provider is missing `model`, `base_url`,
`api_key_env`, or the named environment variable, Quant Forge raises a precise
error such as:

对于云端 provider，如果所选供应商缺少 `model`、`base_url`、`api_key_env`，
或对应环境变量没有设置，系统会给出精确错误，例如：

```text
llm.providers.deepseek.base_url is required
Missing API key for active LLM provider deepseek. Expected environment variable: DEEPSEEK_API_KEY.
```

For a local OpenAI-compatible endpoint that does not require auth, set
`require_api_key` to `false`; then `api_key_env` may be omitted and Quant Forge
does not send an Authorization header.

对于不需要鉴权的本地 OpenAI-compatible endpoint，可将 `require_api_key` 设为 `false`；
此时可省略 `api_key_env`，请求中也不会发送 Authorization header。

The same active LLM provider is used by LLM semantic parsing and RD LLM
features. RD does not have a second API-key setting.

LLM 语义解析和 RD LLM 功能共用同一个当前 provider；RD 不再单独配置第二套 API key。

## Configuration Files / 配置文件

| File | Purpose |
| --- | --- |
| `configs/default.yaml` | Runtime paths, local web settings, simulation defaults, LLM provider registry. |
| `configs/rd.yaml` | RD LLM modes, objective, gates, sample splits, horizon matrix, parameter search settings. |
| `configs/default.draft.yaml` | Copyable runtime config template with explanatory comments. |
| `configs/mounted.draft.yaml` | Copyable mounted-disk config template for portable factor/data roots. |
| `configs/rd.draft.yaml` | Copyable RD config template with explanatory comments. |
| `.env.example` | Environment variable names only; copy to an ignored local env file if desired. |

| 文件 | 用途 |
| --- | --- |
| `configs/default.yaml` | 运行路径、本地 Web、模拟参数、大模型供应商注册表。 |
| `configs/rd.yaml` | RD LLM 模式、目标、门槛、样本切分、周期矩阵、参数搜索配置。 |
| `configs/default.draft.yaml` | 可复制的运行配置模板，带注释说明。 |
| `configs/mounted.draft.yaml` | 可复制的移动硬盘配置模板，用于随盘因子和数据根目录。 |
| `configs/rd.draft.yaml` | 可复制的 RD 配置模板，带注释说明。 |
| `.env.example` | 只放环境变量名；如需本地使用可复制为被忽略的环境文件。 |

Read the full bilingual guide:

阅读完整双语手册：

- [User Manual / 使用手册](docs/USER_MANUAL.md)
- [Architecture / 架构说明](docs/architecture.md)
- [Configuration Reference / 配置参考](docs/configuration.md)
- [Integration Workflow / 联调流程](docs/integration_workflow.md)

## Mounted Factor Database / 移动硬盘因子库

For a fresh checkout on another computer, keep runtime state on the mounted
drive and point an ignored local config at it:

```bash
cp configs/mounted.draft.yaml configs/default.local.yaml
# edit <MOUNT_ROOT> and optional LLM env file settings
qf factor normalize-root --config configs/default.local.yaml
qf factor normalize-store --config configs/default.local.yaml --scan-root <MOUNT_ROOT>/QuantForgeData --link-files
qf doctor --config configs/default.local.yaml --rd-config configs/rd.yaml
qf factor list --config configs/default.local.yaml
```

Recommended mounted layout:

```text
QuantForgeData/
  workbenches/quant_forge_opensource/
    data/panel.parquet
    factor_root/
      原始因子/{active_factors,inactive_factors}/<FACTOR_ID>/factor.yaml
      合成因子/{active_factors,inactive_factors}/<FACTOR_ID>/factor.yaml
    artifacts/
    outputs/
    factor_values_overlay/
      原始因子/factor_id=<FACTOR_ID>/
      合成因子/factor_id=<FACTOR_ID>/
  canonical/factor=cn_a/
    原始因子/factor_id=<FACTOR_ID>/
    合成因子/factor_id=<FACTOR_ID>/
  catalog/manifests/market=cn_a/dataset=factor_values/
```

`factor_root` stores factor definitions and formulas. `canonical/factor=cn_a`
stores read-base daily factor values. `factor_values_overlay` stores new local
incremental values when the canonical store should remain read-only. Factor
definitions and values are split into `原始因子` and `合成因子`: original factors
come from imported/public formulas or precomputed external values, while
synthetic factors come from RD/campaign/composite generation. The manifest
directory stores portable metadata and must not contain machine-local paths.

如果换一台电脑，只需要拉取代码、插入移动硬盘、复制并编辑
`configs/mounted.draft.yaml`。`factor normalize-root` 会把旧版 `factor_root`
非破坏性复制到分类目录；`factor normalize-store --scan-root` 会扫描盘上已有的
前序因子值目录，并非破坏性地合并到
`原始因子/factor_id=<FACTOR_ID>` 或 `合成因子/factor_id=<FACTOR_ID>` 规范目录。

## Data Contract / 数据契约

The minimal local panel must contain:

最小本地面板数据需要包含：

```text
trade_date, instrument, close, market_cap, is_st
```

Optional fields used by built-in examples:

内置示例可使用的可选字段：

```text
volume, return_1d, return_5d, volatility_5d
```

Use `qf data validate` before running evaluation or backtests.

运行评价或回测前，请先执行 `qf data validate`。

## Release Safety / 发布安全

Run the safety checks before publishing:

发布前运行：

```bash
python3 scripts/release_safety_scan.py
PYTHONPATH=src pytest
git diff --check
```

The release scan checks tracked and unignored files for common secret markers,
local absolute paths, large files, and non-public project terms.

release scan 会检查已跟踪和未忽略文件中的常见密钥标记、本地绝对路径、大文件和非公开项目词。

## License / 许可证

This repository uses the Business Source License 1.1 (`BUSL-1.1`) with a
planned Apache-2.0 change license.

```text
Current license: BUSL-1.1
Allowed before Change Date: non-commercial research, education, personal
evaluation, internal non-commercial experimentation, and non-production use
Change Date: 2027-12-31
Change License: Apache License, Version 2.0
```

The maintainers may release any version under Apache-2.0 before the Change
Date. Community pull requests are accepted under the contributor terms in
`CONTRIBUTING.md` and `CLA.md`.

本仓库采用 Business Source License 1.1，并约定未来转为 Apache-2.0。

```text
当前许可证：BUSL-1.1
Change Date 前允许：非商业研究、教育、个人评估、内部非商业实验，以及非生产用途
Change Date：2027-12-31
Change License：Apache License, Version 2.0
```

维护者可以在 Change Date 前提前将任意版本按 Apache-2.0 发布。社区 PR 按
`CONTRIBUTING.md` 和 `CLA.md` 中的贡献条款接收。
