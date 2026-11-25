# 01-2 · 网页动效、微交互与产品级视觉表现规范（Motion & Polish）

为落实 `@caozs2` **“网页动效、用户交互动效、增加产品展示效果的动效都要加进去，整个产品设计要非常完善而不是一个 demo 展示”** 的要求，本文档制定全站统一、克制而高级的产品级动效体系（对标 Linear / Vercel / ARC Prize / LLM Stats 质感），确保**既具备极强的展示感染力与操作反馈，又严守 60fps 流畅度与低视觉疲劳红线**。

---

## 1. 动效设计原则与物理参数 Token

### 1.1 四项核心原则
1. **服务信息理解，拒绝廉价堆砌**：动效用于表达空间关系（下钻/返回）、数据变化（切轴/切榜形变）、状态流转（运行中脉动/完成对账）与焦点引导（悬浮高亮同族轨迹）。
2. **100% GPU 合成属性**：所有动画仅驱动 `transform`、`opacity`、`clip-path` 与 SVG `stroke-dashoffset`；**严禁动画化 `width`、`height`、`top`、`left`、`margin` 等触发重排的属性**。
3. **打断即响应（Interruptible）**：任何过渡动效不得锁定鼠标/键盘点击（`pointer-events` 保持响应），用户快速连切页签或图表轴时立刻平滑过渡到最新目标状态。
4. **无障碍与性能兜底**：默认尊重系统 `@media (prefers-reduced-motion: reduce)`，并在右上角用户菜单提供 **「精简动效」一键开关**（切为 0ms 瞬切）；超大数据量虚拟列表（>200 行）自动关闭逐行入场交错，确保 60fps 零卡顿。

### 1.2 全局动效 Token（CSS Variables + Framer Motion / Motion 预设）

| Token | 数值 / 曲线 | 适用场景 |
|---|---|---|
| `--dur-instant` | `90ms` | 按钮按压回弹（`scale(0.98)`）、复选框勾选、Tooltip 显隐 |
| `--dur-fast` | `160ms` | 页签滑块移动、行悬浮高亮、下拉菜单/列配置弹出、表头排序箭头旋转 |
| `--dur-normal` | `240ms` | 抽屉滑入/滑出、`⌘K` 命令面板缩放显现、Floating Compare Tray 浮现 |
| `--dur-showcase` | `420ms` | 首页 Hero 与 KPI 数字滚升、结论卡交错入场、行内微进度条生长 |
| `--dur-chart` | `520ms` | ECharts `universalTransition` 跨视图/跨轴数据点平滑形变、轨迹虚线绘制 |
| `--ease-out-expo` | `cubic-bezier(0.16, 1, 0.3, 1)` | 抽屉、弹窗、卡片入场（快启缓停，极显干脆） |
| `--ease-spring-snappy` | `stiffness: 420, damping: 32, mass: 0.8` | 共享 `layoutId` 滑块（赛道 Tab、基准快切胶囊、视图图标组）、对比篮 Chip 增删 |
| `--ease-chart` | `cubic-bezier(0.22, 1, 0.36, 1)` | 图表散点位移动画与帕累托前沿扫线 |

---

## 2. 六大核心场景动效详设

### 2.1 首页产品展示动效（Home Showcase Choreography）
- **低功耗流光网格 Hero 背景**：采用纯 CSS 多层径向光晕 + 细粒度几何网格遮罩缓慢呼吸漂移（18s 循环，纯 GPU `transform`，CPU 占用 < 1%，Light/Dark 双主题自适应调光），配合鼠标轻微视差光斑跟随（Spotlight Hover Border），瞬间拉开与普通后台 Demo 的产品级质感。
- **首屏有序交错入场（Staggered Reveal）**：Hero 标题 (`0ms`) → 4 项核心能力卡片 (`+60ms` 递进，`translateY(12px) → 0` + `opacity 0 → 1`) → **Gain vs Guardrail 迭代结论带** (`+120ms`) → `Best by Task` 王者卡带 (`+180ms`)；同一浏览器会话内二次返回首页自动跳过首次入场交错，避免反复等待。
- **KPI 等宽数字平滑滚升（Count-Up）**：总题库数、可读批次数、总有效 Trial 数以 `tabular-nums` 在 `400ms` 内按 `ease-out-expo` 从 0 滚升至真实值，数字位数固定不抖动。
- **卡片微悬浮与流光描边**：`Best by Task` 王者卡与能力入口卡悬浮时 `translateY(-2px)` + 柔光投影 + 顶部 1px 品牌高光边渐现。

### 2.2 高密度主榜与控件微交互（对齐 `LLM Stats` 精炼质感）
- **共享滑块导航（Shared `layoutId` Indicator）**：顶部赛道页签（`All | Agentic | LLM — Text | Vision — Image | Synthetic`）、筛选胶囊与右上角视图切换器（`Table | Trend | Bar | Scatter`）切换时，背景高亮胶囊以物理弹簧 (`--ease-spring-snappy`) 平滑滑移。
- **同上下文视图无缝形变**：点击 `Table ↔ Trend ↔ Bar ↔ Scatter` 时，顶部筛选条与工具栏保持完全静止（零重绘闪烁），主视图区以 `180ms` 淡出/轻缩放过渡到目标图表，且 ECharts 启用 **`universalTransition: true`**——从分组柱图切换到效率散点图时，同一模型×Harness 的数据图形平滑变形飞行至新坐标！
- **表格精细微动效**：
  - `InlineScoreBar` 首次进入视口时以 `scaleX(0 → 1)`（`transform-origin: left`）平滑铺开；
  - 横向滚动表格时，左侧冻结列右侧边缘的柔和分隔投影随 `scrollLeft > 0` 平滑淡入，回滚到 0 自动消失；
  - 鼠标悬停任意分数格时，行内 `≡` 子指标图标轻柔浮现，点击展开带轻弹簧缩放的精细浮层（`scale(0.96) → 1`）；
  - 勾选表格行加入对比时，底部 **Floating Compare Tray** 从屏幕底缘弹簧升起，新增/移除对象 Chip 自动平滑重排占位。

### 2.3 ARC Prize 式效率前沿与同族轨迹动效（ECharts 5 Custom + SVG）
- **基准快切与横轴切换平滑迁移**：点击下方基准胶囊（`DeepSWE 1.1 | Terminal 2.1 | GAIA | WildClawBench`）或切换 X 轴（`耗时 P50 → Total Tokens → 工具步数 → 评测日期`）时，已有相同实体的散点平滑移动至新坐标，新增点缩放淡入，帕累托前沿曲线沿坐标序列顺滑重绘。
- **同族焦点聚光（Focus Spotlight）**：鼠标悬停在任意点或其模型标签上时：
  - 该点所属同族虚线轨迹（如同一模型的不同 Thinking 档位 / Checkpoint / Harness 序列）高亮加粗并产生柔和外圈脉冲光环；
  - 其余无关序列平滑降至 `opacity: 0.20`，让同族演进路径在密集散点中瞬间跃出；
  - 跟随卡片带毛玻璃背板（`backdrop-filter: blur(10px)`）平滑跟随，展示精确分母、覆盖率与四阶段耗时微条。
- **框选入对比篮动效**：用户在散点图上拖拽框选多个点并点击「加入对比」时，选中点短暂闪烁确认态并同步驱动底部对比篮计数数字弹跳更新。

### 2.4 全局 `⌘K`、抽屉与下钻空间感
- **`⌘K` 命令面板**：背景遮罩 `140ms` 柔和模糊淡入，面板从 `scale(0.97) translateY(-8px)` 弹出；键盘 `↑/↓` 切换高亮项带顺滑背景跟随。
- **侧滑抽屉与双栏轨迹 Diff**：点击能力树节点打开 Benchmark 速览抽屉、或点击逐题 Diff 打开全屏双栏对比抽屉时，以 `240ms --ease-out-expo` 从右侧滑入，关闭时原路收回并保持原页面滚动位置零偏移。
- **ATIF 轨迹、网关窗口与效率快捷键（防百步级联重排护栏）**：
  - 单步点击折叠/展开 `reasoning_content` 与长工具输出采用 `160ms` 平滑高度过渡（CSS `grid-template-rows: 0fr → 1fr`）；
  - **长轨迹批量防掉帧保护**：在 100+ 步长轨迹上按快捷键 `t` 一键全局展开/折叠所有思考链时，**自动跳过 `grid-template-rows` 过渡走 0ms 瞬切**，避免百步同时级联重排导致掉帧，稳守 60fps；
  - 工具调用与观察配对卡悬停双向联动高亮；点击左侧 Mini-map 步骤跳转或双栏 Diff 差异跳转时，右侧目标步骤平滑居中并产生 `1.0s` 品牌浅蓝脉冲标记；内置高频效率快捷键（`j/k` 切题、`[`/`]` 切 Trial、`t` 批量切换思考链）。
- **产品完整性与用户偏好持久化（去 Demo 化）**：全站所有页面提供分场景空态/缺测/无权限/报错恢复卡、操作反馈 Toast 轻提示、用户本地偏好持久化（明/暗主题、列预设与顺序、侧栏折叠、精简动效开关、最近浏览记录）以及宽屏桌面（1440px+）与常规笔记本（1280px）的精致响应式布局适配。

### 2.5 实时执行与进度反馈动效（M3 Execution & Live Stream）
- **运行中脉动指示灯**：`running` 与 `running_placeholder` 徽章配备柔和同心圆呼吸波纹（CSS `ping` 降速为 `2.2s` 优雅脉动，不刺眼）。
- **三连进度环平滑插值**：`Overall / Scored / Error` 环形进度条随 SSE 事件以 `360ms` 平滑插值推进 `stroke-dashoffset`，中心数字同步平滑更新。
- **新回收试验高亮淡出**：每当 SSE 推入一条 `trial_finished` 新行，新行顶部淡入并带 `1.2s` 柔和绿/黄/红提示底色渐褪至常规表面色；收到 `reconciled` 终态对账事件时，状态徽章平滑切换为打勾确认态。

### 2.6 骨架屏与零布局抖动（Zero-CLS）规范
- 所有异步加载区（总榜表、图表卡、实体档案、Trial 显微镜、能力树）强制使用**等几何尺寸的柔和光扫骨架屏（Shimmer Skeleton）**，禁止空白页面或居中大转圈导致内容突然下跳。
- **前端性能硬指标**：首次内容绘制后累积布局偏移 **CLS < 0.05**，交互响应延迟 **INP < 100ms**，动效期间稳定 **≥ 55–60fps**（纳入 `07-2` `E-03` 自动化验收）。

---

## 3. 前端动效实现技术栈约束

- **核心库**：`motion`（Framer Motion 轻量版，仅引入 `LazyMotion` + `domAnimation` 特性包，gzip 增量 < 18 KB，严控前端打包体积不影响 197 750 MB 预算！）+ 原生 CSS GPU Transitions + **Apache ECharts 5 内置 `universalTransition` / `animationDurationUpdate`**。
- **统一封装组件**：前端在 `src/components/motion/` 统一沉淀 `<PageTransition>`、`<StaggerContainer>`、`<AnimatedNumber>`、`<SpotlightCard>`、`<SlidingTabs>`、`<CollapsiblePanel>` 与 `<ReducedMotionProvider>`，确保全站 10+ 模块动效语言 100% 统一且易于维护。
