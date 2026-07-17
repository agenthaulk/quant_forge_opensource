# Quant Forge OpenSource

[![GitHub stars](https://img.shields.io/github/stars/agenthaulk/quant_forge_opensource?style=social)](https://github.com/agenthaulk/quant_forge_opensource/stargazers)

If this project helps your research, please star the repository before cloning
or using it. Your star helps others discover the project and supports continued
maintenance.

如果这个项目对你的研究有帮助，欢迎在 clone 或使用前给仓库点一个 Star。你的 Star
能帮助更多研究者发现这个项目，也支持后续持续维护。

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

Python versions:

- Supported: Python 3.11 or newer (`requires-python >= 3.11` in
  `pyproject.toml`).
- Reference (tested) baseline: Python `3.12.x` on macOS/Linux, or Docker
  image `python:3.12-slim`. Tests and full integration runs are executed
  against this baseline.
- `constraints.txt` reproduces the reference baseline package set exactly
  (`pip install -e ".[dev]" -c constraints.txt`); the same pins also install
  cleanly on Python 3.11.
- Package dependency floors are in `pyproject.toml`: `numpy>=1.24`,
  `pandas>=2.0`, `pyarrow>=14.0.1`, `pyyaml>=6.0`, `pytest>=8.0` for dev.
- Avoid starting a new setup with a bleeding-edge image such as
  `python:latest` or a new Python minor line until the dependency stack has
  been checked locally.

Python 版本说明：

- 支持范围：Python 3.11 及以上（`pyproject.toml` 中 `requires-python >= 3.11`）。
- 参考（测试）基线：本机 Python `3.12.x`，Docker 使用 `python:3.12-slim`；
  测试与全量联调均在该基线上执行。
- `constraints.txt` 用于精确复现参考基线依赖
  （`pip install -e ".[dev]" -c constraints.txt`）；同一组 pin 在 Python 3.11
  上同样可以安装。
- 依赖下限见 `pyproject.toml`：`numpy>=1.24`、`pandas>=2.0`、
  `pyarrow>=14.0.1`、`pyyaml>=6.0`，开发测试使用 `pytest>=8.0`。
- 不建议新人第一次就使用 `python:latest` 或过新的 Python 镜像；先用稳定镜像跑通
  `qf doctor`、smoke test 和 Web，再升级环境。

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
# Reproduce the reference baseline exactly instead / 精确复现参考基线：
# python3 -m pip install -e ".[dev]" -c constraints.txt
```

### Docker Quickstart / Docker 快速开始

The repository root ships a reference `Dockerfile` built on the documented
`python:3.12-slim` baseline. It installs with `constraints.txt`, copies an
explicit file list so ignored local config and env files never end up inside
the image, and starts the web workbench against the built-in demo workspace.

仓库根目录提供基于 `python:3.12-slim` 基线的参考 `Dockerfile`：使用
`constraints.txt` 安装依赖，按显式文件清单拷贝源码（被忽略的本地配置和 env
文件不会进入镜像），并针对内置演示工作区启动 Web 工作台。

```bash
docker build -t quant-forge .
export QF_WEB_CONTROL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
docker run --rm -p 127.0.0.1:8765:8765 -e QF_WEB_CONTROL_TOKEN quant-forge
```

Then open `http://127.0.0.1:8765/`. `-e QF_WEB_CONTROL_TOKEN` passes the
environment variable by NAME; its value is inherited from your shell at run
time and never appears in the image or in any tracked file. Pass LLM keys the
same way (`-e DEEPSEEK_API_KEY`) or with `--env-file` pointing at an ignored
local env file. If host port `8765` is busy, publish
`-p 127.0.0.1:8876:8765` and open `http://127.0.0.1:8876/`. To run the test
suite inside the container as a smoke gate:
`docker run --rm quant-forge python -m pytest`.

然后在浏览器打开 `http://127.0.0.1:8765/`。`-e QF_WEB_CONTROL_TOKEN` 只传环境
变量名，值在运行时从当前 shell 继承，不会写入镜像或任何被 git 跟踪的文件。
LLM key 同样通过 `-e DEEPSEEK_API_KEY` 或 `--env-file` 指向被忽略的本地 env
文件传入。宿主机 8765 被占用时，可发布为 `-p 127.0.0.1:8876:8765` 并打开
`http://127.0.0.1:8876/`。如需在容器内跑 smoke 测试：
`docker run --rm quant-forge python -m pytest`。

### Common Local/Docker Issues / 常见本地与 Docker 问题

- Minimal Docker images such as `python:3.12-slim` may not include `git`,
  `curl`, or process-inspection tools. Install the basics before cloning,
  smoke testing, or debugging:
  `apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates procps`.
- Docker containers do not automatically inherit your macOS shell variables.
  Put real LLM keys in an ignored file such as `configs/default.local.env`,
  declare it through `runtime.env_files`, or pass it explicitly with
  `docker --env-file`. Never commit the key file.
- Docker Desktop must be allowed to share the mounted data drive. If the
  mounted drive is not visible inside the container, add it in Docker Desktop
  file-sharing settings and mount it with `-v`.
- Supported Python is 3.11 or newer; the reference tested baseline is 3.12
  with the `constraints.txt` pins (see Install above). If dependency
  installation fails, verify `python --version`, recreate the virtual
  environment, and reinstall with
  `python -m pip install -e ".[dev]" -c constraints.txt`.
- LLM-backed RD plus parameter search on a full mounted dataset can take
  several minutes. For first-time smoke testing, use an ignored local RD config
  with `default_max_candidates: 1` and a small parameter/profile grid; expand
  the grid after `qf doctor` and one `qf research run-once` succeed.
- If host port `8765` is already in use, choose another host port, for example
  publish Docker as `127.0.0.1:8876:8765` and open
  `http://127.0.0.1:8876/`.

最小 Docker 镜像、本机 shell 环境变量继承、挂载盘共享、Python 版本，是新人联调中
最常见的环境类问题。这些问题不应通过提交本地路径或密钥解决；请通过 ignored local
config、Docker 启动参数或镜像依赖安装来处理。

### Full Integration Test Prompt / 全量联调 Prompt

When running a full new-user integration test from a fresh Docker container and
desktop Chrome, use [`docs/full_integration_test_prompt.md`](docs/full_integration_test_prompt.md).
It is the canonical prompt for cloning `main`, configuring ignored local paths
and DeepSeek env variables, exercising three LLM factor seeds, running RD for
2-3 rounds, and reporting issues with subagent-assisted debugging.

当需要做“新用户从 Docker 全量联调”时，默认使用
[`docs/full_integration_test_prompt.md`](docs/full_integration_test_prompt.md)。
该文件是后续全量联调 prompt 的维护入口，会随项目配置、RD 流程和验收标准更新。

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
idea generation or review.

`configs/rd.yaml` 默认 local-first。普通 RD 只聚焦研究 idea 和可选的超参数/profile
搜索；如需大模型生成 idea 或复盘，只在被忽略的本地 RD 配置中打开
`llm.hypothesis_mode` 和 `llm.review_mode`。运行 RD 前请先配置当前
`llm.provider` 的 key；如果只是离线 smoke，可复制
`configs/rd.draft.yaml` 为被忽略的本地 RD 配置，并保持 `llm.hypothesis_mode`
和 `llm.review_mode` 为 `local`。

When LLM RD returns an invalid formula, Quant Forge sends the validation error
back to the LLM for bounded repair. With the default RD config, fallback starts
only after three failed LLM formula attempts: the original formula plus two
repairs. A fallback that only reuses the seed is reported as
`no_optimization_performed`; treat it as a failed or smoke-only research run,
not as an optimized factor.

当 LLM RD 返回非法公式时，系统会把公式校验错误回传给 LLM 进行有限次数修复。
默认配置下，只有连续三次 LLM 公式失败后才进入 fallback：原始公式一次，加两次
修复。若 fallback 只是复用 seed，没有产生新公式或新 profile，结果会标记为
`no_optimization_performed`，这只能说明研究失败或 smoke 闭环完成，不能视为因子优化成功。

LLM semantic parsing is intentionally allowed to be non-deterministic: the same
natural-language idea may produce a different but valid formula on another run.
RD candidate results are controlled separately. By default, RD records formula
fingerprints, result signatures, and candidate-shape fingerprints, then skips
duplicate formulas, duplicate result signatures, and over-concentrated candidate
shapes before promoting results. Keep `deduplication.enabled: true` for normal
research runs unless you are intentionally auditing the duplicate-control layer.

LLM 语义解析保留不确定性：同一条自然语言观点在不同运行中可能得到不同但合法的公式。
RD 候选结果另行去重。默认配置会记录 formula fingerprint、result signature 和
candidate-shape fingerprint，并跳过重复公式、重复结果签名以及候选形态过于集中的结果。
正常研究请保持 `deduplication.enabled: true`，只有在专门审计去重层时才关闭。

The RD command prints a `report_path`. The Markdown report is written under
the workspace artifact root, usually `./qf-demo/artifacts/research_reports/`.

RD 命令会输出 `report_path`。Markdown 研究报告默认写入工作区的
`./qf-demo/artifacts/research_reports/`。

RD runtime depends on dataset size, LLM latency, candidate count, and parameter
search grid size. On a full mounted daily A-share panel, one LLM-backed
`run-once` can reasonably take tens of seconds to several minutes. The Web UI
shows a long-running message after 10 seconds and exposes a cooperative cancel
button; cancellation takes effect at safe checkpoints between LLM, evaluation,
and backtest stages.

RD 运行时长取决于数据量、LLM 延迟、候选数量和参数搜索网格。在完整挂载盘 A 股日频面板上，
一次 LLM-backed `run-once` 可能需要几十秒到数分钟。Web 界面会在 10 秒后展示长任务提示，
并提供协作式中断按钮；中断会在 LLM、评价、回测等安全阶段边界生效。

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

When running inside Docker, bind the container service explicitly and publish
the port only to the host loopback interface. In the ignored local config used
inside Docker, set `web.allow_docker_bind: true` and configure
`web.control_token_env` with the name of an environment variable that contains
a per-run browser control token. Runtime/read APIs and mutating Web actions
then require that token.

```bash
export QF_WEB_CONTROL_TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
qf web --config configs/default.local.yaml --rd-config configs/rd.yaml --host 0.0.0.0 --port 8765
# docker run example: publish as 127.0.0.1:8765:8765 on the host
```

The reference `Dockerfile` at the repository root (see Docker Quickstart in
the Install section) generates an equivalent container-local config at build
time, so the manual steps above are only needed for custom containers.

仓库根目录的参考 `Dockerfile`（见安装章节的 Docker 快速开始）会在构建时生成
等价的容器内配置；只有自定义容器才需要手动执行上述步骤。

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
qf llm-smoke --config configs/default.local.yaml --provider deepseek
qf web --config configs/default.local.yaml --rd-config configs/rd.yaml
```

For DeepSeek, the local config should name the environment variable, not the
secret value. A minimal local snippet looks like this:

```yaml
runtime:
  env_files:
    - default.local.env
llm:
  provider: deepseek
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: DEEPSEEK_API_KEY
```

`configs/default.local.env` should contain the secret and must remain ignored:

```bash
DEEPSEEK_API_KEY=<your-api-key>
DEEPSEEK_MODEL=deepseek-chat
```

DeepSeek 配置只应写“环境变量名”，不要把真实 key 写进 YAML。真实 key 放在
`configs/default.local.env` 这类被 git 忽略的本地文件里，或在 Docker 启动时通过
`--env-file` 传入。

For cloud providers, if a selected provider is missing `model`, `base_url`,
`api_key_env`, or the named environment variable, Quant Forge raises a precise
error such as:

对于云端 provider，如果所选供应商缺少 `model`、`base_url`、`api_key_env`，
或对应环境变量没有设置，系统会给出精确错误，例如：

```text
llm.providers.deepseek.base_url is required
Missing API key for active LLM provider deepseek. Expected environment variable: DEEPSEEK_API_KEY.
```

Run `qf llm-smoke --config configs/default.local.yaml --provider deepseek`
before starting Web when you want to verify the full config → env-file → LLM
request path. The smoke output names the provider, model, `api_key_env`, and
parsed factor, but never prints the secret value.

For a local OpenAI-compatible endpoint that does not require auth, set
`require_api_key` to `false`; then `api_key_env` may be omitted and Quant Forge
does not send an Authorization header.

对于不需要鉴权的本地 OpenAI-compatible endpoint，可将 `require_api_key` 设为 `false`；
此时可省略 `api_key_env`，请求中也不会发送 Authorization header。

The same active LLM provider is used by LLM semantic parsing and RD LLM
features. RD does not have a second API-key setting.

LLM 语义解析和 RD LLM 功能共用同一个当前 provider；RD 不再单独配置第二套 API key。

### Switching providers from the Web UI / 在网页端切换大模型

The workbench can also register a provider and take an API key at runtime, so
you can try a model without editing YAML or restarting the server. In the
expert-mode control rail, the **LLM Provider** menu lists the configured
providers plus the built-in presets (`deepseek`, `openai`, `openai_compatible`,
`glm`, `claude`, `minimax`). Switch **LLM API Key** to *前端输入 / manual*, enter
the key (and, for a preset with no default, the model / base URL), and click
**保存并启用 / Save & enable**.

The key is injected into the **running server process environment only**. It is
never written to disk, never echoed in any response, and never logged; a restart
clears it. For a durable key, keep using the ignored `configs/*.local.env` path
above. Like every mutating Web action, the settings write requires the browser
control token on a Docker/`0.0.0.0` bind. Pointing an existing provider at a new
`base_url` requires re-supplying the key in the same request, so a standing key
is never sent to a newly specified host.

网页端也可以在运行时注册 provider 并接收 API key，无需改 YAML 或重启即可试用某个
模型。在专家模式左侧控制栏，**LLM Provider** 下拉会列出已配置 provider 以及内置预设
（`deepseek`、`openai`、`openai_compatible`、`glm`、`claude`、`minimax`）。把
**LLM API Key** 切到「前端输入」，填入密钥（预设无默认值时再补填模型名 / base URL），
点击「保存并启用」。

密钥只注入**运行中的服务进程环境变量**：不写磁盘、不在任何响应中回显、不进日志，重启即
失效；如需持久保存，仍走上文被忽略的 `configs/*.local.env`。与所有会改状态的 Web 操作
一样，在 Docker / `0.0.0.0` 绑定下该写入需要浏览器控制令牌。把已有 provider 指向新的
`base_url` 时必须在同一请求内重新提交密钥，以确保常驻密钥不会被发往新指定的地址。

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
incremental values while the canonical store remains read-only. Factor
definitions and values are split into `原始因子` and `合成因子`: original factors
come from imported/public formulas or precomputed external values, while
synthetic factors come from RD outputs or other explicitly generated research
candidates. The manifest directory stores portable metadata and must not
contain machine-local paths.

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
