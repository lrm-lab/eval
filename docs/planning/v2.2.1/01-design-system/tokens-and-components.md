# 01-1 · 视觉设计系统、LLM Stats 主榜控件与全站导航状态规范

本文档定义平台的视觉 Token（以 **`LLM Stats`** 直观高信噪比语言为主锚点，融合 `ARC Prize` 效率图视觉与 `Le-AIEval` 工作台骨架）、通用高频组件与主榜基础控件规格，以及全站路由与状态持久化规则。**网页与交互动效详设见同级分册 [`motion-and-microinteractions.md`](motion-and-microinteractions.md)。**

---

## 1. 视觉设计 Token（Light & Dark 双原生主题）

默认提供**清爽现代浅色主题**与**极客高对比深色主题**（对齐 `llm-stats.com` / `arcprize.org` 深邃质感），通过 `data-theme="light" | "dark"` 瞬切并持久化到 `localStorage`，ECharts 5 图表与代码高亮主题同步联动。

### 1.1 基础色板与榜首强调色

| Token | Light Mode | Dark Mode | 使用场景 |
|---|---|---|---|
| `--bg-canvas` | `#F6F8FA` | `#09090B` | 主工作区画布背景 |
| `--bg-surface` | `#FFFFFF` | `#111114` | 卡片、表格、侧边栏、顶栏表面 |
| `--bg-elevated` | `#F8FAFC` | `#18181B` | 表头、次级卡片、悬浮浮层、思考折叠块 |
| `--border-subtle` | `#E5E7EB` | `#27272A` | 1px 细边框、分割线 |
| `--border-strong` | `#CBD5E1` | `#3F3F46` | 输入框聚焦、卡片 Hover、固定列投影边 |
| `--text-primary` | `#0F172A` | `#F4F4F5` | 标题、核心指标数字、模型主名 |
| `--text-secondary` | `#475569` | `#A1A1AA` | 次级标签、表头、时间戳、覆盖率分母 |
| `--text-muted` | `#94A3B8` | `#71717A` | 缺测 `—`、占位说明、辅助提示 |
| `--brand-primary` | `#0068B5` | `#3B82F6` | 品牌强调、主按钮、激活态指示线 |
| `--brand-tint` | `#EFF6FF` | `#172554` | 选中行底色、激活 Pill 背景 |
| `--rank-gold` / `--best-cell` | `#B45309` / `#D97706` | `#F59E0B` / `#FBBF24` | 排名 #1 徽章、每列最高分数字高亮（对齐 `LLM Stats`） |
| `--rank-silver` / `--rank-bronze` | `#64748B` / `#9A3412` | `#CBD5E1` / `#EA580C` | 排名 #2 / #3 圆形勋章 |

### 1.2 语义状态色（严格区分有效 0 分、未评分、护栏退步与运行故障）

| 语义状态 | 前景色（Light / Dark） | 背景色（Light / Dark） | 强制使用位置 |
|---|---|---|---|
| **Pass / 提分 / 护栏维持** | `#15803D` / `#4ADE80` | `#ECFDF5` / `#052E16` | 通过率、Agentic `+Δ` 提升、护栏达标、Official Verified |
| **Valid Zero / 有效 0 分** (`scored, reward=0`) | `#B45309` / `#FBBF24` | `#FFFBEB` / `#451A03` | 模型有效作答但做错（**禁用红色！**） |
| **Error / 运行故障 / 触及退步阈值** | `#B91C1C` / `#F87171` | `#FEF2F2` / `#450A0A` | 运行错误、熔断告警、防退化护栏跌破阈值 |
| **Unscored / Infer-Only / 无法判定** | `#475569` / `#94A3B8` | `#F1F5F9` / `#1E293B` | `vlmevalkit` 纯推理未打分、缺测或不可比护栏 |
| **Running / 运行中占位** | `#0284C7` / `#38BDF8` | `#F0F9FF` / `#082F49` | 仅含 `batch-status.json` 的运行中目录、Runner 实时态 |
| **Legacy 2026-11-20 口径** | `#6D28D9` / `#A78BFA` | `#F5F3FF` / `#2E1065` | 历史基线来源徽章 |
| **Smoke 冒烟 / Reference 参考解** | `#0369A1` / `#38BDF8` | `#F0F9FF` / `#082F49` | 冒烟批、`oracle`/`nop` 标识 |
| **Delta 正向 / 负向** (`+Δ` / `-Δ`) | `#15803D` / `#B91C1C` | 透明或淡底徽章 | 相对基线涨跌幅（耗时/Token/成本反向着色：降为绿、升为红） |

### 1.3 多对象图表色板（10 色色盲友好序列）

按稳定 Hash 绑定对象颜色，确保跨柱状/雷达/散点/趋势图同一模型族或 Harness 同色：
`['#2563EB', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6', '#06B6D4', '#EC4899', '#84CC16', '#F97316', '#6366F1']`。

### 1.4 排版与数字密度规范

- **字体栈**：正文 `Inter, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`；代码/ID/UUID/指标 `JetBrains Mono, ui-monospace, SFMono-Regular, monospace`。
- **等宽对齐数字**：所有分数、覆盖率、Token、耗时、Δ 强制启用 `font-variant-numeric: tabular-nums`。
- **行高密度档位**：高密度表格提供 `紧凑 (36px) / 舒适 (44px)` 切换，默认 `紧凑`（一屏清晰浏览 15–20 行）。

---

## 2. 通用高频 UI 组件与 LLM Stats 主榜基础控件（M1 必验基元）

### 2.1 统一主榜工具条（`LeaderboardToolbar`，100% 绑定 Harbor 真实属性）
- **双层导航与能力快切**：
  - 一层赛道页签（由 `taxonomy.yaml` 动态驱动）：`All` | `Agentic (主提分)` | `LLM — Text (护栏)` | `Vision — Image (护栏)` | `Synthetic (合成)`；
  - 二层高频快捷切片胶囊：`Formal Only (full_split)`、`Trained Checkpoint`、`Thinking`、`Has ATIF`、`Has Gateway`，加真实下拉筛选：`Model Family`、`Harness`、`Runner (harbor/vlmevalkit/direct)`、`Dataset Version / Split`、`Source (harbor_runs/2026-11-20)`、`Time Window`；
  - **严禁出现无数据源的假下拉**（不展示 `License`、`Country`、`Hardware`、`Price` 等商业字段，仅在未来接入可信元数据源后按配置显式开启）。
- **右侧统一操作与同上下文视图切换组**：
  - `Columns` 列配置面板（**内置 4 套防过载场景预设：`核心总览（默认，仅露身份/赛道主分/+Δ/覆盖率/状态，防止首屏铺满 20+ 列）`、`效率与 Token`、`故障诊断`、`自定义`**，支持自由勾选显隐与拖拽排序）；
  - `Copy link`（序列化当前筛选、排序、列配置与视图模式为 URL 或 `/compare/s/:shortId` 短链，带复制成功微提示动效）；
  - `Compare (N)`（勾选 2–8 行后激活，一键进入 `/eval/compare`）；
  - **同上下文 4 视图切换图标组**：`Table 表格` | `Trend 趋势折线` | `Grouped Bar 分组柱图` | `Efficiency Scatter 效率前沿散点`——**切换视图 100% 保留当前筛选条件，并触发 ECharts `universalTransition` 平滑过渡！**

### 2.1.5 全站 4 层渐进披露信息架构（Progressive Disclosure：L0 → L1 → L2 → L3）

为彻底避免海量 Harbor 维度平铺导致的界面杂乱，全站严格执行**“首屏克制看重点、逐层清晰钻细节、返回零丢失上下文”**四层渐进披露规范：

| 信息层级 | 呈现载体 | 默认呈现与用户入口 | 状态保持与返回契约 |
|---|---|---|---|
| **L0 · 结论重点层** | 首页 Gain vs Guardrail 结论带、主榜默认列（`核心总览` 预设）、ARC 效率图 | 只展示决策所需的核心信号：Agentic 4 项提分、Text/Vision 防退化状态、主分、`+Δ`、覆盖率与状态徽章；次级列由场景预设按需展开 | 切换视图/预设/排序均实时同步 URL Query，维持颜色编码与量纲一致 |
| **L1 · 悬停速览层** | 定制毛玻璃 Rich Hover Popover / Chart Tooltip | 悬停分数格展示口径、分母进度条、有效 0 分/故障拆解及「查看差异题」直达入口；悬停耗时/Token 格展示 4 阶段耗时微条或可用 Token 构成；悬停散点聚光高亮同族轨迹 | 不打断当前页面浏览，移出即优雅淡隐，零页面跳动 |
| **L2 · 侧滑穿透抽屉层** | 右侧滑出 Slide-over Drawer（支持一键展开全页） | 点击分数格、散点或差异项优先从右侧滑出抽屉：先展示该 Benchmark 口径、分母、子域与模型/Harness 构成，并提供醒目**「查看差异题」**入口进入预筛选逐题清单与多尝试微点阵 | 背后主榜的**基线、对比对象、筛选条件、滚动位置与 Compare Dock 100% 原地保留**，关闭抽屉无缝续看 |
| **L3 · 全屏证据显微镜层** | Trial 详情页 & 双栏轨迹 Diff 全页（Evidence Studio） | 先呈现逐次结果、评分拆解与失败摘要；按权限按需展开折叠/分页的 ATIF 轨迹、OpenEnv 网关窗口与验证日志，自动聚焦首个报错步或最终答复，支持 `#step-N` 锚点直链与快捷键（`j/k` 切题、`[`/`]` 切 Trial、`t` 切换全思考链） | 顶栏携带完整 `returnTo` 状态栈与面包屑，点击「返回原筛选视图」无损恢复上一组对比/榜单筛选与位置 |

### 2.1.6 6 大核心定制 UI 元素精雕规格（杜绝裸套默认组件库，全部覆盖 7+1 态）

以下 6 个全站最高频视觉元件必须独立精雕实现，且**每一项均须交付完整的 8 态设计稿与自动化验收**：`Default`、`Hover`、`Active`、`Focus-Visible`、`Loading (Skeleton)`、`Missing (缺测)`、`Error (故障)` 及 `Reduced-Motion`：

1. **复合得分单元格（`CompositeScoreCell`）**：`tabular-nums` 等宽主分（列内最高分暖琥珀金加粗）+ 单元格底缘 2px 极细内嵌得分条 + 右上角紧凑 `+Δ` 微标 + 右下角低饱和覆盖率副标（如 `71/89`）+ 悬停浮现 `≡` 子分详情入口；缺测统一淡灰弱对比 `—`。
2. **多尝试微点阵胶囊（`MultiAttemptPill`）**：将单题多次尝试（如 `round1` 8 tries）渲染为紧凑等距语义圆点胶囊（🟢 Pass / 🟡 有效 0 分 / 🔴 运行故障 / ⚪ Unscored）；单点悬停放大并显示单次耗时/状态摘要，点击直达该次 Trial 显微镜。
3. **提分与护栏状态胶囊（`GainGuardrailPill`）**：语义严格区隔——Agentic 提分线采用带向上/向下矢量箭标与绝对 `pp` / 相对 `%` 双显的增益徽章；Text/Vision 防退化线采用盾牌图标三态徽章（🟢 护栏维持 / 🔴 触及退步阈值 / ⚪ 缺测不可判）。
4. **工具调用 ↔ 观察结果配对卡（`PairedAtifStepCard`）**：左侧 2px 语义连接导轨将一条 `tool_call`（等宽终端指令块 + 语法高亮 + 一键复制）与其配对 `observation`（限高折叠 + 行号 + `is_error` 左侧红边高亮）视觉绑定；悬浮任一侧双向联动高亮；`reasoning_content` 采用柔和手风琴收纳（`grid-template-rows` 平滑展开）。
5. **定制毛玻璃图表 Tooltip 与同族轨迹聚光（`GlassChartTooltip`）**：明/暗双主题精致毛玻璃背板（`backdrop-filter: blur(10px)` + 1px 半透明细描边），内嵌覆盖率微进度条、四阶段耗时微型占比条与直达批次提示；悬停 ARC 散点时同族轨迹平滑聚光、背景点淡至 `20%`。
6. **底部悬浮对比停靠栏（`FloatingCompareDock`）**：在主榜、档案页、批次页或散点框选任意 1–8 个对象后，从屏幕底部居中弹性升起（毛玻璃胶囊容器），内置已选对象 Chip（支持拖拽调序）、点击皇冠图标瞬切 Baseline、实时显示共同可比题数/基准数提示，以及主按钮一键进入 `/eval/compare`！

### 2.2 高密度数据表（`EvalDataTable`，TanStack Table v8 + Virtual）
- **左侧冻结身份列**：固定 `[复选框]` + `# Rank（Top 3 金银铜圆形勋章）` + `Model × Harness@Version + 状态徽章`，横向滚动时渐显阴影分割线。
- **每列最高分高亮**：每列在可比范围内最高分以暖琥珀金色 (`--best-cell`) 加粗显示（对齐 `LLM Stats`），不可比或非正式行不参与高亮。
- **行内微进度条（`InlineScoreBar`）与 `≡` 子指标快捷浮层**：分数单元格内嵌进度条 + 等宽主数字 + 覆盖率角标（如 `84/89`）+ `≡` 子分浮层图标；**点击任意分数格直达对应 Benchmark / Batch / Trial 证据**；缺测或无有效分严格渲染淡灰 `—`。
- **多列排序与表头漏斗过滤**：表头带 `↑↓` 排序指示与 `?` 口径问号浮层（展示公式、分母、适用运行器并锚点直达 `/toolkit/methodology`）。
- **Token 分段指示器（`TokenBreakdownBar`，含包含语义护栏）**：
  - 仅当 `token_semantics == "mutually_exclusive_verified"` 时渲染互斥 4 段堆叠条；
  - 当来源包含语义未验证（`unverified_components`）时绝不强行叠加，改用并排独立胶囊（兼容 `reasoning_tokens` 与 `reasoning_output_tokens`）。

### 2.3 全局 `⌘K` 检索、图表容器与短链快照
- **`CommandPalette`（`⌘K / Ctrl+K`）**：调用 `GET /api/v1/search?q=...`（SQLite FTS5，< 30ms），分组展示 `Models / Harnesses / Benchmarks / Batches / Tasks / Trials / Saved Comparisons / Manuals`，服务端强制按 `project_id` 与 6 项明细 Scope 过滤。
- **`ChartCard` 容器**：标题 + 口径/分母徽章 + 工具条（`Graph ↔ Table`、`线性/对数轴`、`全屏放大`、`下载高清 PNG/SVG`、`复制 CSV`），超 6 系列自动分页图例。
- **URL 双向同步与「返回原筛选视图」**：下钻详情页自动携带 `returnTo` 状态栈，左上角一键无损还原原筛选与滚动位置；复杂对比状态通过 `/compare/s/:shortId` 持久化分享。

---

## 3. 全站稳定路由与壳层结构（100% 对齐真实模块，零空壳路由）

- **首页壳层（`/`）**：顶部通栏 + 宽屏流光 Hero + Gain vs Guardrail 结论带 + 资产大盘与最近批次；
- **工作台壳层（业务路由）**：56px 固定顶栏 + 240px/64px 可折叠左侧导航树 + 面包屑（含返回原筛选）+ 主工作区。

| 分组 | 路由路径 | 页面名称 | 所属分册 |
|---|---|---|---|
| 认证 | `/login` | 登录页 | `06-architecture-security-ops` |
| 总览 | `/` | Home 平台首页（含 Gain vs Guardrail 结论带） | `02-pages/01-home-leaderboard-and-profiles.md` |
| 榜单与档案 | `/leaderboard` · `/leaderboard/:track` | 高密主榜、4 视图切换 & ARC Prize 效率前沿轨迹图 | `02-pages/01-home-leaderboard-and-profiles.md` |
| 榜单与档案 | `/models` · `/models/:modelCanonical` | 模型目录 & 模型独立档案页 | `02-pages/01-home-leaderboard-and-profiles.md` |
| 榜单与档案 | `/harnesses` · `/harnesses/:harnessName` | Harness 目录 & Harness/版本档案页 | `02-pages/01-home-leaderboard-and-profiles.md` |
| 榜单与档案 | `/benchmarks` · `/benchmarks/:benchmarkSlug` | Benchmark 独立档案页（含效率轨迹图与难题画廊） | `02-pages/01-home-leaderboard-and-profiles.md` |
| 榜单与档案 | `/recommend` | 约束选型推荐器（仅基于真实 Harbor/历史指标） | `02-pages/01-home-leaderboard-and-profiles.md` |
| 题库 | `/datasets` · `/datasets/:datasetReleaseId/tasks/:taskId` | Datasets 四级能力树图 & Task 浏览器 | `02-pages/02-datasets-and-tasks.md` |
| 评测报告 | `/eval/reports` · `/eval/batches/:batchId` | 评测报告中心 & 批次详情页 | `02-pages/03-reports-and-trial-inspector.md` |
| 评测报告 | `/eval/batches/:batchId/trials/:trialId` | 单次 Trial 细粒度诊断显微镜 | `02-pages/03-reports-and-trial-inspector.md` |
| 对比与诊断 | `/eval/compare` · `/compare/s/:shortId` · `/compare/1v1/:a/vs/:b` | 自由动态对比工作台 / 短链 / 1v1 对决 | `02-pages/04-dynamic-comparison.md` |
| 对比与诊断 | `/eval/diagnostics` | 深度结果诊断与客观轨迹证据中心 | `02-pages/05-diagnostics-and-behavior.md` |
| 执行与验证 | `/eval/execution` | 受控评测执行与实时事件监控 | `02-pages/06-execution-and-direct-lab.md` |
| 执行与验证 | `/eval/direct-lab` | Direct 原生评测验证台（`cases.jsonl + score.py`） | `02-pages/06-execution-and-direct-lab.md` |
| 合成与训练 | `/synth/datasets` · `/synth/selection` · `/training/opd` | 合成题工坊 / RL-SFT 选题 / 训练与 OPD 监控 | `02-pages/07-synthetic-and-training.md` |
| 工具箱与运维 | `/toolkit/verify` · `/toolkit/params` · `/toolkit/manuals` · `/toolkit/methodology` · `/admin/health` | 离线校验 / 运行参数对照台账 / 框架手册 / 独立方法学页 / 管理员健康运维页 | `02-pages/08-toolkit-and-admin.md` |
