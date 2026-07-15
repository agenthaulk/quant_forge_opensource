# Bug List / 缺陷登记表

> 本项目的缺陷登记与闭环跟踪表（canonical bug registry）。评审（review）或联调
> （integration test）发现缺陷后，**先在此登记**；缺陷修复且**评审通过后**，才把状态
> 改为 `✅ DONE` 并填入修复引用。此文件与 `DECISIONS.md` / `ENGINEERING_PROGRESS.md`
> 同为 `docs/coordination/` 下的协调源。

## 约定 / Convention

- **登记时机**：一旦审查或联调确认一个缺陷，立即加一行，状态 `OPEN`。
- **状态取值**：
  - `OPEN` — 已登记，未开始修
  - `IN-PROGRESS` — 正在修
  - `FIXED (review-pending)` — 代码已改，等评审
  - `✅ DONE` — 修复**且评审通过**（必须填 Fix 引用 + Verified-by）
  - `N/A` — 经确认非代码缺陷（环境/预期行为/工具限制），保留记录不再跟踪
- **闭环规则**：只有 review 无问题后才可标 `✅ DONE`；未过评审一律停在 `FIXED (review-pending)`。
- **ID**：递增 `#NNN`，不复用。

## 缺陷表 / Register

| ID | Sev | Area | Summary | Source | Opened | Status | Fix (commit/PR) | Verified-by |
|---|---|---|---|---|---|---|---|---|
| #001 | MAJOR | synthesis / backtest | 多因子回测（尤其 fitted `ic_weighted`）在全 panel 上内存无界，容器内存紧张时被 **OOMKilled (exit 137)**，job 静默变 failed | CP-INT 自动化 | 2026-07-10 | ✅ DONE | fe87642（单次构建缝+及早释放+文档）→ 332d0c7 T-1/T-3（末位帧滞留、取消检查点）→ 6fdfa9b/0d77bea/0a9d8ac（period_ics 溯源+内容哈希+载荷自证+快照式消费） | terra R1(7项发现)→R2(6/7)→R3/R4 全 CLOSED，T2-FINAL3: PASS 2026-07-11 |
| #002 | MAJOR | integrations / worldquant translator | QF→BRAIN 翻译器**拒绝 `derive/review/none` 置信度字段**（如 `return_5d` 有 derive 公式仍报 `NOT_TRANSLATABLE`）；仅 exact/rename 字段可翻译 ⇒ 多数 cn_a 因子无法翻译 | CP-INT 手工 | 2026-07-10 | ✅ DONE | 本地 worldquant/adapter（按设计不入库）：递归 derive 展开 + T-5/T-6（右侧同优先级括号、逐次审计留痕） | terra R1(7项发现)→R2(6/7)→R3/R4 全 CLOSED，T2-FINAL3: PASS 2026-07-11 |
| #003 | MINOR | synthesis / registry+picker | 注册表/picker 列出临时 `COMPOSITE_*` 合成因子，但其物化值在 per-run overlay（易失）；把它当成员再合成 → `composite frame has no rows to materialize` | CP-INT 自动化 | 2026-07-10 | ✅ DONE | fe87642：注册表 precomputed_values_present 三态（FP-4）+ _prepare 前置拒绝 + 取数后备拒绝 + picker 禁用/registry 徽标 → 332d0c7 T-4（探针 I/O 异常穿透，False 仅限可读确无） | terra R1(7项发现)→R2(6/7)→R3/R4 全 CLOSED，T2-FINAL3: PASS 2026-07-11 |
| #004 | MINOR | integrations / worldquant adapter | BRAIN 适配器包只装代码、不带 `worldquant/mapping/*.yaml`；翻译需 `QF_WQ_MAPPING_DIR`，缺失时降级 `BACKEND_NOT_CONFIGURED` | CP-INT 手工 | 2026-07-10 | ✅ DONE | 本地 adapter：三级解析序 → T-7（存在性分支，空白值诚实报错）；README Mapping data 节 | terra R1(7项发现)→R2(6/7)→R3/R4 全 CLOSED，T2-FINAL3: PASS 2026-07-11 |
| #005 | NIT | factor_engine / value_store | `value_store.py:146` 空/全 NA `pd.concat` 触发 `FutureWarning`（无害但污染日志） | CP-INT 手工 | 2026-07-10 | ✅ DONE | fe87642：_concat_score_frames 空帧剔除（pandas 官方补救类；本地未复现原警告，已如实注记） | terra R1(7项发现)→R2(6/7)→R3/R4 全 CLOSED，T2-FINAL3: PASS 2026-07-11 |
| #006 | MAJOR | rd / research_loop | demo 工作区（documented 新用户路径，160 交易日）Web RD **任一目标**（balanced 与纯 rank_ic 实测）均整体失败 `net_annualized_return is unavailable (insufficient_sample: INSUFFICIENT_ANNUALIZATION_HISTORY)`——年化指标被无条件强制、与所选目标无关；且以 job-failed 裸串呈现而非结构化 RD 结局（no_optimization_performed / stopped_reason） | Phase-F CP-INT 真 Chrome（双目标复现） | 2026-07-11 | ✅ DONE | fe23744：真根因=种子自评估无异常处理（候选侧本有权重门控）；typed metric_unavailable 理由 + seed_unscorable 降级 + None-safe 消费端，run 以既有诚实结局字段完成；零权重组件不再取数 → bfce31f PF-F1 窄化捕获(非指标异常复响)+ PF-F2 报告 n/a 取代合成零 | sol R5 (qf-phasef-solR5-20260714): #006 关闭曲线 PF-F1/F2 → 修复; #007 PF-F3/F4 → 修复+复验(qf-phasef-reverify-20260714b: F1-F3 CLOSED, F4 残余窗口)→ e2aa2f4 终检查点+生产窗口回归, adjudicated CLOSED 2026-07-14 |
| #007 | MAJOR | web / lineage | 纯 Web 用户的验证/回测/稳健性运行**从不写 RunIndex**（`_validate_factor_workflow` 直连 evaluate/backtest，绕过 workbench 记录路径）⇒ 注册表证据链与「研究历史」页对 Web 用户恒空（FP-H 溯源缺口；实测 3 因子验证+2 次稳健性后 runs/index.jsonl 不存在） | Phase-F CP-INT 真 Chrome + API/容器核对 | 2026-07-11 | ✅ DONE | fe23744：workbench 记录段逐字节抽为 lineage/recording.record_run 共享；web 三工作流成功即记录（validate=1 evaluate+2 backtest；staggered=1；multi-factor=1 under COMPOSITE_ id）；5 个幻影 artifact 假缝测试改为守约 → bfce31f PF-F3 记录后置于 payload 成功之后 + PF-F4 completion-wins 边界 → e2aa2f4 全工作流记录前最后一望检查点 | sol R5 (qf-phasef-solR5-20260714): #006 关闭曲线 PF-F1/F2 → 修复; #007 PF-F3/F4 → 修复+复验(qf-phasef-reverify-20260714b: F1-F3 CLOSED, F4 残余窗口)→ e2aa2f4 终检查点+生产窗口回归, adjudicated CLOSED 2026-07-14 |

## 详情 / Detail

### #001 — 多因子 fitted 回测内存无界导致容器 OOM
- **Repro**：内存紧张环境下（本例 4 个容器同挂 cn_a）经 `POST /api/jobs/multi-factor-backtest` 连跑 `weighted` + `ic_weighted`（成员为全宇宙、`ic_min_periods=3`）→ 容器 `OOMKilled=true, ExitCode=137`；释放内存后同样两 job 在新容器上 230s 顺利完成。
- **Root cause (推测)**：fitted IC/ICIR 在全 panel（cn_a 3.19M 行）上的点位时序估计一次性占用，未分块/流式约束；容器 qf web 作为 PID1，进程被杀即容器死。
- **Suggested fix**：对 fitted 计算的内存占用做分块/流式或按日窗切分；`Dockerfile`/README 写明容器最小内存要求；job 失败时区分 OOM 与逻辑失败。

### #002 — 翻译器拒绝 derive 字段，覆盖过窄
- **Repro**：`qf factor submit --target worldquant <用 return_5d 或 volatility_5d 的因子> --json` → `translation.warnings=['NOT_TRANSLATABLE']`, `ok:false`。纯 `close/volume/market_cap→cap/delay/rank` 的因子（如 `rank(close/delay(close,20))`）可翻译。
- **Root cause**：`worldquant/adapter/src/quant_forge_worldquant/translator.py`（文件头注释 line 6-7）对 `review/derive/none` 行与未映射 token 一律 `NOT_TRANSLATABLE`。`qf_to_wq_fields.yaml` 里 `return_5d` 明明有 `derive: ts_sum(returns,5)`。
- **Suggested fix**：翻译时自动套用 `derive.formula`（递归展开），或在 UI/CLI 明示"可翻译字段白名单"，让用户提前知道哪些因子可上 BRAIN。诚实拒绝本身正确，问题是覆盖面。

### #003 — 悬空合成因子（picker 展示但不可再合成）
- **Repro**：注册表按名排序时 `COMPOSITE_*` 在前；选其为多因子成员回测 → job failed `composite frame has no rows to materialize`。
- **Root cause**：合成因子**定义**持久化到 `factor_root`（RF-2 `FactorRepository.save`），但**物化值**落 per-run overlay（容器 artifact 目录，跨运行/跨容器易失）。picker 用 `/api/registry/factors` 全量展示，含这些值已失效的合成因子。
- **Suggested fix**：把合成值物化到持久 overlay 以支持复用；或 picker 过滤/标注不可复用（值缺失）的 `COMPOSITE_*`。

### #004 — 适配器不自带 mapping
- **Repro**：不设 `QF_WQ_MAPPING_DIR` 时翻译 → `translation.warnings=['BACKEND_NOT_CONFIGURED']`, note 提示设置该变量。
- **Root cause**：`worldquant/adapter/pyproject.toml` 仅 `packages.find where=["src"]`，未纳入 `worldquant/mapping/*.yaml`。
- **Suggested fix**：把 mapping 作为 package data 随 adapter 打包，或在 adapter README / 部署文档明写需单独提供 mapping 目录。

### #005 — pandas FutureWarning
- **Repro**：容器日志反复出现 `value_store.py:146 ... concat ... FutureWarning`。
- **Suggested fix**：concat 前排除空/全 NA 列，或按新语义显式指定 dtype。

## 评审记录 / Review trail

### Round 1 — terra (gpt-5.6-terra, xhigh, 2026-07-10, fingerprint qf-phasef-review-20260710)

判词：#005 **ACCEPT**；#001/#003/#002/#004 **REWORK**（7 项 MAJOR，0 BLOCKING；
猎项 no-finding 清单见评审输出：IC 排序奇偶、探针/取分同根构造、注册表探测范围、
前端三态、单存活帧别名、标识符碰撞、间接环旁路均无发现）。

| 发现 | 位置 | 缺陷 | 状态 |
|---|---|---|---|
| T-1 | api.py 取数循环 | 局部变量滞留末位成员整帧，穿透释放点 | 返工中（暂停时 in-flight） |
| T-2 | service.py period_ics | 只验形状不验溯源，外来 IC 可污染 PIT 拟合 | 返工中（PeriodICSweep 指纹绑定方案） |
| T-3 | api.py | 先验路径取消检查点被挪到昂贵扫描之后 | 返工中 |
| T-4 | value_store.has_stored_values | I/O 失败被折叠为 False（误报确证缺失→错误拒绝） | 返工中 |
| T-5 | adapter translator._render | `+`/`*` 右侧同优先级括号被丢弃，浮点求值序改变 | ✅ 已修（121 测试） |
| T-6 | adapter applied_derives | 去重丢失重复代换审计记录 | ✅ 已修（逐次留痕） |
| T-7 | adapter default_mapping_dir | 空白 QF_WQ_MAPPING_DIR 静默穿透到回退 | ✅ 已修（存在性分支+诚实报错） |

闭环规则已履行：T-1..T-4 落地于 332d0c7；T-2 的三层收口（内容哈希 6fdfa9b、载荷自证 0d77bea、快照式消费 0a9d8ac）经 terra R3/R4 聚焦复核，终判 **T2-FINAL3: PASS**（2026-07-11，指纹 qf-phasef-t2final3-20260711a，模型经 rollout 核验）。五行据此翻 ✅ DONE。

### Rounds 2–4 — terra 复核链
- R2（reverify）：T-1/3/4/5/6/7 CLOSED；T-2 NOT-CLOSED（结构指纹不验内容）。
- R3（t2final）：内容哈希确认有效无新病理；发现载荷本身可变未哈希 → 0d77bea。
- R4（t2final2/3）：发现冗余面未验载荷 + 校验-使用竞态 → 0a9d8ac 快照式消费；终判 PASS。

## 观察 / 非缺陷（记录但不作为 OPEN bug）

- **N-1（环境）** BRAIN 账户处于 **Tutorial mode**，`Check Submission`/`Submit Alpha` 被门控置灰；需 owner 亲自 Exit tutorial mode。非代码问题。
- **N-2（预期）** 单因子/合成回测在评价/回测区间**留空（full data）**时，样本内回测与外部 OOS 段数值相同——未做日期切分所致，符合设计。
- **N-3（工具限制）** 375px 布局视口无法用 `resize_window` 真验（需 CDP `setDeviceMetricsOverride`，当前浏览器工具未暴露）；暗色模式已确认。

---

*首次建立 2026-07-10（CP-INT PR#16/#17 联调）。缺陷复现证据见联调报告与本会话记录。*

## 2026-07-15 — CP-INT（双轨联合）联调登记

| # | 位置 | 症状 | 状态 |
| --- | --- | --- | --- |
| U-1 | `apps/cli/main.py` `llm-smoke` | 缺 key 时 RuntimeError 未捕获、裸 traceback（同条件 `doctor` 是结构化 check；无 key 泄漏） | OPEN（合并后修） |
| U-2 | `static/views/provenance.js:28` | 规则解析降级下仍显示「AI 推断」徽标（底层 `agent_inferred` 源值本身诚实；建议改模式中立的「系统推断」） | OPEN（合并后修） |
| U-3 | `constraints.txt` + README | py3.13/3.14 环境按 constraints 安装失败（numpy 1.26.4 / pyarrow 16.1.0 不可构建）；floor-only 安装后全套 1975 通过（pandas 3.0.3） | OPEN（README 注明 py3.12 基线 + floor-only 备选） |

观察（非缺陷）：LLM 无 key 降级前的确认用原生 `window.confirm`——符合 §4.1 契约（先询问、命名 provider/env、不泄 key），体验上可换页内确认框，不列 OPEN。
