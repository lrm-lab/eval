# 02-4 · 自由动态对比工作台、1v1 对决与报告导出

> **现行对比**见 [开发要求与上线状态](../CURRENT.md#已上线)，含三轴、公开榜第三条系列与可比条件。

> **关联元数据**
> - **真实数据源**：SQLite 热层聚合（`batches`, `episodes`, `legacy_leaderboard_rows`, `official_results`）+ `sample_sets`（自定义样本集）+ `saved_comparisons`（实时方案 & 冻结报告快照）+ 按需懒加载 ATIF/Verifier/`vlmeval_result` 数据用于双栏 Diff
> - **核心 API**：`GET /api/v1/compare/options`、`POST /api/v1/compare/query`、`POST /api/v1/compare/task-diff`、`GET/POST/PUT/DELETE /api/v1/sample-sets`、`GET/POST/DELETE /api/v1/compare/saved`、`GET /api/v1/compare/s/{short_id}`、`POST /api/v1/compare/export`
> - **权限要求**：浏览与临时导出 `Viewer+`；保存团队对比方案/样本集 `Researcher+`；双栏轨迹 Diff 与导出严格复用 6 项明细 Scope（无权用户不返回轨迹/网关正文）
> - **交付里程碑**：**M1（核心工作台、交集双分母、双栏 Diff、短链与 CSV/MD/HTML/PDF 导出全量交付）**

---

## 0. 本页三层信息层级与状态恢复契约

| 披露层 | 用户看到什么 | 交互入口 | 返回后恢复哪些状态 |
|---|---|---|---|
| **默认展示 (L0)** | 顶部对比构建器 + **Gain vs Guardrail 三线结论卡** + 自动差异结论卡 + 核心矩阵（`Comparative Macro Avg` 与共同覆盖/有效分母） | 切换预设、调整退步阈值、切换交集模式、勾选可视化图表组件 | 完整状态同步至 `comparison_id` / URL 或短链 `/compare/s/:shortId` |
| **点击展开 (L1/L2)** | 悬停矩阵格/图表点看口径与分母；点击差异格或胜负象限优先右侧滑出预筛选逐题 Diff 抽屉（消费同一 `comparison_id`） | 关闭抽屉 / 筛选 `A胜B负` / 勾选题目保存为自定义样本集 | 原地保留背后矩阵、基线、对比对象、图例状态与滚动位置 |
| **完整证据 (L3)** | 打开全屏双栏轨迹/问答 Diff 抽屉或单次 Trial 显微镜（按权限展开折叠/分页的 ATIF/网关/验证日志） | 面包屑或「返回对比工作台」 | 携带 `comparison_id` 与 `returnTo` 状态栈无损还原原对比视图与筛选 |

---

## 1. 核心理念：基于真实数据的完全自由动态对比

### 1.1 多源入口汇聚（零摩擦加入对比）
用户不仅可以在 `/eval/compare` 从零构建对比，还可以从全站任意位置一键送入对比工作台：
- 从 **Leaderboard 主榜**、**Model/Harness/Benchmark 独立档案页**、**Batch 报告页**勾选多行 → 悬浮对比停靠栏一键 `发起对比` 或 `1v1 对决`；
- 从 **Diagnostics 诊断页**点击某个故障聚类/子指标行 → 一键带入对应对比对象与异常题集合；
- 从 **Datasets / Batch 逐题表**勾选任意题目 → 保存为 **自定义样本集（Custom Sample Set）** 并直接作为对比范围；
- 顶部提供 **热门预设快捷入口（Presets，全部基于 197 实测精确共同基准数）**：
  - `Qwen3.8-27B 基线 vs Trained ck133（23 项总表中共同有分 12 项；Thinking 组共同有分 10 项）`
  - `Qwen3.5-4B 基线 vs Trained ck105（共同有分 5 项）`
  - `Qwen3.8-27B 同模型跨 Harness 对照（Pi / OpenClaw / OpenCode / Codex / Claude Code）`
  - `Terminal-Bench 2.1 六组历史原生 Harbor 全量对照（共同有效 71 题效率图）`
  - `DeepSWE 1.1 两组对照：Pi v5 vs OpenClaw（共同覆盖 78 / 共同有效 60，45.00% vs 23.33%）`
  - `DeepSWE 1.1 三组全量对照（共同覆盖 48 / 共同有效 39）`
  - `GAIA validation 两组历史全量对照（共同覆盖 165 / 共同有效 118）`
  - **预设防误导规则**：任意预设打开后，先在顶部显式展示**实际共同有分基准数**（如 `12/23`、`10/23`、`5/23`），再逐项按真实版本与评分协议核验是否可比；未测项或不可比项在 Text/Vision 防退化护栏中一律标「无法判定」，绝不被未测项误导结论。

### 1.2 顶部四步自由构建器（Comparison Builder）

```text
+---------------------------------------------------------------------------------+
| [方案: 切换已保存方案 ▼] [实时方案 | 冻结快照] [+保存] [🔗 复制短链] [⬇ 导出 ▼]  |
+---------------------------------------------------------------------------------+
| ① 对比对象 (可拖拽排序, 设1个Baseline):                                          |
|    [★ Qwen3_8-27B × Pi (Baseline)] [Qwen3_8-27B × OpenClaw] [+ 添加对象/批次]   |
| ② 对比范围 (可自由裁剪):                                                         |
|    方向:[All/Agent/通用/多模态/合成] 模态:[All/Agent/Text/Vision] 语种:[All/EN/ZH]|
|    基准:[多选/搜索] 样本集:[全量 | 选自定义样本集 ▼] 难度/子域:[按任务元数据筛选] |
|    对齐模式: (○ 全集展示各自覆盖) (● 严格交集模式: 共同覆盖 vs 共同有效)          |
| ③ 展示指标 (全部真实数据列, 可任意勾选排序):                                      |
|    [x]主分 [x]覆盖率 [x]Δ变化 [x]通过率 [x]平均耗时(s) [x]总Token/分段 [ ]步数   |
|    [ ]可选估算成本(按价目表) [ ]原生已记录成本(未验账不标实付)                      |
| ④ 可视化组件开关:                                                               |
|    [x]结论卡 [x]分组柱状 [x]效率帕累托 [x]雷达图 [x]演进曲线 [x]汇总矩阵 [x]胜负矩阵 |
+---------------------------------------------------------------------------------+
```

---

## 2. 严格匹配、聚合与禁用提示规则（杜绝误导性对比）

1. **精确同题匹配键**：样本级对比与交集计算严格按 `(dataset_slug, dataset_version, split, task_id, task_checksum)` 对齐；当同一 `task_id` 出现不同 `task_checksum`（题面或评分器变动），界面弹出「版本漂移提示」并默认按 checksum 分组。
2. **训练前后对比硬护栏**：当用户发起「训练前后 Checkpoint 对比」时，系统自动校验双方具有**明确 `checkpoint` 身份、相同题集且相同评分协议**，满足才允许计算训练增益 `+Δ` 与绘制演进折线；不满足时仍可并排查阅但禁用 `+Δ` 并明示原因。
3. **单次与多次尝试聚合选择**：当对比对象含多尝试批次（如 `round1` 40×8），显式提供聚合选择器：`有效均分 (valid_mean)` | `pass@1` | `pass@k` | `pass^k 稳定性` | `单题最优 (best-of-n)`，严禁静默混用。
4. **共同有分基准集 Macro Avg（彻底杜绝列缺基准偏置）**：
   - 顶行 **`Comparative Macro Avg`** 仅在**所有参与对比列共同有分且各自基准内协议一致**的基准集合上等权计算（明示 `共同 X/Y 基准`），此行允许高亮 `Best` 与计算 `Δ`；
   - 次行 **`Self-Coverage Avg (非可比参考)`** 展示各列在其自身已测基准上的独立均分并标明各自基准数，**不高亮 `Best`、不算 `Δ`**。
5. **指标禁用显式解释**：协议不同、`run_scope` 不同、样本不足（`k > n_scored`）或无逐题分（如仅官方汇总分的 `vlmevalkit` 批次）时，对应单元格或图表显示灰态 `—` 并带清晰的悬浮原因说明。

---

## 3. 9 大动态可视化与分析组件

0. **三线模型迭代结论卡（Gain vs Guardrail Verdict，对齐 `@caozs2` 核心评估目标）**：
   - 当对比对象选定 Baseline 与候选 Checkpoint/Harness 时，顶部自动展示左右双区结论卡：
     - **左侧 · Agentic 4 项提分主线（`DeepSWE 1.1 / Terminal 2.1 / GAIA / WildClawBench`）**：逐项展示基线分、候选分、绝对分差（`+Δ pp`，明示单位）、相对变化（`%`）、可比题数与覆盖率；
     - **右侧 · LLM-Text 7 项 + Vision-Image 3 项防退化护栏**：内置可调退步容差阈值（默认 `-1.0 pp`），逐项标注 `维持/提升`、`触及退步阈值` 或 `缺测/不可比·无法判定`（三条线判断依据彼此独立，点击任意一项直接下钻该基准的同题双栏试验证据）。
1. **自动差异结论卡（At a Glance，借用 `LLM Stats`）**：完全由确定性数值汇总生成并绑定 `comparison_id`（不虚构评价），例如：“在 DeepSWE 1.1 共同有效的 60 道题上，`Pi v5 (45.00%)` 领先 `OpenClaw (23.33%)` +21.67pp（18 胜 / 9 同过 / 28 同错 / 5 负）”，每句附可点击的证据筛选链接（直接带 `comparison_id` 进入同口径逐题 Diff）。
2. **分组柱状图（Grouped Bar）**：支持按能力维度聚合或按 Benchmark 展开，超 6 列自动**图例分页**。
3. **帕累托效率前沿散点图（Pareto Frontier，默认真实耗时 & Token）**：
   - Y 轴为得分；
   - **X 轴默认仅提供 100% 真实已有指标**：`单题总耗时 P50 / 均值 (duration_sec)`、`Agent 执行阶段耗时 (agent_exec_s，仅原生/补丁批次可用)`、`平均总 Tokens`、`平均 Reasoning Tokens`；
   - 成本轴为可选切换项：**Harbor 原生 `cost_usd` 在来源未与账单对账时统一标注为「已记录成本（`recorded_native`）」**，仅经真实账单对账后才标「实付成本（`billed_reconciled`）」；若用户主动开启价格表计算则醒目标注「估算成本（价目表日期）」。缺覆盖点不参与帕累托前沿。
   - **绝不将总 Trial 时长伪装为 TTFT 或吞吐 TPS**（仅在未来网关/Direct 采集验证通过后才解锁 `TTFT` / `TPS` 选项，历史批次缺测标灰）。
4. **时间 / Checkpoint 演进折线图（Progress Over Time）**：标注数据日期、协议变更线与覆盖率，支持 `Frontier Only` 开关。
5. **多维能力雷达图（Radar）**：仅基于共同有分且协议一致的能力维度渲染。
6. **高密度汇总矩阵表（Pivot Matrix）**：支持列固定、排序、`InlineScoreBar`、行内 `≡` 子指标浮层、一键 `Copy Markdown Table`；严格交集模式下，表头同时固定展示 **`common_covered_tasks`** 与 **`common_valid_scored_tasks`**（DeepSWE `48 / 39`、GAIA `165 / 118`），故障按 0 计入的官方协议单独成列与独立分母。
7. **胜负关系矩阵（Win / Tie / Loss）**：任选 2 个有逐题分的对比对象，展示 4 格统计（`A 胜 B 负` / `同过` / `同错` / `A 负 B 胜`，总和严格等于 `common_valid_scored_tasks`），另有独立行统计「一方或双方运行故障未进入有效交集的题数」；点击任意一格立即过滤下方逐题 Diff 表。
8. **逐题并排轨迹 / 问答 Diff（Side-by-Side Task Diff）**：
   - 支持按 `结果不同 (A对B错 / A错B对)`、`子指标差异最大`、`耗时/Token 差异最大` 排序；
   - 点开任意题目打开全屏分屏抽屉：**左侧 Object A vs 右侧 Object B** 同步对照最终评分、WildClaw 17 项子指标差异、4 阶段耗时差、Token 构成差、以及双栏 ATIF 轨迹 / `vlmeval_result` 图文问答 / `direct_result` 与验证日志！

---

## 4. 实时方案 vs 不可变快照与可复现报告导出

| 模式 | 路由与行为 | 适用场景 |
|---|---|---|
| **实时动态方案（Live Saved Config）** | `/compare/s/:shortId`（`mode="live"`）：仅保存对比构建器 JSON 配置，每次打开按当前最新 SQLite 索引实时重算 | 持续跟踪训练 Checkpoint 与日常回归 |
| **不可变报告快照（Frozen Report Snapshot）** | `/compare/s/:shortId`（`mode="snapshot"`）：保存配置 + 生成时刻完整计算结果 JSON + 所有参与批次指纹与分母元数据，**永不随后续数据变化漂移** | 阶段评审汇报、周报引用、内部复盘留档 |

- **多格式导出（`POST /api/v1/compare/export`）**：支持导出 **CSV**、**JSON**、**Markdown 报告**、**高清图表 PNG** 以及 **带筛选条件、完整分母、数据出处、方法学口径声明与静态图表的独立离线 HTML / 打印版 PDF 报告**（严格按当前请求者 6 项明细 Scope 过滤，绝不泄露受限正文）。
