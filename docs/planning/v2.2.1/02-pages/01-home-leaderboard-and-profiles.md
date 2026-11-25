# 02-1 · 首页（三线结论卡）、高密主榜控件、ARC Prize 效率轨迹图与实体独立档案页

> **关联元数据**
> - **真实数据源**：SQLite 热层聚合表（`batches`, `episodes`, `legacy_leaderboard_rows`, `datasets`, `official_results`）+ `config/taxonomy.yaml`（含 `goal_type` 动态驱动）+ 冷层元数据（`bundles/*.tar(.zst)`）
> - **核心 API**：`GET /api/v1/system/overview`、`GET /api/v1/leaderboard`、`GET /api/v1/leaderboard/efficiency-scatter`、`GET /api/v1/models`、`GET /api/v1/models/{canonical}`、`GET /api/v1/harnesses`、`GET /api/v1/harnesses/{name}`、`GET /api/v1/benchmarks/{benchmark_slug}`、`POST /api/v1/recommend`
> - **权限要求**：基础榜单与档案 `Viewer+`；敏感样本/留出题链接受 6 项明细 Scope 独立控制
> - **数据与动效自检**：100% 绑定 Harbor / `lrm-eval` / `2026-11-20` 真实字段（无 License/Country/Hardware/Price 假筛选项）；全页动效执行 [`01-design-system/motion-and-microinteractions.md`](../01-design-system/motion-and-microinteractions.md) 规范
> - **交付里程碑**：**M1（核心主交付：含三线结论卡、LLM Stats 主榜全套控件、ARC Prize 效率前沿轨迹图、实体档案与动效）**；Model Recommender 与难题画廊随 M1 尾声 / M2 初完善

---

## 0. 本页三层信息层级与状态恢复契约（先重点、再展开、再证据）

| 披露层 | 用户看到什么 | 点击 / 交互入口 | 返回后恢复哪些状态 |
|---|---|---|---|
| **默认展示 (L0)** | Home 的 `Gain vs Guardrail` 三线结论带、KPI、王者卡；主榜 `核心总览` 默认列（身份 / 赛道主分 / `+Δ` / 覆盖率 / 状态）与 ARC 效率图主视图 | 切换赛道页签、调整退步阈值、切换 `Columns` 预设（`效率与Token` / `故障诊断`）、切换 4 视图（保留全部筛选） | — |
| **点击展开 (L1/L2)** | 悬停分数格看口径/分母/有效 0 分与故障拆解；**点击分数格或散点优先右侧滑出 L2 抽屉**，先展示该 Benchmark 口径、分母、子域与模型/Harness 构成，并提供醒目**「查看差异题」**进入预筛选任务列表 | 关闭抽屉 / 点「展开为全页」 / 勾选加入底部 `FloatingCompareDock` | 关闭抽屉原地保留背后主榜的**基线、筛选条件、排序、对比对象、Compare Dock 与滚动位置** |
| **完整证据 (L3)** | 进入 Benchmark / Model / Harness 独立档案页、动态对比双栏 Diff 或单次 Trial 显微镜 | 面包屑或左上角「返回原筛选视图」 | 通过 `returnTo` 状态栈无损恢复原榜单/对比筛选、视图模式与滚动定位 |

---

## 1. Home 平台总览（`/`）

### 1.1 页面布局（流光产品级 Hero + 三线业务目标结论带 + 实时资产大盘）

```text
+---------------------------------------------------------------------------------+
| [Logo LRM Eval]  Leaderboard  Datasets  Compare  Diagnostics  Execution  [⌘K] [☀] [user] |
+---------------------------------------------------------------------------------+
|  HERO (低功耗流光几何网格背景 + 交错入场动效):                                      |
|  LRM Eval · 天禧大模型与 Agent 统一评测与分析平台                                  |
|  CTA: [进入综合主榜 >]  [ARC 效率前沿图 >]  [自由动态对比 >]  [Datasets 能力树 >]   |
+---------------------------------------------------------------------------------+
|  ★ 模型迭代三线结论带 (Gain vs Guardrail Verdict · Baseline vs Candidate 快捷切换)|
|  ┌─ 左侧: Agentic 4项提分主线 (optimize_gain) ─┐ ┌─ 右侧: 10项防退化护栏 (guardrail) ─┐ |
|  │ DeepSWE 1.1  │ Terminal 2.1                 │ │ LLM-Text (7项) · 阈值 [-1.0pp ▼]  │ |
|  │ GAIA         │ WildClawBench                │ │ Vision-Image (3项)                │ |
|  │ [逐项基线/候选/+Δ pp/相对%/覆盖率 · 点击看证据]│ │ [维持/触及退步/缺测无法判定 · 点进证据]│ |
|  └─────────────────────────────────────────────┘ └───────────────────────────────────┘ |
+---------------------------------------------------------------------------------+
|  实时资产 KPI 带 (tabular-nums Count-Up): 题库数 | 可读批次(含告警) | 总有效Trial | 模型×Harness |
+---------------------------------------------------------------------------------+
|  Best by Task 各维度当前王者卡带 (Hover 微抬升 + 高光描边，一键筛选/加对比)         |
+---------------------------------------------------------------------------------+
|  最近运行的 8 个批次 (含 running_placeholder 脉动徽章、覆盖率条、官方分/有效均分)    |
+---------------------------------------------------------------------------------+
```

### 1.2 三线「Gain vs Guardrail」模型迭代结论卡详设
直接落实 `@caozs2` 三线评测核心目标（`Agentic 4 项看提升` vs `LLM — Text 7 项 + Vision — Image 3 项检查是否退步`）：
- 顶部提供快捷选择器：`Baseline 模型/批次` vs `Candidate 候选模型/Checkpoint/Harness` + **可调退步容差阈值**（默认 `-1.0 pp`，支持用户实时调节如 `-0.5 pp` / `-2.0 pp`）；
- **左侧 Agentic 提分区（4 项：`DeepSWE 1.1`、`Terminal 2.1`、`GAIA`、`WildClawBench`）**：每张子卡清晰展示 `基线分`、`候选分`、`绝对分差 (+Δ pp，明示单位)`、`相对变化 (+X.X%)`、`可比题数与覆盖率`；点击任意卡直达该基准的同题双栏对比证据！
- **右侧防退化护栏区（Text 7 项 + Vision 3 项）**：逐项以紧凑状态条展示基线、候选、`Δ`、覆盖与三态判定徽章：
  - 🟢 **维持 / 提升**（`Δ >= 退步阈值` 且口径可比）；
  - 🔴 **触及退步阈值**（`Δ < 退步阈值` 且口径可比，高亮置顶提醒）；
  - ⚪ **无法判定**（任一方缺测、仅原始资产未接跑分如 `MMLU-Pro`/`BFCL-v4`/`Global-MMLU-Lite`，或评分协议不一致），绝不把缺测当成“无退步”！

### 1.3 空态与容错表现
- 共享盘无权限目录（如 `synth-v1-qwen-opencode-20261124T022715`）在 KPI 带显示琥珀色微型提示，点击进 `/admin/health`，不阻断渲染；
- 仅含 `batch-status.json` 的运行中目录以蓝色脉动徽章 `运行中 (等待发布)` 展示脱敏来源与批次名。

---

## 2. LLM Stats 高密主榜与全套基础控件（`/leaderboard` & `/leaderboard/:track`，M1 必验）

1. **顶部双层导航（100% 由 `taxonomy.yaml` 动态驱动，零硬编码）**：
   - 一层赛道页签（弹簧共享滑块）：`All` | `Agentic (提分主线)` | `LLM — Text (护栏)` | `Vision — Image (护栏)` | `Synthetic (合成)` | `Extended (历史扩展)`；
   - 二层快捷能力切片 + 高频过滤胶囊：**仅绑定 Harbor 真实属性**（`Formal Only`、`Trained`、`Thinking`、`Has ATIF`、`Has Gateway`、`Model Family`、`Harness`、`Runner (harbor/vlmevalkit/direct)`、`Dataset Version / Split`、`Source (harbor_runs/2026-11-20)`、`Time Window`），**严禁出现无数据源的 `License / Country / Hardware / Price` 假下拉**。
2. **右上角统一工具条（M1 必验基础功能）**：
   - **`Columns` 列配置面板**：自由勾选显隐、拖拽调整列序、按赛道一键切预设列组；
   - **`Copy link`**：把当前筛选、排序、列配置与视图模式序列化到 URL 或短链 ID 一键复制；
   - **`Compare (N)`**：勾选表格 2–8 行后激活，一键进入 `/eval/compare`；
   - **同上下文 4 视图切换图标组**：`Table 表格` | `Trend 趋势折线` | `Grouped Bar 分组柱图` | `Efficiency Scatter 效率前沿散点`（切换视图 **100% 保留当前筛选条件**，触发平滑形变动效！）。
3. **高信噪比表格细部（对齐 `LLM Stats`）**：
   - 左侧冻结 `# (Top 3 金银铜勋章)` + `Model × Harness@Version + Runner/Scope 徽章`；
   - **每列最高分暖琥珀金色高亮**、`tabular-nums` 等宽数字、表头 `?` 弹出口径/分母并可跳 `/toolkit/methodology`、`↑↓` 排序；
   - **缺测或无有效分一律留白 `—`**（如 `vlmevalkit` 全失败或纯推理批次绝不填 `0`）；
   - **点击任意分数格直达该组合在对应 Benchmark 的批次 / 题目 / Trial 证据**；底部勾选行浮现弹簧动画对比篮。

---

## 3. ARC Prize 式「得分 × 效率前沿与同族轨迹图」（Leaderboard 散点视图 & Agentic 基准页内置）

完全借用 `ARC Prize` 视觉表现力并 **100% 锚定 Harbor 真实已有字段**（ECharts 5 Custom/Scatter + Line，`GET /api/v1/leaderboard/efficiency-scatter`）：

```text
+---------------------------------------------------------------------------------+
|                         TERMINAL-BENCH 2.1 LEADERBOARD                          |
|  [ ✓ VERIFIED / FORMAL ]  [ N = 71 common valid tasks ]                         |
| 100% ┼                                                                          |
|  80% ┼                               ● Qwen3_8-27B · Pi (High)                  |
|  60% ┼ - - - - - - - - - - - - - - ╱─┼─ Pareto Frontier                         |
|  40% ┼              ● Model A (Med)   ● Model B (OpenCode)                      |
|  20% ┼       ● Model A (Low)                                                    |
|   0% └──────────┬──────────┬──────────┬──────────┬──────────┬──────────── [⬇]  |
|                100s       300s       600s      1,000s     2,000s  (Log/Linear)  |
|                            TASK DURATION P50 (s)                                |
|                                                                                 |
|        [ DeepSWE 1.1 | (● Terminal 2.1) | GAIA | WildClawBench ]                |
|  Model:[All ▼]  Harness:[All ▼]  Color:[Model|Harness]  X-axis:[P50耗时|Token|步数|日期|已记录成本] |
+---------------------------------------------------------------------------------+
```

### 3.1 严格数据口径与交互规格
1. **单基准同口径严格对齐**：先锁定 `(dataset_release_id, scoring_protocol)`，图中每个点代表一个可比较的 `Model × Harness@Version × Batch/Config`；左上角常驻 `Verified / Formal` 与精确共同有效题数徽章（如 `N = 71 common valid`）。
2. **100% 真实横轴切换器（切换时明示 `task_set_policy` 并区分记录覆盖与可绘制点数）**：
   - **`单题总耗时 P50 / 均值 (s)`（默认，支持对数/线性切换）**——所有 Harbor 作业均具备；
   - **`输入 / 输出总 Tokens`**——同时返回 `batches_with_any_records` 与 `plottable_full_coverage_points`，缺覆盖点灰显提示“样本不足”且**严禁参与帕累托前沿**；
   - **`平均工具调用步数 (ATIF)`**——仅对记录工具事件的 Harness 有效；
   - **`评测完成日期 / Checkpoint`**——查看随时间/迭代的爬坡演进；
   - **`已记录成本 ($ · recorded_native)`**——Harbor 原生 `cost_usd` 在来源未与账单对账时统一标注「已记录成本」（仅账单对账通过后才标「实付」）；若 `plottable_full_coverage_points == 0`（如 TB2.1 仅 1 组 53/71 题有部分记录），明确提示部分记录覆盖并禁止绘制前沿。
   - **切轴策略显式可选**：默认 `fixed_y_common`（锁定同一 71 题基线，保留缺测标记与缺覆盖提示），可切换 `recompute_xy_common`（按 X/Y 均有效重取共同题集并同步刷新分母徽章）。
3. **同族虚线轨迹（Series Connector）与帕累托前沿护栏**：
   - **仅在存在明确记录的有序配置系列**（同一模型不同 `thinking` 档位、同系列 `base → ck105 → ck133`、或登记的同模型多版本序列）且共同有效题可比时绘制虚线轨迹；严禁在无序无关点间乱连线；
   - **帕累托前沿曲线**仅覆盖协议可比且横纵轴均有效的点；
   - **产品级交互**：鼠标悬浮某点自动聚光高亮其同族轨迹（其余点淡至 `20%` 不透明度），弹出包含分母、覆盖率与四阶段耗时摘要的毛玻璃卡片；点击点下钻批次与逐题明细；支持框选 / `Shift` 多选直接送入 `/eval/compare`；右下角一键导出高清 PNG/SVG！
4. **197 实测首期黄金验收基准（立即可跑）**：
   - 6 组 `2026-11-20/terminal-bench2.1/` 原生 Harbor 作业按 `(task_name, task_checksum)` 对齐，同时具备原生 `reward_valid=true` 有效分与总耗时的共同题恰好为 **71 题**，可直接渲染 6 个点（分数 **`25.35%–54.93%`**，P50 耗时 **`574–1,091s`**）；同一 71 题上仅 3 组有完整输入/输出 Token、仅 1 组 53 题有原生 `cost_usd`，切轴时准确提示覆盖不足并保持缺测标记！

---

## 4. 三大实体独立档案页（稳定可分享 URL，全链路互联）

### 4.1 模型目录与独立档案页（`/models` & `/models/:modelCanonical`）
> **元数据诚信约束**：仅展示从真实评测产物解析出的身份与评测字段（`model_canonical`、历史 `model_route`、已核验 `checkpoint` 血缘、已测 `harness` / `runner` / `thinking_mode` 与全部成绩）。绝不幻想外部商业字段。
- **头部名片**：规范名、所有 `model_route`、已核验 `checkpoint` 血缘（存在同题集同协议基线时点亮「一键 Gain vs Guardrail 1v1 对比」）、一键加对比；
- **4 大 Tab**：`全维度能力画像（雷达+三线成绩单）` | `跨 Harness/Runner 对照` | `Checkpoint 演进趋势（仅同题集同协议核验连线）` | `全部关联批次`。

### 4.2 Harness 目录与版本档案页（`/harnesses` & `/harnesses/:harnessName`）
- 对比全部已测适配器与运行器（`pi`、`openclaw`、`opencode`、`codex`、`claude-code`、`vlmevalkit`、`direct`、`oracle`、`nop`，未知新值前向兼容）；
- 展示已测版本清单、记录边界（如 `openclaw@2026.11.5` 无逐条工具事件、`vlmevalkit` 为 `model_request_response`）、工具调用画像（仅对记录工具事件的 Harness 渲染）与跨基准成绩矩阵。

### 4.3 Benchmark 独立档案页（`/benchmarks` & `/benchmarks/:benchmarkSlug`）
- Benchmark 档案 URL 使用 URL-safe `benchmark_slug`（版本/Split 下挂 `dataset_release_id`），页面标题完整保留 `raw_dataset_name`（含空格括号）；
- **5 大 Tab**：`Leaderboard 小榜` | **`Efficiency & Progress 效率前沿轨迹图（内置 ARC Prize 散点图）`** | `典型难题与高失败率画廊（仅对有逐题分的基准启用）` | `题目清单与子域/学科拆解` | `Properties & Methodology`。

---

## 5. 约束选型推荐器（`/recommend`，严格基于真实已有字段）

左侧约束仅用真实字段（目标赛道权重、最低质量分门槛、单题平均耗时上限、单题平均总 Token 上限、允许的 Harness/Runner 与正式口径），右侧返回帕累托最优候选与权衡说明，一键送入 `/eval/compare`。
