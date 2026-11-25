# 03-2 · 能力分类体系、正交评分口径、Token 语义校验与统计方法学规范

本文档定义平台的四级能力分类（仅含真实存在模态）、4 维正交属性、严格可比性与共同有分 Macro Avg 规则、**Token 4 段包含语义先验校验规范**、`pass@k` / `pass^k` / 置信区间数学公式、跨来源去重及方法学版本管理。

---

## 1. 三线业务目标四级能力分类体系（`config/taxonomy.yaml` 零代码扩展 + Bundle 自动抽取）

```text
Level 1: 评测方向与目标 (Track + goal_type)
         · Agentic         -> goal_type = "optimize_gain" (主提分战场: 4 项核心基准)
         · LLM — Text      -> goal_type = "non_regression_guardrail" (防退化护栏: 7 项核心基准)
         · Vision — Image  -> goal_type = "non_regression_guardrail" (防退化护栏: 3 项核心基准)
         · Synthetic       -> goal_type = "synthesis_loop" (合成与进化闭环)
         · Extended        -> goal_type = "extended_baseline" (其余历史扩展基准，默认折叠可展开)
  └── Level 2: 能力维度 (Capability Dimension — 中英双语命名)
        └── Level 3: Benchmark / Dataset（以 URL-safe dataset_slug 为主键，保留 raw_dataset_name 显示）
              └── Level 4: 子域 / 题目级标签（从 bundles/*.tar task.toml 与 vlmevalkit/metrics.json 抽取）
```

> **零代码扩展规则**：前端严禁硬编码基准与分类枚举，所有菜单、能力树、榜单列组与效率图基准快切均由 `taxonomy.yaml`（支持热加载）+ 共享盘自动发现动态驱动；`MM-IFEval` 能力维度统一规范为「**多模态指令遵循（Multimodal Instruction Following）**」；`MMLU-Pro` / `BFCL-v4` 标注为「原始数据可见、未接入评测」，`Global-MMLU-Lite` 标注为「上游题库可定位、待固定版本/语言/split 接入」。

### 预置核心映射表（100% 对应 197 实测题库）

| 评测方向 (Track) | 能力维度 (Capability) | Benchmark / Dataset | 主分字段 & 量纲 | 语种 / 模态 / 运行器 |
|---|---|---|---|---|
| **Agent 沙箱能力** | 终端运维与系统工程 (Terminal & SysOps) | Terminal-Bench 2.1 (`89`) | `reward` (0/1, Resolution Rate) | EN / Agent / `harbor` |
| **Agent 沙箱能力** | 软件工程与代码修复 (SWE & Coding) | DeepSWE 1.1 (`113`) | `reward` (0/1, Pass Rate) | EN / Agent / `harbor` |
| **Agent 沙箱能力** | 开放检索与多步推理 (Web & Tool QA) | GAIA validation (`165`) | `reward` (0/1, Official / Raw) | EN / Agent / `harbor` |
| **Agent 沙箱能力** | 综合办公与多约束任务 (Real-World Office Agent) | WildClawBench (`60`, 6 子域) | `overall_score` (0~1 + 17 子指标) | EN / Agent / `harbor` |
| **合成与进化能力** | 离线文件与数据对账 (Offline Files) | `tianxi-synth-v1` (`40 train / 12 holdout`) | `reward` (0/1) | EN / Agent / `harbor` |
| **合成与进化能力** | 图工具、自提问与多跳推理 (Synthetic Reasoning) | `synthetic/*` (`toolspace`/`self-questioning`/`multi-hop`) | `reward` (0/1) | EN / Agent / `harbor` |
| **通用基础能力** | 深度知识与科学推理 (Knowledge & Science) | GPQA_diamond, HLE (`hle_llmjudge`), MMLU-Pro, Global-MMLU-Lite | `accuracy` (0~100) | EN / Text / `legacy`/`direct` |
| **通用基础能力** | 指令遵循与约束满足 (Instruction Following) | IFEval, IFBench, Multi-Challenge | `strict-acc` / `score` (0~100) | EN / Text / `legacy`/`direct` |
| **通用基础能力** | 代码与工具调用 (Coding & Tool Use) | Multi-LCB, BFCL-v4, Apex-shortlist | `pass@1` / `accuracy` (0~100) | EN / Text / `legacy`/`direct` |
| **通用基础能力** | 长上下文与记忆 (Long Context & Memory) | AA-LCR v1.1, LongMemEval (4 模式) | `accuracy` (0~100) | EN / Text / `legacy`/`direct` |
| **多模态能力** | 视觉问答与科学图表 (Visual Reasoning) | RealWorldQA, AI2D_TEST, CharXiv_reasoning, MathVista, HallusionBench | `acc` / `avg_acc` (0~100 / 0~1) | EN / Vision / `vlmevalkit`/`legacy` |
| **多模态能力** | 多模态综合知识与指令 (Multimodal Knowledge & IF) | CMMMU (ZH), MMMU-Pro (10c), MMBench_v1_1_CN (ZH), MM-IFEval | `acc` (0~100 / 0~1) + 学科分项 | ZH+EN / Vision / `vlmevalkit`/`legacy` |
| **多模态能力** | OCR 与文档识别 (OCR) | OCRBench | `score_of_1000` (归一化 `/10`，保留原值) | Multi / Vision / `legacy` |

> **带空格与括号数据集名规范化规则**：对 `manifest.dataset`（如 `"CMMMU validation smoke (6 items)"`、`"MMMU-Pro (10c) smoke (2 items)"`）采用确定性规则生成 URL-safe `dataset_slug`（小写化、非 `[a-z0-9]` 连续串折叠为 `-`、去首尾 `-`，必要时叠加短哈希防碰撞），数据库主键与 URL 统一使用 `dataset_slug`，前端展示完整保留 `raw_dataset_name`。未登记自定义数据集自动归入 `Custom / Unclassified` 节点正常展示。

---

## 2. 4 维正交口径模型（含 `vlmevalkit` 汇总打分与无有效分留白）

| 正交维度 | 枚举与格式 | 精确判定规则 |
|---|---|---|
| **1. `run_scope`** | `full_split` \| `subset` \| `smoke` \| `reference` | `reference`: `harness in ("oracle", "nop")`；`smoke`: `planned <= 2`（无计划时 `recorded <= 2`）或数据集名/批次名显式标为 `smoke`/`demo`（如 `"CMMMU validation smoke (6 items)"`）；`full_split`: **仅当**计划题集与固定 `(dataset_slug, dataset_version, split)` 完整题集精确匹配；`subset`: 其余多题抽测（**严禁按 80% 比例或挂载官方分推定全量！**） |
| **2. `effective_coverage`** | `scored / planned`（+ 题覆盖 `unique_tasks_scored / split_total_tasks`） | 与分数强制同框；若 `vlmevalkit` 批次仅提供 `official-result.json` 汇总分而逐题 `reward` 为空（如 CMMMU 6 题），分别展示 `官方汇总分母=6` 与 `逐题评分=0/6（原生仅出汇总）`；若纯推理 `mode="infer"` 或全失败（无有效分且无官方分），榜单主分强制留空 `—` |
| **3. `scoring_protocol`** | 协议 ID + `fault_policy` | 如 `harbor-reward-v1`、`wildclaw-overall-score-v1`、`vlmevalkit-native-v1`、`gaia-official-scorer`、`gaia-raw-correct`、`deepswe-f2p-p2p`、`legacy-20261120-csv`；明确 `fault_policy` 为 `exclude_errors_valid_mean` 或 `faults_as_zero_official`（**后者必须独立字段与独立分母呈现**） |
| **4. `official_status`** | `official_verified` \| `unofficial_valid_mean` \| `unscored` | 批次自带有效 `official-result.json`（如 `vlmevalkit`）或管理员挂载有效官方分 JSON 时为 `official_verified`（不反向改写 `run_scope`）；无任何有效评分时为 `unscored` |

### 2.1 Harbor 原生 `reward_valid` 与 `lrm-eval` 共享导出双协议独立保留（关键实测护栏）
经 197 源码与历史数据核验：
- **Harbor 0.26.0 原生口径（`scoring_protocol = "harbor-native-reward-valid-v1"`）**：`TrialResult.reward_valid` 依据 verifier 分数与 `exception_info.policy` 综合计算。197 上 Terminal-Bench 2.1 历史 534 条原生 trial 中有 **490 条 `reward_valid = true` 且主分有限**，其中 **230 条同时携带 `exception_info`**（其异常策略明确允许该有效分生效）。
- **`lrm-eval/run.py` 共享导出口径（`scoring_protocol = "lrm-eval-report-v1"`）**：`run.py:98–106` 按自身规则将带异常的主分置空（`status="failed"`, `reward=null`），按 `report.py` 统计。
- **平台硬性规则**：
  1. 原生 Harbor 适配器必须**同时独立保留** `native_reward_valid` / 原生主分与 `exception_info`（含 `policy`）两个维度，**严禁用通用的“有异常即无分”规则去重算或改写历史原生 Harbor 作业**！
  2. 共享导出批次严格按 `report.py` 口径保留，保持与 `report.py` 100% 数值一致；
  3. 跨来源按 `trial_id (UUID)` 去重时，保留两个来源各自的评分语义（`source_scoring_semantics_json`），聚合与对比严格**分协议统计**，禁止跨协议静默混算。

---

## 3. 严格可比性、共同有分 Macro Avg、交集双分母与 Token 包含语义校验

### 3.1 Comparative Macro Avg（彻底杜绝“列缺基准导致的虚高均分”）
在任意多列对比（设参与对比列集合为 $S = \{s_1, \dots, s_m\}$）中：
1. **单基准内可比条件**：对于基准 $b$，仅当列 $s_i$ 与基准列（或所有参与列）具有相同 `(dataset_slug, dataset_version, split, scoring_protocol)` 时，允许在该基准行参与 `Best` 高亮与 `Δ` 计算；不可比列仍可并排展示并附「协议差异」徽章，但不标 `Best`、不算 `Δ`。
2. **共同有分基准集 $B_{\text{common}}$**：定义 $B_{\text{common}} = \{b \in B_{\text{selected}} \mid \forall s \in S,\ s\text{ 在 }b\text{ 上有可比有效分}\}$。
3. **Comparative Macro Avg（顶行，可比，允许 `Best` 和 `Δ`）**：
   $$\text{MacroAvg}_{\text{comp}}(s) = \frac{1}{|B_{\text{common}}|} \sum_{b \in B_{\text{common}}} \text{norm}_{0..100}(s, b)$$
   强制明示 `基于 |B_common| / |B_selected| 个共同可比基准等权计算`（当 $|B_{\text{common}}| == 0$ 时显示 `—（无共同可比基准）`）。
4. **Self-Coverage Avg（次行，非可比参考）**：展示各列在自己所有已测基准 $B_s$ 上的独立均分并标明 `自覆 |B_s| 基准（非可比）`，**永不标 `Best`、永不算 `Δ`**。

> **验收用例（单列缺一基准）**：选定 `{Bench1, Bench2, Bench3}`，列 A 测得 `[80, 70, 95]`（自均分 `81.67`），列 B 仅测前 2 个 `[82, 72]`（缺 `Bench3`）→ `B_common = {Bench1, Bench2}`，`Comparative Macro Avg` 必须计算为 **A=`75.00` vs B=`77.00`（B 获 Best，Δ=`+2.00`）**，绝不能拿 A 的 `81.67` 与 B 的 `77.00` 对比。

### 3.2 严格交集对比三类分母区分与未舍入精度规范（`align_mode == "intersection"`）
1. **共同有效评分集（`common_valid_scored_tasks`，默认主口径）**：在共同覆盖题中，所有参与列均满足有效评分的子集（197 实测：DeepSWE 两组 `Pi v5 vs OpenClaw` = **60** 题，`27/60 = 45.00%` vs `14/60 = 23.33%`；DeepSWE 三组 = **39** 题；GAIA 两组 = **118** 题）。主分对比、胜负矩阵与点击「查看交集题目」默认使用此口径。
2. **共同覆盖故障按零口径（`common_covered_faults_as_zero`，仅在存在对应协议时展示）**：以所有参与列均有尝试记录的 **`common_covered_tasks`** 为分母（DeepSWE 两组 = **78** 题、三组 = **48** 题；GAIA 两组 = **165** 题），分子必须取**各自在全部共同覆盖题上的真实通过数**（故障按 0 计），**严禁把共同有效集的通过数直接除以共同覆盖分母**！例如 DeepSWE 两组 78 题中，Pi v5 实通过 **31/78 = 39.74%**（并非 27/78），OpenClaw 为 **14/78 = 17.95%**。
3. **各自完整批次有效均分（`self_full_batch_valid`，非可比参考）**：各列在其自身全部有效样本上的独立表现（如 DeepSWE 独立总样本：Pi v5 **107 有效 / 113 捕获 = 44.86%**，OpenClaw **62 有效 / 78 捕获 = 22.58%**），仅作参考展示，不参与交集 `Δ` 与 `Best`。
4. **未舍入双精度计算红线**：所有 `delta_pp`（绝对分差）与 `rel_pct`（相对变化百分比）**必须使用原始未舍入分数（`float64` 精确分数）计算，仅在最终展示时四舍五入至 2 位小数**（例如 DeepSWE 两组 `rel_pct = (14/60 - 27/60) / (27/60) * 100 = -48.1481...% → -48.15%`，而非用已舍入的 `23.33` 与 `45.00` 推算出的 `-48.16%`）。

### 3.3 Token 4 段包含语义先验校验规范（严防错误堆叠）
经 197 实测 336 份 Harbor ATIF：`reasoning_tokens`（329 份）与 `reasoning_output_tokens`（3 份）两种键名并存，`final_metrics.total_cached_tokens` 仅 9 份，且不同 Harness/模型网关对「`cached_tokens` 是否包含在 `input_tokens` 内」、「`reasoning_tokens` 是否包含在 `output_tokens` 内」的语义存在差异。因此平台建立**来源级包含语义校验器（`token_semantics_registry`）**：
1. **双键兼容提取**：解析 ATIF `steps[].metrics.extra` 时同时读取 `reasoning_tokens` 与 `reasoning_output_tokens`（优先明确存在且非负的有限整数，记录 `reasoning_key_source`）。
2. **包含语义核验**：按 `(runner, harness, harness_version, api_format)` 登记校验规则并在数据入库时做一致性断言：
   - 若已验证 **`cached_tokens <= input_tokens`**（包含关系，则 `non_cached_input = input_tokens - cached_tokens`）且 **`reasoning_tokens <= output_tokens`**（包含关系，则 `non_reasoning_output = output_tokens - reasoning_tokens`），或已登记为严格外加关系（且 `total == sum(4 segments)`），标记 `token_semantics = "mutually_exclusive_verified"` → **允许绘制互斥 4 段堆叠条**；
   - 若字段缺失、存在 `cached > input` / `reasoning > output` 违例、或来源语义尚未核实，标记 `token_semantics = "unverified_components"` → **严禁把各数字强行堆叠成一根总条**，前端自动降级为并排独立指标胶囊并附口径说明！

---

## 4. `pass@k`、`pass^k` 与置信区间精确规范

对二值评分（`reward in {0, 1}`）的多尝试任务 $t$，记有效尝试数 $n_t = |\{r \mid r.\text{status}=\text{"scored"}\}|$、有效通过数 $c_t = \sum \mathbb{I}[r.\text{reward}=1]$：
- **单题无偏 `pass@k`**：当 $n_t \ge k$ 时，$\text{pass@}k(t) = 1 - \frac{\binom{n_t - c_t}{k}}{\binom{n_t}{k}}$；**当 $k > n_t$ 时强制记为 `null`（缺测 `—`）**。
- **单题稳定性 `pass^k`**：当 $n_t \ge k$ 时，$\text{pass\^{}}k(t) = \frac{\binom{c_t}{k}}{\binom{n_t}{k}}$；当 $k > n_t$ 时记 `null`。
- **任务间聚合**：默认对所有可算题（$\neq \text{null}$）求等权均值并强制标注分母 `computable_tasks / total_tasks`（如 `round1` `pass@8` 为 `19/40`）；保守下界口径（故障按 0 计入 $n_t = \text{planned}$）放在独立字段 `pass_at_k_faults_as_zero`。
- **统计置信区间**：单尝试二值通过率提供 **95% Wilson Score 置信区间**；多尝试任务均值提供 **1000 次固定种子非参 Bootstrap 95% 置信区间**，悬浮明示样本量与置信算法。

---

## 5. 双层数据身份、按协议独立去重与方法学版本化

1. **双层数据身份与 URL-safe 稳定批次 ID**：
   - `benchmarks`（稳定 `benchmark_slug`）与 `dataset_releases`（`(benchmark_slug, dataset_version, split)` 唯一键）解耦，允许同一 Benchmark 不同版本/Split 共存同名 `task_id`；`dataset_source_aliases` 显式登记已核验来源别名（把 `smoke (6 items)` 归入 `run_scope` 而不污染数据集身份，未核验来源保持 `unmapped`）；
   - 所有 REST API 使用 URL-safe 稳定 `batch_id`（不含 `/` 或 `:`），内部 `source_locator` 完整保留原始路径标识；运行中占位批次使用内部未脱敏指纹 `internal_source_fingerprint` 解析回填（永不暴露到 API DTO）。
2. **先授权裁剪、再按评分协议独立去重（区分唯一试验身份与协议评分记录）**：
   - 批次表 `episodes` 始终保留每批次自身原始记录与本地分母；
   - 跨批次汇总、全文检索与对比**必须先按当前用户授权的 `project_id` 集合与选定数据源裁剪 `trial_protocol_scores` 候选集，再在授权集合内按 `(canonical_trial_key, scoring_protocol)` 取确定性赢家**——杜绝无权项目副本泄露或压掉有权项目下的合法副本！同时，同一 UUID 拥有 `harbor-native-reward-valid-v1` 有效分与 `lrm-eval-report-v1` 失败记录时分协议独立保存、互不覆盖；
   - 无 UUID 历史样本的 `canonical_trial_key` 必须包含**真实 `run_id`（`batch_id`/`source_locator`）、`checkpoint` 与 `harness_version`**（或经显式登记的判重映射），**同配置两次重跑必须保留为两条独立记录**。
3. **成本术语规范**：Harbor 原生 `cost_usd` 在未经账单对账时统一称为**「已记录成本（`recorded_native`）」**；仅与真实账单核对通过后才标「实付成本（`billed_reconciled`）」；按价格表推算的强制标「估算成本（`estimated`）」。
4. **旁挂式方法学与版本号**：所有计算规则注册于后端 `METHODOLOGY_REGISTRY`（当前 `v2026.11.24`），每个响应附 `methodology_version`，全站 `?` 浮层读取同一注册表并锚点直达 `/toolkit/methodology`。
