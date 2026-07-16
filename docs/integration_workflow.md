# Quant Forge 联调流程与程序反馈标准

本文档总结 Quant Forge OpenSource 在本地联调时的推荐流程，以及每个关键节点应该给出的程序反馈。目标是让人类用户和 agent 都能判断：流程是否跑通、数据是否用对、结果是否可信、失败时应该从哪里排查。

当用户要求“项目全量联调”“新用户 Docker 联调”“从 main 拉取并完整跑通”时，默认使用
[`docs/full_integration_test_prompt.md`](full_integration_test_prompt.md) 作为 Codex/agent
执行 prompt。该 prompt 会随项目配置、RD 流程和验收标准更新。

## 1. 联调原则

- 所有路径、时间范围、成本假设、LLM provider、RD 参数都应来自 config、CLI 参数或环境变量。
- Web、CLI、RD、报告生成应共享同一套 `data_root`、`factor_root`、`factor_values_root`、`factor_values_overlay_root`、`factor_values_manifest_root`、`artifact_root`。
- 程序反馈必须包含可追溯的 artifact 路径，不只说“完成”。
- 研究回测指标应明确区分研究口径与生产交易口径。
- LLM API key 只读环境变量，不写入配置文件、日志或报告。
- 前端联调必须优先使用 Computer Use 操作桌面 Chrome app；如果当前会话没有
  Computer Use 工具，或 Computer Use 无法稳定读取窗口，才允许降级为桌面 Chrome
  自动化 fallback，并在反馈中标记 `fallback_used=true` 和降级原因。
- API/HTTP 调用只能用于补充核对 JSON、artifact、trace 和后端参数，不得替代前端
  输入框、按钮和页面反馈联调。

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
| 记忆先验复核 | `qf memory priors` 或 Web 记忆 review 页 | 返回 `as_of` revision、分维度 passed/blocked 计数与比率、`invalid_rows` | `unknown`/`not_applicable` 只计数、不进比率分母 |
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
- HAC t-stat，并保留 naive t-stat 作为诊断字段。
- Coverage、IC days 和 coverage lineage。
- Horizon matrix：如 5/10/21/63 日。
- IS/OOS1/OOS2 split metrics。
- Evaluation artifact path。
- `schema_version=qf.metrics.v2`、metric status/method/N 和 warning codes。
- 常量或近常量 IC 序列应显示 HAC t-stat `n/a`，并带有
  `DEGENERATE_IC_SERIES` warning code。

如果 OOS 明显弱于 IS，程序应给出 warning，而不是只展示全样本均值。

### 3.6 回测反馈

回测反馈应同时展示收益和口径：

- `holding_days`
- evaluation period / backtest period 是否分别符合配置
- `execution_delay_days`
- `top_quantile`
- `gross_annualized_return`
- `net_annualized_return`
- `reportable_annualization` 与 `extrapolated_annualization`，短样本时主年化应为
  `null` 而不是机械外推值
- `max_drawdown`
- `long_short_sharpe`
- `daily_nav`，最大回撤应来自日频 mark-to-market NAV
- `initial_build_turnover`
- `rebalance_turnover_mean`
- `rebalance_rate`，即成分替换率/调仓。
- `turnover_rate`，即按组合权重变化计算的真实换手率。
- transaction cost assumptions
- Backtest artifact path

特别注意：UI 和报告必须区分 `rebalance_rate` 与 `turnover_rate`，不能把成分替换率
包装成真实交易换手。
首期从空仓建立 long/short 组合的 `initial_build_turnover` 可以产生交易成本，但不得
进入持续再平衡换手均值。样本不足、无再平衡观测或来源序列缺失时，JSON 和页面应
显示 `null`/`n/a` 加 status，不得显示为 `0.00%`。

### 3.7 RD 反馈

RD 运行反馈应包含：

- Seed factor id。
- Objective 和 objective weights。
- 候选数量。
- RD 迭代次数；如果大于 1，应展示每轮 seed、下一轮 seed 选择原因和最终 factor id。
- 每个候选的公式、score、gate 结果。
- 失败原因或风险原因。
- Accepted candidates。
- Report path。
- `workflow_type=research`；普通 research 只做 idea research 和参数/profile search。
- 如果 LLM review 返回不完整 JSON，应展示 `normalization_warnings`，但不泄露 raw key。
- 如果多轮 RD 在第 2 轮或之后失败，应展示已完成轮次的候选、报告和对比表，同时返回
  `partial_result=true`、`failed_round_index` 和 `chain_error`。只有第 1 轮没有任何
  可用结果时，才应把整个 RD job 视为无结果失败。
- LLM self-review 是辅助复盘，不应阻塞已完成候选评价。self-review 超时可以记录为
  `llm_self_review_error` 并继续；LLM hypothesis generation 超时则必须明确写入
  失败原因或部分结果停止原因。

如果未来增加因子合成，应作为独立 workflow 重新设计，不应混入普通 research 的
单因子 idea 生成逻辑。

如果候选只在 IS 好、OOS 弱，应反馈：

```text
Gate warning: full-sample return is positive, but OOS return or OOS ICIR is weak.
```

### 3.8 自我进化记忆反馈

RD 结果会以中性 outcome 契约（`qf.research_outcome.v2`）写入本地研究记忆
（`artifact_root` 下的 append-only ledger）。联调时应检查：

- `qf memory priors [--json]` 输出应包含：`as_of`（outcomes ledger
  revision）、每个泛化维度单元的 `passed`/`blocked` 计数与比率、
  `unknown`/`not_applicable` 仅作为计数展示（不进入比率分母）、
  `invalid_rows`（schema 校验失败被剔除的行数，必须显式展示，不得静默丢弃）。
- ledger 不存在（尚未运行过 RD）时，priors 应返回空视图而不是报错。
- `qf memory rules list [--active|--pending]` 展示已晋升规则及其状态；
  `activate`/`deactivate`/`retire`/`unretire` 必须要求 `--actor`，rationale
  经 redact 后落盘；事件只追加，不改写历史。
- Web 记忆 review 页（`GET /api/memory/review`）应展示晋升的
  findings/failures、retired 状态与规则状态，且来自同一次锁内快照；
  `POST /api/memory/review/rule|promoted` 的字段必须是字符串，null/数字等
  非字符串输入应返回 400，不得把 `"None"` 落成 reviewer 身份。
- 外部插件产生的 outcome 只写入插件自己的 store（插件本地根目录），不进入
  本地晋升池；本地主 store 只接受 `origin="local"`。
- 所有指标读数遵循 null+status 原则：不可得的值不得显示为 `0.0`。

## 4. Web 联调时的推荐反馈状态

| 页面动作 | 运行中状态 | 成功状态 | 失败状态 |
| --- | --- | --- | --- |
| 解析并验证 | `运行中...` | `验证完成`，展示 factor/evaluation/backtest/artifacts | 显示 parser/provider/config/key/field/operator 错误 |
| RD 运行一次/多轮递进 | `RD 运行中...` | `RD 完成`，展示 iteration chain、accepted candidates 和 report path | 显示 objective/gate/config/data/iterations 错误 |

Web 已删除自动 RD 开启/停止控件；后端 `POST /api/research/schedule` 与 CLI 调度保留，仅在 API/CLI 联调中验证。

### 4.1 前端交互等级

#### Level 1: 严格用户模拟，优先使用

使用 Computer Use 操作桌面已经安装的 Chrome app，以鼠标、键盘和视觉反馈完成
真实用户路径。必须覆盖：

1. 打开本地 Web URL。
2. 选择 LLM provider。
3. 检查 LLM API key 控件状态。
4. 输入自然语言因子观点。
5. 点击“解析因子”。
6. 修改持有期、Decay、Top Quantile、Delay、评价时间窗、回测时间窗和交易成本。
7. 点击“验证并评测”。
8. 设置 RD 迭代次数。
9. 点击 RD “运行一次”。

联调报告必须保留截图、页面状态摘要或逐步操作记录。

#### Level 2: Chrome 自动化 fallback

仅当 Computer Use 不可用或无法稳定读取窗口时使用。允许用 Chrome Plugin、
`node_repl` + Playwright 或其他可打开桌面 Chrome app 的方式自动化操作，但必须：

- 打开本机 Chrome app，不使用 Codex 内置浏览器。
- 仍通过前端输入框和按钮完成流程。
- 在报告中写明 `frontend_interaction.mode=chrome_playwright_fallback`
  或等价字段。
- 标记 `fallback_used=true`。
- 说明降级原因，例如 `computeruse_tool_unavailable`。
- 不把 Chrome 自动化 fallback 描述为严格 Computer Use。

#### Level 3: API 验证，仅作补充

API/HTTP 调用只允许核对后端 contract、artifact 路径、日期窗口、RD trace 和错误
细节。不得直接调用 `/api/jobs/parse-idea`、`/api/jobs/validate-idea` 或
`/api/jobs/research-run-once` 来替代点击“解析因子”“验证并评测”或 RD “运行一次”。
如果某一步只能通过 API 完成，报告中必须列入 `api_only_steps`，并说明该步骤未完成
真实前端联调。

### 4.2 前端配置输入框联调清单

每次涉及 Web 前端、LLM provider、时间窗或评价参数的修改后，除正常解析/回测外，还应按下列步骤做浏览器联调：

1. 打开桌面 Chrome，访问本地 Web URL。
2. 检查左侧 runtime strip 是否展示当前 `LLM`、`data`、`factors`、`values`、`overlay`、`artifacts`。
3. 在 `LLM Provider` 中选择 DeepSeek 或当前配置的云端 provider。
4. 检查 `LLM API Key` 控件：
   - 默认模式应为“配置文件 / 环境变量加载”。
   - 如果 provider key 已通过配置和环境变量加载，password 输入框必须置灰。
   - 页面不得显示真实 API key，只能显示环境变量名或状态说明。
   - 切换到“手动输入（仅前端联调）”时，输入框才可编辑。
   - 手动输入的内容不得进入 parse/validate 请求体、日志、artifact 或报告；正式调用仍使用后端配置的环境变量。
5. 输入自然语言因子观点，点击“解析因子”。
6. 解析完成后应只展示 factor 草稿和待确认参数，不应立即执行评价/回测。
7. 检查评测参数区是否自动填入默认值：
   - `持有期 / 天`
   - `Decay / 天`
   - `Top Quantile`
   - `Delay / 天`
   - `评测开始`、`评测结束`
   - `回测开始`、`回测结束`
   - 手续费、滑点、融券成本
8. 修改一次时间窗并验证是否生效，例如：
   - 因子评价/验证时间段：`2025-01-01` 到 `2025-12-31`
   - 因子持仓回测时间段：`2026-01-01` 到最新可用日期或显式结束日期
9. 点击“验证并评测”。
10. 成功结果中必须同时展示：
    - `evaluation period`
    - `backtest period`
    - 持有期、Delay、Decay、Top Quantile
    - 毛收益、净收益、日频回撤、Sharpe、initial build turnover、rebalance turnover、真实换手率
    - `reportable annualization` 和 `extrapolated annualization` 的区别
    - 样本充分性 warning code 与 metric status/method/N
    - 因子值缓存状态和 artifact 路径
11. 验证后端实际使用的时间窗：
    - `evaluation.simulation_profile.test_period_start/end` 应等于前端评测时间窗。
    - `backtest.simulation_profile.test_period_start/end` 应等于前端回测时间窗。
    - 两套时间窗不能被一个共享字段互相覆盖。
12. 负向测试：
    - 输入非法日期格式时，应在计算前返回可读错误。
    - 开始日期晚于结束日期时，应在计算前返回可读错误。
    - 缺少 LLM key 时，应提示 provider 和环境变量名，不打印真实 key，并在用户确认后才改用本地规则解析。

### 4.3 RD 迭代次数联调清单

每次涉及 RD Web 控件、后端 RD workflow 或 scheduler 的修改后，应检查：

1. `RD迭代次数` 输入框存在，默认值为 `1`，只接受正整数，并有上限。
2. `候选数量` 仍表示每轮候选数，不应被误解为递进轮数。
3. 点击 `运行一次` 时，前端请求体应同时包含：
   - `seed_factor_id`
   - `objective`
   - `max_candidates`
   - `iterations`
4. 当 `iterations=1` 时，行为应与旧版单轮 RD 兼容。
5. 当 `iterations>1` 时，后端应连续运行多轮：
   - 第 1 轮使用用户输入 seed。
   - 下一轮优先使用上一轮 accepted candidate。
   - 如果没有 accepted candidate 但有候选，则使用最高分候选继续，并标记 `fallback_best_score`。
   - 如果没有候选，或最高分候选仍是原 seed，应提前停止，并展示停止原因。
6. 成功结果应展示：
   - `requested_iterations`
   - `iteration_count`
   - `original_seed_factor_id`
   - `recommended_factor_id` / `final_factor_id`（兼容别名）：最终推荐因子，只能来自通过 gate 的候选；没有通过 gate 的候选时保留原始 seed。
   - `last_accepted_factor_id`：最近一次通过 gate 的候选。
   - `last_explored_factor_id`：最近一次探索到的候选，可能未通过 gate。
   - `next_exploration_seed_factor_id` / `next_exploration_seed_reason`：如果继续 RD，下一轮探索会使用的 seed 及原因。
   - `stopped_reason`
   - 每轮 seed、top candidate、next seed、selection reason。
   - 当前端显示 `fallback_best_score` 且未通过 gate 时，必须明确说明“仅用于探索，不构成最终推荐”。
7. 如果第 N 轮失败但前 N-1 轮已经完成，结果应保留：
   - `iteration_count` 等于已完成轮次。
   - `failed_round_index` 等于失败轮次。
   - `stopped_reason=iteration_failed`。
   - `chain_error` 展示可读错误摘要。
8. 仅在后端 API/CLI 调度联调中：`POST /api/research/schedule` 携带 `iterations` 时，每次调度触发都应执行同样的 N 轮链式 RD；Web 不再提供自动周期控件。
9. 点击 `中断本次RD` 时，链式 RD 应在轮间或安全检查点响应取消。

### 4.4 RD 长任务与性能诊断清单

真实 DeepSeek + 挂载盘全量数据的 RD 不是毫秒级 smoke。联调模板应区分三档：

1. 快速 smoke：`iterations=1`，小 `max_candidates`，验证配置、LLM、评价/回测和报告路径。
2. 标准联调：`iterations=2` 或 `3`，验证递进 seed、部分失败保留和前端对比表。
3. 重型验收：`iterations=5`，只在明确要求长链 RD 时运行，并记录每轮 trace、耗时和终态。

当 RD job 超过 10 分钟仍无终态：

- 查询 `/api/jobs/<job_id>`、`run.json` 和 `trace.jsonl` 最新事件。
- 如果 CPU 低且 trace 停止更新，优先排查 LLM/network/self-review 等等待点。
- 如果 CPU 高且 trace 停止更新，优先采样本地 Python 进程，检查是否卡在滚动算子或
  factor-value cache miss，例如 `ts_rank`、`decay_linear`、`correlation`、`covariance`。
- 对滚动算子性能修复，必须保留语义测试，至少覆盖 `ts_rank` 的最后值百分位排名和
  `decay_linear` 的“越近权重越大”方向。
- 对 LLM 超时韧性修复，必须分别测试普通生成请求的有限重试和 self-review 快速降级。

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
| 记忆 outcome 行损坏或 schema 不符 | 计入 priors 的 `invalid_rows` 并显式展示，不静默按 0 处理 |
| 规则签名前缀不唯一或不存在 | 返回可读错误（HTTP 400 / CLI 错误），不做模糊匹配 |

## 6. 最小验收清单

完成一次正常联调后，至少应留下：

- 一份生效 config 和 RD config。
- 一份 `qf doctor` 输出，且没有 error。
- 前端交互方式记录：`computeruse`、`chrome_playwright_fallback` 或 `api_only`。
- `fallback_used`、降级原因、`chrome_app_used` 和 `api_only_steps`。
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
