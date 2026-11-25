# 02-2 · Datasets 三线能力树图（零代码扩展）、版本化媒体匹配与 Task 浏览器

> **关联元数据**
> - **真实数据源**：`config/taxonomy.yaml`（含 `goal_type` 与热加载配置）+ `/home/share-data/tianxi_benches/framework/harbor.toml` + `datasets/`（热层）+ `bundles/*.tar(.zst)` 元数据冷缓存 + `2026-11-20/` 历史题库统计 + `/home/share-data/dataset/` 原始资产目录清单
> - **核心 API**：`GET /api/v1/datasets/tree`、`GET /api/v1/datasets`、`GET /api/v1/datasets/{dataset_release_id}`、`GET /api/v1/datasets/{dataset_release_id}/tasks`、`GET /api/v1/datasets/{dataset_release_id}/tasks/{task_id}`、`POST /api/v1/admin/taxonomy/reload`
> - **权限要求**：能力树与公开题面 `Viewer+`；`holdout/` 留出题面需 `dataset:holdout_read`；标准答案 `answer` / `solution/` / `tests/` 需 `dataset:answer_read`
> - **交付里程碑**：**M1（三线能力树与题库/题目浏览器）**；7.5 GB `.tar.zst` 冷层抽取随 M0/M1 异步后台就绪

---

## 1. 页面布局、三线目标树与零代码扩展机制（`/datasets`）

顶部提供三大视图切换：`交互式能力树图（默认）` | `结构化题库清单（Table）` | `197 共享盘资产全景`。

### 1.1 交互式四级能力树图（突出 14 项三线核心基准 + 无 Audio）

```text
+---------------------------------------------------------------------------------+
|  [过滤条] 目标: (All) (Agentic提分) (Text/Vision护栏) (合成训练) (历史扩展)        |
|  状态: (已接入/实跑) (历史基线) (原始数据可见·未接入) (待定版本接入)               |
|  [搜索节点关键字...]  [展开全部 / 折叠到能力层]  [重置缩放]  [切换为表格视图]        |
+---------------------------------------------------------------------------------+
|                                                                                 |
|                 ┌─ Agentic [主提分·optimize_gain]                               |
|                 │   ├─ DeepSWE 1.1 (113) 🟢 [Harbor + 2026-11-20]               |
|                 │   ├─ Terminal 2.1 (89) 🟢 [Harbor + 2026-11-20]               |
|                 │   ├─ GAIA val (165) 🟢 [Harbor + 2026-11-20]                  |
|                 │   └─ WildClawBench (60) 🟢 [Harbor + 2026-11-20]              |
|  LRM Eval ──────┼─ LLM — Text [防退化护栏·non_regression_guardrail]             |
|  Taxonomy       │   ├─ HLE / IFBench / Multi-LCB / AA-LCR v1.1 🟣 [历史明细]     |
|  (配置热驱动)    │   ├─ MMLU-Pro / BFCL-v4 (tool call) ⚪ [原始数据可见·未接入]  |
|                 │   └─ Global-MMLU-Lite 🔵 [上游可定位·待定版本接入]             |
|                 ├─ Vision — Image [防退化护栏·non_regression_guardrail]         |
|                 │   ├─ CMMMU (中文多学科) 🟢 [VLMEvalKit + 2026-11-20]          |
|                 │   ├─ MMMU-Pro (10c) (英文多学科) 🟢 [VLMEvalKit + 2026-11-20] |
|                 │   └─ MM-IFEval (多模态指令遵循) 🟢 [VLMEvalKit + 2026-11-20]   |
|                 ├─ Synthetic [合成进化·synthesis_loop]                          |
|                 └─ Extended Baselines [其余2026-11-20历史基准 · 默认折叠可展开]  |
|                                                             +----------------+  |
|                                                             |   [Minimap]    |  |
|                                                             +----------------+  |
+---------------------------------------------------------------------------------+
```

### 1.2 14 项核心基准首期接入状态精确台账

| 三线分组 (`goal_type`) | 基准名称 | 规范化能力说明 | 197 当前真实状态 | 平台展示状态徽章 |
|---|---|---|---|---|
| **Agentic** (`optimize_gain`) | `DeepSWE 1.1` | Agentic software engineering | Harbor 题包 + 2026-11-20 三组明细（48/78/113，共同 48/39） | 🟢 已接入实跑 |
| **Agentic** (`optimize_gain`) | `Terminal 2.1` | Agentic terminal operation | Harbor 题包 + 2026-11-20 六组原生作业（89 题，六组共同 71 题） | 🟢 已接入实跑 |
| **Agentic** (`optimize_gain`) | `GAIA` | Multi-step reasoning & tool-assisted problem solving | Harbor 题包 + 2026-11-20 两组明细（165 题，共同有效 118） | 🟢 已接入实跑 |
| **Agentic** (`optimize_gain`) | `WildClawBench` | Real-world, multi-domain agentic tasks | Harbor 题包 + 6 组 60 题（17 维子指标） | 🟢 已接入实跑 |
| **LLM — Text** (`non_regression_guardrail`) | `HLE` | Multi-disciplinary reasoning | 2026-11-20 历史全量明细可用 | 🟣 历史基线可用 |
| **LLM — Text** (`non_regression_guardrail`) | `IFBench` | Instruction following | 2026-11-20 历史全量明细可用 | 🟣 历史基线可用 |
| **LLM — Text** (`non_regression_guardrail`) | `Multi-LCB` | Code generation | 2026-11-20 历史全量明细可用 | 🟣 历史基线可用 |
| **LLM — Text** (`non_regression_guardrail`) | `AA-LCR v1.1` | Long-context reasoning | 2026-11-20 历史全量明细可用 | 🟣 历史基线可用 |
| **LLM — Text** (`non_regression_guardrail`) | `MMLU-Pro` | General knowledge | 197 `/home/share-data/dataset/` 原始资产可见，暂未验证跑分 | ⚪ 原始数据可见·未接入 |
| **LLM — Text** (`non_regression_guardrail`) | `BFCL-v4 (tool call)` | Tool calling | 197 `/home/share-data/dataset/` 原始资产可见，暂未验证跑分 | ⚪ 原始数据可见·未接入 |
| **LLM — Text** (`non_regression_guardrail`) | `Global-MMLU-Lite` | Multilingual cultural knowledge audit | 上游 `CohereLabs/Global-MMLU-Lite` 可定位，待固定版本/语言/split 接入 | 🔵 待定版本接入 |
| **Vision — Image** (`non_regression_guardrail`) | `CMMMU` | Multi-disciplinary knowledge (Chinese) | 2026-11-20 历史 + 最新 `vlmevalkit` 批次；87/87 唯一原图命中 | 🟢 已接入实跑 |
| **Vision — Image** (`non_regression_guardrail`) | `MMMU-Pro (10c)` | Multi-disciplinary knowledge (English) | 2026-11-20 历史 + 最新 `vlmevalkit` 批次；74/80 含重复文件名降级规则 | 🟢 已接入实跑 |
| **Vision — Image** (`non_regression_guardrail`) | `MM-IFEval` | **多模态指令遵循 (Multimodal Instruction Following)** *(修正原表公式残留)* | 2026-11-20 历史 + 最新 `vlmevalkit` 批次；原始 TSV `id/index/image` | 🟢 已接入实跑 |

### 1.3 面向未来更多分类与数据集的「零代码扩展」规范
- **全站零硬编码枚举**：主榜赛道 Tab、能力树节点、对比构建器筛选树、ARC 效率图基准快切胶囊均 100% 由 `GET /api/v1/datasets/tree` 与 `config/taxonomy.yaml` 动态下发渲染。
- **热加载与自动发现**：管理员编辑 `taxonomy.yaml`（或在 `/admin/health` 配置界面新增 Track / `goal_type` / Capability / Dataset / 协议 / 量纲）后调用热加载即刻生效；共享盘一旦发布尚未登记的新数据集，热层自动将其收录至 `Custom / Unclassified` 可立即浏览与对比，登记后无缝归位！

---

## 2. Task 题面与元数据浏览器（`/datasets/:datasetReleaseId/tasks/:taskId`）

- **自动抽取真实元数据**：从 `bundles/*.tar(.zst)` 与 `datasets/` 的 `task.toml` 自动提取 `difficulty`、`category`、`tags`、`language`、`repository_url`、`display_title`、`split`、`source`、`environment` 与 CPU/RAM/超时配额。
- **单题预览与权限隔离**：公开题面 Markdown 渲染 + 历史全量评测成绩小表（点击任意行直达 Trial 轨迹）；`holdout/` 题面严格需 `dataset:holdout_read`，标准答案与测试脚本需 `dataset:answer_read`。

---

## 3. 版本化多模态媒体匹配规则与 197 原始资产全景

| 数据集与版本 | 197 实测核查结果 | 版本化匹配规则 (`config/media_matching_rules.yaml`) | 命中 / 缺失 / 多重命中处理 |
|---|---|---|---|
| **CMMMU** (`2026-11-20`) | 抽样 80 行 87 条图像引用，**87/87 唯一文件名命中** | `rule_v1`: 规范化 basename 精确唯一命中 | `unique_hit` 渲染原图；`missing` 占位徽章 |
| **MMMU-Pro-10c** (`2026-11-20`) | 抽样 80 行命中 **74/80，存在重复文件名** | `rule_v1`: 结合 `subject/split` 上下文匹配，仍多命中则标 `ambiguous_multi_hit` | **`ambiguous_multi_hit` 严禁盲猜**，渲染安全占位提示；`missing` 占位徽章 |
| **MM-IFEval** (`2026-11-20`) | 结果 JSONL 无图路径，原始 TSV 含 `id/index/image` | `rule_v1`: 按 `sample_id -> TSV(id/index) -> image` 唯一映射 | 唯一命中渲染原图，否则占位降级 |
| **新版 `vlmevalkit` 批次** | `result.json` `vlmeval_result.request_messages` 含内联 base64 图 | `runner_inline_media`: 索引期剥离 base64，详情页经 `/media/{idx}` 按权限流式解码 | 100% 呈现模型实际所见画面且不膨胀 SQLite |
| **其余 197 原始目录** | `/home/share-data/dataset/` 未注册目录、`/home/share-data/agentevals/`、`rsi_eval/`、`claw_eval/` | 在「197 资产全景」Tab 如实标注「原始数据可见、未接入评测」或「历史外部归档」 | 不混排、不可误触发在线执行 |
