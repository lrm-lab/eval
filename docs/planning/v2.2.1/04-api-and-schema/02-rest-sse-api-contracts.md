# 04-2 · REST & SSE API 契约、核心 JSON Schema 与错误码字典

本文档给出全站 `/api/v1/*` 路由清单、9 大核心接口的精确 Request/Response JSON 示例（全部经 JSON 语法与数值一致性校验，所有路径段均为 URL-safe `batch_id` / `benchmark_slug` / `dataset_release_id`）及统一业务错误码字典。

---

## 1. 统一响应封装与业务错误码字典

- 成功响应直接返回类型化 JSON 对象（列表统一 `{ "items": [...], "total": int, "page": int, "page_size": int, "methodology_version": "v2026.11.24" }`）。
- 失败响应统一返回 RFC 9457 风格：
  ```json
  { "error": { "code": "SCOPE_FORBIDDEN", "message": "缺少 gateway:read 权限，当前仅返回脱敏摘要", "detail": {"missing_scope": "gateway:read"}, "request_id": "req_01a2..." } }
  ```

| HTTP | 错误码 (`error.code`) | 语义与前端表现 |
|---|---|---|
| 400 | `INVALID_PATH_OR_ID` | 非法 `batch_id`/`trial_id`/`benchmark_slug`/路径穿越字符，直接拒绝 |
| 400 | `INCOMPARABLE_PROTOCOL` | 强制对比校验失败（提示具体冲突协议、跨版本或缺少 checkpoint 身份） |
| 401 | `UNAUTHORIZED` | 未登录或会话过期，跳转 `/login` 并保留 `returnTo` |
| 403 | `CSRF_INVALID` | 写请求缺失/非法 CSRF Token 或非白名单 `Origin` |
| 403 | `SCOPE_FORBIDDEN` / `PROJECT_FORBIDDEN` | 缺少 5+1 项明细 Scope 之一、无该项目授权或撤权后访问受限字段 |
| 404 | `RESOURCE_NOT_FOUND` | 批次/试验/任务/短链/媒体下标不存在 |
| 409 | `BATCH_ALREADY_EXISTS` / `RESUME_UNSUPPORTED` / `GENERATION_MISMATCH` | 批次重名、对不支持原生 `--resume` 的运行器发起了原生续跑、或惰性读取明细时检测到跨代覆盖 |
| 413 | `PAYLOAD_TOO_LARGE` | 离线上传包超过 50 MB 上限 |
| 422 | `SCHEMA_VALIDATION_FAILED` | 参数或 `direct.py` 原生 `cases.jsonl` 格式校验失败（`detail.fields` 精确到字段路径） |
| 503 | `RUNNER_UNAVAILABLE` / `LOCAL_DISK_LOW` / `STAGING_PERMISSION_UNSAFE` | 隔离 Runner 未就绪、197 本地盘剩余 < 1.5 GB、或隔离待审存储权限探针不通过触发写入保护 |

---

## 2. 核心接口精确 Request / Response JSON Schema（9 大核心契约）

### 2.1 `GET /api/v1/leaderboard`（主榜与分赛道子榜 — 一致性夹具示例）
> 注：下例为内部数值 100% 自洽的标准响应夹具：`common_comparable_benchmarks` 含 2 项且参与列均非 `null`（`comparative_bench_count = 2`，均分 `(68.48 + 50.00) / 2 = 59.24`）；未纳入共同集的基准可为 `null` 并明示于非可比自覆统计。

Query: `?track_slug=agentic&run_scope=full_split&include_legacy=true&protocol=harbor-native-reward-valid-v1&page=1&page_size=50&sort=-comparative_score`
```json
{
  "methodology_version": "v2026.11.24",
  "track_slug": "agentic",
  "track_display_name": "Agentic",
  "common_comparable_benchmarks": ["gaia__2026-11-20__val", "terminal-bench__2.1__default"],
  "best_by_task": [
    {
      "dimension": "Terminal SysOps",
      "benchmark_slug": "terminal-bench",
      "dataset_release_id": "terminal-bench__2.1__default",
      "model_canonical": "Qwen3_8-27B",
      "harness": "jhpPiV5",
      "runner": "harbor",
      "scoring_protocol": "harbor-native-reward-valid-v1",
      "denominator_kind": "full_valid",
      "score": 50.0,
      "coverage": "84/89",
      "batch_id": "b_tb21_jhppiv5_qwen38_27b"
    }
  ],
  "items": [
    {
      "rank": 1,
      "subject_id": "model:Qwen3_8-27B|harness:pi@0.87.1",
      "model_canonical": "Qwen3_8-27B",
      "harness": "pi",
      "harness_version": "0.87.1",
      "runner": "harbor",
      "comparative_macro_avg": 59.24,
      "comparative_bench_count": 2,
      "self_coverage_avg": 59.24,
      "self_bench_count": 2,
      "benchmarks": {
        "gaia__2026-11-20__val": {
          "benchmark_slug": "gaia",
          "raw_dataset_name": "GAIA validation",
          "score": 68.48,
          "raw_score": 68.48,
          "scale_type": "pct_0_100",
          "run_scope": "full_split",
          "scoring_protocol": "gaia-official-scorer",
          "official_status": "unofficial_valid_mean",
          "source_type": "legacy_20261120",
          "scored": 165,
          "planned": 165,
          "coverage": 1.0,
          "batch_id": "b_gaia_pi_qwen38_27b"
        },
        "terminal-bench__2.1__default": {
          "benchmark_slug": "terminal-bench",
          "raw_dataset_name": "Terminal-Bench 2.1",
          "score": 50.0,
          "raw_score": 0.5,
          "scale_type": "ratio_0_1",
          "run_scope": "full_split",
          "scoring_protocol": "harbor-native-reward-valid-v1",
          "official_status": "unofficial_valid_mean",
          "source_type": "native_harbor",
          "scored": 84,
          "planned": 89,
          "coverage": 0.9438,
          "batch_id": "b_tb21_jhppiv5_qwen38_27b"
        },
        "wildclawbench__2026-11-20__default": null
      },
      "tokens": {
        "semantics": "unverified_components",
        "input": 84200,
        "cached": null,
        "output": 4100,
        "reasoning": 1950
      },
      "avg_duration_sec": 182.4,
      "cost": {
        "amount_usd": null,
        "source": "none"
      }
    }
  ],
  "total": 12,
  "page": 1,
  "page_size": 50
}
```

### 2.2 `POST /api/v1/compare/query`（自由动态对比 — 197 实测 DeepSWE 两组三类分母精确示例）
> **197 实测三类分母与未舍入精度核验**：DeepSWE 仅选 `Pi v5` 与 `OpenClaw` **两组**时：
> 1. **共同有效集（60 题，默认主口径）**：`Pi v5 = 27/60 = 45.00%`，`OpenClaw = 14/60 = 23.33%`；按未舍入精确值算 `delta_pp = -13/60 × 100 = -21.67 pp`、`rel_pct = -13/27 × 100 = -48.15%`；胜负 `18 / 9 / 28 / 5` + `18` 题故障排除；
> 2. **共同覆盖故障按零（78 题，仅在存在对应故障按零协议时展示）**：按各自在 78 题上的真实通过数计——`Pi v5 = 31/78 = 39.74%`（绝非 27/78），`OpenClaw = 14/78 = 17.95%`（未舍入 `delta_pp = -17/78 × 100 = -21.79 pp`）；
> 3. **各自历史总样本有效均分（非可比参考）**：`Pi v5 = 48/107 = 44.86%`（107 有效 / 113 捕获），`OpenClaw = 14/62 = 22.58%`（62 有效 / 78 捕获）。
> 选齐三组时共同覆盖 **48** / 共同有效 **39**（见 `A-02`）。

Request:
```json
{
  "subjects": [
    {"id": "s1", "kind": "batch", "batch_id": "b_deepswe_pi_v5_qwen38_27b", "alias": "Pi v5"},
    {"id": "s2", "kind": "batch", "batch_id": "b_deepswe_openclaw_qwen38_27b", "alias": "OpenClaw"}
  ],
  "baseline_subject_id": "s1",
  "scope": {
    "tracks": ["agentic"],
    "dataset_release_ids": ["deepswe__1.1__default"],
    "sample_set_id": null,
    "difficulties": [],
    "run_scopes": ["full_split", "subset"]
  },
  "scoring_protocol": "deepswe-f2p-p2p",
  "align_mode": "intersection",
  "multi_attempt_agg": "valid_mean",
  "regression_threshold_pp": -1.0,
  "include_verdict": true,
  "metrics": ["score", "coverage", "avg_duration_sec", "tokens"]
}
```
Response（含 Home 与 Compare 顶部「Gain vs Guardrail 三线结论卡」完整契约；也可通过独立轻量接口 `GET /api/v1/compare/verdict?baseline=...&candidate=...&regression_threshold_pp=-1.0` 拉取；**所有 `delta_pp` 与 `rel_pct` 均按未舍入精确分数计算后展示**）:
```json
{
  "comparison_id": "cmp_01a20cfe_deepswe_2way",
  "methodology_version": "v2026.11.24",
  "bound_snapshot": {
    "scoring_protocol": "deepswe-f2p-p2p",
    "align_mode": "intersection",
    "multi_attempt_agg": "valid_mean",
    "regression_threshold_pp": -1.0,
    "sample_set_id": null,
    "batches": [
      {"subject_id": "s1", "batch_id": "b_deepswe_pi_v5_qwen38_27b", "generation_id": "gen_legacy_20261120_a"},
      {"subject_id": "s2", "batch_id": "b_deepswe_openclaw_qwen38_27b", "generation_id": "gen_legacy_20261120_b"}
    ]
  },
  "gain_vs_guardrail_verdict": {
    "baseline_subject_id": "s1",
    "candidate_subject_id": "s2",
    "regression_threshold_pp": -1.0,
    "agentic_gains": [
      {
        "benchmark_slug": "deepswe",
        "dataset_release_id": "deepswe__1.1__default",
        "track_slug": "agentic",
        "scoring_protocol": "deepswe-f2p-p2p",
        "denominator_kind": "common_valid_scored",
        "baseline_score": 45.0,
        "candidate_score": 23.33,
        "delta_pp": -21.67,
        "rel_pct": -48.15,
        "coverage": "60/78 common valid",
        "status": "regressed",
        "reason": "在 60 道共同有效题上相对基线下降 21.67 pp（未舍入相对变化 -48.15%）"
      }
    ],
    "guardrails": [
      {
        "benchmark_slug": "mmlu-pro",
        "dataset_release_id": null,
        "track_slug": "llm-text",
        "scoring_protocol": null,
        "denominator_kind": null,
        "baseline_score": null,
        "candidate_score": null,
        "delta_pp": null,
        "rel_pct": null,
        "coverage": "0/0",
        "status": "undeterminable",
        "reason": "原始数据可见、未接入评测，无法判定是否退步"
      }
    ]
  },
  "auto_insights": [
    {
      "text": "在 DeepSWE 1.1 共同有效的 60 道题上，Pi v5 (45.00%) 比 OpenClaw (23.33%) 高 +21.67 pp（18 胜 / 9 同过 / 28 同错 / 5 负；另 18 题因运行故障未进入有效交集）。",
      "filter_link": {"comparison_id": "cmp_01a20cfe_deepswe_2way", "dataset_release_id": "deepswe__1.1__default", "diff_type": "a_wins"}
    }
  ],
  "comparative_summary": {
    "common_comparable_benchmarks": ["deepswe__1.1__default"],
    "by_subject": {
      "s1": {"comparative_macro_avg": 45.0, "is_best": true, "delta_pp": 0.0, "self_full_batch_avg": 44.86, "self_bench_count": 1},
      "s2": {"comparative_macro_avg": 23.33, "is_best": false, "delta_pp": -21.67, "self_full_batch_avg": 22.58, "self_bench_count": 1}
    }
  },
  "rows": [
    {
      "benchmark_slug": "deepswe",
      "dataset_release_id": "deepswe__1.1__default",
      "raw_dataset_name": "DeepSWE 1.1",
      "scoring_protocol": "deepswe-f2p-p2p",
      "comparable": true,
      "intersection": {
        "common_covered_tasks": 78,
        "common_valid_scored_tasks": 60,
        "fault_excluded_tasks": 18
      },
      "cells": {
        "s1": {
          "common_valid": {"score": 45.0, "passed": 27, "denominator": 60, "is_best": true, "delta_pp": 0.0, "rel_pct": 0.0},
          "common_covered_faults_as_zero": {"enabled_by_protocol": true, "score": 39.74, "passed": 31, "denominator": 78, "delta_pp": 0.0},
          "self_full_batch_valid": {"score": 44.86, "valid_scored": 107, "captured_tasks": 113, "split_total": 113}
        },
        "s2": {
          "common_valid": {"score": 23.33, "passed": 14, "denominator": 60, "is_best": false, "delta_pp": -21.67, "rel_pct": -48.15},
          "common_covered_faults_as_zero": {"enabled_by_protocol": true, "score": 17.95, "passed": 14, "denominator": 78, "delta_pp": -21.79},
          "self_full_batch_valid": {"score": 22.58, "valid_scored": 62, "captured_tasks": 78, "split_total": 113}
        }
      }
    }
  ],
  "pareto_frontier": {"x_metric": "avg_duration_sec", "points": [], "frontier_subject_ids": ["s1"]},
  "disabled_metrics": [{"subject_id": "s2", "metric": "ttft_ms", "reason": "历史批次未采集逐请求流式时间戳"}]
}
```

### 2.3 `POST /api/v1/compare/task-diff`（绑定同一 `comparison_id` 或完整规范化查询的逐题 Diff）
> **硬契约**：`task-diff`、`POST /compare/export` 与从任意分差单元格的下钻**严禁仅凭临时 `s1/s2` 别名查询**，必须消费同一 `comparison_id`（或传入完整规范化查询体含 `batch_ids` / `generation_ids` / `scoring_protocol` / `sample_set_id` / `align_mode` / `multi_attempt_agg`），确保总表、胜负矩阵、逐题 Diff 与导出文件 100% 复算一致！

Request:
```json
{
  "comparison_id": "cmp_01a20cfe_deepswe_2way",
  "subject_a_id": "s1",
  "subject_b_id": "s2",
  "dataset_release_id": "deepswe__1.1__default",
  "filter": "divergent",
  "page": 1,
  "page_size": 50
}
```
Response:
```json
{
  "comparison_id": "cmp_01a20cfe_deepswe_2way",
  "scoring_protocol": "deepswe-f2p-p2p",
  "intersection": {"common_covered_tasks": 78, "common_valid_scored_tasks": 60},
  "win_tie_loss": {"a_wins": 18, "both_pass": 9, "both_fail": 28, "b_wins": 5, "fault_excluded": 18},
  "items": [
    {
      "task_id": "repo__issue-1042",
      "task_checksum_match": true,
      "a": {"batch_id": "b_deepswe_pi_v5_qwen38_27b", "trial_id": "trial-a", "status": "scored", "reward": 1.0, "duration_sec": 142.3, "tool_calls": 14, "verifier_summary": "f2p 4/4"},
      "b": {"batch_id": "b_deepswe_openclaw_qwen38_27b", "trial_id": "trial-b", "status": "scored", "reward": 0.0, "duration_sec": 298.1, "tool_calls": 31, "verifier_summary": "f2p 2/4"}
    }
  ]
}
```

### 2.4 `GET /api/v1/batches/{batch_id}` 与 `.../tasks`（URL-safe `batch_id` + 三运行器兼容）
- `GET /api/v1/batches/b_cmmmu_vlmkit_nonthinking_20261124`（`vlmevalkit` 仅官方汇总打分样例）：
```json
{
  "batch_id": "b_cmmmu_vlmkit_nonthinking_20261124",
  "source_locator": "harbor_runs:cmmmu-vlmkit-qwen38-27b-20261124-nonthinking",
  "generation_id": "gen_01a20ce2a9b1",
  "is_running_placeholder": false,
  "runner": "vlmevalkit",
  "benchmark_slug": "cmmmu",
  "dataset_release_id": "cmmmu__2026-11-24__val",
  "identity_status": "mapped",
  "raw_dataset_name": "CMMMU validation smoke (6 items)",
  "run_scope": "smoke",
  "official_status": "official_verified",
  "official_result": {"metric": "accuracy", "score": 0.6666666667, "denominator": 6, "source": "vlmevalkit/metrics.json"},
  "vlmeval_metrics": [{"总准确率": 0.6666666667, "技术与工程": 1.0, "科学": 0.0, "健康与医学": 1.0, "商业": 0.0, "艺术与设计": 1.0, "人文社会科学": 1.0}],
  "totals": {"unique_tasks": 6, "planned": 6, "recorded": 6, "scored": 0, "unscored": 6, "valid_mean": null},
  "source_host_redacted": "10.110.x.144",
  "source_path_redacted": "<redacted>/jobs/cmmmu-vlmkit-qwen38-27b-20261124-nonthinking"
}
```
- `GET /api/v1/batches/b_synth_v1_opencode_round1/tasks`（多尝试点阵 + `pass@k` 缺测）：
```json
{
  "batch_id": "b_synth_v1_opencode_round1",
  "unique_tasks": 40,
  "total_attempts": 320,
  "pass_at_k_summary": {
    "k1": {"value": 1.0, "computable_tasks": 40, "total_tasks": 40, "ci95": [0.91, 1.0]},
    "k8": {"value": 1.0, "computable_tasks": 19, "total_tasks": 40, "note": "21 题因存在失败尝试有效数 <8 记缺测"}
  },
  "items": [
    {
      "task_id": "tianxi-synthetic/log-rollup-train-001",
      "planned": 8,
      "recorded": 8,
      "scored": 7,
      "passed": 7,
      "valid_mean": 1.0,
      "pass_at_1": 1.0,
      "pass_at_8": null,
      "pass_hat_8": null,
      "attempt_dots": [
        {"trial_id": "u1", "status": "scored", "reward": 1.0},
        {"trial_id": "u8", "status": "failed", "reward": null, "failure_kind": "agent_exit"}
      ],
      "selection_lane": "review"
    }
  ]
}
```

### 2.5 `GET /api/v1/batches/{batch_id}/trials/{trial_id}/trajectory`（ATIF 预配对分页 + 同代校验）
Query: `?step_offset=0&step_limit=50&only_tools=false`（需项目权限 + `trace:read`）
```json
{
  "batch_id": "b_synth_v1_opencode_round1",
  "generation_id": "gen_01a20c8810aa",
  "trial_id": "ce27896a-99f0-44f9-b956-3907bcf875f1",
  "trajectory_status": "atif",
  "total_steps": 11,
  "step_offset": 0,
  "step_limit": 50,
  "agent": {"name": "opencode", "version": "1.19.3", "model_name": "intercepted/Qwen3_8-27B"},
  "token_semantics": "unverified_components",
  "final_metrics": {"total_prompt_tokens": 113606, "total_completion_tokens": 2253, "total_reasoning_tokens": 412, "reasoning_key_source": "reasoning_tokens"},
  "observable_clues": [],
  "steps": [
    {
      "step_id": 2,
      "source": "agent",
      "timestamp": "2026-11-24T06:55:46.224Z",
      "reasoning_content": "First, let's examine the input files...",
      "paired_tools": [
        {
          "tool_call_id": "chatcmpl-tool-b58a",
          "function_name": "bash",
          "arguments": {"command": "ls /app/inputs/"},
          "observation": {"content": "part-00.jsonl\n...", "is_error": false, "truncated": false}
        }
      ],
      "metrics": {"prompt_tokens": 7641, "completion_tokens": 46, "reasoning_tokens": 17}
    }
  ]
}
```

### 2.6 `GET /api/v1/batches/{batch_id}/trials/{trial_id}/gateway`（默认脱敏摘要 + 授权分页正文）
Query: `?turn_offset=0&turn_limit=10&include_tools=false`
- **无 `gateway:read` 或过渡期未批准源**：返回 `redacted: true` + `summary`（`model_calls`, `validated`, `turn_count`, `discarded_turns`, `compaction_turn_indices`, `finish_reasons`），省略消息正文；
- **获授权且源已审批**：核验 `generation_id` 与 `sha256` 后返回经二次凭据正则清洗的分页 `turns[]`。

### 2.7 `POST /api/v1/execution/jobs`（受控提交执行作业，需 `Researcher+` + CSRF）
```json
{
  "kind": "vlmevalkit",
  "suite_id": "cmmmu-val",
  "selection_mode": "all",
  "vlmevalkit_mode": "all",
  "model_route": "Qwen3_8-27B",
  "gateway_profile_id": "qwen197-vllm",
  "judge_profile_id": null,
  "concurrent": 3,
  "batch_name": "cmmmu-vlmkit-qwen38-27b-20261124-01",
  "notify": {"in_app": true, "webhook_profile_id": "eval-channel", "on_events": ["finished", "failed"]}
}
```
Response (`202 Accepted`): `{"job_id": "job_01a2...", "status": "queued", "batch_name": "cmmmu-vlmkit-qwen38-27b-20261124-01", "stream_url": "/api/v1/execution/jobs/job_01a2.../stream"}`

### 2.8 `GET /api/v1/execution/jobs/{job_id}/stream`（SSE 实时事件流 + `Last-Event-ID` 断线补读）
```text
id: 42
event: trial_finished
data: {"seq_id":42,"job_id":"job_01a2...","source":"runner_live","trial_id":"uuid-1","task_id":"657","status":"scored","reward":1.0,"progress":{"planned":6,"completed":4,"scored":3,"errors":1},"ts":"2026-11-24T10:02:11Z"}

id: 43
event: reconciled
data: {"seq_id":43,"job_id":"job_01a2...","source":"shared_snapshot","batch_id":"b_cmmmu_vlmkit_01","generation_id":"gen_01a20d01","status":"finished","snapshot_digest":"sha256:...","ts":"2026-11-24T10:08:04Z"}
```

### 2.9 `GET /api/v1/leaderboard/efficiency-scatter`（ARC Prize 效率前沿 — 区分“记录覆盖”与“可绘制点数”）
> **口径要点**：① 切轴时通过 `task_set_policy` 明确声明 `"fixed_y_common"`（锁定同一 71 题基线，缺 X 覆盖点灰显且**严禁参与 Pareto 前沿**）或 `"recompute_xy_common"`（按 X/Y 均有效重取交集并刷新 `common_valid_tasks` 分母）；② 每条 X 轴清晰拆分 **`batches_with_any_records`**（含任意记录批次数）与 **`plottable_full_coverage_points`**（在该共同题集上 100% 完整覆盖、可入图且可参与前沿的点数）；③ Harbor 原生 `cost_usd` 在未与账单对账时统一标记 **`recorded_native`（已记录成本）**，而非「实付成本」。

Query: `?dataset_release_id=terminal-bench__2.1__default&scoring_protocol=harbor-native-reward-valid-v1&x_axis=duration_p50_sec&task_set_policy=fixed_y_common`
```json
{
  "methodology_version": "v2026.11.24",
  "benchmark_slug": "terminal-bench",
  "dataset_release_id": "terminal-bench__2.1__default",
  "raw_dataset_name": "Terminal-Bench 2.1",
  "scoring_protocol": "harbor-native-reward-valid-v1",
  "task_set_policy": "fixed_y_common",
  "common_valid_tasks": 71,
  "x_axis": "duration_p50_sec",
  "x_axis_coverage": {
    "duration_p50_sec": {
      "total_batches": 6,
      "batches_with_any_records": 6,
      "plottable_full_coverage_points": 6,
      "note": "全部 6 组在 71 道共同题上均具备有效总耗时"
    },
    "total_tokens_avg": {
      "total_batches": 6,
      "batches_with_any_records": 3,
      "plottable_full_coverage_points": 3,
      "note": "仅 3 组在 71 题上完整记录输入/输出 Token，其余 3 组缺测不参与前沿"
    },
    "recorded_cost_usd": {
      "total_batches": 6,
      "batches_with_any_records": 1,
      "plottable_full_coverage_points": 0,
      "partial_record_coverage": "1 组仅 53/71 题有原生已记录成本",
      "note": "0 组覆盖全部 71 道共同题；已记录成本未做账单对账，不可绘制完整前沿"
    }
  },
  "points": [
    {
      "batch_id": "b_tb21_jhppiv5_qwen38_27b",
      "model_canonical": "Qwen3_8-27B",
      "harness": "jhpPiV5",
      "series_id": null,
      "series_order": null,
      "score_pct": 54.93,
      "x_value": 574.2,
      "x_coverage_tasks": 71,
      "common_valid_n": 71,
      "full_coverage": "84/89",
      "eligible_for_pareto": true,
      "on_pareto": true
    }
  ],
  "series_connectors": [],
  "pareto_frontier_batch_ids": ["b_tb21_jhppiv5_qwen38_27b"]
}
```

---

## 3. 全站其余接口速查表（统一 URL-safe ID + 项目/Scope 授权）

- **实体档案与推荐**：`GET /models` · `GET /models/{canonical}` · `GET /harnesses` · `GET /harnesses/{name}` · `GET /benchmarks/{benchmark_slug}` · `GET /benchmarks/{benchmark_slug}/hardest-tasks` · `POST /recommend` · `GET /leaderboard/efficiency-scatter`
- **题库与媒体**：`GET /datasets/tree` · `GET /datasets` · `GET /datasets/{dataset_release_id}/tasks` · `GET /datasets/{dataset_release_id}/tasks/{task_id}` · `GET /media/{source}/{path}`（版本化白名单 + 项目/Scope 鉴权）· `GET /batches/{batch_id}/trials/{trial_id}/media/{idx}`（同代摘要校验 + 项目/Scope 鉴权）
- **Trial 补充明细**：`GET /batches/{batch_id}/trials/{trial_id}/{verifier-output,artifacts,training-capture}`
- **诊断**：`GET /diagnostics/{failures,submetrics,efficiency,tools,clues}`
- **Direct 验证台**：`GET /direct-lab/suites` · `GET /direct-lab/scorers` · `POST /direct-lab/validate-cases`（严格复用 `direct.py` 原生字段）· `POST /direct-lab/runs` · `GET /direct-lab/runs/{id}/diff/{other_id}`
- **合成与训练**：`GET /synth/datasets` · `POST /synth/{generate,check}` · `GET /synth/selection/{batch_id}` · `POST .../preview` · `GET /training/runs` · `GET /training/runs/{id}`
- **Toolkit 与 Admin**：`POST /toolkit/verify/{batch_id}` · `POST /toolkit/offline-calc` · `GET/DELETE /toolkit/offline-reports` · `PUT /toolkit/official-result/{batch_id}` · `GET/POST /toolkit/params` · `GET /toolkit/{manuals,methodology}` · `GET /admin/health` · `POST /admin/reindex/{batch_id}` · `POST /admin/taxonomy/reload`
