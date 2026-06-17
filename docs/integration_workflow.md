# Quant Forge 联调流程与程序反馈标准

本文档总结 Quant Forge OpenSource 在本地联调时的推荐流程，以及每个关键节点应该给出的程序反馈。目标是让人类用户和 agent 都能判断：流程是否跑通、数据是否用对、结果是否可信、失败时应该从哪里排查。

## 1. 联调原则

- 所有路径、时间范围、成本假设、LLM provider、RD 参数都应来自 config、CLI 参数或环境变量。
- Web、CLI、RD、报告生成应共享同一套 `data_root`、`factor_root`、`factor_values_root`、`factor_values_overlay_root`、`factor_values_manifest_root`、`artifact_root`。
- 程序反馈必须包含可追溯的 artifact 路径，不只说“完成”。
- 研究回测指标应明确区分研究口径与生产交易口径。
- LLM API key 只读环境变量，不写入配置文件、日志或报告。
- 桌面 Chrome 联调属于 agent/orchestration 层；如果 Computer Use 无法读取窗口但
  AppleScript 能读取 Chrome URL，可继续用桌面 Chrome 的 DOM/fetch 路径，并在反馈中
  标记 `fallback_used=true`。

## 2. 正常联调主路径

| 阶段 | 操作 | 正常程序反馈 | 必须确认 |
| --- | --- | --- | --- |
| 环境检查 | 安装依赖或进入虚拟环境 | `qf --help` 或 `PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help` 正常输出命令列表 | Python 环境可用，包导入无错误 |
| 运行前自诊断 | `qf doctor --config <local-config> --rd-config configs/rd.yaml` | 返回 `ok`、逐项 `checks`、生效路径、数据摘要、LLM readiness、下一步命令 | 先修复 error，再进入 Web/CLI 联调 |
| 配置加载 | 指定 `--config`、`--rd-config`、`--workspace` | 打印或接口返回 `data_root`、`factor_root`、`artifact_root`、web host/port、LLM provider | 路径指向预期工作区或挂载盘 |
| 数据校验 | `qf data validate` 或 Web 状态检查 | 显示必需字段、行数、标的数、日期范围、缺失字段 | 至少包含 `trade_date, instrument, close, market_cap, is_st` |
| 因子来源 | 因子列表、自然语言解析、研报解析或已有 factor id | 返回 `factor_id`、`name`、`formula`、`status`、`source` | 公式、过滤条件、预测周期符合预期；预计算因子应使用 `precomputed:factor_id=<FACTOR_ID>` |
| LLM 解析 | 在 Web 或 CLI 选择 provider | 成功时返回 provider/model；失败时明确提示缺少哪个 env key | 不泄露真实 API key |
| 因子计算 | 执行 factor engine | 返回计算行数、覆盖率、缺失率、缓存/落盘路径 | 如果已有因子值，应提示复用或增量补齐 |
| 因子评价 | `eval-factor` 或 Web 评价 | 返回 Rank IC、ICIR、coverage、IC days、horizon matrix、IS/OOS split | OOS 是否衰减应显式展示 |
| 回测 | `run-backtest` 或 Web 回测 | 返回持有期、调仓间隔、执行延迟、毛/净收益、回撤、Sharpe、换手口径、artifact path | 换手率必须说明口径 |
| RD 单次运行 | `research run-once` 或 Web RD | 返回 seed factor、objective、候选列表、score、gate 结果、accepted candidates、report path | 不应只看全样本收益 |
| 报告生成 | RD report 或最终报告 | 输出 Markdown/HTML/PDF 路径，报告内含配置、数据源、关键指标、风险说明 | 报告能复现本次联调路径 |
| 结束复核 | 测试和安全扫描 | pytest/help/git diff check 通过，或明确说明未运行原因 | 无秘密、无绝对私有路径被提交 |

## 3. 关键节点的反馈要求

### 3.1 配置加载反馈

程序应该返回或展示：

- `config_path`
- `rd_config_path`
- `workspace`
- `data_root`
- `factor_root`
- `artifact_root`
- `factor_values_root`
- `factor_values_overlay_root`
- `factor_values_manifest_root`
- `output_root`
- `simulation.test_period`
- `evaluation.simulation.test_period`（如配置，控制因子评价/IC 证据）
- `backtest.simulation.test_period`（如配置，控制持仓回测证据）
- `llm.provider`
- 可选 LLM provider 列表和对应 `api_key_env`

异常反馈应精确到字段：

```text
paths.data_root is required
llm.providers.deepseek.base_url is required
Missing API key for active LLM provider deepseek. Expected environment variable: DEEPSEEK_API_KEY.
```

`qf doctor` 是新人联调的第一入口。它不会打印真实 API key，只会告诉用户哪个
provider 缺少哪个 `api_key_env`：

```bash
qf doctor --config configs/default.local.yaml --rd-config configs/rd.yaml
```

正常情况下 `checks` 中不应有 `status: error`。`factor_values_root` 未配置或尚未
创建可以是 warning，因为程序会在需要时执行本地计算或写入增量结果。
本地规则解析和 LLM 语义解析必须在 UI 与返回结果中明确区分。选择 LLM 解析时，
如果缺少 API key 或 LLM 请求失败，程序应先返回具体原因，并在用户确认后才改用
本地规则解析；用户拒绝时本次解析/运行应终止。配置 `require_api_key=false`
的本地 OpenAI-compatible endpoint 可不设置 `api_key_env`。

如果配置了挂载盘因子值库，`doctor` 还应展示：

- `factor_root.local_factor_count`
- `factor_root.precomputed_factor_count`
- `factor_values.configured_path`
- `factor_values.path`，即程序最终识别到的 canonical 或 Hive-style 因子值根目录
- `factor_values.overlay_root`，即新增增量值的写入 overlay
- `factor_values.precomputed_factor_count`

如果挂载盘里有历史旧命名目录，或前序研究把 WQ/GTJA/FTR 因子值存在其他
`factor_values` 根下，应先做一次非破坏性规范化：

```bash
qf factor normalize-store --config configs/default.local.yaml --dry-run
qf factor normalize-root --config configs/default.local.yaml
qf factor normalize-store --config configs/default.local.yaml \
  --scan-root <MOUNT_ROOT>/QuantForgeData \
  --link-files
qf factor import-precomputed --config configs/default.local.yaml --all
```

规范化后，因子定义应进入
`factor_root/{原始因子,合成因子}/{active_factors,inactive_factors}/<FACTOR_ID>/factor.yaml`，
新增和注册的预计算因子值应使用
`factor_values_root/{原始因子,合成因子}/factor_id=<FACTOR_ID>` 目录；旧的
`worldquant_alpha_*`、中文名、风格名等目录只作为可读兼容资产保留。跨来源合并
只向 configured `factor_values_root` 写入规范目录，不删除源目录。运行时补算缺失日期时，
新增增量只在配置了 `factor_values_overlay_root` 时写入 overlay 的对应分类目录；未配置
overlay 时只在本次运行内计算，不写回 canonical 根。

### 3.2 数据校验反馈

正常反馈应包含：

- 数据文件路径。
- 行数、标的数、交易日数。
- 最小日期、最大日期。
- 必需字段是否齐全。
- ST、停牌、成交量、市值等可选字段覆盖情况。

如果数据来自挂载盘或外部快照，报告中应记录来源快照路径或 catalog id。

### 3.3 自然语言解析反馈

正常反馈应包含：

- 原始输入文本。
- parser mode：`rule` 或 `llm`。
- provider/model。
- 生成的 `factor_id`。
- 公式与 universe filters。
- 公式使用到的字段和算子。
- 保存路径。

示例：

```text
Parsed factor FTR_XXXX
source: llm / deepseek / deepseek-chat
formula: -rank(market_cap)
filters: is_st == false
saved: factor_root/原始因子/inactive_factors/FTR_XXXX/factor.yaml
```

### 3.4 因子计算反馈

因子计算不应沉默完成。正常反馈应包含：

- 是否命中已有因子值缓存。
- 是否进行了增量补齐。
- 本次计算日期范围。
- 本次计算标的数和输出行数。
- 输出文件路径。
- 缺失率、异常值处理、截尾/中性化/decay 设置。

当前 CLI/Web 的直接反馈字段包括：

- `score_source`: `factor_values_cached`、`factor_values_incremental` 或 `computed_formula`。
- `score_cached_rows`
- `score_computed_rows`
- `factor_values_path`

如果缺字段或算子，应提示：

```text
Unknown field: market_cap
Unknown operator: ts_rank
Factor values missing for 2025-06-01 to 2025-06-30; incremental compute required.
```

如果普通 research 生成了当前公开算子库无法执行的复杂公式，程序应返回
`requires_operator_draft_review`，并写入
`artifact_root/operator_drafts/<draft_id>/`。这些草稿算子只供 Codex/开发者审计，
不会自动 import 或执行。草稿产物仅包含 JSON/Markdown 元数据，例如
`manifest.json`、`semantics_request.json`、`generated_tests.json`、
`audit_status.json` 和 `review.md`，不得在 `artifact_root` 中落地可执行
Python 算子文件。

### 3.5 评价反馈

评价反馈应至少包含：

- Rank IC、Rank ICIR。
- Coverage、IC days。
- Horizon matrix：如 5/10/21/63 日。
- IS/OOS1/OOS2 split metrics。
- Evaluation artifact path。

如果 OOS 明显弱于 IS，程序应给出 warning，而不是只展示全样本均值。

### 3.6 回测反馈

回测反馈应同时展示收益和口径：

- `holding_days`
- evaluation period / backtest period 是否分别符合配置
- `execution_delay_days`
- `top_quantile`
- `gross_annualized_return`
- `net_annualized_return`
- `max_drawdown`
- `long_short_sharpe`
- `rebalance_rate`，即成分替换率/调仓。
- `turnover_rate`，即按组合权重变化计算的真实换手率。
- transaction cost assumptions
- Backtest artifact path

特别注意：UI 和报告必须区分 `rebalance_rate` 与 `turnover_rate`，不能把成分替换率
包装成真实交易换手。

### 3.7 RD 反馈

RD 运行反馈应包含：

- Seed factor id。
- Objective 和 objective weights。
- 候选数量。
- 每个候选的公式、score、gate 结果。
- 失败原因或风险原因。
- Accepted candidates。
- Report path。
- `workflow_type=research`；普通 research 只做 idea research 和参数/profile search。
- 如果 LLM review 返回不完整 JSON，应展示 `normalization_warnings`，但不泄露 raw key。

如果未来增加因子合成，应作为独立 workflow 重新设计，不应混入普通 research 的
单因子 idea 生成逻辑。

如果候选只在 IS 好、OOS 弱，应反馈：

```text
Gate warning: full-sample return is positive, but OOS return or OOS ICIR is weak.
```

## 4. Web 联调时的推荐反馈状态

| 页面动作 | 运行中状态 | 成功状态 | 失败状态 |
| --- | --- | --- | --- |
| 解析并验证 | `运行中...` | `验证完成`，展示 factor/evaluation/backtest/artifacts | 显示 parser/provider/config/key/field/operator 错误 |
| RD 运行一次 | `RD 运行中...` | `RD 完成`，展示 accepted candidates 和 report path | 显示 objective/gate/config/data 错误 |
| 自动 RD 开启 | `调度启动中...` | `调度已开启`，展示 last result | 显示 interval/objective/config 错误 |
| 自动 RD 停止 | 按钮 disabled | `调度已停止，累计运行 N 次` | 显示 scheduler 错误 |

Web 页面应始终展示当前生效路径：

```text
data_root: ...
factor_root: ...
artifact_root: ...
```

## 5. 常见异常与期望反馈

| 异常 | 期望反馈 |
| --- | --- |
| 配置文件不存在 | 指出缺失路径和启动参数 |
| 数据目录不存在 | 指出 `data_root`，提示运行 init 或检查挂载盘 |
| 面板字段缺失 | 列出缺失字段和最小数据契约 |
| LLM key 未设置 | 指出 provider 和环境变量名，不打印 key |
| LLM 调用失败 | 返回 HTTP/网络错误摘要，询问是否改用本地规则解析 |
| 算子不存在 | 指出公式中的未知算子 |
| 字段不存在 | 指出公式中的未知字段 |
| 因子值已有 | 提示复用缓存和缓存路径 |
| 因子值部分缺失 | 提示缺失日期范围并执行或建议增量补齐 |
| OOS 失效 | 在评价、回测和 RD report 中明确 warning |
| 换手口径过轻 | 标注为成分替换率，不作为真实交易换手 |
| RD objective 未配置 | 指出缺失 `weight_profile` 或 objective 配置 |

## 6. 最小验收清单

完成一次正常联调后，至少应留下：

- 一份生效 config 和 RD config。
- 一份 `qf doctor` 输出，且没有 error。
- 一个可追溯的 factor definition。
- 一份 evaluation artifact。
- 一份 backtest artifact。
- 一份 RD report。
- Web 或 CLI 输出中的完整 artifact 路径。
- 对数据范围、执行延迟、持有期、成本假设、换手口径的说明。
- 如果产生最终报告，报告中应记录所有关键路径和风险说明。

## 7. 推荐最终反馈模板

```text
联调完成。

数据源：<data_root / panel / date range>
因子：<factor_id / formula / filters>
评价：Rank IC <x>, ICIR <y>, coverage <z>, OOS <summary>
回测：gross ann <x>, net ann <y>, max drawdown <z>, turnover口径 <description>
RD：accepted <ids>, report <path>
Artifacts:
- evaluation: <path>
- backtest: <path>
- report: <path>
风险：
- <OOS/turnover/cost/data limitation>
```

这类反馈比单纯输出“完成”更适合联调，因为它能让后续 agent 或人类用户直接接手复核。
