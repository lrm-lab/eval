# 03-1 · “所有 Harbor” 可验收数据清册与【页面字段 ↔ 真实数据来源】双向总表

本文档严格落实 `@caozs2` **“100% 对齐 Harbor / `lrm-eval` 真实已有及未来可开发采集数据，绝不幻想无源数据”** 的硬红线，提供：
1. 197 共享盘截至 2026-11-24 的实测全量数据源与覆盖统计（含新落地 `vlmevalkit` 第三运行器）；
2. **全站【页面/字段/图表 ↔ 真实来源文件与字段 ↔ 适用运行器 ↔ 当前覆盖 ↔ 状态（现成可直取 / 未来可开发采集 / 已明确删除）↔ 验收用例】双向对照总表**，与 [`07-roadmap-and-qa/02-e2e-acceptance-matrix.md`](../07-roadmap-and-qa/02-e2e-acceptance-matrix.md) 逐条双向互联；
3. 私有执行机接入、历史回填、重复发布与未来可开发采集点的清晰验证前提。

---

## 1. 197 共享盘全量数据源实测清册（2026-11-24 实时核验）

| 资产根路径 | 文件系统 | 实测规模与覆盖现状 | 所属分层 | 索引与访问策略 |
|---|---|---|---|---|
| `/home/share-data/tianxi_benches/harbor-runs/` | CIFS (`ro` 视角) | **38 个目录**（37 可读 + 1 无读权限 `synth-v1-qwen-opencode-20261124T022715`）：<br>· **Harbor（28 批，352 trials）**：含 336 份 ATIF（其中 **334 份有工具调用**、**329 份含 `steps[].metrics.extra.reasoning_tokens`**、**3 份用别名 `reasoning_output_tokens`**、**9 份含 `final_metrics.total_cached_tokens`**）、6 份 OpenEnv 网关抓包（尚无逐请求耗时字段）、2 条原生已记录 `cost_usd`；21 份 `batch-status.json` 中 15 份可回溯本地原生作业、6 份跨机/私密不可读；<br>· **Direct（1 批，1 trial）**：含 `direct_result`（带 `reasoning_tokens` / `cached_tokens`）；<br>· **VLMEvalKit（8 批，31 trials）**：覆盖 AI2D / CMMMU / MM-IFEval / MMMU-Pro(10c) 冒烟，含 `vlmeval_result`（内联 base64 图单文件可达 611 KB）、`official-result.json`、`vlmevalkit/metrics.json`、`trajectory_status="model_request_response"`、运行中仅含 `batch-status.json` 目录、以及保留的 `.publish.lock` 文件 | **热层** | 15s 增量扫描 + `publish-manifest.json` 摘要或重算一致性校验（**不因 `.publish.lock` 存在而延后**）+ 剥离内联 base64 后按需流式解码 |
| `/home/share-data/tianxi_benches/2026-11-20/` | CIFS (`ro` 视角) | **总计 7.7 GB**：`leaderboard.csv`（23 基准 × 8 列）+ 26 个基准目录；单 JSON 最大 **264 MB**；含 **Terminal-Bench 2.1**（6 组 × 89 = 534 条完整原生 Harbor 作业：490 条 `reward_valid=true` 且主分有限，其中 **230 条同时带 `exception_info` 且策略允许有效分**；6 组按题名与 checksum 匹配共有 **71 道共同有效且具总耗时的题**，分数 25.35%–54.93%，P50 耗时 574–1,091s，同 71 题中仅 3 组完整记录 Token、仅 1 组 53 题有成本）、**DeepSWE**（48/78/113 题，三组共同覆盖 **48** / 共同有效 **39**）、**GAIA**（两组 165 题，共同有效 **118**）、**WildClawBench**（6 组 × 60 题，17 子指标）；多模态归档缺图片原件 | `leaderboard.csv` **热层**；7.7 GB 明细 **冷层** | 后台一次性异步流式字节切片索引 + `(file_path, mtime_ns, size)` 持久化缓存；多模态走版本化 `media_matching_rules` |
| `/home/share-data/tianxi_benches/datasets/` & `bundles/` | CIFS (`ro` 视角) | `tianxi-synth-v1`（40 train + 12 holdout，含 `synthesis.json`/`harbor.toml`）、`synthetic/`；`bundles/` 4 套预置题包（`wildclawbench-20261120-tasks.tar.zst` 达 **7.5 GB**），内部 `task.toml` 自带 `difficulty/category/tags/language/repo` | `datasets/` **热层**；`bundles/` **冷层** | `bundles/` 仅后台一次性流式抽取 `task.toml`/`instruction.md` 元数据入冷缓存，绝不进 15s 轮询 |
| `/home/share-data/tianxi_benches/training-runs/` | CIFS (`ro` 视角) | **当前为空**（0 次实跑产物；源码 `training_opd.py` 已定型 `routing.json` + `checkpoint-XXXX/opd.json` 契约） | **热层** | 轮询检测新目录；空态清晰引导，契约夹具仅限单测不混入线上数据 |
| `/home/share-data/tianxi_benches/framework/` | CIFS (`ro` 视角) | 24 个脚本（含最新 `vlmevalkit.py`）+ 4 份核心文档（`README.md`, `SETUP.md`, `DATASET_FORMAT.md`, `VLMEVALKIT.md`） | **热层** | 文档直读渲染 + 受控 Runner CLI 入口 |
| `/home/share-data/dataset/` | CIFS (`ro` 视角) | 25+ 原始数据集目录（CMMMU 抽样 87/87 唯一命中；MMMU-Pro-10c 74/80 含重复文件名；MM-IFEval 原始 TSV 含 `id/index/image`） | 白名单只读媒体/清册源 | 未注册目录标「原始数据可见、未接入评测」；已登记数据集按版本化规则提供历史原图回溯 |

---

## 2. 【页面字段 / 图表 ↔ 真实数据来源】双向可查总表

> 本表按 **现成可直取**、**未来可开发采集**、**已明确删除** 三类逐条登记，右侧「验收编号」直接对应 [`07-roadmap-and-qa/02-e2e-acceptance-matrix.md`](../07-roadmap-and-qa/02-e2e-acceptance-matrix.md)，实现设计、数据与验收 100% 双向闭环。

### 2.1 现成可直取（197 已有真实文件与字段）

| 页面与展示元素 / 指标 | 真实来源文件与字段路径 | 适用运行器 / 来源 | 197 当前实测样本覆盖 | 状态 | 对应验收 |
|---|---|---|---|---|---|
| 批次身份、数据集显示名与 URL 主键 | `manifest.json`（`dataset`, `harness`, `model`, `batch`, `runner`；无 `runner` 默认 `harbor`）；规范化生成 `dataset_slug` 保留 `raw_dataset_name` | `harbor`, `direct`, `vlmevalkit`, `legacy` | 37 可读批（含带空格括号 `"CMMMU validation smoke (6 items)"`） | **现成可直取** | `A-01`, `A-10`, `A-11` |
| 运行中占位批次状态 | 仅有 `batch-status.json`（`status="running"`, `source.host`, `source.path` 脱敏），尚无 `manifest.json` | 全部新运行批次 | 实测 `ai2d-vlmkit-*` 等运行期间触发 | **现成可直取** | `I-06` |
| 批次计划数、覆盖率、有效均分与状态分布 | `batch-report.json`（`totals.{planned,recorded,scored,coverage,valid_mean,passed,outcomes}`）+ `episodes.jsonl` 重算核验 | `harbor`, `direct`, `vlmevalkit` | 37 批 / 384 条可读 Trial | **现成可直取** | `A-01`, `A-08`, `I-01` |
| 官方汇总分与多学科分项 | `official-result.json`（`dataset, metric, score, denominator, source`）+ `vlmevalkit/metrics.json`（如 CMMMU 6 学科、MMMU-Pro 学科） | `vlmevalkit`（原生写出）+ `harbor`（手动挂载） | 4 批 `vlmevalkit` 已落地真实 `official-result.json` 与 `metrics.json` | **现成可直取** | `A-06`, `A-10` |
| 单题评分、有效 0 分、未评分与多维 Verifier 子分 | `episodes.jsonl` & `trials/<id>/result.json`（`status`, `reward`, `verifier_rewards` 如 WildClaw 17 项、DeepSWE `f2p/p2p`、GAIA `correct` & `correct_official_gaia_scorer`） | `harbor`, `direct`, `vlmevalkit`, `legacy` | 384 条可读 Trial + 26 个历史基准目录 | **现成可直取** | `A-02`, `A-03`, `A-08` |
| 14 类运行故障聚类与脱敏故障定位 | `episodes.jsonl` & `result.json`（`exception_type`, `failure.{kind,summary,raw_log}`，对 `/home/*` 与 `/tmp/*` 全量脱敏）+ `batch_guard.py` | `harbor`, `direct`, `vlmevalkit` | 实测含 `agent_protocol`、`agent_exit`、`VLMEvalKitInferenceError` 等 | **现成可直取** | `A-08`, `A-10`, `S-03` |
| 总 Trial 耗时 & 原生 4 阶段耗时 | `usage.duration_seconds`（热层已有）；原生 Harbor `TrialResult` 四阶段时间戳（`environment_setup`, `agent_setup`, `agent_execution`, `verifier`） | 总耗时全运行器；4 阶段限原生 Harbor 作业 | 总耗时 370/384；4 阶段在 TB2.1 6×89=534 原生 trial 完整 | **现成可直取**（新批补齐见下节） | `A-04` |
| Input / Output / Reasoning / Cached Token | `episodes.jsonl` `usage.{input_tokens,output_tokens}` + ATIF `steps[].metrics.extra.{reasoning_tokens,reasoning_output_tokens}` + `final_metrics.total_cached_tokens` + `direct_result` | `harbor`, `direct`（按包含语义先验校验决定是否互斥堆叠） | 336 Harbor ATIF 中 329 `reasoning_tokens`、3 `reasoning_output_tokens`、9 `total_cached_tokens` | **现成可直取**（新批透传见下节） | `A-12` |
| 原生已记录成本（`recorded_native`，有账单对账再标实付） | `episodes.jsonl` / `result.json` `usage.cost_usd`（未验账标 `recorded_native`，账单核对后标 `billed_reconciled`；价格表推算强制标 `estimated`） | `harbor`（仅原生返回批次） | 197 仅 2/352 条有原生 `cost_usd`（TB2.1 原生仅 1 组 53/71 题有记录） | **现成可直取（低覆盖，未验账标已记录成本）** | `A-12`, `A-15` |
| ATIF 工具轨迹、工具画像与客观轨迹线索 | `trials/<id>/trajectory.json`（`steps[]`, `reasoning_content`, `tool_calls`, `observation.results[].is_error`） | 仅实际记录工具事件的 Harbor Harness（明确排除 OpenClaw 2026.11.5 / Direct / VLMEvalKit） | 334/352 份 Harbor ATIF 含工具调用 | **现成可直取** | `A-12`, `S-02` |
| OpenEnv 网关请求窗口与 `discarded` 分支 | `trials/<id>/model-gateway.json`（`turns[]`, `node_id`, `root_id`, `discarded`, `request_messages`, `request_tools`, `response_message`, `sampling_params`, `atif_reconciliation`） | Harbor Gateway 模式 | 197 已有 6 份网关抓包（尚无逐请求 timing） | **现成可直取** | `S-02` |
| VLMEvalKit / Direct 问答与内联/历史多模态图像 | `result.json`（`vlmeval_result.{dataset,sample_id,request_messages,prediction,http_status}` 含内联 base64 图，`direct_result`）；历史多模态按版本化 `media_matching_rules` 匹配 `/home/share-data/dataset/` | `vlmevalkit`, `direct`, `legacy` | 31 条 `vlmevalkit` + 1 条 `direct` + CMMMU(87/87)/MMMU-Pro(74/80 含重名)/MM-IFEval(TSV) | **现成可直取** | `A-09`, `A-10` |
| 原生验证器 stdout/stderr 与沙箱产物 | 原生 Harbor 作业 `verifier/test-stdout.txt`, `test-stderr.txt`, `artifacts/`（如 `model.patch`）, `config.json` | 原生 Harbor 作业（如 TB2.1 534 条） | TB2.1 6 组完整可读 | **现成可直取**（共享导出补齐见下节） | `A-04`, `S-02` |
| 题库元数据、多尝试 `pass@k`/`pass^k`/CI 与四通道选题 | `bundles/*.tar(.zst)` 内 `task.toml`/`instruction.md`、`datasets/tianxi-synth-v1/synthesis.json`、`selection.json`、`episodes.jsonl` | `harbor`, `synthetic` | 4 套预置题包 + `tianxi-synth-v1` + `round1` (40×8=320 trials) | **现成可直取** | `A-07`, `S-02` |
| 历史 23 项总榜、14 项三线核心基准与模型 Checkpoint 血缘 | `2026-11-20/leaderboard.csv` + `2026-11-20/*` + `config/taxonomy.yaml (goal_type)` + `episodes.jsonl` `checkpoint`/`model_route` | `legacy_20261120`, `harbor`, `vlmevalkit` | 14 项核心基准中 11 项已有实跑/历史、2 项(`MMLU-Pro`/`BFCL-v4`)原始资产可见待接、1 项(`Global-MMLU-Lite`)待定版本接入 | **现成可直取 + 零代码配置扩展** | `A-02`, `A-03`, `A-05`, `A-14` |
| Harbor 原生 `reward_valid` 与异常策略双维度 | 原生 `TrialResult.{reward_valid, verifier_result, exception_info.policy}` 与共享导出 `report.py` 口径分协议保留 | `native_harbor` vs `harbor_runs` | TB2.1 534 原生 trial 中 490 有效、**230 条带 `exception_info` 仍策略有效** | **现成可直取（禁止统一置空改写）** | `A-13` |
| ARC Prize 式效率前沿与同族轨迹散点图 | 共同有效题上按 `(task_name, task_checksum)` 聚合的得分、单题总耗时 P50/均值、总 Tokens、工具步数、完成日期及原生 `cost_usd` | `native_harbor`, `harbor`, `vlmevalkit`, `direct` | TB2.1 六组原生作业已有 **71 道共同有效题**（25.35%–54.93%，P50 574–1,091s；3 组有完整 Token；1 组 53 题有成本） | **现成可直取（切轴按覆盖诚实提示）** | `A-15` |
| 训练与多专家 OPD 路由/步级指标 | `training-runs/<id>/{routing.json, checkpoint-XXXX/opd.json}`（源码 `training_opd.py` 契约） | `training_opd` | 当前目录为空（0 实跑；契约测试用隔离夹具，验证前提见 `02-7 §3.2`） | **契约已有、待首跑产出** | `E-02` |

### 2.2 未来可开发采集（明确采集代码位置、契约、适用运行器与验证门槛）

| 待采能力 / 字段 | 采集端代码位置 | 输出契约与适用运行器 | 历史批次表现 | 验收门槛 |
|---|---|---|---|---|
| **共享导出完整 4 阶段耗时、Token 汇总、脱敏配置、截断验证日志、白名单产物与发布清单** | `framework/run.py:publish()` 向后兼容增强补丁（详见 `05-1 §2`） | `episodes.jsonl` 追加可选字段 + `run-config.json` + `verifier-output.txt`（≤128 KiB 脱敏截断）+ 最后原子写 `publish-manifest.json`（SHA-256）；适用于 `harbor` / `direct` / `vlmevalkit` | 历史批次不补造，诚实显示缺测或从可读原生作业受控回填 | 单题实跑验证摘要校验与 128 KiB 脱敏截断（`I-01`, `M2 Gate`） |
| **逐请求 `latency_ms`、流式 `ttft_ms` 与 `tps`** | OpenEnv 网关代理（`framework/gateway_capture.py`）与 `framework/direct.py` / `vlmevalkit.py` 流式转发层 | 追加 `timing: {ttft_ms, latency_ms, output_tokens, tps}`；**必须先实测验证流式首包时间点与输出 token 计数均可靠后才在 UI 启用**；仅适用于经过该采集层的批次 | 现有 6 份网关抓包与历史批次一律留白 `—`，**绝不用总耗时替代** | 跑 1 条短题核对 `ttft_ms > 0` 与 `latency_ms >= ttft_ms` 后开放展示 |
| **运行中逐题实时事件流（进度/阶段/完成）** | M3 隔离 Execution Runner（Harbor 只读 `--plugin` hooks + Direct/VLMEvalKit 结构化事件行） | 写入 `exec_job_events`（单调 `seq_id`），经 SSE + `Last-Event-ID` 推送，完工后与共享快照对账发 `reconciled`；适用于通过平台隔离 Runner 发起的批次 | 外部 CLI 直接启动的批次仅在运行中显示 `running_placeholder`，完工后切共享快照 | 模拟断线重连补读 + 终态对账一致（`I-05`） |
| **沙箱容器级峰值内存 / CPU 时间遥测（远期可选）** | M3 隔离 Harbor Runner 采样容器 cgroup 统计 | `trials/<id>/telemetry.json`（仅适用于隔离 Runner 调度的 Harbor 容器任务，与端侧硬件无关） | 未采集时完全隐藏该模块 | 仅在容器 Runner 实测产出后开启 |

### 2.3 已明确删除的无源项（100% 移出范围、表结构、路由与验收）

| 已删除项 | 删除依据 | 确认移出位置 |
|---|---|---|
| `Small Model Test`（Domain/Intent/Slot/Language 5 类小模型测试与混淆矩阵） | Harbor / `lrm-eval` 无该评测框架与标签数据 | 已删 `02-pages/08-specialized-tracks.md`、路由 `/tracks/small-model`、表 `specialized_reports`、API 与 E2E |
| `On-Device Test`（端侧多集群设备卡、在线 IP、GPU VRAM/芯片性能与多版本对比） | Harbor / `lrm-eval` 无端侧设备与硬件数据提供方 | 已删 `02-pages/08-specialized-tracks.md`、路由 `/tracks/on-device`、表 `specialized_reports`、API 与 E2E |
| 能力树 `Audio / Speech` 分支 | 197 无音频题库与运行器 | 已从 `02-2` 能力树、`04-1` DDL 与过滤条彻底删除 |
| `Qira Param Tuning` 独立调参工具及虚构 TTFT CSV 上传表 | 脱离 `lrm-eval` 且无历史 TTFT 源 | 已替换为严格绑定真实 `batch_id` 的 `/toolkit/params` 参数对照台账 |
| 脱离 `direct.py` 的任意 CSV/XLSX 通用 `Prompt Optimization` 上传器 | 脱离框架且有任意执行风险 | 已替换为绑定 `direct.py` 原生 `cases.jsonl + score.py` 的 `/eval/direct-lab` |
| 公网商业/社区字段（ELO / Knowledge Cutoff / License / Release Date / Market Share / Top Apps） | Harbor 产物中完全不存在 | 已从模型档案、榜单与筛选器全部移除 |
| Safety Refusals（安全拒答率）与 Reward Hacking / 提前摆烂定性判定 | Harbor 无对应分类器 | 诊断页已改为 4 项纯客观 ATIF/Gateway 证据线索与覆盖计数 |
| GUI 录屏主功能位与虚构 API 实付账单 | 当前 4 套 Agent 基准为 CLI/代码沙箱；原生已记录 `cost_usd` 仅 2/352 条 | `recording` 降为未来桌面题库可选备注；默认效率轴用真实耗时与 Token |

---

## 3. 私有执行机接入、历史回填与重复发布规范

1. **授权发布与登记（绝不越权扫 `/home/*`）**：各执行机（如 `10.110.158.144`）在本地跑完的原生 Harbor 作业，必须由原运行者使用 `run.py --export-job <job_dir> --output /home/share-data/tianxi_benches/harbor-runs --batch <name> --source-host <host>` 发布到共享盘后自动纳入索引；本机授权只读挂载的原生作业根目录可在 `NATIVE_HARBOR_ROOTS` 显式登记。
2. **历史受控回填**：对 `harbor-runs/` 中已在 `batch-status.json` 指向本机可读路径的历史批次（当前 15/21），允许管理员触发一次性只读回填（仅提取 4 阶段耗时与 `verifier/test-stdout.txt` 摘要缓存至本地 SQLite，绝不写回共享目录）。
3. **重复发布与重试血缘**：同一 `batch_id` 重导按 `trial_id (UUID)` 幂等覆盖更新；同一题重跑保留全部尝试并记录 `retry_ids` 血缘，与 `report.py` 统计口径一致。
4. **来源覆盖报告**：在 `/admin/health` 公开实时覆盖报告（按运行器统计可读批次数、无权限目录数、运行中占位目录数、原生作业可回溯数、ATIF/工具事件/Reasoning/Cached/Gateway/Cost 各字段覆盖率）。
