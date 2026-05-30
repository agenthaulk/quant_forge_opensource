# Quant Forge OpenSource

Quant Forge OpenSource is a clean, local-first factor research workbench. It
turns a natural-language idea or report text into a validated factor draft,
evaluates the factor on local panel data, runs a lightweight research backtest,
and can run a bounded research-development loop to compare improved candidates.

Quant Forge OpenSource 是一个干净、面向本地运行的因子研究工作台。它可以把自然语言观点
或研报文本解析成经过校验的因子草稿，在本地面板数据上完成因子评价、轻量回测，并通过
可控的 RD 研究循环比较候选因子的改进效果。

## What This Project Does / 项目能力

- Natural-language idea to factor draft / 自然语言观点转因子草稿
- Local Parquet panel validation / 本地 Parquet 面板数据校验
- Safe formula execution with a small public operator set / 安全执行公开算子集合
- Rank IC, ICIR, coverage, IS/OOS split metrics, horizon matrix / 因子评价指标
- Lightweight next-trading-day factor backtest / 次交易日执行语义的轻量回测
- RD loop with smoke gates, objective weights, and optional successive halving /
  带门槛、权重和可选 successive halving 参数搜索的 RD 循环
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
qf data validate --workspace ./qf-demo
qf factor list --workspace ./qf-demo
qf idea-to-factor --text "small non-st stocks perform better" --workspace ./qf-demo
qf eval-factor FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
qf run-backtest FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
qf research run-once FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
```

The RD command prints a `report_path`. The Markdown report is written under
the workspace artifact root, usually `./qf-demo/artifacts/research_reports/`.

RD 命令会输出 `report_path`。Markdown 研究报告默认写入工作区的
`./qf-demo/artifacts/research_reports/`。

## Local Web Workbench / 本地 Web 工作台

```bash
qf init --workspace ./qf-demo
qf web --workspace ./qf-demo --config configs/default.yaml --rd-config configs/rd.yaml
```

Open the printed local URL in your browser. The web adapter is local-only.

在浏览器打开命令行打印的本地地址。本项目 Web 适配器只面向本地运行。

## LLM Provider Setup / 大模型配置

Configuration files store provider metadata and environment variable names
only. Real API keys must stay in your shell, secret manager, or ignored local
environment file.

配置文件只保存供应商元信息和环境变量名。真实 API key 必须放在用户自己的 shell、
密钥管理器或被忽略的本地环境文件中。

Example:

```bash
export DEEPSEEK_API_KEY="<your-api-key>"
qf web --workspace ./qf-demo --config configs/default.yaml --rd-config configs/rd.yaml
```

If a selected provider is missing `model`, `base_url`, `api_key_env`, or the
named environment variable, Quant Forge raises a precise error such as:

如果所选供应商缺少 `model`、`base_url`、`api_key_env`，或对应环境变量没有设置，
系统会给出精确错误，例如：

```text
llm.providers.deepseek.base_url is required
Missing API key for LLM provider deepseek. Set one of these environment variables: DEEPSEEK_API_KEY.
```

## Configuration Files / 配置文件

| File | Purpose |
| --- | --- |
| `configs/default.yaml` | Runtime paths, local web settings, simulation defaults, LLM provider registry. |
| `configs/rd.yaml` | RD objective, gates, sample splits, horizon matrix, parameter search settings. |
| `configs/default.draft.yaml` | Copyable runtime config template with explanatory comments. |
| `configs/rd.draft.yaml` | Copyable RD config template with explanatory comments. |
| `.env.example` | Environment variable names only; copy to an ignored local env file if desired. |

| 文件 | 用途 |
| --- | --- |
| `configs/default.yaml` | 运行路径、本地 Web、模拟参数、大模型供应商注册表。 |
| `configs/rd.yaml` | RD 目标、门槛、样本切分、周期矩阵、参数搜索配置。 |
| `configs/default.draft.yaml` | 可复制的运行配置模板，带注释说明。 |
| `configs/rd.draft.yaml` | 可复制的 RD 配置模板，带注释说明。 |
| `.env.example` | 只放环境变量名；如需本地使用可复制为被忽略的环境文件。 |

Read the full bilingual guide:

阅读完整双语手册：

- [User Manual / 使用手册](docs/USER_MANUAL.md)
- [Architecture / 架构说明](docs/architecture.md)
- [Configuration Reference / 配置参考](docs/configuration.md)

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

## Release Safety / 开源安全

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

No license file is included yet. Add a license before publishing the repository
publicly.

当前尚未包含 license 文件。正式公开仓库前请先补充许可证。
