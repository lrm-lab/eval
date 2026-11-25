# 05-1 · Harbor 0.26.0 源码审计、VLMEvalKit 第三运行器与公共 Finalizer 同代发布契约

本文档基于对 197 上 **Harbor 0.26.0 源码**（`harbor/viewer/`、`harbor/server/`、`harbor/models/`）与 **`/home/share-data/tianxi_benches/framework/`**（含 `vlmevalkit.py`、`VLMEVALKIT.md`、`direct.py`、`run.py`、`report.py`）的代码级核查编写。

---

## 1. Harbor 0.26.0 原生接口与 `lrm-eval` 三大运行器核验

### 1.1 `harbor view` 与 `harbor serve`
- **`harbor view`（40+ 端点）**：仅认原生 Harbor 作业目录，不认 `harbor-runs/` 独有产物（`episodes.jsonl`、`selection.json`、`model-gateway.json`、`direct_result`、`vlmeval_result`、`official-result.json`、`vlmevalkit/metrics.json`、`training-runs/`）与 `2026-11-20/`，且无多用户项目/Scope 鉴权与动态对比工作台。
- **`harbor serve`（22 端点 + 6 只读 SQL 视图）**：197 无常驻实例；M3 若采用 `harbor serve` 作为隔离 Runner 后端，可直接对接其作业控制 API。
- **Harbor 原生 `reward_valid` 语义核验**：Harbor 0.26.0 `TrialResult.reward_valid` 依据 verifier 分数和 `exception_info.policy` 计算（197 TB2.1 534 条原生 trial 中 490 条有效，其中 **230 条同时带 `exception_info`** 且策略允许有效分），而 `lrm-eval/run.py:98-106` 按自身规则把带异常的主分置空。平台必须**分协议独立保留两者**，绝不用通用“有异常就无分”改写历史原生作业。

### 1.2 第三运行器 `vlmevalkit` 源码核验（`framework/vlmevalkit.py` & `VLMEVALKIT.md`）
- 仅需 `VLMEVALKIT_ROOT`、`VLMEVALKIT_PYTHON` 与 OpenAI Chat Completions 网关（+ 可选 `VLMEVALKIT_JUDGE_*`），无需 Docker / Agent CLI。
- **单题 `--task` 强制 `infer`（`VLMEVALKIT.md:44`）**：`run.py` 在指定 `--task` 时强制仅推理不运行官方评测器，产出 `status="unscored"` 且无 `official-result.json`；平台 UI 与数据模型须将其与 `--all` 全量评分批次明确区分。
- **内联图像与隐私边界**：`vlmeval_result.request_messages` 含内联 base64 图（单文件可达 611 KB）。面向新架构，受限请求/图像正文应落**独立授权证据存储**（或经鉴权执行机 API 按 `evidence_id` 供给），共享公开层仅放脱敏索引 DTO（详见 `06-1 §3`）。
- **`.publish.lock` 持久化行为（`run.py:64`）**：释放 `flock` 后不删除 `.publish.lock`，索引器严禁以锁文件存在作为延后依据。

---

## 2. 跨运行器公共 Finalizer 与不可变 `generation_id` 发布契约（上游补丁规格）

针对仅对 6 个顶层文件做摘要仍可能遗漏 `vlmevalkit/metrics.json` 与惰性读取明细（`result.json` / `trajectory.json` / `model-gateway.json` / 图像）的问题，升级为**公共 Finalizer + 不可变 `generation_id` + 明细摘要清册**协议：

### 2.1 `episodes.jsonl` 追加的可选字段（向后兼容 `schema_version: 1`）
```json
{
  "started_at": "2026-11-24T06:55:30.996032Z",
  "finished_at": "2026-11-24T06:57:03.802660Z",
  "usage": {
    "input_tokens": 113606,
    "cached_tokens": 12400,
    "output_tokens": 2253,
    "reasoning_tokens": 412,
    "cost_usd": null,
    "duration_seconds": 92.806628
  },
  "phase_timings": {
    "environment_setup_s": 2.1,
    "agent_setup_s": 8.4,
    "agent_execution_s": 76.2,
    "verifier_s": 6.1
  },
  "tool_call_count": 9,
  "reward_valid": true,
  "evidence_refs": {
    "result": {"evidence_id": "ev_r_01a2...", "generation_id": "gen_01a20d01", "sha256": "..."},
    "trajectory": {"evidence_id": "ev_t_01a2...", "generation_id": "gen_01a20d01", "sha256": "..."},
    "verifier_output": {"evidence_id": "ev_v_01a2...", "generation_id": "gen_01a20d01", "sha256": "..."}
  }
}
```

### 2.2 公共 Finalizer 提交顺序与全量同代清单（绝不在 `publish()` 返回前提前封存）
由 `harbor` / `harbor_gateway` / `direct` / `vlmevalkit` 四条链路共享的**最外层公共 Finalizer** 在全部状态、评分、`vlmevalkit/metrics.json`、`official-result.json`、脱敏 `run-config.json`、截断脱敏 `verifier-output.txt`（≤128 KiB，头 32 KiB + 尾 96 KiB）与所有附件均写入不可变 **`generations/<generation_id>/`** 目录后，**最后一步**原子提交 `publish-manifest.json`：

```json
{
  "schema_version": 2,
  "batch": "cmmmu-vlmkit-qwen38-27b-20261124-01",
  "generation_id": "gen_01a20d01f9a2",
  "published_at": "2026-11-24T10:08:04Z",
  "top_level_files": {
    "manifest.json":          {"sha256": "...", "size": 214},
    "episodes.jsonl":         {"sha256": "...", "size": 48910, "lines": 6},
    "batch-status.json":      {"sha256": "...", "size": 312},
    "batch-report.json":      {"sha256": "...", "size": 2450},
    "official-result.json":   {"sha256": "...", "size": 162},
    "vlmevalkit/metrics.json":{"sha256": "...", "size": 148},
    "run-config.json":        {"sha256": "...", "size": 680},
    "trial-digests.jsonl":    {"sha256": "...", "size": 1920, "entries": 6}
  },
  "absent_optional_files": ["selection.json"]
}
```

- **可选文件缺失/移除明确语义**：未生成的合法可选文件显式写入 `absent_optional_files[]`；若某文件既不在 `top_level_files` 也不在 `absent_optional_files`（或列在 `top_level_files` 中却在磁盘缺失/摘要不符），索引器一律拒收该代。
- **惰性读取同代验证**：每次按需读取某 Trial 的 `result.json`、`trajectory.json`、`model-gateway.json`、`verifier-output.txt` 或内联图像时，服务端核对目标文件归属 `generations/<generation_id>/` 且实际字节流 `sha256` 与 `trial-digests.jsonl` 一致；若检测到跨代覆写立即返回 `409 GENERATION_MISMATCH` 并触发重扫，彻底杜绝旧代摘要拼接新代明细！
- **历史老批次兼容**：历史 `batch-report.json` 无 `trial_id` 集合，老批次按同一份可信 `episodes.jsonl` 重算可核验聚合（`recorded`/`scored`/`reward_sum`/`valid_mean`/`passed`/`outcomes`/逐题汇总），不一致时以该份 `episodes.jsonl` 为准并标 `report_origin = "recomputed_from_episodes"`。

---

## 3. 实时执行事件流协议（专用可信事件通道，绝不解析普通日志为进度）

- **专用可信事件通道（彻底防范日志伪造/注入）**：
  - **原生 Harbor**：附加只读 `--plugin` hooks（`on_trial_started` / `on_trial_ended`）直接写入 Runner 独占 `0700` 目录下的**专用结构化事件通道**（独立 FD / 本地 Unix Socket / 独占 `--event-file`）并带单调递增 `seq_id` 入库 `exec_job_events`；
  - **Gateway / Direct / VLMEvalKit**：由框架/Runner 自身通过独立专用事件通道写出结构化事件（**严禁解析子进程普通 `stdout/stderr` 日志文本来驱动进度或状态机！**普通日志仅作脱敏展示，即便模型输出或沙箱命令打印出伪造的 JSON/事件标记也绝不改变任务进度或状态）。
- **断线补读去重与终态对账**：
  1. SSE 支持 `Last-Event-ID`，断线重连后服务端按 `seq_id > last_event_id` 补发，前端与服务端均以 `(job_id, seq_id)` **严格去重**确保无重复追加、无漏计；
  2. 公共 Finalizer 提交 `publish-manifest.json` 后触发单批热索引并发送携带新 `generation_id` 的 `reconciled` 事件，前端无缝切换至权威共享快照。

---

## 4. 框架全脚本参数对接总表

| 框架脚本 | 平台对接页面 | 受控白名单参数 | 隔离存储与续跑约束 |
|---|---|---|---|
| `run.py`（Harbor 标准） | `/eval/execution` | `suite`, `--task`/`--all`, `--agent`, `--agent-version`, `--model`, `--api-format`, `--thinking`, `--batch`, `--concurrent`, `--attempts`, `--pause-after`, `--output`（仅写批准公开 DTO）, `--work`（隔离 `0700` 私有存储） | 支持原生 `--resume`（锁定 `harness_version` 且私有 `--work` 锁完整） |
| `run.py --gateway-capture` | `/eval/execution` | 同上加 `--gateway-capture`, `--capture-host` | 原始网关抓包落独立授权证据存储；原生不支持 `--resume`（M3 任务级派生续跑） |
| `run.py`（VLMEvalKit） | `/eval/execution` | `suite`, `--task`（**强制 `mode="infer"`，局部不评官方分**）/`--all`, `--model`, `--batch`, `--concurrent`, `--work`（隔离 `0700`）, `--output` | 无需 Docker；原生不支持 `--resume`；受限问答与内联图按授权证据存储管理 |
| `run.py`（Direct） | `/eval/execution` & `/eval/direct-lab` | 原生 `cases.jsonl`（`id`, 字符串 `prompt`, 可选相对 `images`, 无自定义评分器必填 `answer`）+ 白名单 `score.py` | 无需 Docker；原生不支持 `--resume`；`system_prompt`/`thinking` 变体标为 M2 待实现适配项 |
| `report.py` | `/toolkit/verify` | `<batch_dir>`, `--planned`, `--attempts-per-task`, `--pass-threshold`, `--official-result`, `--check-*`, `--format json` | 纯只读校验 |
| `synthesize.py` | `/synth/datasets` & `/synth/selection` | `generate` / `check` / `select` | `--teacher` 仅注入受保护环境变量凭据 |
| `train.py` & `training_opd.py` | `/training/opd` | 读取 `routing.json` 与 `checkpoint-*/opd.json`；197 无 GPU，表单仅做 `--plan` 与 CLI 生成 | 结果监控与预检 |
