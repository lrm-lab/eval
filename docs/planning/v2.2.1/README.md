# 天禧 · LRM Eval 前端展示与分析平台开发设计文档（v2.2）

> **现行开发要求**见 [v2.2.1 后续产品变更与验收](CURRENT.md)。其中列出的已上线行为与待发布要求覆盖相应的原设计条目。

> **文档状态**：v2.2 终审收口版（完整对齐 `@caozs2` 全部产品/数据/动效要求，并全量闭环 `@theshy` 与 `@369` 全部轮次 197 实机源码、权限先于去重与三类分母精度审校意见）  
> **编写人**：`@chovy`（开发文档规划）  
> **评审人**：`@caozs2`（项目负责人）、`@369`（全栈开发）、`@theshy`（代码审核）  
> **协作代码仓**：`197 (LRM-02): /home/share-data/eval/`（CIFS 协作源码与构建缓存；生产发布包、SQLite WAL 与受保护密钥部署于服务账号独占本地 ext4 目录；Runner 原始工作区与离线待审区部署于经权限验证的隔离存储，绝不落 `0777` CIFS）  
> **对接评测框架**：[`lrm-lab/lrm-eval`](https://github.com/lrm-lab/lrm-eval) 与 197 共享盘 `/home/share-data/tianxi_benches/`

---

## 🌳 完整文档树地图（20 份模块化详设）

本文档按「**设计系统（含产品级动效）→ 模块页面详设 → 数据与方法学 → 数据库与 API 契约 → Harbor 与框架补丁 → 架构安全与 197 运维 → 路线图与全链路验收**」7 级树形结构组织。**全站每一个页面字段、指标、图表、状态徽章均可双向追溯到真实底层文件/字段或明确的可开发采集点**（总对照表见 [`03-data-and-methodology/01-data-inventory-and-coverage.md`](03-data-and-methodology/01-data-inventory-and-coverage.md)）：

```text
lrm-eval-web-plan/
├── README.md                                          # 总索引、三线业务目标(14核心基准)、Harbor数据边界(含删除项公示)、核心不变量与路线
├── 01-design-system/
│   ├── tokens-and-components.md                       # LLM Stats 主视觉 Token、明暗双主题、高密主榜控件、Token语义护栏、⌘K与全站路由
│   ├── motion-and-microinteractions.md                # 产品级网页动效与微交互规范（Motion Tokens、ECharts形变、聚光轨迹、60fps与无障碍）
│   └── reference-mapping.md                           # 参考站与新参考图（仅借视觉/交互组织）→ Harbor 真实落地映射与明确裁剪清单
├── 02-pages/
│   ├── 01-home-leaderboard-and-profiles.md            # Home(Gain vs Guardrail结论卡) + LLM Stats主榜控件 + ARC Prize效率前沿轨迹图 + 实体档案
│   ├── 02-datasets-and-tasks.md                       # Datasets 四级能力树图(三线目标/零代码扩展/Minimap) + Task浏览器 + 版本化媒体匹配
│   ├── 03-reports-and-trial-inspector.md              # 评测报告中心 + 批次详情 + Trial显微镜（Harbor/Direct/VLMEvalKit三运行器全兼容）
│   ├── 04-dynamic-comparison.md                       # 自由动态对比（Gain vs Guardrail对比/1v1对决/交集双分母/实时 vs 冻结快照/双栏Diff/导出）
│   ├── 05-diagnostics-and-behavior.md                 # 深度诊断：研究员 5 步定位→证据流、14类故障聚类、子分热图、帕累托、工具画像与客观轨迹线索
│   ├── 06-execution-and-direct-lab.md                 # 受控评测执行（run.py 真实状态机 + 实时事件流 vs 共享快照）+ Direct原生验证台(cases.jsonl+score.py)
│   ├── 07-synthetic-and-training.md                   # 合成题工坊 + RL/SFT 四通道（pass@k/pass^k/CI）+ Training & OPD 监控（含空目录验证前提）
│   └── 08-toolkit-and-admin.md                        # Offline Verify(3 Tab) + Harbor/Direct/VLMEvalKit参数对照台账 + Manuals/方法学 + Admin运维页
├── 03-data-and-methodology/
│   ├── 01-data-inventory-and-coverage.md              # "所有 Harbor" 可验收清册 + 【页面字段 ↔ 真实数据来源（已有/待采/已删）双向总表】
│   └── 02-taxonomy-and-scoring-methodology.md         # 三线目标四级分类(零代码扩展)、4维正交口径、原生reward_valid协议保留、Macro Avg与Token校验
├── 04-api-and-schema/
│   ├── 01-sqlite-ddl-and-indexing.md                  # 完整 SQLite 建表 DDL（dataset_slug / 运行中占位 / reward_valid策略 / 前向兼容）+ 冷热索引
│   └── 02-rest-sse-api-contracts.md                   # 全站 REST/SSE 路由表 + 8 大核心接口精确 Request/Response JSON Schema + 错误码字典
├── 05-harbor-and-framework/
│   └── 01-harbor-audit-and-upstream-patch.md          # Harbor 0.26.0 源码审计 + VLMEvalKit 第三运行器解析 + run.py:publish() 增强补丁与实时事件协议
├── 06-architecture-security-ops/
│   └── 01-architecture-security-and-197-ops.md        # 系统架构、HTTPS/CSRF/6项Scope零旁路、Runner私有工作区与0777 CIFS彻底隔离、750 MB预算
└── 07-roadmap-and-qa/
    ├── 01-milestones-and-collaboration.md             # M0–M3 里程碑、阶段准出门槛（Gate）与 4 人分工协同
    └── 02-e2e-acceptance-matrix.md                    # 全链路黄金验收矩阵（含TB2.1 71题散点/230条reward_valid/三线验证/动效与安全断言）
```

---

## 1. 产品愿景、三线业务目标与 Harbor 真实数据边界

面向 `lrm-lab/lrm-eval`（天禧大模型与 Agent 统一评测与训练框架），在 197（`LRM-02`）构建一座**美观流畅、产品级动效完备、严谨可审计且 100% 扎根真实数据**的统一评测展示与分析平台。

### 1.1 三线业务目标与首期 14 项核心基准（配置驱动、零代码无限扩展）
按 `@caozs2` 明确的核心评测业务目标，全站 `taxonomy.yaml` 引入 **`goal_type`** 语义，并支持后续随时零改前端代码接入新分类与新数据集：
1. **`Agentic`（主提分战场 · `goal_type = "optimize_gain"`，重点衡量提升百分比）**：
   - **`DeepSWE 1.1`**（Agentic software engineering）、**`Terminal 2.1`**（Agentic terminal operation）、**`GAIA`**（Multi-step reasoning and tool-assisted problem solving）、**`WildClawBench`**（Real-world, multi-domain agentic tasks）。
   - 作为首页、总榜与 ARC Prize 式效率前沿轨迹图的第一优先入口，重点突出相对基线的绝对分差（`+Δ pp`）、相对变化（`%`）、覆盖率与逐题轨迹证据。
2. **`LLM — Text`（7 项 · `goal_type = "non_regression_guardrail"`，防退化护栏，维持能力不下降）**：
   - `HLE`、`IFBench`、`Multi-LCB`、`AA-LCR v1.1`（197 已有历史明细）；`MMLU-Pro`、`BFCL-v4 (tool call)`（197 `/home/share-data/dataset/` 原始资产可见，待接跑分）；`Global-MMLU-Lite`（上游 `CohereLabs/Global-MMLU-Lite` 可定位，待固定版本/语言/split 接入）。
3. **`Vision — Image`（3 项 · `goal_type = "non_regression_guardrail"`，防退化护栏，维持能力不下降）**：
   - `CMMMU`（中文多学科知识）、`MMMU-Pro (10c)`（英文多学科知识）、`MM-IFEval`（**多模态指令遵循 / Multimodal Instruction Following**）——均有 `2026-11-20` 历史样本与最新 `vlmevalkit` 批次。
4. **合成训练闭环（`synthesis_loop`）与其余历史扩展基准（`extended_baseline`）**：无缝共存并支持自由勾选展开；未来新增任意数据集与分类仅需更新 `taxonomy.yaml`（或由共享盘自动发现归入 `Unclassified`），**前端零硬编码**！

### 1.2 参考站定位与明确删除的无源项公示
- **外部 7 站、ARC Prize 效率轨迹图、LLM Stats 控件与 Le-AIEval 截图**：仅提供视觉语言、交互控件与动效组织参考；**所有页面字段、指标、图表均 100% 绑定 Harbor / `lrm-eval` / 197 真实已有或明确可开发采集的数据**。
- **已彻底删除的无源项（从导航、页面、DDL、API 与验收全面移除）**：
  1. 整块删除 **`Small Model Test`** 与 **`On-Device Test`**（无 Harbor 数据源，已删 `specialized_reports` 表与 `/tracks/*` 路由）；
  2. 删除能力树 **`Audio / Speech`** 分支；
  3. 删除公网榜单商业/社区与无分类器支撑字段：Human Preference / Arena ELO、Knowledge Cutoff、License、Release Date、Country、Hardware、Market Share、Top Apps、Safety Refusals，以及无分类器支撑的「Reward Hacking / 提前摆烂」定性判定（诊断页改用纯客观 ATIF/Gateway 证据线索与覆盖分母）；
  4. 删除脱离框架的 **`Qira Param Tuning`** 独立调参工具与任意 CSV/XLSX 通用 **`Prompt Optimization`** 上传器（分别重聚焦为绑定真实 `batch_id` 的 `/toolkit/params` 参数对照台账与绑定 `direct.py` 原生 `cases.jsonl + score.py` 的 `/eval/direct-lab`）；
  5. 移除 GUI 录屏主功能位与虚构计费账单（默认效率轴用真实 **单题耗时 P50/均值** 与 **Tokens / 工具步数**，原生 `cost_usd` 标为「已记录成本」，有账单对账再标「实付」）。

---

## 2. 必须严守的 9 条工程与口径不变量

1. **有效 0 分 ≠ 未评分 ≠ 运行故障**：`status == "scored"` 且 `reward == 0` 计入有效均分；`failed / timeout / cancelled / unscored` 的 `reward` 为 `null`，无有效分批次（如 `vlmevalkit` 全失败或纯推理 `mode="infer"`）在榜单留白 `—`，绝不填 `0`。
2. **Harbor 原生 `reward_valid` 与 `lrm-eval` 共享导出双协议独立保留**：197 上 Terminal-Bench 2.1 534 条原生 trial 中有 490 条 `reward_valid=true` 且主分有限，其中 **230 条同时带 `exception_info`**（Harbor 0.26.0 策略明确允许有效分），而 `run.py:98-106` 导出按自身规则将带异常的主分置空。原生适配器必须保留 `reward_valid`、原生评分与异常策略两个维度，共享导出按 `report.py` 口径保留；跨来源同 UUID 去重时保留各来源评分语义并**分协议统计，严禁按通用“有异常就无分”改写历史原生作业**。
3. **4 维正交口径与三线目标判定**：每个批次独立记录 `run_scope`（严禁用 80% 或官方分推定全量）、`effective_coverage`、`scoring_protocol`、`official_status`（兼容 `vlmevalkit` 仅出汇总 `official-result.json` + `vlmevalkit/metrics.json` 而逐题无分的场景）；防退化护栏缺测或不可比一律标为「无法判定」。
4. **严格可比性、共同有分 Macro Avg 与严格交集双分母**：`Best`、`Δ` 与 `Comparative Macro Avg` 仅在同协议且所有参与列共同有分基准集上等权计算；严格交集固定展示 `common_covered_tasks` 与 `common_valid_scored_tasks`（DeepSWE 48/39、GAIA 165/118、Terminal-Bench 2.1 六组共同 71 题效率图基准）。
5. **三运行器前向兼容 + `.publish.lock` 正确处理 + 内容摘要防混合版本**：前向兼容 `harbor`、`direct`、`vlmevalkit` 及未知新枚举；含空格括号的 `manifest.dataset` 规范化为 `dataset_slug` 并保留 `raw_dataset_name`；兼容仅含 `batch-status.json` 的运行中目录；**因 `run.py:64` 释放 `flock` 后不删 `.publish.lock`，严禁仅凭锁文件存在延后索引**，一律按 `publish-manifest.json` SHA-256 或一致性重算判断就绪。
6. **Token 包含语义先验校验与诚实延迟**：兼容 `reasoning_tokens`（329 份）与 `reasoning_output_tokens`（3 份）双键；按来源验证 `cached ⊆ input`、`reasoning ⊆ output` 后才绘制互斥 4 段堆叠条；绝不用总 Trial 时长替代逐请求 `TTFT / TPS`。
7. **197 三区存储隔离与冷热两层索引**：① 协作源码/缓存/筛选脱敏公开导出放 CIFS；② 生产发布包（静态 `uv` 精简 `.venv` + 扁平 `npm` `dist/`）、SQLite WAL 与 `0600` 密钥放本地 ext4（**统一本地峰值预算 ≤ 750 MB**，或切换独立 PostgreSQL）；③ **Runner 原始 `--work` 与离线待审上传区严禁落 `0777` CIFS，必须落经 POSIX 权限验证的 `0700` 隔离私有存储**。热层首扫 < 5s / 15s 增量 < 200ms；7.7 GB 历史 JSON 与 7.5 GB `.tar.zst` 走冷层异步缓存。
8. **传输安全、通用路径脱敏、6 项 Scope 零旁路与执行隔离**：默认 HTTPS + `COOKIE_SECURE=true` + CSRF + CLI 初始化 Admin；所有 `source.path` 与 `raw_log.path`（含 `/home/*` 与跨机 `/tmp/*`）统一掩码；6 项明细 Scope 在 5 条出口全拦截；Web 账号零 `docker` 组。
9. **产品级动效与零卡顿体验**：所有视图切换、效率散点轨迹聚光、表格交互、抽屉与实时进度环均配齐 GPU 加速动效与骨架屏，并遵守 `prefers-reduced-motion` 与 `CLS < 0.05` / `INP < 100ms` 性能红线。

---

## 3. 交付路线与关键准出门槛（摘要）

| 阶段 | 核心交付范围 | 阶段开放/准出门槛（Gate） |
|---|---|---|
| **M0 · 基建与数据底座** | 三区存储拓扑、认证/CSRF/6 项 Scope、完整 SQLite DDL（含双层数据身份/`batch_id`/`goal_type`/运行中占位/原生 `reward_valid` 保留/6 预留列）、冷热原子索引器（支持 `harbor`/`direct`/`vlmevalkit` + 媒体匹配规则）、核心只读 API | 197 存储/数据库与 Runner 私有存储方案定稿；热层首扫 < 5s / 增量 < 200ms；三运行器/TB2.1 230 条 `reward_valid`/混合版本/`.publish.lock`/去重单测全绿 |
| **M1 · 核心展示、档案、效率轨迹图与自由对比 (MVP)** | Home（`Gain vs Guardrail` 结论卡）+ LLM Stats 高密主榜与全套基础控件（赛道页签/Harbor筛选/`Columns`/`Copy link`/`Compare`/4 视图切换）+ **ARC Prize 式效率前沿与同族轨迹图** + Model/Harness/Benchmark 档案 + Datasets 零代码扩展能力树 + Report/Trial 显微镜（三运行器全兼容）+ Dynamic Comparison 与双栏 Diff/导出 + **全站产品级动效体系** | HTTPS/CSRF + 通用脱敏 + 6 项 Scope 零旁路；三线验收路径 + TB2.1 六组 71 题效率图 + DeepSWE(48/39) + GAIA(165/118) + Macro Avg + 媒体降级 + `CLS < 0.05` 动效流畅度全部通过 |
| **M2 · 深度诊断、合成闭环与工具箱** | Diagnostics（5 步流/14 类故障/WildClaw 17 子分与 `vlmevalkit` 学科热图/帕累托/工具画像/4 项客观轨迹线索）、Synthetic & Selection（`pass@k`/`pass^k`/CI）、Model Recommender、Toolkit（Offline Verify 3 Tab / 运行参数对照台账 / Manuals & 旁挂方法学）、Direct 原生验证台、完整 `/admin/health`、`run.py:publish()` 补丁 | `round1` 40×8 选题与 `pass@8` 缺测（19/40）通过；Direct 验证台网关凭据隔离、白名单评分器与隔离待审权限（`S-06`）通过审计；`run.py:publish()` 补丁验收通过 |
| **M3 · 隔离执行与 OPD 监控收口** | 隔离 Execution Runner（私有 `0700` `--work` 隔离）+ 实时事件流 SSE/断线补读/通知/续跑、Training & OPD 监控（空态 + 隔离契约夹具 + 首跑条件）、可选流式 TTFT/TPS 与容器 cgroup 遥测、压测与安全审计 | 隔离 Runner 方案通过（Web 零 `docker` 组 + 另一普通账号无法读取原始 `--work` 与待审上传，授权用户在站内可见批准细节）；本地盘峰值 ≤ 750 MB 压测通过 |

> **开工前待 `@caozs2` 确认的 2 项运维前提**：
> 1. 197 本地 ext4 独占运行目录配额（`/opt/lrm-eval` + `/var/lib/lrm-eval`，≤ 750 MB；并提请运维清理/扩容当前已用 99%（仅余 1.7 GB）的 `/` 或提供独立 PostgreSQL 实例）以及内网 HTTPS 入口（M0/M1 演示期若临时直连 HTTP 可显式配 `COOKIE_SECURE=false`）。
> 2. M3 隔离执行节点与私有 `0700` 工作区落点（Rootless Docker 或独立受控执行节点，用于隔离存放 Runner `--work` 与离线待审上传区，彻底脱离 `0777` CIFS）。
