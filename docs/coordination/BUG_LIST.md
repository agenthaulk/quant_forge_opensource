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
| #001 | MAJOR | synthesis / backtest | 多因子回测（尤其 fitted `ic_weighted`）在全 panel 上内存无界，容器内存紧张时被 **OOMKilled (exit 137)**，job 静默变 failed | CP-INT 自动化 | 2026-07-10 | FIXED (review-pending) | phase-f 分支：单次构建共享缝（build_directed_matrix + period_ics 复用 + redundancy_from_period_ics）+ 成员帧及早释放与定点 gc.collect（jobs 关 GC）+ architecture.md 容器内存说明；数值逐位不变（12 项新回归 + 金样全绿） | — |
| #002 | MAJOR | integrations / worldquant translator | QF→BRAIN 翻译器**拒绝 `derive/review/none` 置信度字段**（如 `return_5d` 有 derive 公式仍报 `NOT_TRANSLATABLE`）；仅 exact/rename 字段可翻译 ⇒ 多数 cn_a 因子无法翻译 | CP-INT 手工 | 2026-07-10 | FIXED (review-pending) | 本地 worldquant/adapter（按设计不入库）：translate_formula_tracked + 递归 derive 展开（括号化、环检测、深度≤8、applied_derives 全记录）；review/none/未知照旧拒绝 | — |
| #003 | MINOR | synthesis / registry+picker | 注册表/picker 列出临时 `COMPOSITE_*` 合成因子，但其物化值在 per-run overlay（易失）；把它当成员再合成 → `composite frame has no rows to materialize` | CP-INT 自动化 | 2026-07-10 | IN-PROGRESS | — | — |
| #004 | MINOR | integrations / worldquant adapter | BRAIN 适配器包只装代码、不带 `worldquant/mapping/*.yaml`；翻译需 `QF_WQ_MAPPING_DIR`，缺失时降级 `BACKEND_NOT_CONFIGURED` | CP-INT 手工 | 2026-07-10 | FIXED (review-pending) | 本地 adapter：解析序 env（无效即报错不穿透）→ 包相对 repo-layout 回退（验 YAML 存在）→ 诚实 BACKEND_NOT_CONFIGURED；README 新增 Mapping data 节 | — |
| #005 | NIT | factor_engine / value_store | `value_store.py:146` 空/全 NA `pd.concat` 触发 `FutureWarning`（无害但污染日志） | CP-INT 手工 | 2026-07-10 | FIXED (review-pending) | phase-f 分支：_concat_score_frames 先剔除空帧（pandas 官方补救类）；两处调用点收敛；注：pinned pandas 2.2.3 下未能本地复现原警告（dtype 全路径一致），容器内触发条件存疑但风险类已消除 | — |

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

## 观察 / 非缺陷（记录但不作为 OPEN bug）

- **N-1（环境）** BRAIN 账户处于 **Tutorial mode**，`Check Submission`/`Submit Alpha` 被门控置灰；需 owner 亲自 Exit tutorial mode。非代码问题。
- **N-2（预期）** 单因子/合成回测在评价/回测区间**留空（full data）**时，样本内回测与外部 OOS 段数值相同——未做日期切分所致，符合设计。
- **N-3（工具限制）** 375px 布局视口无法用 `resize_window` 真验（需 CDP `setDeviceMetricsOverride`，当前浏览器工具未暴露）；暗色模式已确认。

---

*首次建立 2026-07-10（CP-INT PR#16/#17 联调）。缺陷复现证据见联调报告与本会话记录。*
