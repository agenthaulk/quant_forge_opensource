# Quant Forge — 全量联调测试规范 / Full Integration Test Specification

当用户说“项目全量联调”“新用户 Docker 联调”“从 main 拉取并完整跑通”“full
integration”“new-user Docker integration”“project-wide 联调”，或要求验证某个分支
是否可被全新用户跑通时，默认采用本文件作为执行规范（canonical execution
spec）。

本规范与具体 agent 无关（agent-agnostic）：Claude、GPT/Codex，或任何具备 shell +
真实浏览器驱动能力的编码 agent 都应能据此执行。文中不再出现任何“Codex 专用”措辞，
凡涉及能力的地方一律用能力/机制语言描述（capability / mechanism），各 agent 按
附录 B 的机制映射对号入座。

本文件是“全量联调”的维护源（maintenance source）：Web 参数、RD 流程、信息架构
（IA）、算子库、缓存策略、报告格式、多因子模块发生任何变化时，都应优先更新这里。
`AGENTS.md` 指向本文件，请勿改变它的维护源角色。

---

## 0. 第一性原理 / First principles

这个测试存在的唯一理由：**在没有任何上下文、没有人工 debug 的前提下，验证一个全新用户
能否真正用起这套研究平台，并且平台报出的每一个数字都诚实。** 它强制以下五条不变量
（invariants），每条都直接对应一条验收判据（acceptance）。

- **P1 真实用户保真 / real-user fidelity。** 用真实浏览器（real browser）逐项点击完成
  流程；API 只能用于交叉核对（cross-check），**永远不能作为前端验收依据**。api-only
  的步骤 = 该前端环节 INCONCLUSIVE（未完成真实联调）。
- **P2 诚实研究 / honest research（FP-4 / FP-7）。** 每个数字都带 validity：缺失就是
  `n/a` + 明确 status 标签，**绝不回填 0**；不用 silent fallback 掩盖错误；无前视
  （no-lookahead）与先验（a-priori）性质必须显式呈现。
- **P3 全新可复现 / reproducible from zero。** 从零上下文、从干净容器、只用全新用户的
  本地配置（fresh-user config）起步，不复用当前工作区源码。
- **P4 边界安全 / boundary safety。** secrets 只从环境变量注入；任何 secret 不得进入
  tracked 文件、日志、artifact 或报告；仅研究口径（research-only，无 broker / 实盘下单），
  不引入数据库或重依赖。
- **P5 发现纪律 / discipline of findings。** BLOCKING 问题 → 修复并**从失败阶段继续**；
  非阻塞问题 → 登记后批量修复；**绝不停在单元测试就宣布通过**。

---

## 1. 触发与源模式 / Trigger and source modes

触发短语见开头。进入后先确定“测什么版本”，有两种源模式：

- **已发布检查 / released check：** 从 GitHub 干净 clone `main` 分支到全新临时目录。
- **合并前分支检查 / pre-merge branch check：** 用 `git archive` 把分支 tip 导出到一个
  干净的 build 目录（clean build dir），**绝不复用当前工作区 checkout**。这是一个尚未
  合并的分支（例如某个前端/功能分支）获得真实“新用户测试”的方式：

  ```bash
  # 在源仓库里，把待测分支的树导出为一个干净构建目录（示例占位路径）
  git archive --format=tar <branch-tip> | (mkdir -p /tmp/qf-fresh && tar -x -C /tmp/qf-fresh)
  # 之后所有安装、配置、启动都在 /tmp/qf-fresh 里进行，不碰源 checkout
  ```

记录：源模式、clone URL 或分支名、commit hash、导出/构建目录。

---

## 2. Agent 能力与浏览器驱动阶梯 / Capability and browser-driver ladder

**必备能力：** ① shell；② 驱动一个**真实 Chrome**；③（可选）派生 sub-agents。

**阶梯，取“当前可用的最高级”/ pick the HIGHEST available：**

- **L1 真实桌面 Chrome（PREFERRED）。** 用 Computer Use / 桌面视觉自动化
  （desktop vision-automation）操作桌面上已安装的 Chrome app，通过鼠标、键盘、视觉反馈
  逐项完成。
- **L2 真实 Chrome 程序化控制（fallback）。** 用 CDP / Playwright / Puppeteer 以
  `channel:"chrome"`、`headless:false` 启动**本机真实 Chrome**；响应式验证用 CDP
  `Emulation.setDeviceMetricsOverride` 设置布局视口。降级时必须记录 `fallback_used=true`
  与 `fallback_reason`。
- **L3 API / HTTP —— 不是验收（NOT acceptance）。** 只用于核对 JSON contract、artifact
  路径、后端日期窗口、RD trace、错误细节。任何只能靠 API 完成的环节，其前端结论标记为
  INCONCLUSIVE。

**禁止的捷径 / forbidden shortcuts：**

- 不得用 `/api/jobs/*` 调用替代点击动作（解析、验证并评测、RD 运行、合成回测）。
- 不得把某个“内置于 agent 的嵌入式浏览器”（in-agent embedded browser）称作
  “真实 Chrome”或“Computer Use”。
- 不得把 api-only 的一遍称作“前端联调通过”。

**报告必须写明：** `frontend_interaction.mode`（`computeruse` |
`chrome_automation_fallback` | `api_only`）、`fallback_used`、`fallback_reason`、
`chrome_mechanism`（具体机制，如 computer-use / CDP-Playwright-channel-chrome）、
`api_only_steps`。

---

## 3. 环境搭建 / Fresh new-user environment

- **隔离 / isolation。** 不复用当前工作区 checkout；建一个干净的临时/构建目录；优先在
  全新容器里完成安装、配置、启动、前端使用与 RD 联调。
- **容器基线 / container baseline。** 使用仓库参考 `Dockerfile`（`python:3.12-slim` +
  `constraints.txt`，与 README 安装章节一致）；若自定义容器，保持同一基线。镜像缺失、
  依赖缺失、系统包缺失等摩擦，**作为新用户 findings 记录**，不要绕过 env 缺口去自动改
  代码（do NOT auto-patch code around env gaps）。
- **secrets / 数据路径。** DeepSeek/LLM key 只从宿主环境变量或 ignored 本地 env 文件
  注入（`-e DEEPSEEK_API_KEY` 透传，或 `--env-file <ignored-file>`）；配置文件里只写
  环境变量**名字**（例如 `DEEPSEEK_API_KEY`），不写真实值；`data_root`、`factor_root`、
  `factor_values_root`、`artifact_root` 等路径只写进 local/ignored 配置
  （例如 gitignored 的 `configs/default.local.yaml`）或运行时 env，不写进公开配置
  （`configs/default.yaml`）。若没有 key，就诚实地测“无 key 失败路径”的用户体验
  （UX）：优雅报错 = pass，同时把这个 gap 记录下来。
- **绑定与控制令牌 / bind + control token。** 绑定 loopback（`127.0.0.1`）；每次运行一个
  控制令牌写入 scratch；若运行环境支持，可预置到浏览器 `sessionStorage`，否则在页面
  提示处粘贴。
- **UI 之前的 preflight 门 / preflight gates before UI（全绿才进前端）：**

  ```bash
  qf doctor --config configs/default.local.yaml --rd-config configs/rd.yaml
  qf llm-smoke --config configs/default.local.yaml --provider deepseek
  python3 -m pytest
  PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
  ```

  `qf doctor` 必须显示 DeepSeek runtime-ready；`qf llm-smoke` 必须完成一次真实自然语言
  解析（one real parse），且**不打印/不记录真实 key**。参数以 `qf doctor --help` /
  `qf llm-smoke --help` 为准。

启动 Web：`qf web --config configs/default.local.yaml`（或 `python3 -m
quant_forge.apps.cli.main web ...`）。确认服务绑定到可访问的 loopback 端口（默认
`http://127.0.0.1:8765/`，冲突时换端口并记录）。检查启动日志：配置已加载、DeepSeek
provider 已识别、key env 已继承但真实 key 未打印、各数据/因子/值/overlay/artifact 路径
存在且可读写。

---

## 4. 前端联调流程 / Frontend integration flow（验收核心 / acceptance core）

用**真实 Chrome**，按**当前 Phase D 的信息架构（IA）**逐项走。每一步保留截图或页面状态
摘要（screenshots / page-state）作为真实前端操作证据。

**当前 IA：7 个顶层页签（tabs）** —— 「LLM 因子工作台」（内含两个模块：单因子研究 /
多因子策略回测）·「研究历史」·「数据」·「注册表」·「文档」·「扩展」·「记忆治理」。
顶层 tab id 依次为 `lab-tab-factor` / `lab-tab-history` / `lab-tab-data` /
`lab-tab-registry` / `lab-tab-docs` / `lab-tab-extensions` / `lab-tab-memory`；
工作台内两个模块 id 为 `lab-module-single`（单因子研究）与
`lab-module-multi`（多因子策略回测）。

### 4.0 Shell 与 IA / shell and IA

- 6-tab 页签条 + 工作台内两模块导航（module nav）正确渲染。
- 旧的深链（legacy deep links）能迁移、不出现死链（no dead-ends）：`#lab-tab-rd` →
  工作台的 RD 阶段（`#workbench-rd`）；`#lab-tab-bench` → 报告内的对比区
  （`#report-comparison`）。`#lab-module-multi` 直接进入多因子模块。

### 4.1 Provider 与 key 控件 / provider and key control

- LLM Provider 选择（`#llm-provider`）；解析方式 `#parser`（LLM 语义解析 / 本地规则解析）。
- LLM API Key 控件（`#llm-api-key-mode`）：
  - “配置文件 / 环境变量加载”模式 → 输入框 `#llm-api-key` 置灰（disabled），**不显示
    真实 key**；
  - “手动输入（仅前端联调）”模式 → 输入框才可编辑；手动输入内容**仅前端使用、不持久化**，
    绝不写入配置、日志、artifact 或报告（`data-secret-policy="not-submitted"`）。
- 控制令牌门控 UX（token-gating）：需要令牌时页面提示明确，粘贴/预置后功能解锁。

### 4.2 单因子研究 / single-factor module（真实 DeepSeek）

切到「单因子研究」模块。用三个 seed（**逐字**，见附录 C）各跑一遍。

对每个 seed：在 `#idea` 输入自然语言 → 点「解析因子」（`#run`，真实 LLM 调用）→ 服务端把
这次解析包成一个**管线 A（factor_study）**，管线卡（`#pipeline-card-mount`）打开**假设确认
闸门**，其四段阶段条 **解析 → 假设确认 → 计算 → 报告**（stage_id：`parse` / `confirm` /
`compute` / `report`；旧的 `.lab-stepper` 五步条已在 P1 删除，由管线卡取代）依次推进。解析
阶段**只生成 factor 草稿 + 待确认参数（draft + pending params），不得提前触发评价/回测**。

- **诚实性检查（HONESTY）：** 在“本地规则解析”模式下输入乱码/无意义文本，报告顶部必须
  出现 fallback 警告卡（warn notice，`renderParseWarnings`）——fallback 解析不能伪装成
  自信解析。
- **调整 11 个评测参数（在管线确认卡的**专家密度**网格 `#pipeline-expert-params` 内，输入项
  `data-pipeline-param-field="…"`；旧的常驻 `#validation-controls` 网格已在 P1 删除并吸收进
  确认卡）：** 持有期/天（`data-pipeline-param-field="holding_days"`）、
  Decay/天、Top Quantile、Delay/天、评测开始、评测结束、回测开始、回测结束、手续费 bps、
  滑点 bps、融券成本 bps/年。切到「专家」密度后逐项修改，负面证据（`INSUFFICIENT_*` 等）
  两档密度均可见。
- 点「验证并评测」（`#validate-run`），因子报告应包含：
  - **诚实 MetricValue 状态：** 缺失/不足样本显示 `insufficient_sample` / `n/a` 等
    status 标签，**绝不是裸 0**（`metric.js` 单一渲染器纪律）。
  - **新版图表（inline SVG，`charts.js`）：** 首月逐日建仓稳健性回测里的 staggered NAV
    vs 基准折线（strategy net NAV vs cash benchmark）与相对净值折线；分位桶平均收益柱状
    （分组收益）；按 horizon 的 Rank IC 柱状（多周期 Rank IC）+ 按 split 的 ICIR 柱状
    （三段验证 ICIR）+ 回测分段净年化柱状。缺失点是路径 gap 或 `n/a` tick，**不是 0**。
  - DSL 高亮公式（`dsl.js`，token 高亮）。
  - 三段：样本内研究评价（`#report-evaluation`）、样本内组合回测（`#report-insample`）、
    外部样本外组合评测（`#report-oos`）即 IS / OOS1 / OOS2。
  - HAC t-stat；常量/近常量 IC 序列输出 `DEGENERATE_IC_SERIES` + `null` t-stat，**不显示
    极端 t 值**。
  - turnover（initial build turnover、rebalance turnover、replacement rate）、成本后表现
    （net cumulative / net annualized）、artifact 路径（`#report-artifacts`）。
- **报告后续动作栏（`#report-followups`，P3 / R3.1）：** 报告是管线 A 的**终点**，报告下方
  出现后续动作栏，只含入口按钮、**没有 A→B 自动桥**：
  - 「首月逐日建仓稳健性回测」（`#staggered-run`）——R3.1 已从 01 Parse 控制栏**迁移**到这里；
    点击后确认 `#report-staggered` 的 NAV 图与 cohort 明细。
  - 「编辑并预验证公式」（`#formula-edit`）——打开专家可编辑公式卡（见 §4.2.1）。
  - 「开始 RD 优化（管线 B）」（`#rd-entry`）——显式发起管线 B（见 §4.2.1）。

**每个 seed 记录（carry over 老规范的完整字段清单）：**

- 输入的自然语言；DeepSeek 返回的结构化结果。
- 生成的 `factor_id`、`formula`、`operators`。
- 是否通过公式校验；是否命中已有因子值缓存；是否使用挂载盘数据计算。
- 前端提交的评价/验证日期范围，以及后端 `evaluation.simulation_profile` 实际日期范围。
- 前端提交的持仓回测日期范围，以及后端 `backtest.simulation_profile` 实际日期范围。
- 解析按钮与验证按钮是否按预期拆分；解析阶段是否没有提前触发评价/回测。
- LLM API Key 控件状态：配置加载置灰、手动联调才可编辑、真实 key 不展示/不提交。
- 收益、Sharpe、回撤、调仓率、真实换手率、成本后表现。
- HAC t-stat、coverage lineage、metric `status`/`method`/`N`。
- reportable annualization 与 extrapolated annualization。
- daily NAV 最大回撤；initial build turnover 与 rebalance turnover。
- IS / OOS1 / OOS2 表现；报告与 artifact 路径。

**RD 循环（`#workbench-rd`，右栏「02 Research」控制）：** 把上面 3 个因子作为 RD seed
（`#rd-seed`）。设置目标优先级（`#rd-objective`）、候选数量（`#rd-max`，注意是每轮候选
数，不是轮数）、RD 迭代次数（`#rd-iterations`，上限 `MAX_RD_ITERATIONS`=5）。点「运行
一次」（`#rd-run`）。**R3.1（owner 裁决）：rd-interval 自动周期 select 与 开启/停止
定时循环控件已删除**——RD 周期/样本区间继承因子评测设置，无独立 interval 参数；页面上
不应再出现 `#rd-interval` / `#rd-start` / `#rd-stop`（CLI `research run-once` 与调度端点
保留，仅前端自动周期 UI 删除）。

- **迭代档位：** `iterations=1` 是快速 smoke；`2` / `3` 是标准新用户递进联调；`5` 是重型
  回归模式，只在需验证长链 RD 时用，运行前把候选数量控制在较小值，并预期真实 DeepSeek +
  全量数据耗时很长。
- accepted vs fallback 标注：`selection_reason=fallback_best_score` 且
  `next_exploration_seed_gate_passed=false` 时，前端与报告必须说明该候选**仅用于下一轮
  探索，不构成最终推荐**。
- partial_result 渲染：后续轮失败但前序轮已完成时，前端仍展示已完成轮次、候选与对比表，
  不留空白。

**每轮 RD 记录：** hypothesis；修改后的公式或参数；formula fingerprint；result signature；
是否重复候选；是否通过候选多样性检查；是否通过公式/算子校验；是否需要 draft operator
（若需要，须放入 draft 算子库并标记待审计，不得直接进正式库）；因子评价结果；
accept/reject reason。

**多轮递进 RD 记录：** `requested_iterations`、`iteration_count`、
`original_seed_factor_id`、每轮 `selected_next_seed_factor_id`、每轮 `selection_reason`
（`accepted_candidate` / `fallback_best_score` / `no_candidates` / `no_new_seed`）、
`recommended_factor_id` / `final_factor_id`（只能来自通过 gate 的候选，否则保留原始
seed）、`last_accepted_factor_id`、`last_explored_factor_id`、
`next_exploration_seed_factor_id` / `next_exploration_seed_reason` /
`next_exploration_seed_gate_passed`、`stopped_reason`；失败时 `partial_result=true`、
`failed_round_index`、`chain_error`。

**非法公式路径：** LLM 生成非法公式时，把 validation error 回传 LLM 修复，连续 3 次失败
才进 fallback；参数搜索只能作为独立 research lane，不得把非法公式当作参数搜索成功；若
既没产生公式变体也没产生参数变体，必须输出 `no_optimization_performed`，不能算 RD 成功。

### 4.2.1 可编辑公式预验证 + 管线 B（rd_optimize，P3）

报告后续动作栏（§4.2 `#report-followups`）承载两条 P3 专家路径；两者都从**已完成的
报告**（管线 A 终点）出发，**没有 A→B 自动桥**——只有点击这些入口按钮才会发生。

**可编辑公式卡（`#formula-edit` → `#formula-card-mount`，spec §5.3）：**

- 点「编辑并预验证公式」打开公式卡：一个 `<textarea>`（`#formula-input`，唯一事实源）+
  一个 `aria-hidden` 高亮 overlay（`#formula-overlay`，复用 `views/dsl.js` 规范高亮器，
  **不自绘公式**）。拒绝 `contenteditable`。
- **IME 联调（必测）：** 用中文输入法在 textarea 里输入（组合态），确认 overlay 在
  **组合期不重绘**（不打断候选窗/光标），组合结束后才刷新高亮。
- 点「预验证公式」→ `POST /api/pipelines/pre-validate`（canonicalize + ValidationGate，
  **不落盘、不评测、不回测**）。确认返回：
  - 可解析公式 → `status=ready` + canonical `fingerprint`，且 `executed=false` /
    `persisted=false`。
  - 未知算子 → `status=review_required` + `review_packet.channel=operator_drafts` +
    `hot_executed=false`——**只生成算子审阅包，绝不热执行、绝不落盘**（对照
    `POST /api/jobs/validate-idea` 会跑完整评测链）。

**管线 B（`#rd-entry`，rd_optimize，spec §2.1/§5.4）：**

- 点「开始 RD 优化（管线 B）」→ `POST /api/pipelines`（`kind=rd_optimize`，
  `seed_factor_id` 来自本报告因子）。管线卡（`#pipeline-card-mount`）打开 **RD 确认闸门**：
  三段阶段条 **RD 确认 → RD 运行 → 排行榜**（`confirm`/`run`/`leaderboard`）。
- **RD 确认卡：** 迭代轮数（`rounds`，1..`MAX_RD_ITERATIONS`=5，**服务端校验越界**）、
  每轮候选数（`candidates_per_round`）、目标优先级（`objective`）+ 成本预告 +
  **fixed_policy 披露**（周期/样本区间继承因子评测设置，R3.1：无独立 interval 参数）。
  每个值带服务端派生的 provenance 徽章（经 `views/provenance.js` 唯一渲染器）。
- 点「确认并运行 RD」→ 单个后台 research 任务跑 N 轮（**不拆每轮灯**），终态到达
  `leaderboard`。管线卡显示「排行榜已生成」（终点，无闸门按钮）。
- **排行榜（复用 `views/research.js` 渲染器，渲染进 `#rd-result`）：** external-OOS 列标注
  **「审计」**（只审计、不参与 winner 选择）；**去重处置**按 executed / reused / skipped
  披露（服务端权威计数：`candidates` / `deduplication.result_duplicates` /
  `formula_skipped+diversity_skipped`）。状态词指标（`insufficient_sample` 等）走
  `metric.js` 唯一渲染器 → `n/a`/status，**绝不被当作数字进入比较**。
- **记录：** `pipeline_id`、`kind=rd_optimize`、三段 stages、`rounds`/`candidates_per_round`/
  `objective` 及其徽章来源、`planning_influence_hash`（当前保留为空——SE 真实捕获在
  CP-INT 接线，input_hash 稳定）、排行榜的审计列与去重处置。

### 4.3 多因子策略回测 / multi-factor strategy backtest（NEW）—— 合成

切到「多因子策略回测」模块（`#lab-module-multi` / `#multi-result`）。

- **因子选择（`#synth-factors`）：** 勾选 ≥ 2 个因子；每个因子显式设定方向
  （`.synth-direction`，`+1 按定义使用` / `-1 反向使用`），无静默符号翻转。
- **合成方法（`#synth-method`）：** 从方法目录选择——`equal_weight` / `weighted`（先验，
  `is_fitted:false`）与 `ic_weighted` / `icir_weighted`（拟合，`is_fitted:true`，PIT
  embargo）四个可用（P6 后的 §9 终态；P6 前拟合两项渲染为 disabled 预留）。
- **动态、schema 驱动的参数表单（`#synth-params`）：** 表单纯由所选方法的 `params[]`
  ParamSpec 渲染，无任何按方法硬编码：
  - `weighted` → 为**每个已勾选因子**生成一个权重输入框；勾选集合变化时表单随之更新。
  - `ic_weighted` / `icir_weighted` → `ic_min_periods` 整数参数（默认 6，范围 3–60）。
- **必填 holding_days 守卫：** `#synth-param-holding-days` 为必填（预填 5 仅为建议值）。
  未满足运行前置条件时「合成并回测」按钮（`#synth-run`）disabled，且 `#synth-run-hint`
  实时说明原因（已选因子数不足 2 / 方法目录不可用 / 无可用方法 / job 运行中）——按钮
  从不无理由置灰。
- 点「合成并回测」（`#synth-run`）→ 组合报告（`#synth-report`）应包含：
  - evaluation（**同窗诊断**，`meta.basis = same_window_diagnostics`——本模块 backtest-only，
    无独立评价区间）+ 外部样本外回测段；`in_sample_backtest` 为 null 且渲染安全。
  - **合成 provenance 卡（`#synth-provenance`）：** 先验方法 RAW 声明权重**不做归一化展示**
    （`weights_effective` 原样回显）；拟合方法**没有** `weights_effective`，改为
    `fitted_weights_latest` / `fitted_weights_path` / `fitted_period_fraction` /
    `warmup_period_count`；成员条目携带**钉定公式**（`factors[].formula`）。
  - **validity 横幅（`#synth-validity`）：** 徽标按权重制度如实分支——先验 =「先验声明」、
    拟合 =「拟合权重（时变）」；caveats 含 RB-1 非重叠/相位敏感警示与同窗诊断说明。
  - **覆盖表（`coverage_by_role`）：** 单一 `external_oos_backtest` 角色一张表；
    `coverage_ratio` 为 null → `n/a`；complete-case 剔除以 caveat 呈现
    （“missing values are never imputed / 缺失从不填补”）。
  - **诚实性检查（HONESTY）：** 全程**不出现任何 optimizer 措辞**（no optimizer /
    covariance / risk model language anywhere）；短窗拟合应诚实降级
    （`WARM_UP_IC_UNFITTED` / `NO_FITTED_PERIODS`，`is_fitted` 如实回落 false）。
- **记录：** 参与因子 + 方向；方法 + 参数；composite `COMPOSITE_` id（`composite_id`，
  全输入哈希含 `holding_days`）；覆盖；validity（`is_fitted`、basis、caveats）；**动态
  表单是否与所选方法的 schema 匹配**（例如 weighted 是否恰好每因子一个权重框、拟合方法
  是否出现 `ic_min_periods`）。

### 4.4 只读面 / read-only surfaces

- 「数据」（`#lab-panel-data`）：字段目录（catalog）、覆盖范围（coverage）、质量门
  （quality gate）。
- 「注册表」（`#lab-panel-registry`）：因子列表 + 详情 + 证据链（evidence chain）。
- 「文档」（`#lab-panel-docs`）：docs 目录 + 渲染后的 markdown（应能看到本文件与
  `frontend_contributing.md`）。
- 「扩展」（`#lab-panel-extensions`）：扩展 manifest 清单 + 校验状态（validation status）。

### 4.5 横切验证 / cross-cutting

- **深链 reload 存活：** `#registry-factor-<id>`、`#docs-doc-<relpath>`、`#lab-module-multi`
  在刷新后仍定位到正确 tab/模块/条目（`applyHash` + 各视图 hash 前缀）。
- **控制台零错误：** 遍历所有 tab 与流程，浏览器 console **零 error**。
- **暗色模式 + 375px 窄屏：** 用 CDP device metrics（`Emulation.setDeviceMetricsOverride`）
  设置布局视口与 `prefers-color-scheme: dark`。**不要用旧式 `--window-size`**——它不设置
  布局视口（layout viewport），验不出真正的响应式断点。

### 4.6 记忆治理页 / research-memory review tab

「记忆治理」（`#lab-tab-memory` → `#lab-panel-memory`）是研究记忆的人工复核面：

- **激活方式：** 点击 tab 或深链 hash 激活；面板内容渲染进 `#memory-result`。
  已知缺口（登记在案）：方向键 roving-tabindex 循环暂不包含该 tab，用原生
  Tab/Shift+Tab 或点击可达——按已知问题记录，不作为新发现。
- **读面：** 应展示晋升的 findings / failures 列表、retired 状态与规则
  （rule）状态，数据来自 `GET /api/memory/review` 的单次锁内快照；尚无记忆
  数据时应显示空态而不是报错。
- **治理动作：** activate / deactivate / retire 等动作必须要求 actor
  （`#mem-actor` 输入框），空 actor 应被拒绝；提交后事件只追加、可在列表中
  看到状态变化。非字符串字段（null/数字）应得到 400，不得把 `"None"` 落成
  reviewer 身份。
- **诚实展示：** 计数与比率区分 `passed`/`blocked`（进分母）与
  `unknown`/`not_applicable`（只计数）；schema 校验失败的行以
  `invalid_rows` 显式呈现，不得静默按 0 处理（后端口径见
  `qf memory priors --json`）。

---

## 5. 后端 / API 交叉核对 / Backend cross-check（supporting, NOT acceptance）

只做核对，不作前端验收依据。

- **令牌门控：** 需要控制令牌的 `/api/*` 无令牌返回 `401`（JSON `{"error":"unauthorized"}`），
  带令牌返回 `200`。
- **路由分流：** 未知 `/api/*` → `404` JSON（`{"error":"unknown API path: ..."}`）；非
  `/api` 路径 → 返回 index shell（深链与拼写错误都落到首页外壳）。
- **payload 卫生（hygiene）：** 任何 payload 都**不得含绝对路径**；尤其
  `GET /api/data/status` 不得泄漏 `data_root` / `panel_path`（artifact 字段应为 basename）。
- **合成端点形状：**
  - `GET /api/synthesis/methods` 返回 `{methods, standardizations}`：4 个方法
    （`equal_weight` / `weighted` 先验 + `ic_weighted` / `icir_weighted` 拟合，P6 终态全部
    `available:true`；拟合两项 `is_fitted:true` 且带 `ic_min_periods` ParamSpec）+ 2 个
    标准化器（`zscore` / `rank`）。请求体的标准化字段是 `standardization.method`。
  - `POST /api/jobs/multi-factor-backtest` 能完成（compute-heavy，耐心轮询；**终态
    status 是 `completed`**，另有 `failed` / `cancelled`）；结果 payload 携带
    `synthesis_provenance{coverage_by_role, factors[].formula 钉定公式}` 与 `validity`；
    契约违约（<2 因子 / 缺 holding_days / 未知方法 / 窗口过短 / 宇宙冲突）应是**同步的
    干净 4xx JSON**，不是失败的后台 job。
- **P3 端点形状（管线 B + 预验证，routing.py，api.py 零改动）：**
  - `POST /api/pipelines/pre-validate`（`{formula}`）→ 读-only：可解析公式 `status=ready`
    + `fingerprint`；未知算子 `status=review_required` + `review_packet.channel=operator_drafts`
    + `hot_executed=false`；全程 `executed=false` / `persisted=false`，**不评测不落盘**。
  - `POST /api/pipelines`（`kind=rd_optimize`, `seed_factor_id`, `rounds`,
    `candidates_per_round`, `objective`）→ 三段 `confirm`/`run`/`leaderboard`、
    `planning_influence_hash=""`；**越界 `rounds` 同步 4xx**（服务端校验，不是失败 job）。
    `/{id}/confirm` 启动单个 research 任务，终态 `leaderboard`；管线 A 跑完**不会**自动
    创建任何 rd_optimize 管线（无 A→B 桥）。
- **禁止用 API 替代点击：** 不得用 `/api/jobs/parse-idea` 替代「解析因子」、
  `/api/jobs/validate-idea` 替代「验证并评测」、`/api/jobs/research-run-once` 替代 RD
  「运行一次」、`/api/jobs/multi-factor-backtest` 替代「合成并回测」、
  `/api/pipelines/pre-validate` 替代「编辑并预验证公式」、`POST /api/pipelines`
  （`rd_optimize`）替代「开始 RD 优化（管线 B）」。

**长任务诊断规则（carry over）：**

- Web job 超过 10 分钟仍无终态：先查 job 状态、run directory、`run.json`、`trace.jsonl`
  最新 phase，**不要直接判失败**。
- 进程 CPU 低且 trace 停止更新：优先怀疑 LLM provider / network / self-review 等待点；
  LLM review 超时应允许快速记为 `llm_self_review_error` 并继续，不拖垮整条 RD 链。
- 进程 CPU 高且 trace 停止更新：采样定位本地算子/回测热点，例如 `ts_rank`、
  `decay_linear`、`correlation`、`covariance`，或 factor-value cache miss。
- 本地算子性能修复须补 focused tests（如 `tests/test_signal_processing.py` 的 `ts_rank` /
  `decay_linear` 语义与性能敏感路径）；LLM 网络韧性修复须补 focused tests（如
  `tests/test_llm_client.py` 与 RD review timeout 行为测试）。

---

## 6. 诚实研究不变量 / Honest-research invariants（first-class；到处都必须成立）

- **FP-4：缺失 → `n/a` + status，绝不 0。** metric 单元、chart gap、coverage ratio 全部
  适用。
- **无 silent fallback。** 规则解析 fallback 出警告卡；非法 LLM 公式进入修复回路
  （repair loop）≤ 3 次，仍失败则 `no_optimization_performed`。
- **无前视 / 先验显式。** 合成按角色做 decay-warmup（per-role decay warmup）；权重是先验
  `is_fitted=false`；complete-case 从不填补（never imputed）；常量 IC → `DEGENERATE_IC_SERIES`
  + `null` t-stat。
- **contract 纪律。** LLM 输出经 JSON-schema 校验；metric 携带 `status`/`method`/`N`。
- **仅研究口径。** 无 broker / 实盘下单；开源合成**没有 optimizer / covariance / risk
  model**（D6 商业边界）。

---

## 7. 发现登记与修复回路 / Findings register and fix loop

**登记格式（register format）：** `id`、`severity`（BLOCKING / MAJOR / MINOR / DOC-GAP）、
`stage`（失败阶段）、`repro`（复现步骤）、`evidence refs`（截图/日志/trace 引用）、
`suspected root-cause`（`file:line`）。

**修复回路：**

- **BLOCKING** → 做最小 source fix，重建/重启，**从失败阶段 CONTINUE**（never stop at
  unit tests）。
- **非阻塞** → 记录后继续，事后批量修（batch-fix，disjoint-scope 分工 + review），修完
  **重跑受影响阶段**。
- **env / 依赖缺口** → 记为新用户摩擦 + 文档改进建议，**不要绕过去改代码**（do NOT code
  around）。

**修复边界（fix boundaries）：** 优先修配置 / preflight / 错误提示 / 路径发现 / schema
校验；不引入数据库、重依赖、私有模块；不用 silent fallback 掩盖错误；不把真实 key、私有
绝对路径、私有数据样本写入 tracked 文件。

---

## 8. 验收基线与最终报告 / Acceptance baseline and final report

**常驻门（standing gate，全绿才算通过）：**

```bash
python3 -m pytest
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
git diff --check
python3 scripts/release_safety_scan.py
```

**最终报告章节：**

1. **环境信息 / environment：** 容器 image、Python 版本、commit、启动命令、前端 URL、
   使用的配置文件路径（隐藏 secret 与私有绝对路径）；`frontend_interaction.mode`、
   `chrome_mechanism`、`fallback_used` / `fallback_reason`、`api_only_steps`。
2. **配置结果 / config results：** 各路径可读/可写状态、DeepSeek env 是否成功继承、是否
   存在任何 fallback。
3. **三个因子解析 + 回测结果 / per-seed parse + backtest：** 用 4.2 的完整字段清单。
4. **RD 结果 / RD results：** 用 4.2 的完整（含递进）字段清单。
5. **多因子合成结果 / multi-factor synthesis results（NEW）：** 因子 + 方向、方法 + 参数、
   composite `MFC_` id、per-role coverage、validity、动态表单与 schema 是否匹配。
6. **遇到的问题 / findings：** 用第 7 节格式。
7. **最终结论 / conclusion：** 新用户能否按 README 完成全流程？哪些步骤仍需人工理解？
   README / configuration / docs 是否需更新？是否建议合并（merge recommendation）？
8. **泄漏检查 / leak check：** 任何 tracked 文件与本报告都不含 secret 或私有绝对路径。

---

## 附录 A / Appendix A —— 可复制执行 Prompt / copy-paste run prompt

以下 prompt 与 agent 无关，任何具备 shell + 真实浏览器驱动的 agent 都可直接复制执行，
覆盖阶段 0–6，已更新到 Phase D 与多因子模块。

```text
你是 Quant Forge 的全流程联调负责人。模拟一个全新用户，在一个干净隔离环境（全新临时
目录，优先全新容器）中，从零完成配置、启动、真实前端使用与 RD/合成联调。你没有任何
既有上下文，只能依据 README.md、docs/configuration.md、docs/integration_workflow.md 和
本规范（docs/full_integration_test_prompt.md）。

目标：验证新用户在完成必要本地配置后，无需人工 debug 即可完成——
1) LLM 自然语言因子解析；2) A 股日频数据上的因子计算；3) 因子回测/评价；
4) RD 模块 2–3 轮研究（思路优化 + 参数搜索）；5) 多因子策略合成与回测；
6) 查看最终因子结果与研究过程。

不变量（invariants，必须全程成立）：
- 用真实浏览器逐项点击完成前端；API 只做交叉核对，绝不作为前端验收依据。
- 每个数字带 validity：缺失 → n/a + status，绝不回填 0；不用 silent fallback。
- 从零上下文、干净容器、只用全新用户本地配置起步，不复用当前工作区源码。
- secrets 只从环境变量注入；不把 key / 私有绝对路径 / 私有数据写入 tracked 文件或报告。
- 仅研究口径（无 broker / 实盘）；不引入数据库或重依赖。
- BLOCKING → 修复并从失败阶段继续；非阻塞 → 登记后批量修；绝不停在单元测试就宣布通过。

源模式：released → 干净 clone main；pre-merge 分支 → git archive 分支 tip 到干净构建目录，
绝不复用当前 checkout。记录源模式、commit、构建目录。

阶段 0 准备与隔离：确认本地路径与 Git 状态（仅记录）；建干净临时目录/容器；取回待测源码；
记录 clone URL 或分支、commit、容器 base、Python 版本、依赖安装命令、启动命令、端口映射。

阶段 1 按 README 配置：安装依赖；创建 ignored 本地配置（如 configs/default.local.yaml），
只写环境变量名不写真实值；配置 data_root / factor_root / factor_values_root /
factor_values_overlay_root / artifact_root、llm provider = deepseek、以及评价/回测/RD 时间窗；
运行 preflight：
  qf doctor --config configs/default.local.yaml --rd-config configs/rd.yaml
  qf llm-smoke --config configs/default.local.yaml --provider deepseek
  python3 -m pytest
  PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
doctor 必须 DeepSeek runtime-ready；llm-smoke 必须完成一次真实解析且不打印真实 key。

阶段 2 启动服务：qf web --config configs/default.local.yaml；确认绑定 loopback 端口
（默认 127.0.0.1:8765），检查日志（配置加载、provider 识别、key env 继承但未打印、各路径
可读写）。端口冲突则换端口并记录。

阶段 3 真实前端联调（按能力阶梯取最高级：L1 真实桌面 Chrome，L2 真实 Chrome 程序化，
L3 API 仅补充非验收）：走 4.0–4.5——6-tab + 两模块 IA；provider 与 key 控件；单因子
三个 seed（附录 C，逐字）解析→评测→RD；多因子模块合成→回测；只读面（数据/注册表/文档/
扩展）；横切（深链 reload、console 零错误、暗色 + 375px via CDP device metrics）。保留截图/
页面状态证据。禁止用 /api/jobs/* 替代点击。

阶段 4 RD 与多因子：RD 至少 iterations=1 + 一个 2/3 递进；记录每轮与递进字段；非法公式
走修复回路≤3，否则 no_optimization_performed。多因子：≥2 因子 + 显式方向，方法/标准化/
动态参数由方法目录驱动，holding_days 必填；验证 RAW 权重、per-role coverage、先验 validity
（is_fitted=false），全程无 optimizer 措辞。

阶段 5 问题处理：立即记录失败位置/操作/日志/trace/报错；BLOCKING 做最小 source fix 后从
失败阶段继续；非阻塞登记后批量修；env/依赖缺口记为新用户摩擦 + 文档建议，不绕过改代码。
长任务：>10 分钟无终态先查 job/run.json/trace.jsonl；低 CPU+停滞 → LLM/network 等待；
高 CPU+停滞 → 采样本地算子/回测热点。

阶段 6 最终报告：environment（含 mode/mechanism/fallback）、config 结果、per-seed 解析+
回测（完整字段）、RD 结果（完整字段）、多因子合成结果（NEW）、findings、conclusion（新
用户能否完成/哪些需人工理解/需更新哪些文档/是否建议合并）+ leak check。
收尾常驻门全绿：pytest；cli --help；git diff --check；scripts/release_safety_scan.py。
```

---

## 附录 B / Appendix B —— 各 agent 机制映射 / per-agent mechanism map

各 agent 按自身能力对号入座，并在报告里**显式写明所用机制**。只有 L3（api-only）的一遍
一律记为 INCONCLUSIVE。

- **Claude：** L1 = Computer Use / claude-in-chrome MCP 驱动桌面真实 Chrome；L2 = 若浏览器
  分类器/Computer Use 不可用，降级为 CDP（Playwright/Puppeteer，`channel:"chrome"`,
  `headless:false`）驱动本机真实 Chrome。Shell 用 Bash；可用 Task / sub-agent 编排派生
  reviewer / debugger / verifier。若既无 L1 也无 L2 → 前端结论 inconclusive。
- **GPT / Codex：** L1 = computer-use 驱动桌面真实 Chrome；L2 = Playwright / CDP，
  `channel:"chrome"`, `headless:false` 驱动本机真实 Chrome。Shell 用自身 shell；用自身
  orchestration 派生子任务。**绝不**把 agent 内置嵌入式浏览器当作真实 Chrome。
- **两者共同：** 显式声明机制（`chrome_mechanism`）；只有 L3 = inconclusive；响应式一律
  用 CDP device metrics，不用旧式 `--window-size`。

---

## 附录 C / Appendix C —— 种子与一个合成配方 / seeds and a synthesis recipe

**三个因子 seed（逐字 / verbatim，覆盖不同类型）：**

1. 低波动小市值：
   “选择市值较小、近期波动较低、过去 20 个交易日收益稳定的股票，构造一个偏低波动小市值的横截面因子。”
2. 短期反转：
   “过去 5 到 20 个交易日跌幅较大，但最近成交活跃度没有显著下降的股票，未来可能存在短期反转机会。”
3. 量价动量：
   “过去 20 个交易日收益较强，同时成交量相较过去 60 日均值有所放大的股票，构造一个量价确认的动量因子。”

**一个多因子合成配方 / a multi-factor recipe（用于 4.3）：**

- 从注册表/已产出的因子里挑 2–3 个（例如上面三个 seed 验证后注册的因子）。
- 方法选 `weighted`，为每个因子填一个权重，例如 `0.5 / 0.3 / 0.2`（方向按定义 `+1`，
  除非要显式反向 `-1`）；标准化选 `zscore`。
- `holding_days = 5`（必填）；其余留空由后端 profile 默认值决定。
- 点「合成并回测」运行。**验证：** provenance 卡里权重原样回显（RAW，未归一化）；单一
  `external_oos_backtest` 覆盖表；先验 validity 徽标「先验声明」、`is_fitted=false`；
  evaluation 段为同窗诊断（`same_window_diagnostics`）；全程无 optimizer 措辞。
- 再切 `ic_weighted` 跑一次（`ic_min_periods` 默认 6，可降到 3 适配短窗）：确认
  `weights_effective` **消失**、出现 `fitted_weights_latest/_path` 与
  `fitted_period_fraction`、validity 徽标变为「拟合权重（时变）」；短窗诚实降级
  （`NO_FITTED_PERIODS` → `is_fitted=false`）也算通过——**如实**是验收标准，不是拟合成功。

---

## 维护规则 / Maintenance

- 本文件是“全量联调”的 canonical source。当 Web 参数、RD 流程、IA、算子库、缓存策略、
  报告格式或**多因子模块**发生任何变化时，优先更新这里。
- 若某次联调发现 README、configuration 或其它 docs 与实际不一致，除修代码外也要更新对应
  文档。
- 本文件引用挂载盘路径时**只写占位符或容器内路径示例**（例如 `<data_root>`、
  `/workspace/...`），**绝不写个人绝对路径**。
