# Quant Forge 全量联调 Prompt 模板

当用户要求“项目全量联调”“新用户 Docker 联调”“从 main 拉取并完整跑通”时，默认采用本文件作为 Codex/agent 的执行 prompt。项目架构、配置项、RD 规则或验收标准变化时，应同步更新本文件。

## 使用原则

- 模拟一个全新的使用者，不复用当前工作区源码。
- 从 GitHub 重新 clone `main` 分支到干净临时目录。
- 使用新的 Docker 容器或 Docker compose 环境完成安装、配置、启动、前端使用和 RD 联调。
- 不修改远端 `main`，不提交任何 API key、挂载盘绝对私密路径或私有数据样本。
- DeepSeek key 只能从本机环境变量或 ignored local env 文件继承，不得写入 git tracked 文件。
- A 股数据源、因子库、因子值库、artifact/result 路径可参考挂载盘，但只能写入 local/ignored 配置或 Docker runtime env。
- 环境、依赖、Docker 镜像、系统包缺失等问题不优先改代码自动兜底；记录为新人常见问题，并建议更新 README 或配置文档。
- 如果遇到 bug，先记录复现路径和日志，再启动 subagents 分别进行根因分析、修复、复核和验证。
- 修复后必须从失败阶段继续联调，不得只跑单元测试就结束。

## 前端交互等级

全量联调的前端阶段必须优先模拟真实用户操作，不得只调用后端 API。

### Level 1: 严格用户模拟，优先使用

- 使用 Computer Use 操作桌面已经安装的 Chrome app。
- 通过鼠标、键盘和视觉反馈逐项完成操作。
- 必须覆盖：
  - 打开本地 Web URL。
  - 选择 LLM provider。
  - 检查 LLM API key 控件状态。
  - 输入自然语言因子观点。
  - 点击“解析因子”。
  - 修改持有期、Decay、Top Quantile、Delay、评价时间窗、回测时间窗和交易成本。
  - 点击“验证并评测”。
  - 设置 RD 迭代次数。
  - 点击“运行一次”启动 RD。
- 必须保留截图或页面状态摘要，作为真实前端操作证据。

### Level 2: Chrome 自动化 fallback

仅当当前 Codex 会话没有暴露 Computer Use 工具，或 Computer Use 无法稳定读取桌面窗口时，才允许使用本级。不要因为方便而直接使用 Codex 内置浏览器。

- 优先使用 `node_repl` + Playwright 打开 macOS 已安装的 Chrome app：
  `chromium.launch({ channel: "chrome", headless: false })`。
- 如果 Computer Use 的 `get_app_state` 能读取桌面 Chrome，但 `click` / `set_value` 被拒绝并提示 action session 不活跃，也视为本级 fallback；记录
  `fallback_reason=computeruse_action_session_inactive`，然后改用 Chrome 接口控制桌面 Chrome app。
- 如果使用 Chrome Plugin 或其他自动化方式，也必须打开本机 Chrome app；不能使用 Codex 内置浏览器代替。
- 仍必须通过前端输入框、下拉框和按钮完成流程。
- 最终报告必须写明：
  - `frontend_interaction.mode=chrome_playwright_fallback`。
  - `fallback_used=true`。
  - `fallback_reason=computeruse_tool_unavailable` 或 `computeruse_action_session_inactive`。
- 不得把 Chrome 自动化 fallback 描述为严格 Computer Use。

### Level 3: API 验证，仅作补充

- API/HTTP 调用只允许用于核对 JSON contract、artifact 路径、后端日期窗口、RD trace 和错误细节。
- API 调用不得替代点击“解析因子”“验证并评测”“运行一次”等前端动作。
- 如果某一步只能通过 API 完成，必须在最终报告中标记 `api_only_steps`，并说明该步骤未完成真实前端联调。

### 禁止 shortcuts

- 不得直接调用 `/api/jobs/parse-idea` 来替代点击“解析因子”。
- 不得直接调用 `/api/jobs/validate-idea` 来替代点击“验证并评测”。
- 不得直接调用 `/api/jobs/research-run-once` 来替代点击 RD “运行一次”。
- 不得在没有明确 fallback 标记时，把 Playwright DOM 操作、HTTP 请求或 Codex 内置浏览器联调称为 Computer Use。
- 不得使用 API-only 流程作为“全量前端联调通过”的依据。

## 可直接复制给 Codex 的全流程 prompt

```text
你是 quant_forge_opensource 项目的全流程联调负责人。请模拟一个全新的使用者，在全新的 Docker 环境中，从 GitHub 拉取 quant_forge_opensource 项目的 main 分支，并根据 README.md、docs/configuration.md、docs/integration_workflow.md 和 docs/full_integration_test_prompt.md 完成配置、启动、前端使用和 RD 联调。

目标：
验证一个新用户在完成必要的本地配置后，是否可以不依赖人工 debug，直接使用本项目完成：
1. LLM 自然语言因子解析
2. A 股日频数据上的因子计算
3. 因子回测/评价
4. RD 模块 2-3 轮研究，包括因子思路优化和参数搜索优化
5. 查看最终因子结果和 RD 研究过程

约束：
- 不要修改远端 main。
- 不要提交任何 API key、挂载盘绝对私密路径、私有数据样本。
- 不要把 DeepSeek key 写入 git tracked 文件。
- LLM API key 必须从本机环境变量或 ignored local env 文件继承，例如 DEEPSEEK_API_KEY、DEEPSEEK_API_BASE、DEEPSEEK_MODEL；具体变量名按项目配置文档和代码实际要求确认。
- 如果仓库中存在 ignored 的 `configs/default.local.yaml`，并且其中声明了 `runtime.env_files`，DeepSeek 联调必须使用该 local config，不得用公开 `configs/default.yaml` 代替。
- A 股原始数据源、因子定义库、因子值库、artifact/result 路径参考当前机器挂载盘中的已有路径，但必须写入 local/ignored 配置文件或 Docker runtime env，不得写入正式配置。
- 配置文件中只能写环境变量名，不能写真实 key。
- 若遇到不确定问题，先通过代码、README、docs、config sample 自查；只有方向性问题才向用户求证。
- 若遇到 bug，记录复现路径，同时启动 subagents：
  - architect/debugger：根因分析和修复边界
  - executor：实现最小修复
  - reviewer/verifier：复核是否破坏轻量内核边界，并重跑失败路径
- 项目依赖安装、Docker 镜像、Python 版本、系统包缺失等环境问题可以不改为自动修复，但必须记录到最终报告，并说明 README 应如何提示用户。
- 不引入 PostgreSQL、复杂前端框架、私有 provider 代码或重依赖。
- 不使用 silent fallback 掩盖错误。
- 对 LLM 输出使用明确 JSON schema / contract 校验。
- 对 evaluation/backtest 输出使用 `qf.metrics.v2`；不可用指标必须是 `null`/status，
  不得回填为 `0.0`。
- 常量或近常量 IC 序列不得展示极端 HAC t-stat；应输出 `null`/`n/a`
  并带有 `DEGENERATE_IC_SERIES`。
- 对新用户常见错误给出可操作错误信息。
- 前端联调必须优先使用 Computer Use 操作桌面 Chrome app。只有当前会话没有 Computer Use 工具，或 Computer Use 无法稳定读取窗口时，才允许降级为 Chrome 自动化 fallback。降级时必须记录 `fallback_used=true` 和降级原因。API/HTTP 调用只能作为补充验证，不得替代前端点击流程。

阶段 0：准备和隔离
1. 确认当前本地项目路径和 Git 状态，仅用于记录，不复用当前源码。
2. 创建干净临时目录，模拟新用户环境。
3. 使用 Docker 新容器或 docker compose 启动隔离环境。
4. 从 GitHub 重新 clone 项目 main 分支。
5. 记录：
   - clone URL
   - commit hash
   - Docker image/base
   - Python 版本
   - 依赖安装命令
   - 启动命令
   - 端口映射

阶段 1：按 README 配置
1. 阅读 README.md、docs/configuration.md、docs/integration_workflow.md。
2. 按文档安装依赖。
3. 创建本地 ignored 配置，例如 configs/default.local.yaml、configs/default.local.env 或 README 推荐的 local config。
4. 配置项至少包括：
   - data_root：指向 Docker 内可见的挂载盘 A 股日频数据路径
   - factor_root：指向 Docker 内可见的统一因子定义/注册因子库路径
   - factor_values_root：指向 Docker 内可见的统一因子值库路径
   - factor_values_overlay_root：如需要增量写入，指向可写 overlay
   - factor_values_manifest_root：如项目配置要求
   - artifact_root/result_root：指向挂载盘或临时 result 目录
   - llm provider：deepseek
   - llm api key env：DeepSeek 环境变量名，不直接写 key
   - evaluation/backtest/RD 的测试时间窗，按项目当前配置文档执行
5. 确认 Docker 容器能读取 data_root、factor_root、factor_values_root，并能写 artifact/result。
6. 校验配置加载是否成功。DeepSeek 联调必须先通过：
   - `qf doctor --config configs/default.local.yaml --rd-config configs/rd.yaml`
   - `qf llm-smoke --config configs/default.local.yaml --provider deepseek`
   `doctor` 必须显示 DeepSeek runtime-ready；`llm-smoke` 必须完成一次真实自然语言解析，但不得打印或记录真实 API key。
7. 运行项目推荐 smoke test：
   - python -m pytest
   - PYTHONPATH=src python -m quant_forge.apps.cli.main --help
   - qf doctor 或等价 preflight/doctor 命令

阶段 2：启动后端和前端
1. 在 Docker 中启动项目 Web 服务。
2. 确认服务绑定到 host 可访问端口，例如 http://127.0.0.1:8765/ 或冲突后的替代端口。
   (Confirm the service binds to a host-reachable port, e.g. http://127.0.0.1:8765/ or a fallback port after a conflict.)
3. 检查服务日志，确认：
   - 配置已加载
   - DeepSeek provider 已识别
   - DeepSeek API key env 已被继承，但真实 key 未打印
   - data_root 存在且可读
   - factor_root 存在且可读
   - factor_values_root 存在且可读
   - overlay/artifact/result 路径可写
4. 如果端口冲突，换端口并记录原因。

阶段 3：使用 computeruse 做真实前端联调
1. 优先使用 Computer Use 操作桌面已经安装的 Chrome app，不使用 Codex 内置浏览器。
2. 如果 Computer Use 工具不可用、无法稳定读取窗口，或 `get_app_state` 成功但 `click` / `set_value` 被拒绝，不要使用 Codex 内置浏览器；降级为 Chrome 自动化 fallback：
   - 优先使用 `node_repl` + Playwright，并通过 `chromium.launch({ channel: "chrome", headless: false })` 打开 macOS 已安装的 Chrome app。
   - 通过前端输入框、下拉框和按钮完成操作，不得用 API 替代用户动作。
   - 在最终报告中记录 `frontend_interaction.mode=chrome_playwright_fallback`、`fallback_used=true`、`fallback_reason=computeruse_tool_unavailable` 或 `computeruse_action_session_inactive`。
3. 打开 Chrome，访问项目 Web 前端 URL。
4. 模拟普通用户操作：
   - 选择 DeepSeek LLM
   - 确认 LLM API key 输入框在“配置文件 / 环境变量加载”模式下置灰，且不显示真实 key
   - 切换到“手动输入（仅前端联调）”，确认输入框才可编辑；不要把手动输入内容写入配置、日志、artifact 或报告
   - 打开自然语言因子解析入口
   - 输入三个因子 seed 的自然语言描述
   - 分别触发“解析因子”，确认解析后只生成 factor 草稿和待确认参数，不立即评价/回测
   - 确认或修改验证参数，包括持有期、decay、top quantile、delay、交易成本
   - 确认或修改因子评价/验证时间段，以及因子持仓回测时间段
   - 点击“验证并评测”，确认结果页同时展示 evaluation period、backtest period、缓存状态和 artifact 路径
5. 禁止用直接 API 调用替代上述点击流程。API 只能用于事后核对返回 JSON、artifact 路径和后端实际参数。

三个 seed 需要覆盖不同类型，可使用：
1. 低波动小市值：
   “选择市值较小、近期波动较低、过去 20 个交易日收益稳定的股票，构造一个偏低波动小市值的横截面因子。”
2. 短期反转：
   “过去 5 到 20 个交易日跌幅较大，但最近成交活跃度没有显著下降的股票，未来可能存在短期反转机会。”
3. 量价动量：
   “过去 20 个交易日收益较强，同时成交量相较过去 60 日均值有所放大的股票，构造一个量价确认的动量因子。”

对每个 seed 记录：
- 输入的自然语言
- DeepSeek 返回的结构化结果
- 生成的 factor_id、formula、operators
- 是否通过公式校验
- 是否命中已有因子值缓存
- 是否使用挂载盘数据计算
- 前端提交的因子评价/验证日期范围，以及后端 `evaluation.simulation_profile` 实际日期范围
- 前端提交的因子持仓回测日期范围，以及后端 `backtest.simulation_profile` 实际日期范围
- 解析按钮和验证按钮是否按预期拆分，解析阶段是否没有提前触发评价/回测
- LLM API key 输入框状态是否符合配置加载置灰、手动联调才可编辑、真实 key 不展示/不提交
- 收益、Sharpe、回撤、调仓率、真实换手率、成本后表现
- HAC t-stat、coverage lineage、metric status/method/N
- reportable annualization 与 extrapolated annualization
- daily NAV 最大回撤
- initial build turnover 与 rebalance turnover
- IS / OOS1 / OOS2 表现
- 报告和 artifact 路径

阶段 4：RD 模块联调
1. 将上述 3 个因子作为 RD seed。
2. 使用 DeepSeek 对每个因子进行 2-3 轮 RD research。
   - `iterations=1` 是快速 smoke；`iterations=2` 或 `3` 是标准新用户联调。
   - `iterations=5` 是重型回归/验收模式，只在需要验证长链 RD 时使用。运行前应把
     `max_candidates` 控制在较小值，并预期真实 DeepSeek + 全量挂载盘数据可能耗时
     很长。
   - 若要求 5 轮，必须记录每轮 run directory、trace 最新 phase、运行时长和是否进入
     下一轮；不得因为 job 仍在 running 就声明通过。
3. RD 优化路径必须包括：
   - idea research：提出改进假设
   - 参数搜索：对窗口、权重、阈值、profile 等做轻量搜索
4. Campaign / 多因子组合模式不是本次重点，除非项目文档要求必须进入。
5. 在前端设置 `RD迭代次数`：
   - 至少测试 `iterations=1` 的兼容路径
   - 至少测试一个 `iterations=2` 或 `iterations=3` 的递进路径
   - 确认 `候选数量` 是每轮候选数，不是递进轮数
6. 每轮 RD 记录：
   - hypothesis
   - 修改后的公式或参数
   - formula fingerprint
   - result signature
   - 是否重复候选
   - 是否通过候选多样性检查
   - 是否通过公式/算子校验
   - 是否需要 draft operator
   - 若需要 draft operator，必须放入 draft 算子库并标记待审计，不得直接进入正式算子库
   - 因子评价结果
   - accept/reject reason
7. 对多轮递进 RD 记录：
   - requested_iterations
   - iteration_count
   - original_seed_factor_id
   - 每轮 selected_next_seed_factor_id
   - 每轮 selection_reason，例如 accepted_candidate、fallback_best_score、no_candidates、no_new_seed
   - recommended_factor_id / final_factor_id：最终推荐因子，只能来自通过 gate 的候选；没有通过 gate 的候选时保留原始 seed
   - last_accepted_factor_id：最近一次通过 gate 的候选
   - last_explored_factor_id：最近一次探索到的候选，可能未通过 gate
   - next_exploration_seed_factor_id、next_exploration_seed_reason、next_exploration_seed_gate_passed
   - stopped_reason
   - 如果后续轮次失败但前序轮次已完成，记录 `partial_result=true`、
     `failed_round_index` 和 `chain_error`；前端应继续展示已完成轮次、候选和
     对比表，而不是空白结果。
   - 如果 `selection_reason=fallback_best_score` 且 `next_exploration_seed_gate_passed=false`，前端和报告必须明确说明该候选仅用于下一轮探索，不构成最终推荐
8. 如果 LLM 生成非法公式：
   - 将 validation error 回传给 LLM 修复
   - 连续 3 次失败后才进入 fallback
   - 参数搜索只能作为独立 research lane 运行，不得把非法公式直接视为参数搜索成功
   - 如果没有产生公式或参数变体，必须输出 no_optimization_performed，不能算 RD 成功
9. RD 结束后输出：
   - 每个 seed 的最佳候选
   - 是否优于原始 seed
   - 是否 OOS 稳定
   - 是否成本后仍有效
   - 是否低共线性
   - 最终推荐 top 因子

阶段 5：问题处理机制
如果任何阶段失败：
1. 立即记录失败位置、用户操作、日志、trace、报错。
2. 判断类别：
   - 配置问题
   - Docker 环境问题
   - LLM key/provider 继承问题
   - 数据路径/字段问题
   - 因子公式解析问题
   - 算子缺失问题
   - 回测/评价问题
   - 前端交互问题
   - RD 结构化输出问题
   - 增量缓存或 lookback 性能问题
3. 启动 subagents：
   - architect/debugger：分析根因和修复边界
   - executor：实现最小修复
   - reviewer：审查是否引入复杂度或破坏轻量内核边界
   - verifier：重跑失败路径和相关测试
4. 修复原则：
   - 优先修复配置、preflight、错误提示、路径发现、schema 校验
   - 不引入数据库服务、私有平台模块或大型新依赖
   - 不用 silent fallback 掩盖错误
   - 不把真实 key、挂载盘绝对私密路径、私有数据样本写入 tracked 文件
5. 修复后继续从失败阶段重跑。

长任务诊断规则：
- 如果 Web RD job 超过 10 分钟仍无终态，先查询 job 状态、run directory、`run.json`
  和 `trace.jsonl` 最新 phase；不要直接判断为失败。
- 如果进程 CPU 低且 trace 停止更新，优先怀疑 LLM provider/network/self-review 等等待点；
  LLM review 超时应允许快速记录为 `llm_self_review_error` 并继续，不应拖垮整条 RD 链。
- 如果进程 CPU 高且 trace 停止更新，优先采样定位本地算子/回测热点，例如
  `ts_rank`、`decay_linear`、`correlation`、`covariance` 或 factor-value cache miss。
- 对本地算子性能修复，必须增加 focused tests，例如
  `tests/test_signal_processing.py` 中的 `ts_rank` / `decay_linear` 语义与性能敏感路径。
- 对 LLM 网络韧性修复，必须增加 focused tests，例如
  `tests/test_llm_client.py` 和 RD review timeout 行为测试。

阶段 6：最终验收报告
完成后必须给出完整报告，包含：

1. 环境信息
   - Docker image
   - Python 版本
   - 项目 commit
   - 启动命令
   - 前端 URL
   - 使用的配置文件路径，隐藏 secret 和私有绝对路径
   - 前端交互方式：`computeruse`、`chrome_playwright_fallback` 或 `api_only`
   - `fallback_used` 和降级原因
   - `chrome_app_used`
   - `api_only_steps`

2. 配置结果
   - data_root 是否可读
   - factor_root 是否可读
   - factor_values_root 是否可读写或只读
   - factor_values_overlay_root 是否可写
   - artifact/result 路径是否可写
   - DeepSeek env 是否成功继承
   - 是否存在任何 fallback

3. 三个因子解析与回测结果
   - seed 描述
   - formula
   - 校验结果
   - 计算结果路径
   - 评价指标
   - IC series / naive t-stat / HAC t-stat
   - 常量或近常量 IC 的 `DEGENERATE_IC_SERIES` 诊断
   - IS/OOS 表现
   - 成本后表现、daily NAV 回撤、reportable/extrapolated 年化
   - initial build turnover、rebalance turnover、replacement rate

4. RD 研究结果
   - 每轮 hypothesis
   - 每轮 candidate
   - 去重/多样性检查
   - 最佳 RD 因子
   - 是否进入推荐列表
   - RD trace/report 路径

5. 遇到的问题
   - 问题编号
   - 复现步骤
   - 根因
   - 修复方案
   - 修改文件
   - 验证方式
   - 是否仍有残留风险

6. 最终结论
   - 新用户是否可以按 README 完成全流程
   - 哪些步骤仍需要人工理解
   - 是否需要更新 README/configuration/integration_workflow
   - 是否建议合并本次修复
```

## 维护规则

- 本文件是“全量联调”默认 prompt 源。后续如果 Web 参数、RD 流程、算子库、缓存策略、报告格式变化，应优先更新这里。
- 如果某次联调发现 README、configuration 或 integration_workflow 与实际不一致，修复代码之外也要更新对应文档。
- 如果本文件需要引用本机挂载盘路径，只写占位符或 Docker 内容器路径示例，不写个人绝对路径。
