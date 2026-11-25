# 02-3 · 评测报告中心、批次详情与单次 Trial 显微镜

> **关联元数据**
> - **真实数据源**：`harbor-runs/<batch>/`（`manifest.json`, `batch-status.json`, `batch-report.json`, `official-result.json`, `vlmevalkit/metrics.json`, `episodes.jsonl`, `trials/<uuid>/*`）+ 可读原生 Harbor 作业（如 `2026-11-20/terminal-bench2.1/*` 含 4 阶段耗时、`verifier/test-stdout.txt`、`lock.json`）+ `2026-11-20/` 历史明细
> - **核心 API**：`GET /api/v1/batches`、`GET /api/v1/batches/{batch_id}`、`GET /api/v1/batches/{batch_id}/tasks`、`GET /api/v1/batches/{batch_id}/episodes`、`GET /api/v1/batches/{batch_id}/trials/{trial_id}`、`GET .../trajectory`、`GET .../gateway`、`GET .../verifier-output`、`GET .../artifacts`、`GET .../training-capture`、`GET .../media/{idx}`、`GET /api/v1/media/...`
> - **权限要求**：报告与汇总 `Viewer+`；轨迹/网关正文/验证日志/产物/受限媒体按 6 项明细 Scope 独立拦截
> - **交付里程碑**：**M1（三运行器全兼容交付）**；验证器日志与 4 阶段耗时对可读原生作业立即可用，对新批次随 M2 上游 `publish()` 补丁生效

---

## 0. 本页三层信息层级与状态恢复契约

| 披露层 | 用户看到什么 | 交互入口 | 返回后恢复哪些状态 |
|---|---|---|---|
| **默认展示 (L0)** | 报告中心批次摘要表（默认仅露身份/赛道/主分/覆盖/三态灯）；批次详情页先呈现 `Basic Information`、核心得分卡与逐题状态摘要（含多尝试 `MultiAttemptPill` 点阵） | 切换模态/运行器/口径筛选；按需打开次级效率/Token 列；点击任务行展开独立 Trial | URL Query 完整记录筛选、排序与分页 |
| **点击展开 (L1/L2)** | 悬停分数/多尝试圆点看单次耗时与失败摘要；点单题优先展示逐次结果、多维 Verifier/学科分项拆解与固定中文失败摘要 | 点击具体 Trial 圆点或「打开完整证据」进入 L3 显微镜 | 关闭抽屉原位保留当前批次筛选与任务定位 |
| **完整证据 (L3)** | 单次 Trial 显微镜按权限按需懒加载折叠/分页的 ATIF 配对工具卡（默认定位首个报错步或终答步）、OpenEnv 网关窗口、截断脱敏验证日志与 `vlmeval_result` 懒加载原图；支持 `j/k` 切题、`[`/`]` 切 Trial、`t` 切换全思考链 | 顶栏「返回批次（保留原筛选）」与深链接锚点 `#step-N` | 完整恢复原批次筛选、对比上下文与原表格滚动位置 |

---

## 1. 评测报告中心（`/eval/reports`）

借用 `Le-AIEval ref4`（多视角 + 模态 Tab + 漏斗过滤 + 子分浮层）并 100% 对齐 `lrm-eval` **三大真实运行器（`harbor` / `direct` / `vlmevalkit`）+ 历史基线**：

- **顶部视角切换**：`View by Batch（批次列表）` | `View by Model` | `View by Benchmark`。
- **顶置筛选栏（双向同步 URL）**：
  - 模态切换 Tab：`All` | `Agent` | `Text` | `Vision`；
  - 运行器筛选：`All / harbor / vlmevalkit / direct / legacy`（对未来新 `runner` 枚举前向兼容自动收录展示，不报错）；
  - 语种筛选：`All / English / Chinese / Multilingual`（覆盖 `CMMMU`、`MMBench_v1_1_CN` 等）；
  - 正交口径过滤：`run_scope`（`full_split / subset / smoke / reference`）、`scoring_protocol`、`official_status`、`batch_status`（`finished / running / running_placeholder / paused_by_guard / interrupted / failed`）、数据源（`harbor_runs / native_harbor / legacy_20261120`）、是否含网关抓包/选题报告。
- **批次列表表格字段**：
  - Batch 名称（含 `running_placeholder` 蓝色运行中状态徽章：对仅生成 `batch-status.json` 尚未写入 `manifest.json` 的目录，先以脱敏来源主机 + 批次名展示，待发布后自动补全身份与得分）；
  - 数据集（显示完整 `raw_dataset_name`，如 `"CMMMU validation smoke (6 items)"`，链接使用规范化 `dataset_slug`）；
  - Model（`model_canonical` + `model_route`）、Harness@Version + Runner 徽章；
  - `run_scope` 徽章、进度与覆盖率（`scored/planned` + `unscored` + `failed` 计数）；
  - **主分列（兼容三类真实评分形态）**：
    1. 同时有 `official-result.json` 与逐题分（如 `mmmu-pro-vlmkit-*`）：展示官方分 + `≡` 浮层（内含 `vlmevalkit/metrics.json` 学科分项与逐题 `valid_mean`）；
    2. 仅有官方汇总分而逐题 `reward` 为空（如 `cmmmu-vlmkit-*-nonthinking`，CMMMU 原生评分器仅出 6 学科汇总）：主分展示 `official-result.json`（标明 `官方汇总 · 分母 6`，`≡` 浮层展开 6 学科分项），同时注明 `逐题分未提供`；
    3. 仅推理未打分（`mode="infer"`, `status="unscored"`，如 `mm-ifeval-vlmkit-*`）或全失败无有效分（如早期 CMMMU 失败冒烟批）：**主分留白 `—`，严禁填 `0`**，保留失败数、耗时与失败详情入口；
  - Token 构成指示器（按包含语义校验渲染，见 `01-1 §2.1`）、平均耗时、三大校验灯（Smoke / Gateway / Training）、`Detail` 入口、`加入对比` 复选框。

---

## 2. 批次详情页（`/eval/batches/:batchId`）

### 2.1 Basic Information 概览卡
- **身份与配置**：`raw_dataset_name`（及 `dataset_slug` / 版本 / split）、Model Route & Canonical & Checkpoint、Harness & Version、Runner（前向兼容原样展示）、`run_scope`、`scoring_protocol`、`official_status`、脱敏来源（**统一对 `source.path` 与 `raw_log.path` 做通用前缀掩码，覆盖 `/home/*` 与跨机 `/tmp/*`**）、以及 `run-config.json` 脱敏参数（若已导出）。
- **核心结果卡**：
  - 官方正式分（含 `official-result.json` 与 `vlmevalkit/metrics.json` 分项预览）与逐题 `valid_mean` 分离展示；
  - 覆盖率进度条明确分别展示 **题数 `unique_tasks`** 与 **尝试数 `planned / recorded / scored`**（例如 `round1` 显示 `40 题 × 8 尝试 = 320 trials`）；
  - 状态分布胶囊：🟢 `scored_pass` / 🟡 `scored_zero` / ⚪ `unscored` / 🔴 `failed` / ⏱ `timeout` / `cancelled` / `unrecorded`。
- **效率与耗时拆解**：
  - 总耗时（`usage.duration_seconds`）；
  - **四阶段平均耗时堆叠条**（`env_setup` / `agent_setup` / `agent_exec` / `verifier`：对原生 Harbor 作业与补丁后新批次展示，旧共享导出与 `vlmevalkit`/`direct` 诚实标注 `仅总耗时`）；
  - **Token 构成指示器**：严格遵守包含语义校验规则（先按来源核验 `cached ⊆ input`、`reasoning ⊆ output`，兼容 `reasoning_tokens` 与 `reasoning_output_tokens` 双键；已验证互斥才画 4 段堆叠条，否则以并排独立指标胶囊展示）；
  - 成本在存在原生 `cost_usd` 且未账单对账时标注「已记录成本（`recorded_native`）」（账单对账后才标「实付」），可选价格表估算默认折叠且强制标注「估算」；
  - 一键导出本批次 **独立 HTML / 打印 PDF / CSV / JSON 报告**。

### 2.2 逐题结果明细表（`/api/v1/batches/{batch_id}/tasks`）
- 表头漏斗过滤：按能力子域、难度、状态（含 `unscored`）、失败类型、分数区间筛选。
- **单题多尝试点阵（Multi-Attempt Matrix）**：当 `attempts > 1` 时，行内直接渲染状态圆点序列 + 该题 `pass@1` / `pass@k`（`k > n_scored` 留白 `—`）与 `valid_mean`。点击行展开独立 Trial 列表，点击圆点直达 Trial 显微镜。
- **快捷操作**：勾选多道题「保存为自定义样本集」或「带入动态对比」。

---

## 3. 单次 Trial 细粒度诊断显微镜（`/eval/batches/:batchId/trials/:trialId`）

顶部固定信息栏：`返回批次（保留原筛选）` · `Prev / Next Trial` · `Task ID` · `Model × Harness@Version (Runner)` · `Reward & Status` · `Duration & Tokens` · `与另一 Trial 并排 Diff`。

主体采用 **6 个按需懒加载 Tab**（按运行器与实际产物覆蓋自动亮起/灰态说明，对未知 `trajectory_status` / `runner` 前向兼容原样展示不报错）：

| Tab | 数据来源与接口 | 展示详设 | 权限与缺失/降级处理 |
|---|---|---|---|
| **1. 概览与评分拆解** | `result.json` + `task.toml`/`instruction.md` | ① 题目说明（受留出集权限控制）；<br>② **多维 Verifier / 官方分项面板**：二值题显示 PASS/FAIL；WildClawBench 自动分组展示 **17 项子指标**；DeepSWE 展示 `f2p/p2p/apply_failed`；GAIA 展示 `level/official_scorer/leak_category`；VLMEvalKit 展示逐题 `verifier_rewards`（若有）与所属批次 `vlmevalkit/metrics.json` 学科分项；<br>③ **四阶段耗时甘特微条**（原生/补丁批次展示，其余仅显示总耗时 `usage.duration_seconds`）；<br>④ 故障诊断卡（若 `status != scored` 置顶：`failure.kind`、`summary`、`exception_type`、`http_status`、**通用脱敏后的主机与路径**）；<br>⑤ **客观轨迹线索摘要条**（仅对含 ATIF/网关记录的 Trial 展示可观测线索计数，不做意图定性）。 | `Viewer+`（留出题面需 `dataset:holdout_read`）；纯推理 `unscored` 显示中性灰态说明 |
| **2. ATIF 轨迹时间轴** | `GET .../trajectory`（分页 + 虚拟滚动） | 左侧 **Step 缩略大纲（Mini-map）**（区分 User / Reasoning / Tool Call / Final / Error + 单步 Token 消耗 + 仅看工具/仅看报错/关键词检索）；右侧对话流：折叠 `reasoning_content`、**按 `tool_call_id == source_call_id` 配对渲染工具调用与观察输出**（Shell/代码高亮、报错标红、长输出折叠复制）、支持单步直链锚点 `#step-12`。 | 需 `trace:read`；OpenClaw 2026.11.5 等 `no_tool_calls_recorded` 明确提示仅导出会话摘要；`vlmevalkit`（`trajectory_status="model_request_response"`）与 `direct` 自动引导至 Tab 5 查看模型问答 |
| **3. OpenEnv 网关请求窗口** | `GET .../gateway?turn_offset=&turn_limit=&include_tools=false` | 顶部展示 `model_calls`、`validated`、`atif_reconciliation`、`upstream_model`；左侧 **Turn 列表** 标明 `index`、`node_id`、`root_id`、`finish_reason`、消息条数骤降标识（上下文压缩观测点）与 `discarded` 分支高亮；右侧查 `Request Messages`、`Request Tools`（按需加载）、`Response Message`、`Sampling Params`（未来若 `gateway_capture.py` 补采了 `timing` 则附带展示 `ttft_ms`/`latency_ms`，当前 6 份存量文件诚实留白）。 | **默认脱敏摘要**（仅见元信息与轮次计数）；需 `gateway:read` 才返回二次凭据清洗后的正文 |
| **4. 验证日志与沙箱产物** | `GET .../verifier-output`、`.../artifacts` | 分栏展示：① 验证器原始输出 `verifier/test-stdout.txt` / `test-stderr.txt`（精确看到哪条 pytest 断言失败，限 128 KiB 截断与脱敏）；② 沙箱产出物清单与代码 Diff 预览（如 DeepSWE `model.patch`）；③ 多步 `step_results` 折迭表。<br>*(注：当前 4 套 Agent 基准均为 CLI/代码沙箱，不设 GUI 录屏主功能位；未来若接入桌面环境基准，按 Harbor 原生 `recording` 扩展透传。)* | 需 `verifier_log:read`；未导出的旧共享批次或非沙箱运行器明确提示「该批次未包含验证器原始日志」 |
| **5. Direct / VLMEvalKit 问答与多模态样本** | `result.json`（`direct_result` / `vlmeval_result`）或历史 `.jsonl` + `GET .../media/{idx}` / `GET /api/v1/media/...` | 左右分栏展示请求消息（含多模态图片灯箱）、Thinking 思考过程、Prediction 预测、`http_status`、Gold 标准答案（需 `dataset:answer_read`）及评分器判定结果。<br>· **VLMEvalKit 内联 base64 防膨胀**：`vlmeval_result.request_messages` 含最高 600KB+ 内联 `data:image/...;base64,...`，索引与列表接口剥离为图像占位元数据 `{media_idx, mime, sha256}`，详情页按需调用 `GET /api/v1/batches/{batch_id}/trials/{id}/media/{idx}` 解码流式渲染！ | **版本化多模态降级**：`2026-11-20` 历史样本严格执行 `media_matching_rules`（`unique_hit` 渲染原图；`missing` 或 `ambiguous_multi_hit` 渲染占位徽章说明原因，绝不盲猜或破图） |
| **6. 训练采集证据** | `GET .../training-capture` | 展示 `checkpoint`、`trace_sha256` 校验灯、每轮 `prompt_token_ids` / `completion_token_ids` 长度与 `loss_mask` 监督 Token 占比及 logprob 分布直方图。 | `Researcher+`；`not_captured` 时灰态提示 |
