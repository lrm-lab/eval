# 07-2 · 全链路黄金验收矩阵（pytest + Playwright E2E）与风险预案

本文档提供可直接落为 `pytest`（后端）与 `Playwright`（前端 E2E）自动化断言的精确验收用例，**所有编号与 [`03-data-and-methodology/01-data-inventory-and-coverage.md`](../03-data-and-methodology/01-data-inventory-and-coverage.md) 中的双向数据来源总表逐条对应**，100% 基于真实 197 样本与可验证契约，零无源空壳用例。

---

## 1. 数据口径、三运行器与黄金样本精确验收（M0–M2）

| 编号 | 验收场景 | 测试输入 / 197 实测黄金样本 | 精确通过断言 |
|---|---|---|---|
| **A-01** | 与 `report.py` 数值 100% 一致 | `synth-v1-qwen-opencode-round1`、`wildclaw-pi-qwen197-20261124T062059`、`gaia-pi-qwen197-20261124T062119`、`direct-gateway-qwen197-20261124T082911` | `planned`/`recorded`/`scored`/`coverage`/`valid_mean`/`passed` 与 `report.py` JSON 输出逐位一致 |
| **A-02** | DeepSWE 三类分母区分（共同有效 / 故障按零 / 独立全量）、未舍入精度与绑定快照下钻 | `2026-11-20/DeepSWE/` 历史文件：① **两组**（`Pi v5` vs `OpenClaw`）；② **三组**（48 / 78 / 113 题） | ① 两组**共同有效**固定返回 **`60` 题**（`Pi v5 = 27/60 = 45.00%`，`OpenClaw = 14/60 = 23.33%`，未舍入 `delta_pp = -21.67pp`、`rel_pct = -48.15%`，胜负 `18/9/28/5` + `18` 故障）；② 启用故障按零协议时基于 **`78` 共同覆盖题**各自计通过数，返回 **`Pi v5 = 31/78 = 39.74%`**（绝非 27/78）与 **`OpenClaw = 14/78 = 17.95%`**；③ 各自独立总样本返回 **`44.86% (107/113)`** 与 **`22.58% (62/78)`**；④ 三组返回 **`48 / 39`**；⑤ 从分差单元格下钻 `task-diff` 与导出消费同一 `comparison_id`，逐题统计 100% 复算一致 |
| **A-03** | GAIA 严格交集双分母与口径锁 | `2026-11-20/GAIA/` 两组 165 题 | 保留 `correct` 与 `correct_official_gaia_scorer` 双字段；严格交集固定 **`common_covered_tasks == 165`** 且 **`common_valid_scored_tasks == 118`**；点击交集下钻进入 **118** 题；混选不同协议时禁用 `Best`/`Δ` 并显示原因 |
| **A-04** | Terminal-Bench 2.1 原生作业覆盖 | `2026-11-20/terminal-bench2.1/` 6 组原生 Harbor 作业 | 每组展示 89 条 trial、74–84 条有效评分、对应失败状态，且详情页能读取原生 4 阶段耗时与 `verifier/test-stdout.txt` |
| **A-05** | 单列缺一基准的 Macro Avg 防偏置 | 构造对比：列 A 测 `[B1=80, B2=70, B3=95]`，列 B 仅测 `[B1=82, B2=72]`（缺 `B3`） | **`Comparative Macro Avg` 严格基于 `{B1, B2}` 计算为 A=`75.00` vs B=`77.00`（B 获 Best，Δ=`+2.00`）**；A 的 `81.67 (3/3)` 仅在非可比次行展示且无 Best/Δ |
| **A-06** | 正交 `run_scope` 严禁误判全量 | 构造 85% 题数抽测批次 + 挂载 `official_results` | `run_scope` 保持 `subset`（不升级为 `full_split`），`official_status` 为 `official_verified` |
| **A-07** | 多尝试题数/尝试数拆分与 `pass@k` 缺测 | `synth-v1-qwen-opencode-round1`（40 题 × 8 尝试 = 320 trials，21 题含失败） | 详情页分别显示 **40 题 / 320 尝试**；单题 `n_scored < 8` 时 `pass@8 == null`（渲染 `—`）；批次聚合 `pass@8` 显式标明可算分母 `19/40` |
| **A-08** | 有效 0 分 vs 未评分 vs 运行故障隔离 | `wildclaw-pi-qwen197-20261124T062059`（有效 0 分）& `synth-v1-qwen-opencode-1193-20261124T023653`（`failed`, `agent_protocol`） | WildClaw 渲染琥珀色「有效 0 分」并完整展开 17 项子指标；1193 渲染红色 `agent_protocol` 故障且 `valid_mean` 显示 `—` 而非 `0` |
| **A-09** | 版本化多模态媒体匹配（唯一命中 / 缺失 / 多重命中降级） | `2026-11-20/` 的 `CMMMU`（87/87 唯一命中）、`MMMU-Pro-10c`（74/80 含重复文件名）、`MM-IFEval`（原始 TSV `id/index/image`）及 `RealWorldQA`（未登记） | CMMMU 唯一命中样本经 `/api/v1/media/...` 正常渲染原图；MMMU-Pro 重复文件名返回 `ambiguous_multi_hit` 并降级占位（**不盲猜任一文件**）；MM-IFEval 按 TSV 规则命中；缺失/未登记返回清晰占位徽章，零 404 破图 |
| **A-10** | `vlmevalkit` 第三运行器三态与前向兼容 | 197 实测 8 批 `vlmevalkit`：① `mmmu-pro-vlmkit-*` / `ai2d-vlmkit-*`（官方分 + 逐题分）；② `cmmmu-vlmkit-*-nonthinking`（仅官方汇总 0.6667 + 6 学科分项、逐题 `unscored`）；③ `cmmmu-vlmkit-qwen38-27b-20261124*`（全失败 `VLMEvalKitInferenceError`）& `mm-ifeval-vlmkit-*`（纯推理 `unscored`）；④ 构造未知 `runner="custom_x"` / `trajectory_status="future_v2"` 夹具 | ①② 正确展示 `official-result.json` 与 `vlmevalkit/metrics.json` 学科分项；③ 无有效分时榜单留空 `—`，详情页完整保留失败原因、时间、`http_status` 与 `vlmeval_result`；内联 base64 图剥离不入 SQLite 且按需经鉴权 `/media/{idx}` 渲染；④ 未知枚举原样展示不报错崩溃 |
| **A-11** | 双层数据集身份、版本同名题、Slug 碰撞与含斜线批次 URL | ① 同一 Benchmark 两个不同 `(dataset_version, split)` 均含同名 `task_id`；② 两个不同原始名产生 slug 碰撞；③ 已核验别名 `"CMMMU validation smoke (6 items)"` 与未核验来源名；④ 原始 `source_locator` 含 `/` 的原生/归档批次（如 `native_harbor:terminal-bench2.1/jhpPiV5__Qwen3_8-27B`） | ① `task_definitions(dataset_release_id, task_id)` 同名题并存无覆盖；② 碰撞后确定性区分且不覆盖；③ 已核验 CMMMU smoke 归入 `/benchmarks/cmmmu` 并在 `run_scope="smoke"` 分层展示，未核验源保持 `unmapped` 且展示 `raw_dataset_name`；④ 所有含 `/` 的原生/归档批次经 URL-safe `batch_id` 单段路由 `/api/v1/batches/{batch_id}` 正常访问详情 |
| **A-12** | Token 双键/包含语义校验、工具画像与客观线索覆盖 | 197 实测 336 份 Harbor ATIF（含 329 `reasoning_tokens`、3 `reasoning_output_tokens`、9 `total_cached_tokens`、334 含工具事件）+ OpenClaw 2026.11.5（无工具事件） | 同时准确提取 `reasoning_tokens` 与 `reasoning_output_tokens`；仅 `token_semantics == "mutually_exclusive_verified"` 渲染互斥 4 段堆叠条，未验证来源改用独立数值胶囊不误叠；工具画像与客观线索卡明确展示适用覆盖分母（排除无工具记录的 Harness），且零主观「作弊/摆烂」定性标签 |
| **A-13** | Harbor 原生 `reward_valid` 230 条双协议独立去重 & 无 UUID 重跑保留 | ① 197 TB2.1 534 条原生 trial（490 条 `reward_valid=true`，其中 **230 条同时含 `exception_info`**）；② 构造同 UUID 在 `native_harbor` 为有效分、在 `harbor_runs` 为失败；③ 构造两条无 UUID、相同模型/Harness/题集配置的独立重跑 | ① 230 条原生有效分完整保留；② `trial_protocol_scores` 按协议独立保存，切 `harbor-native-reward-valid-v1` 取回有效分、切 `lrm-eval-report-v1` 取回失败态，互不覆盖且各批次本地分母完整保留；③ 无 UUID 同配置两次重跑因含真实 `run_id/checkpoint/harness_version` 均保留为 2 条独立记录不被误合并 |
| **A-14** | 三线业务目标闭环与 `taxonomy.yaml` 零代码扩展 | 选定基线与候选模型，覆盖 Agentic 4 项 + LLM-Text 7 项 + Vision-Image 3 项（含 `MMLU-Pro`/`BFCL-v4` 未接、`Global-MMLU-Lite` 待定、`MM-IFEval` 规范名「多模态指令遵循」），并动态在 `taxonomy.yaml` 新增 1 个自定义分类/基准热加载 | 完整走通「选基线与候选 → 查 Agentic 4 项绝对 `+Δ pp`、相对 `%` 与覆盖率 → 查 Text/Vision 各项维持/触及可调退步阈值/缺测无法判定 → 点任意项直达同题双栏试验证据」；热加载新增分类无需改前端代码即时出现在能力树与主榜 |
| **A-15** | ARC Prize 效率前沿散点图（TB2.1 六组 71 题实测 & 记录覆盖拆分） | 197 上 `2026-11-20/terminal-bench2.1/` 六组原生 Harbor 作业按 `(task_name, task_checksum)` 对齐 | 共同有效且具总耗时题数固定 **`N = 71`**，画出 6 点（分数 **`25.35%–54.93%`**，P50 耗时 **`574–1,091s`**）；切 X 轴明确 `task_set_policy` 并同时返回 **`batches_with_any_records`** 与 **`plottable_full_coverage_points`**（Token：`3 / 3`；原生已记录成本 `recorded_native`：`1 组 53/71 部分记录` vs **`0` 可绘制满覆盖点**，不标「实付」且缺覆盖点严禁参与 Pareto 前沿）；仅在记录有序系列点间画虚线轨迹，点选回批次明细、框选加对比 |

---

## 2. 同代一致性、`.publish.lock`、实时事件与性能验收（M0 / M3）

| 编号 | 验收场景 | 精确通过断言 |
|---|---|---|
| **I-01** | 同条数重发 & 延迟读取旧代明细防跨代拼接 | ① 模拟新 `episodes.jsonl`（与旧版**行数相同**但 reward 变更）重发：带 `publish-manifest.json`（含 `generation_id`、`vlmevalkit/metrics.json`、`absent_optional_files` 与 `trial-digests.jsonl`）在未全部落盘提交前被拒收并保留上一代；② 模拟索引已读旧代 `generation_id`，随后磁盘上未分代明细文件被新代覆盖后再发起延迟惰性读取 `trajectory.json`/`result.json`：校验 `sha256` 不符立即返回 `409 GENERATION_MISMATCH` 并触发重扫，绝不跨代拼接；③ 历史无 manifest 老批次按可重算聚合（不假定存在 `trial_id` 集合）比对并在不一致时基于同一份 `episodes.jsonl` 重算标 `recomputed_from_episodes` |
| **I-02** | 全量/增量重扫幂等与跨来源按协议去重 | 连续重扫 3 次记录数零增长；同一 `trial_id` 跨来源重复时按 `(canonical_trial_key, scoring_protocol)` 独立去重并保留全部 `source_tags` |
| **I-03** | 权限目录与损坏行容错 | 扫描含 `synth-v1-qwen-opencode-20261124T022715`（无权限）与含 1 条损坏 JSONL 行的目录不崩溃，`/admin/health` 精准记录告警 |
| **I-04** | 冷热分层性能红线 | 热层首扫 **< 5s**，15s 增量轮询 **< 200ms**；重启命中 7.7 GB 历史 JSON + 7.5 GB `.tar.zst` 冷缓存跳过重建；常规 API P95 **< 100ms** |
| **I-05** | 可信事件通道、日志防伪造、断线补发去重与终态对账（M3） | ① 在子进程普通 `stdout/stderr` 中故意打印伪造事件标记或类似 JSON 行：确认任务进度与状态**完全不受影响**（仅专用可信事件通道可驱动状态）；② 运行中模拟 SSE 断线重连携带 `Last-Event-ID`，无遗漏补齐全部 `seq_id` 且前端/后端按 `(job_id, seq_id)` **严格去重**无重复行；③ 公共 Finalizer 提交后自动收到带新 `generation_id` 的 `reconciled` 事件并与最终快照一致 |
| **I-06** | 运行中无 manifest 占位内部指纹回填 & `.publish.lock` 不阻塞 | ① 构造仅含 `batch-status.json`（`status="running"`）的目录：登记 `is_running_placeholder=1` 并以内部未脱敏指纹 `internal_source_fingerprint`（API 不返回）关联，发布后确定性无损回填为同一批次；② 已结束但保留 `.publish.lock` 的 CMMMU/AI2D 批次立即可索引不延后 |

---

## 3. 安全、权限零旁路、隔离证据存储与端到端链路验收（M1–M3）

| 编号 | 验收场景 | 精确通过断言 |
|---|---|---|
| **S-01** | HTTPS / Cookie / CSRF / 无弱口令 | 生产模式 Cookie 带 `HttpOnly; Secure; SameSite=Lax`；跨源或无 CSRF Token 的写请求返回 `403 CSRF_INVALID`；源码 grep 确认零硬编码默认密码 |
| **S-02** | 先授权再去重、`project_id` 撤权严禁 `owner_id` 绕过与 6 项 Scope 零旁路 | ① 构造同 UUID 同协议分属项目 A（高优先源）与项目 B，测试账号仅有项目 B 权限；② 测试账号作为项目 A 资源 `owner_id` 被移出项目 A 团队后重访资源及先前保存的 `/compare/s/:shortId` 冻结快照；③ 测试个人项目（`personal:<uid>`）归属隔离；④ 分别缺 6 项 Scope 访问详情/搜索/媒体（含内联图）/下载/导出/过渡期未批目录 | ① 仅有项目 B 权限时，汇总/搜索/对比**必须先授权裁剪再去重**，准确返回项目 B 有效分——既不泄露项目 A，也不被项目 A 全局赢家压掉；② 团队项目撤权后即使为 `owner_id` 仍立即 `403 PROJECT_FORBIDDEN`（禁止绕过），个人项目仅属主可见；③ 6 项 Scope 在详情、搜索、媒体、下载、导出与撤权后快照重开全拦截；未批历史过渡目录降级隐藏正文但保留状态与分母 |
| **S-03** | 路径穿越与通用路径/凭据脱敏 | 构造 `../` 路径返回 `400/404`；API 响应、批次详情、故障卡与 SSE 日志中**零 `/home/<user>/` 且零远程 `/tmp/...` 原样路径**、零 `internal_source_fingerprint` 泄露、零 API Key 明文 |
| **S-04** | M2 Direct Lab & M3 Runner 执行隔离 | Web 进程所属账号 `id` 不含 `docker` 组；Direct Lab 仅接受原生 `cases.jsonl` 字段 + 服务端白名单评分器；对 Gateway / Direct / VLMEvalKit 调用原生 `--resume` 返回 `409 RESUME_UNSUPPORTED`；VLMEvalKit 选 `--task` 时强制标为 `infer` 并与全量评分明确区分 |
| **S-05** | 197 本地盘统一 **750 MB** 红线 | 全量索引 + 蓝绿升级保留双版本 + WAL/SHM + 1 份本地备份 + 满载日志测试下，`/opt/lrm-eval` + `/var/lib/lrm-eval` 总占用 **≤ 750 MB**，`/admin/health` 准确上报 `/` 剩余水位（当前已达 99% / 1.7 GB 逼近红线告警状态） |
| **S-06** | **公开索引层 vs 受限证据/Runner 私有存储 POSIX 隔离** | ① 故意将 `STAGING_DIR`、`RUNNER_WORK_DIR` 或 `EVIDENCE_STORE_DIR` 指向 `/home/share-data`（`0777` CIFS）时，探针 Fail-Closed 返回 `503 STAGING_PERMISSION_UNSAFE`；② 新发布批次仅将批准公开的索引 DTO 写共享层，受限正文/图像/日志与 Runner `--work` 落 `0700` 隔离区，**切换为宿主机另一普通 Linux 账号尝试读取原始作业、待审上传及新批次受限证据均返回 `Permission denied`**；③ 与此同时，具备项目与 Scope 权限的已登录 Web 用户可在站点正常查看已批准细节 |
| **E-01** | 全站核心用户链路 E2E（Playwright） | 登录 → `⌘K` 搜模型 → 进 Model 档案（仅见真实评测字段）→ 进 Benchmark 档案难题画廊 → 勾选 2 对象进自由对比（绑定 `comparison_id` + 自定义样本集 + 交集模式 + 双栏轨迹 Diff + 保存冻结快照 + 导出独立 HTML）→ 点击「返回原筛选视图」筛选态零丢失 |
| **E-02** | 工具箱（3 格式分流）、Direct 原生验证台与 OPD 契约验收（M2–M3） | ① Offline Verify 3 Tab（`0700` 待审区上传并入 `offline_reports`：共享 `episodes.jsonl` 走 `report.py`、原生 Harbor `result.json` 走原生适配器、仅汇总文件只展汇总绝不补数 + 官方分挂载）；② Param Compare 台账严格绑定真实 `batch_id` 一键送对比；③ Direct Lab 严格按 `direct.py:33-37`（`id`, 字符串 `prompt`, 可选相对 `images`, 无自定义评分器必填 `answer`）校验上传，记录 `cases_sha256`+`scorer_sha256` 完成同题 A/B 对照并通过 `report.py`（若启用扩展参数变体须同步验收请求映射）；④ Training OPD 在真实 `training-runs/` 为空时显示空态引导，隔离契约夹具单测渲染路由拓扑与步级双轴曲线（不污染生产库） |
| **E-03** | LLM Stats 主榜基础控件与产品级动效/性能验收（M1） | Playwright 验证：① 主榜默认 `核心总览` 预设精简聚焦，具备赛道页签、Harbor 真实筛选（严禁 `License/Country/Hardware/Price` 假下拉）、冻结左列、排序、`Columns` 预设与自定义、`Copy link` 分享还原、多选唤起 `FloatingCompareDock`、`Table/Trend/Bar/Scatter` 同上下文切换保留筛选、表头 `?` 口径与点击数字下钻；② 验证 6 大定制组件（复合得分格/多尝试胶囊/提分护栏胶囊/ATIF 配对卡/毛玻璃 Tooltip/Compare Dock）的 `Default/Hover/Active/Focus/Loading/Missing/Error/Reduced-Motion` 状态全部完备，动效不阻塞点击、切视图与散点聚光 60fps、骨架屏 **`CLS < 0.05`**、**`INP < 100ms`** |
| **E-04** | 四层渐进披露（L0→L1→L2→L3）完整钻取与返回状态恢复（M1） | 使用 197 真实数据现场走通：从 Home / 主榜发现某项 Agentic 提升或 Text/Vision 退步（L0）→ 悬停看口径/分母与 0 分/故障拆解（L1）→ 点击分数格或散点滑出侧滑抽屉查子域并点「查看差异题」（L2，背后基线/筛选/对比对象不丢）→ 点具体题先看逐次评分拆解与失败摘要，再按权限展开折叠分页的 ATIF/网关/验证日志定位具体步骤（L3）→ 点击返回原路回到上一组对比与滚动位置，全程颜色编码与单位一致、无整页跳动 |

---

## 4. 关键风险与应对预案

| 风险项 | 影响 | 缓解与兜底方案 |
|---|---|---|
| **197 本地 `/` 仅余 1.7 GB（99%）极度吃紧** | 其他业务写满 `/` 可能波及本地 SQLite WAL | 严格守住 750 MB 预算 + 水位告警；提前请运维清理/扩容 `/`；后端 Repository 层预留 `DATABASE_URL` 一键切独立 PostgreSQL |
| **`/home/share-data` CIFS `nounix 0777` 权限失效 & `/data` 只读** | 误将 `.venv`/WAL/密钥/Runner `--work`/待审上传落共享盘会破坏安全或稳定性 | 强制三区隔离 + 权限探针 Fail-Closed（`S-06`）；静态 `uv` 建本地 `.venv`、扁平 `npm` 构 `dist/`；`0777` CIFS 仅放源码/缓存/筛选脱敏公开导出 |
| **百 MB 历史 JSON、7.5 GB `.tar.zst` 与 600KB+ `vlmevalkit` 内联图** | 同步读取或整包入库导致 OOM / SQLite 膨胀 | 冷热双层异步索引 + 字节偏移 `seek` + 持久化指纹缓存；`vlmevalkit` 内联 base64 索引期剥离、详情页按需流式解码 |
| **M3 隔离执行节点与私有卷审批周期较长** | 可能延迟网页在线触发评测上线 | M0–M2 全程无阻塞交付全量展示、档案、对比、诊断、CLI 生成、`--plan` 预检、合成选题与工具箱；M3 Runner 独立解耦插拔 |
