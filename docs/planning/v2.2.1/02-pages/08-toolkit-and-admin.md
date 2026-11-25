# 02-8 · Toolkit 工具箱、运行参数对照台账、方法学文档与管理员健康运维页

> **关联元数据**
> - **真实数据源**：`/home/share-data/tianxi_benches/framework/{report.py, README.md, SETUP.md, DATASET_FORMAT.md, VLMEVALKIT.md}` + `datasets/README.md` + SQLite `official_results` / `param_compare_groups` / `offline_reports` / `index_warnings` + 197 实时磁盘与进程健康遥测
> - **核心 API**：`POST /api/v1/toolkit/verify/{batch_id}`、`POST /api/v1/toolkit/offline-calc`、`GET/DELETE /api/v1/toolkit/offline-reports`、`PUT /api/v1/toolkit/official-result/{batch_id}`、`GET/POST /api/v1/toolkit/params`、`GET /api/v1/toolkit/manuals/{doc_id}`、`GET /api/v1/toolkit/methodology`、`GET /api/v1/admin/health`、`POST /api/v1/admin/reindex/{batch_id}`
> - **权限要求**：文档与方法学 `Viewer+`；离线校验/参数对照台账 `Researcher+`；官方分覆写与系统运维页 `Admin`
> - **数据边界自检**：彻底删除脱离框架的 `Qira Param Tuning` 与虚构端侧 TTFT 上传表；参数对照页 100% 基于 Harbor / Direct / VLMEvalKit 真实运行配置与已索引批次指标
> - **交付里程碑**：Manuals、方法学页、Offline Verify、运行参数对照台账与完整 Admin Health 全部在 **M2** 交付（Admin Health 基础版随 **M0/M1** 先行提供）

---

## 1. Offline Calculation & Verification 离线校验与算分（`/toolkit/verify`，借用 `ref15` 3 子 Tab 组织）

1. **Tab 1 · Benchmark Calculation（现有批次验收 & 离线 `report.py` 算分）**：
   - **现有批次一键验收**：选择 `harbor-runs/` 任一批次，只读运行 `report.py --check-smoke`、`--check-gateway-capture`、`--check-training-captures`，可视化返回逐项通过/失败原因码（`reasons`）；
   - **离线结果包上传算分（按上传格式严格分流适配，绝不硬塞 `report.py`）**：单包限额 50 MB，未审核临时文件落 **`0700` 隔离上传待审区**（严禁落 `0777` CIFS），入库 `offline_reports` 表并按真实格式走 3 条分支：
     1. **共享批次目录或 `episodes.jsonl`（`upload_format="shared_batch_or_episodes"`）**：调用 `report.py`（核验 `report.py:21–44, 308–328` 仅接受共享批次目录或 `episodes.jsonl`）计算覆盖率、有效均分与状态分布；
     2. **原生 Harbor Job/Trial `result.json`（`upload_format="native_harbor_job"`）**：走**原生 Harbor 适配器**解析（保留 `reward_valid`、`exception_info.policy` 与 4 阶段耗时），不强行喂给 `report.py`；
     3. **仅汇总文件（`upload_format="summary_only"`，如仅传 `batch-report.json` 或 `official-result.json`）**：**只展示汇总结果**，当缺少计划分母或逐题记录时显式标明缺测、**绝不凭空补数或伪造逐题明细**！
   - **官方正式分挂载（`--official-result`）**：表单/JSON 上传官方评分器产出（`dataset`, `metric`, `score`, `denominator`, `source`），写入 SQLite `official_results`（绝不修改共享盘原文件，且严守不改写 `run_scope` 的正交契约）。
2. **Tab 2 · Offline Report（离线报告浏览）**：浏览离线算分生成的完整指标报告与任务明细，支持导出或送入对比。
3. **Tab 3 · Report Management（报告管理）**：集中管理离线报告、官方分挂载记录、已保存对比方案与冻结报告快照（查看创建人、创建时间、关联批次、清理过期临时上传）。

---

## 2. 运行参数跨批次对照台账（`/toolkit/params`，100% 绑定 Harbor / Direct / VLMEvalKit 真实参数）

取代原脱离框架的 `Qira Param Tuning`：用于把同一实验主题下的多组真实评测批次聚合成结构化消融对照表（例如：`Qwen3.8-27B Thinking on vs off 消融`、`同模型不同 Harness 版本对照`、`attempts=1 vs 8 采样对照`）：

- **台账分组元信息**：`Test Topic`（实验主题）、`Label`（对比组名）、`Dataset / Split`、`Creator`；
- **每行严格绑定平台真实已索引的 `batch_id`**，自动提取真实运行配置（绝不允许手填虚构分数或假 TTFT）：
  - **Harbor / Direct 参数**：`runner`、`harness`、`harness_version`、`model_route`、`thinking`、`attempts`、`timeout_multiplier`、`sampling_params`（来自 `model-gateway.json` 或补丁后的 `run-config.json`）；
  - **VLMEvalKit 参数**：`mode (all/infer)`、`model_options`（`max_tokens`, `temperature`, `img_detail`, `chat_template_kwargs.enable_thinking`）；
- **自动拉取的真实对比指标列**：主分（Official 优先 vs Valid Mean）、覆盖率（`scored/planned`）、平均耗时（`duration_sec`）、Token 构成、失败数；
- **一键联动**：勾选台账内任意 2–6 行，一键送入 **Dynamic Comparison** 做交集双分母对照与双栏轨迹/问答 Diff！

---

## 3. Manuals 文档中心 & 独立 Methodology 方法学页（`/toolkit/manuals` & `/toolkit/methodology`）

1. **Manuals 框架与平台手册（`/toolkit/manuals`）**：
   - 左侧文档树直读并渲染 197 共享盘 `/home/share-data/tianxi_benches/framework/` 下的 **`README.md`、`SETUP.md`、`DATASET_FORMAT.md`、`VLMEVALKIT.md`** 与 `datasets/README.md`，以及本平台操作指南（支持右侧 sticky `On this page` 目录、代码块复制、全文检索）。
2. **独立 Methodology 口径与方法学中心（`/toolkit/methodology`，借用 `LLM Stats`）**：
   - 透明公开并带明确版本号（如 `v2026.11.24`）的 7 大章节：
     1. 4 维正交属性判定规则（`run_scope` / `effective_coverage` / `scoring_protocol` / `official_status`）；
     2. 有效 0 分 vs 未评分 (`unscored`) vs 运行故障、以及 `official-result.json` 与逐题 `valid_mean` 的关系；
     3. Comparative Macro Avg（共同有分基准等权）与严格交集双分母（`common_covered` vs `common_valid_scored`）计算公式；
     4. `pass@k` 无偏估计公式、`k > n_scored` 缺测约定与 `pass^k` 稳定性定义；
     5. Token 包含语义校验规则（`reasoning_tokens` / `reasoning_output_tokens` 双键与互斥 4 段准入条件）及延迟/成本诚实披露规则；
     6. 版本化多模态媒体匹配与降级策略；
     7. **Methodology Changelog**：记录每一次口径或基准版本变更，全站表头 `?` 浮层精准锚点直达！

---

## 4. 管理员系统健康与索引运维页（`/admin/health`，Admin 专属）

- **索引引擎健康卡**：热层最近同步时间、最近 10 次 15s 轮询耗时曲线、冷层（7.7 GB 历史 JSON + 7.5 GB `.tar.zst`）缓存命中率与后台进度；
- **数据源覆盖与异常告警表（Source Coverage Report）**：
  - 实时按运行器统计可读批次数与 Trial 数（如 197 当前实测：`28 Harbor (352 trials) + 1 Direct (1 trial) + 8 VLMEvalKit (31 trials)`，总 38 目录含 1 无权限目录 `synth-v1-qwen-opencode-20261124T022715`）；
  - 实时列出 `running_placeholder`（仅含 `batch-status.json` 的运行中目录）状态；
  - 统计可读原生作业回溯覆盖（15/21 可回溯 vs 6 跨机/私有不可读）；
  - 列出所有损坏 JSONL 行、混合版本重试记录（注意：`.publish.lock` 存在不报阻塞告警，按摘要/一致性判定），提供**单批次强制重扫**与**冷层单文件重建**按钮；
- **197 本地盘与隔离存储水位监控卡**：
  - 实时监控 197 本地 ext4 `/` 剩余空间（最新实测已升至 99% / 仅余 1.7 GB，逼近 < 1.5 GB 红色告警）、`/var/lib/lrm-eval/` 与 `/opt/lrm-eval/` 占用明细对照 **750 MB 统一本地预算**进度条；
  - 实时检查隔离上传待审区与 Runner 私有 `--work` 目录的 POSIX 权限位（断言非 `world-readable`，一旦发现误落 `0777` 挂载立即标红阻断写入！）；
- **Runner 与网关 Profile 状态卡**：展示隔离 Runner 连通状态、并发槽位与已登记 Gateway / VLMEvalKit Judge Profile 探活状态（仅显名称与连通态，绝不展示密钥值）。
