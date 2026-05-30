# Quant Forge OpenSource User Manual / 使用手册

This manual is written for both human users and LLM coding agents. It explains
how to configure, run, debug, and safely publish Quant Forge OpenSource.

本文档同时面向下载用户和 LLM 编程助手，说明如何配置、运行、排错并安全发布
Quant Forge OpenSource。

## 1. Project Shape / 项目结构

```text
configs/                  Runtime and RD configuration
docs/                     Architecture, configuration, release docs
src/quant_forge/          Public Python package
tests/                    Regression and release-safety tests
scripts/                  Local maintenance scripts
```

```text
configs/                  运行配置与 RD 配置
docs/                     架构、配置、发布文档
src/quant_forge/          公开 Python 包
tests/                    回归测试与开源安全测试
scripts/                  本地维护脚本
```

Generated workspaces are not part of the source repository. `qf init` creates
data, factors, and artifacts under the workspace you pass with `--workspace`.

生成的工作区不属于源码仓库。`qf init` 会在 `--workspace` 指定的位置生成数据、因子和产物。

## 2. Installation / 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

If `qf` is not on your PATH, use:

如果命令行找不到 `qf`，使用：

```bash
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
```

## 3. First Run / 第一次运行

```bash
qf init --workspace ./qf-demo
qf data validate --workspace ./qf-demo
qf factor list --workspace ./qf-demo
```

Expected result:

预期结果：

- `data validate` reports the required local panel fields.
- `factor list` shows demo factors under the demo `factor_root`.
- `data validate` 会报告本地面板字段。
- `factor list` 会显示 demo 工作区中的示例因子。

## 4. Main Workflows / 主要流程

### 4.1 Idea To Factor / 观点转因子

```bash
qf idea-to-factor --text "small non-st stocks perform better" --workspace ./qf-demo
```

This creates a draft factor. Draft factors are saved through the factor
repository, not written directly by an agent.

该命令创建草稿因子。草稿因子必须通过 factor repository 保存，不能由 agent 直接写入。

### 4.2 Evaluation / 因子评价

```bash
qf eval-factor FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
```

Evaluation calculates Rank IC, ICIR, coverage, chronological sample splits, and
horizon-matrix evidence.

评价会计算 Rank IC、ICIR、覆盖率、时间顺序样本切分和周期矩阵证据。

### 4.3 Lightweight Backtest / 轻量回测

```bash
qf run-backtest FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
```

The public backtest uses signal date to next-trading-day entry semantics. It is
for local research triage, not production execution.

公开版回测采用 signal date 后的下一个交易日入场语义。它用于本地研究筛选，不是生产交易系统。

### 4.4 RD Run Once / RD 单次研究

```bash
qf research run-once FTR_DEMO_SMALL_CAP --workspace ./qf-demo --rd-config configs/rd.yaml
```

The RD loop generates bounded hypotheses, evaluates candidates, applies smoke
gates, and writes a Markdown report under `artifact_root/research_reports`.

RD 循环会生成有限候选假设，评价候选因子，应用 smoke gate，并在
`artifact_root/research_reports` 写入 Markdown 报告。

### 4.5 Local Web / 本地 Web

```bash
qf web --workspace ./qf-demo --config configs/default.yaml --rd-config configs/rd.yaml
```

The web adapter is local-only. Use it for idea parsing, evaluation, backtest,
and RD triggering from a browser.

Web 适配器仅面向本地。可在浏览器里完成观点解析、评价、回测和 RD 触发。

## 5. Configuration Files / 配置文件

### 5.1 `configs/default.yaml`

Purpose:

- `paths`: where data, factors, artifacts, and outputs live.
- `web`: local host and port.
- `research`: general default horizon and top quantile.
- `simulation`: execution delay, top quantile, test period, and signal handling.
- `llm`: provider registry and environment variable names.

用途：

- `paths`：数据、因子、产物、输出目录。
- `web`：本地 host 与端口。
- `research`：默认预测周期与 top quantile。
- `simulation`：执行延迟、top quantile、测试区间和信号处理规则。
- `llm`：大模型供应商注册表与环境变量名。

Important rule:

重要规则：

```text
api_key_env is the environment variable name.
api_key_env 是环境变量名，不是真实 API key。
```

### 5.2 `configs/rd.yaml`

Purpose:

- `objective`: selected RD objective.
- `sample_splits`: IS/OOS split fractions and score weights.
- `horizon_days_matrix`: horizons evaluated for evidence.
- `gate`: minimum quality threshold for candidates.
- `weights`: scoring weights for the default objective.
- `weight_profiles`: named scoring presets.
- `parameter_search`: optional profile search settings.

用途：

- `objective`：RD 目标。
- `sample_splits`：IS/OOS 切分比例与评分权重。
- `horizon_days_matrix`：评价证据使用的周期矩阵。
- `gate`：候选因子最低质量门槛。
- `weights`：默认目标的评分权重。
- `weight_profiles`：命名评分预设。
- `parameter_search`：可选参数搜索配置。

### 5.3 Draft Templates / 配置模板

Use these templates when creating your own config files:

创建自己的配置文件时，可复制这些模板：

- `configs/default.draft.yaml`
- `configs/rd.draft.yaml`

Recommended pattern:

推荐模式：

```bash
cp configs/default.draft.yaml configs/my_default.yaml
cp configs/rd.draft.yaml configs/my_rd.yaml
```

Do not commit machine-local config files if they contain local paths or private
runtime choices.

如果配置中包含本机路径或私有运行选择，请不要提交。

## 6. LLM Configuration / 大模型配置

Example:

```yaml
llm:
  provider: deepseek
  timeout_seconds: 30
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: DEEPSEEK_API_KEY
```

Runtime shell:

运行时 shell：

```bash
export DEEPSEEK_API_KEY="<your-api-key>"
qf web --workspace ./qf-demo --config configs/default.yaml --rd-config configs/rd.yaml
```

If parsing fails with a missing key error, check:

如果解析时报缺少 key，检查：

1. The selected provider name in `llm.provider`.
2. The provider entry under `llm.providers.<provider>`.
3. The spelling of `api_key_env`.
4. Whether the environment variable is set in the same shell that starts `qf`.

1. `llm.provider` 选择的供应商名。
2. `llm.providers.<provider>` 中是否有对应配置。
3. `api_key_env` 拼写是否正确。
4. 启动 `qf` 的同一个 shell 是否设置了该环境变量。

## 7. Data Configuration / 数据配置

The required local panel fields are:

本地面板必需字段：

```text
trade_date, instrument, close, market_cap, is_st
```

Optional fields used by built-in formulas:

内置公式使用的可选字段：

```text
volume, return_1d, return_5d, volatility_5d
```

Validate before research:

研究前先校验：

```bash
qf data validate --workspace ./qf-demo
```

## 8. Common Errors / 常见错误

### Missing `panel.parquet`

```text
data_root does not contain panel.parquet
```

Fix:

修复：

```bash
qf init --workspace ./qf-demo
qf data validate --workspace ./qf-demo
```

### Missing LLM provider field

```text
llm.providers.deepseek.base_url is required
```

Fix the named field in the config file.

修复配置文件中报错指出的字段。

### Missing API key environment variable

```text
Missing API key for LLM provider deepseek
```

Fix:

修复：

```bash
export DEEPSEEK_API_KEY="<your-api-key>"
```

### Unsupported simulation option

```text
neutralization must be none
truncation is not supported in the public first version
```

Fix `simulation` or RD `simulation` config. The first public version supports:

修复 `simulation` 或 RD `simulation` 配置。公开第一版支持：

```yaml
nan_policy: drop
neutralization: none
truncation: null
```

## 9. Release Safety / 开源发布安全

Before publishing:

发布前：

```bash
python3 scripts/release_safety_scan.py
PYTHONPATH=src pytest
git diff --check
```

The scanner checks tracked and unignored files only. It intentionally ignores
local files matched by `.gitignore`, such as local environment files, generated
artifacts, caches, and local data.

扫描器只检查已跟踪和未忽略文件。它会忽略 `.gitignore` 中的本地环境文件、产物、
缓存和本地数据。

## 10. What LLM Agents Should Not Do / LLM Agent 禁止事项

- Do not write directly to `factor_root`, `data_root`, or `artifact_root`.
- Do not store API keys in YAML, tests, docs, or source code.
- Do not add silent fallbacks for missing fields or provider settings.
- Do not add production trading behavior to this open-source workbench.
- Do not commit generated outputs, local paths, or local data.

- 不要直接写 `factor_root`、`data_root` 或 `artifact_root`。
- 不要把 API key 写入 YAML、测试、文档或源码。
- 不要为缺失字段或供应商配置添加静默兜底。
- 不要把生产交易行为加入此开源工作台。
- 不要提交生成产物、本机路径或本地数据。
