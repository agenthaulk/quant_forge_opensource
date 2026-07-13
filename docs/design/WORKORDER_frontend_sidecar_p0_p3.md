# 交给 Fable 的工作单：Agent 副驾前端 P0–P3（简洁模式 + 双管线 + 副驾工具层）

> 权威设计已定稿：**docs/design/agent_sidecar_frontend.md（V1 · R3.1）**，勿重做设计。
> 本单只负责把该规格按 P0→P1→P2→P3 顺序落地。四期共享 `apps/web/html.py` 与
> `static/app.js` 的 file scope，**串行推进、不并行**；每期 = 新功能 + 该期减法项 +
> 测试 pins + 基线 gate + 对抗评审 + owner 批准后 merge。

## 0. 你的角色与工作方式（不可变，沿用多因子工单 §0 全部纪律）
你是 Fable，general manager。亲自做：架构裁决、DECISIONS 记录、阶段分解、模型路由、
对抗评审裁决、gate 归属、owner 检查点。机械开发全部分发，**每个 agent() 必带显式
model（禁止裸调用继承主循环；绝不整批 fable）**：
  - `fable` → 仅限关键架构/裁决/全模块评审，permitted-not-required；
  - `opus` → 契约设计 / 对抗式 verify（可用本仓库 `qf-opus-reviewer` 子代理）；
  - `sonnet` → 机械实现 / 单测 / pins / 修复（可用 `qf-sonnet-dev` 子代理）；
  - `codex`（fresh thread + stale-output 指纹防重放）→ 严格评审 / 深度根因。
**模型路由硬 gate（2026-07-09 实测踩坑）**：fan-out 前自检
`printenv | grep -E 'CLAUDE_CODE_SUBAGENT_MODEL|ANTHROPIC_DEFAULT_(OPUS|SONNET|HAIKU)_MODEL'`
必须为空；dispatch 后 TRUST-BUT-VERIFY 子代理 transcript 的实际 model。
未经 owner 批准不得 merge / push / delete 分支。
**勿动他人 WIP**：`src/quant_forge/operator_registry/{data/core_operators.yaml,resolver.py}`
的未提交改动属暂停中的 bugfix 批次（见 PROJECT_STATUS / memory `bugfix-resume-20260710`），
与本单 file scope 无交集，保持不碰。

## 1. 先读（按 docs/agent_entrypoint.md 的 required read order）
**契约与协调**：AGENTS.md、docs/agent_entrypoint.md、docs/architecture.md、
docs/coordination/DECISIONS.md（尤其 D6/D7/D7a/D8/D9 + 本单的 **D10/D11/D12**）、
docs/coordination/ENGINEERING_PROGRESS.md、docs/WORKING_STATE.md。
**本单权威规格（勿重做设计）**：docs/design/agent_sidecar_frontend.md ——
铁律 FE-L1..L5、双管线契约（§2）、关卡 G1..G4、禁止 FE-X1..X4、组件契约（§5）、
净新增模块（§6）、减法清单（§8）、无 key 降级（§10）、上线门槛（§11）。
**架构背景**：docs/architecture/beginner_expert_workflows.md、
docs/architecture/deterministic_llm_boundary.md、docs/frontend_contributing.md。
**代码基线**：src/quant_forge/apps/web/{html.py,api.py,jobs.py,routing.py}、
static/app.js、static/metric.js、static/views/{lab,factor,research,dsl,charts,tags}.js、
src/quant_forge/specs/{agent_task,run_manifest}.py、
src/quant_forge/research_loop/{contracts,operator_drafts,context_builder}.py、
tests/test_web_static_frontend.py、docs/full_integration_test_prompt.md。

## 2. 第一性原理（全部来自规格，编号即引用）
FE-L1 薄壳不重写：卡片内嵌既有规范渲染器输出，六页签不动，无第二画布。
FE-L2 渲染器唯一出数：副驾/叙述永不自绘指标/图/公式；chat 文本永不是某数字唯一副本。
FE-L3 服务端裁定真相：管线状态、来源徽章、尝试计数、edited_by 全部服务端推导；
      客户端与 agent 声明一律不信（api.py 现回显客户端 parser 元数据的路径不可作徽章依据）。
FE-L4 关卡纵深防御：先契约层拒绝（GateDecision / dry-run submit / 草稿算子 metadata-only），
      UI 只呈现，绝不是唯一强制点。
FE-L5 一进一出：每个新构件先答"取代了谁"；api.py 冻结只准瘦身；每期自带减法项；
      删除与新增同批更新字符串契约 pins（既锁存在也锁不存在）。
诚实粒度：管线阶段 1:1 映射真实执行单元（A 计算段=单 job；B 的 N 轮=单 job），
      后端发阶段/轮次事件前不拆灯。
继承约束：FP-4/FP-5、D6、D7/D7a（无动态加载；agent.workflow 保留）、D8（零构建/零外链）、
      D9（design-parity not stack-parity）全部原样有效。

## 3. 目标（四期串行；分支名即验收单元）
P0 `fable/fe-p0-mode-shell` —— 纯前端：简洁/专家模式壳 + 参数折叠 + 术语词条。
P1 `fable/fe-p1-pipeline-a` —— 管线 A 骨架：服务端 aggregate + 确认卡（双密度 +
   服务端 provenance）+ 幂等 confirm + 刷新重连；**零 LLM 依赖**（rule 解析全链路跑通）。
P2 `fable/fe-p2-sidecar` —— 副驾接入：澄清问答（分级/阻塞/推翻记录）+ typed narration
   AST + 内置类型化工具适配器 + 安全面（loopback token 门禁 / 预算 / 数据-指令隔离）。
P3 `fable/fe-p3-pipeline-b-expert` —— 管线 B（rd_optimize，轮数用户自选 1..MAX_RD_ITERATIONS）
   + 排行榜 + 可编辑公式卡（textarea+overlay）+ 预验证端点 + 对比循环 + RD 计划卡。

## 4. 已裁决决策（不再议；实现必须遵守）
D10（DECISIONS.md）：副驾设计采纳 + FE-L1..L5 + G1..G4 + FE-X1..X4。
D11：双管线拆分（A 终点=报告；B 由用户显式发起、A→B 无自动桥）+ 诚实粒度
     + **R3.1：RD 周期继承因子评测设置，不设任何独立 interval 参数；
     rd-interval 自动周期控件删除（CLI research run-once 保留）**。
D12：反屎山治理——§8 减法清单逐项随替代者落地；api.py 冻结；一面一模块
     （pipeline.js / provenance.js / narration.js 全进 EXPECTED_STATIC_MODULES）；
     provenance.js 为单一渲染器纪律第五席（徽章唯一定义处）。
本单无 CP0 设计工作；开工前只需：WORKING_STATE 认领 lane + §0 模型路由自检。

## 5. 阶段计划（lanes、file scope、减法、pins）

**P0 `fable/fe-p0-mode-shell`（纯前端，零后端改动）**
  范围：apps/web/html.py（简洁模式壳：想法框 + 示例种子 + 开始研究 + 一行运行时状态；
  模式开关；11 参数收进「高级」折叠区；术语 tooltip）、static/app.js（模式切换 +
  优先级：专家深链 > 已存偏好 > 默认简洁；记住选择）、static/views/lab.js（hash 优先级）。
  减法：无（P0 只做可逆纯前端；减法从 P1 起与替代者同批）。
  pins：模式优先级测试；无重复 DOM id；真 375px 零横向溢出；两主题 token 合规；
  既有 test_web_* 全绿（字符串契约不破）。
  验收：无 key 环境下简洁壳 = 种子引导表单可完整走 rule 解析（现有端点，不新增）。

**P1 `fable/fe-p1-pipeline-a`（最大净新增）**
  范围：新建 apps/web/pipeline.py + specs/pipeline.py（aggregate：pipeline_id/kind/
  阶段枚举/合法转移/input_hash/confirm{nonce,version}/attempt/expiry/重连；持久化
  artifact_root/pipelines/ 追加式 journal）、新建 apps/web/provenance.py（7 值逐值
  provenance，自 parse artifact + 指纹推导）、routing.py 挂新路由（api.py 不加行）、
  新建 static/views/pipeline.js（管线 A 状态机 + 卡渲染）+ static/views/provenance.js
  （徽章唯一渲染器）、html.py 挂载点。
  减法（同批）：删 `.lab-stepper`（html.py:535 区段 + 标记）；`#validation-controls`
  常驻网格吸收进确认卡专家密度后删除；控制栏瘦身为想法入口 + runtime strip。
  pins：double-confirm 返回同一 run（nonce 幂等）；刷新/重启重连；确认后卡冻结、
  运行中编辑标注「仅用于下次尝试」；负面证据（INSUFFICIENT_*/synthesized/withheld）
  两档密度均强制可见；徽章缺失即 fail（服务端断言）；快照隔离回归
  （A 失败回滚不得覆盖并行写）。
  验收：**零 LLM key** 下 rule 解析走完 解析→确认→计算→报告 全链路；报告即终点。

**P2 `fable/fe-p2-sidecar`**
  范围：新建 apps/web/narration.py + specs/narration.py（NarrationNode：
  status/question/ref/action_suggestion；message_key；非数值 args；ref 必须解析到
  当前渲染组件）、新建 apps/web/tools.py（allowlist 注册表 v1 封闭目录：读 6 + 动作 5，
  见规格 §5.7；per-run 授权；速率/并发预算；**loopback 亦需 control-token 才可调用
  动作类工具**；bearer 不入模型上下文）、static/views/narration.js（叙述唯一渲染处，
  事件流附着管线卡；375px 抽屉）、澄清问答卡（分级：阻塞级未答不执行；≤3 问带默认；
  跳过=接受默认并记录；后答推翻前答双双入 provenance）、readiness 三态
  unknown/unavailable/ready + 非破坏升降级。
  减法（同批）：app.js 的 parse/validate 散装接线收敛进 pipeline.js；app.js 回归
  「读配置 + 装配 + 路由」。
  pins：narration schema 拒绝数值 args；ref 解析失败即 fail；chat 永不为某数字唯一
  副本（journal+断言）；阻塞级未答不执行；注入语料（想法/因子描述含指令）无法越过
  allowlist；无 token 时动作类工具 401。
  验收：副驾 journal（tool/objective/input refs/request hash/artifact refs/nav target
  落 artifact_root）+ 回放重现同一批渲染卡。

**P3 `fable/fe-p3-pipeline-b-expert`**
  范围：pipeline.py 增 kind=rd_optimize（B：RD确认→RD运行(单job)→排行榜；轮数
  1..MAX_RD_ITERATIONS 服务端校验；周期/样本契约继承评测设置——**不新增任何 RD
  interval 参数**）、RD 确认卡（轮数/每轮候选/objective + 成本预告 + fixed_policy
  披露）、排行榜（复用 research.js 渲染器；external-OOS 列只标「审计」；dedup 处置
  executed/reused/skipped）、可编辑公式卡（textarea 唯一事实源 + aria-hidden 高亮
  overlay，复用 dsl.js；新建 static/views/formula.js 或并入 pipeline.js——一面一模块
  原则裁决后定）、**预验证端点**（canonicalize + ValidationGate，不落盘不评测；
  未知算子 → operator_drafts 评审包 ref）、对比循环（attempt/parent/指纹服务端权威；
  diff 由规范渲染器并排绘制）。
  减法（同批，R3.1 已裁决）：删 rd-interval 自动周期 select + 开启/停止定时循环控件
  及其接线；`#staggered-run` 迁移为报告后续动作。
  pins：A→B 无自动桥（报告只有入口按钮）；轮数越界服务端拒绝；IME composition
  node smoke（组合期不重绘）；未知算子只产评审包绝不执行；attempt 服务端计数
  （失败/取消计入并披露）；状态词指标不参与数值比较。
  验收：满足规格 §11 全部上线门槛；更新 docs/full_integration_test_prompt.md
  覆盖新 UI（AGENTS.md 要求）。

## 6. 验证 gate（每期）
**基线**：`python3 -m pytest`；`PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help`；
`git diff --check`；`python3 scripts/release_safety_scan.py`。
**专项**：该期 pins 全绿 + 减法项的"不存在"断言 + EXPECTED_STATIC_MODULES 同步。
**评审**：每期 merge 前一轮对抗评审（Codex fresh thread + 指纹；或 qf-opus-reviewer），
findings 逐条修复带回归；owner 批准后方可 merge；merge 顺序 P0→P1→P2→P3。

## 7. 硬边界（越界即停并上报）
- D7/D8 不破：无动态加载、无构建步骤、零外部资源、无 npm；agent.workflow 保留 stub。
- 无 MCP server（D3 定版：内置适配器，签名 MCP-shaped 即可）；工具目录 v1 封闭，
  不得自行扩目录；副驾结构性无 promote/submit。
- LLM 永不算数（deterministic_llm_boundary）；数字只经内核 → payload+ref → 规范渲染器。
- api.py 冻结：新端点一律入新模块；api.py 只准减行。
- 凭证/私有路径/claude.ai 会话链接不入任何 tracked 文件；UI 文案 CN-first；
  发布措辞用 source-available（BUSL 边界）。
- 不动 operator_registry 的暂停 WIP；不碰 quant-forge-studio 分支（只读）。

## 8. 期望产出
(1) P0–P3 四个分支按序绿 gate + 对抗评审闭环 + owner 批准 merge；
(2) 每期 ENGINEERING_PROGRESS.md 更新 + WORKING_STATE lane 认领/释放；
(3) P3 末：full_integration_test_prompt.md 更新 + 一次完整 CP-INT 式联调
    （新用户视角：简洁模式一句话→报告→自选 RD→排行榜；专家接管；无 key 降级）；
(4) 规格 §12 未决问题的逐项处置记录（做/延/砍 + 理由，回写 DECISIONS 或 spec）。
