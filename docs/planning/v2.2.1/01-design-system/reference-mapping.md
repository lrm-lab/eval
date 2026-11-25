# 01-3 · 参考平台与核心参考图（视觉与交互组织）→ LRM Eval 真实落地映射与裁剪清单

> **核心原则（落实 `@caozs2` 硬红线）**：外部 7 大平台、最新 3 张核心参考图及 16 张 `Le-AIEval` 截图**仅提供视觉排版、交互控件与动效组织灵感**；本平台**绝不照搬任何 Harbor / `lrm-eval` 无法获取的数据字段或模块**。所有保留项均在 [`03-data-and-methodology/01-data-inventory-and-coverage.md`](../03-data-and-methodology/01-data-inventory-and-coverage.md) 登记了精确真实来源。

---

## 1. `@caozs2` 重点指定参考图与外部参考站落地映射（100% 对齐 Harbor 真实数据）

| 参考来源 / 核验参考图 | 借鉴的视觉/交互/业务组织精华 | 落地页面与模块 | 面向 Harbor / `lrm-eval` 真实数据的本土化改造 | 对应验收用例 |
|---|---|---|---|---|
| **★ 新图 1 · 三线业务目标 14 项核心基准表** *(Agentic 4 项提分 vs Text 7 项 + Vision 3 项防退化)* | ① `Agentic` 4 项作为提升主线看 `+Δ pp` 与相对涨幅；<br>② `LLM — Text` 7 项 + `Vision — Image` 3 项作为防退化护栏看可调退步阈值；<br>③ 面向未来更多分类与数据集的配置化扩展 | `taxonomy.yaml (goal_type)`、`Home / Compare Gain vs Guardrail 结论卡`、`Datasets 能力树` | 明确 14 项在 197 的真实首期状态（11 项已有实跑/历史、2 项原始数据可见待接、1 项待定版本）；修正 `MM-IFEval` 能力名为「多模态指令遵循」；全站榜单与树图 100% 由 `taxonomy.yaml` 动态驱动、零硬编码 | `A-14`, `E-01` |
| **★ 新图 2 · ARC Prize 效率前沿与同族轨迹图** *(`ARC-AGI-3` Score vs Cost 散点+虚线连线)* | ① `VERIFIED` 徽章 + 顶部居中大标题 + 深邃网格散点；<br>② 同族点间彩色虚线轨迹 + 清晰标签；<br>③ 底部基准快切胶囊 + 筛选下拉 + X 轴切换 + 右下角导出 PNG/SVG | `Leaderboard 散点视图`、`Agentic Benchmark 档案页图表 Tab` | **100% 绑定 Harbor 真实字段**：纵轴得分 `%`，默认横轴为共同有效题 **单题总耗时 P50 / 均值 (s)**（支持对数轴），可切 **总 Tokens / 工具步数 / 完成日期 / 已记录成本 `recorded_native`（有账单对账再标实付，且仅满覆盖点入前沿）**；底部快切 `DeepSWE 1.1 \| Terminal 2.1 \| GAIA \| WildClawBench`；**197 实测 Terminal-Bench 2.1 六组原生作业已有 71 道共同有效题（25.35%–54.93%，P50 574–1,091s）立即可验收** | `A-15`, `E-03` |
| **★ 新图 3 · LLM Stats 高密主榜与全套基础控件** *(双层赛道导航 + 胶囊快筛 + `Columns`/`Copy link`/`Compare`/4 视图图标组 + 最高分金色高亮)* | ① 一层赛道 + 二层能力快切 + 筛选栏；<br>② 右上角 `Columns`、`Copy link`、`Compare` 及 `Table / Trend / Bar / Scatter` 同上下文视图切换；<br>③ 冻结左列、Top 3 金银铜勋章、每列最高分高亮、表头 `?` 与 `↑↓`、缺测 `—`、点数字下钻 | `Leaderboard 主榜（M1 必验基元）`、`全站 EvalDataTable` | 筛选下拉仅保留 Harbor 真实字段（`Model Family`、`Harness`、`Runner`、`Version/Split`、`Formal Only`、`Thinking`、`Has ATIF`、`Has Gateway`、`Source`、`Time`）；**严格隐藏无数据源的 `License / Country / Hardware / Price`**；行粒度升级为 `Model × Harness@Version` | `A-01`, `A-05`, `E-01`, `E-03` |
| **Epoch AI** (`/benchmarks`, `/eci`, `/about`) | `Graph ↔ Table` 切换、时间/Checkpoint 演进前沿、置信区间误差线与方法学透明度 | `Benchmark 档案 Trends`、`Dynamic Comparison`、`RL/SFT Selection` | 仅在核验 `checkpoint` 身份、同题集且同协议记录间连线；多尝试 `round1` 展示 Wilson/Bootstrap 95% 置信区间 | `A-07` |
| **Artificial Analysis** (`/models`, `/coding-agents`, `/recommend`) | 帕累托效率前沿、Token 构成可视化、难题画廊、约束选型推荐器 | `Diagnostics`、`Benchmark 难题画廊`、`Model Recommender` | 默认效率轴用真实耗时与 Token；Token 堆叠条必须先通过包含语义校验；难题画廊基于真实跨批次最低通过率 + `instruction.md` | `A-04`, `A-12` |
| **DesignArena** (`/leaderboard`, `/about`) | 细粒度场景分榜与两两 Win/Loss 胜负记录 | `Dynamic Comparison (Win/Tie/Loss)` | 用严格交集 `common_valid_scored_tasks` 客观结果计算 4 格胜负矩阵，点击直开双栏轨迹 Diff | `A-02`, `A-03` |
| **OpenRouter Rankings** (`/rankings`, `/methodology`) | 时间窗、绝对/占比切换与 Tool Calls 画像 | `Diagnostics (Tool Call 画像)`、`Harness 档案` | 聚合 ATIF `tool_calls[].function_name` 与 `is_error`（仅覆盖记录工具事件的 Harness，标注覆盖分母） | `A-12` |
| **SuperCLUE** & **Le-AIEval (16 截图)** | 中英双语能力名、雷达图、工作台侧栏、能力树 Minimap、Execution 3 进度环、Report 子分 `≡` 浮层、Offline Calc 3 Tab | `工作台骨架`、`Datasets`、`Execution`、`Report`、`Toolkit` | 仅保留与 `lrm-eval` 真实数据结构匹配的交互骨架，所有无 Harbor 源的截图页面已彻底删除 | `A-09`, `A-10`, `A-11`, `E-02` |

---

## 2. 明确裁剪清单（参考站有、但 Harbor 无法获取，已 100% 移出规划）

| 原参考来源 | 已明确删除项 | 删除原因（Harbor / 197 核实结论） | 替代处理（若适用） |
|---|---|---|---|
| `Le-AIEval ref7–ref9` | **`Small Model Test`**（Domain/Intent/Slot/Language 5 套小模型分类测试执行、报告与混淆矩阵） | Harbor / `lrm-eval` 无此测试集与分类框架 | 彻底删除页面、表与 API |
| `Le-AIEval ref10–ref14` | **`On-Device Test`**（手机/平板/PC 端侧集群调度、在线 IP、端侧 GPU VRAM/芯片硬件遥测） | Harbor / `lrm-eval` 无端侧设备与硬件数据提供方 | 彻底删除页面、表与 API；未来仅按需做容器级 cgroup 资源遥测 |
| `Le-AIEval ref2` | 能力树 **`Audio / Speech`** 模态分支 | 197 当前无音频评测题库与运行器 | 仅保留 `Agent / Text / Vision / Synthetic` |
| `Le-AIEval ref16` | **`Qira Param Tuning`** 独立调参工具及端侧 TTFT CSV 上传表 | 脱离 `lrm-eval` 框架且无历史 TTFT 源 | 改为绑定真实 `batch_id` 的 `/toolkit/params` 运行参数对照台账 |
| `Le-AIEval ref6` | 脱离 `direct.py` 的任意 CSV/XLSX 通用 **`Prompt Optimization`** 上传器 | Harbor 只有原生 `direct.py` (`cases.jsonl + score.py`) | 改为绑定 `cases.jsonl + 白名单 score.py` 的 `/eval/direct-lab` |
| `LLM Stats` / `OpenRouter` | **Human Preference / Arena ELO、Knowledge Cutoff、License、Release Date、Country、Hardware、Price 下拉、Market Share、Top Apps** | 公网商业/众包字段，Harbor 产物中不存在 | 隐藏不展示（仅当未来接入可信内部注册表后按配置开启） |
| `Artificial Analysis` | **Safety Refusals（安全拒答率）** 与 **Reward Hacking / 提前摆烂定性判定** | Harbor 无安全分类器与作弊意图标注器 | 诊断页仅展示 4 项纯客观 ATIF/Gateway 证据线索与覆盖分母 |
| `Artificial Analysis` / `Harbor` | **虚构实付成本账单** & **GUI 录屏主功能位** | 197 原生已记录 `cost_usd` 极稀疏且未账单对账；当前 4 套 Agent 基准为 CLI/代码沙箱 | 默认效率轴用真实耗时 P50/均值与 Tokens；`recording` 降级为未来桌面题库可选透传备注 |
