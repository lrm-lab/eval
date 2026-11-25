from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import ijson

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.indexing.hot_indexer import SourceApprovalRegistry
from app.security.auth import AuthUser
from app.security.authz import has_scope, is_holdout_dataset_or_batch, sql_project_filter
from app.security.redaction import redact_host, redact_path, redact_text
from app.security.storage_probe import resolve_within_root
from app.services.methodology import METHODOLOGY_VERSION, finite

# Official 14+ failure signatures aligned with framework/batch_guard.py + vlmevalkit exceptions
FAILURE_KIND_LABELS: dict[str, tuple[str, str]] = {
    "agent_tls": ("TLS 证书校验失败", "infra"),
    "agent_protocol": ("模型 API 协议不兼容", "infra"),
    "agent_cli": ("Agent CLI 启动参数异常", "harness"),
    "agent_auth": ("模型网关凭据或权限拒绝", "infra"),
    "agent_rate_limit": ("模型网关限流 (429)", "infra"),
    "agent_connect": ("沙箱网络连接模型 API 失败", "infra"),
    "agent_install": ("Agent 安装或依赖启动失败", "harness"),
    "agent_exit": ("Agent 非零退出", "agent"),
    "agent_timeout": ("Agent 作答超时", "timeout"),
    "agent_setup_timeout": ("Agent 安装/启动超时", "timeout"),
    "environment_timeout": ("容器构建/启动超时", "timeout"),
    "environment_error": ("容器环境构建/执行失败", "infra"),
    "verifier_timeout": ("评分器运行超时", "timeout"),
    "verifier_error": ("评分器执行失败", "verifier"),
    "cancelled": ("试验已取消", "cancelled"),
    "run_error": ("未归类运行异常", "other"),
    "VLMEvalKitInferenceError": ("VLMEvalKit 推理失败", "vlm"),
    "VLMEvalKitPromptError": ("VLMEvalKit 提示构建失败", "vlm"),
    "model_empty_answer": ("网关响应成功但缺失有效回答", "vlm"),
    "model_gateway_http_error": ("模型网关 HTTP 错误", "infra"),
}


def compute_atif_tool_and_clues(
    data: dict[str, Any],
    *,
    min_repeat: int = 3,
    min_consec_errors: int = 4,
) -> dict[str, Any]:
    """Extract 100% objective ATIF tool statistics and observable trajectory clues (v2.2.1 02-5 §2.4-2.5).
    Zero subjective behavioral labels (never outputs cheating/laziness verdicts)."""
    steps = data.get("steps")
    if not isinstance(steps, list):
        return {
            "has_tool_events": False,
            "step_count": 0,
            "tool_call_count": 0,
            "max_prompt_tokens": None,
            "tools": {},
            "clues": [],
        }

    obs_by_call: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        obs = step.get("observation")
        if isinstance(obs, dict):
            for res in obs.get("results") or []:
                if isinstance(res, dict) and isinstance(res.get("source_call_id"), str):
                    obs_by_call[res["source_call_id"]] = res

    tools_stats: dict[str, dict[str, int]] = {}
    has_tool_events = False
    total_tool_calls = 0
    max_prompt_tokens: int | None = None

    last_sig: str | None = None
    last_fn: str | None = None
    cur_repeat = 0
    max_repeat = 0
    max_repeat_fn: str | None = None

    cur_err_streak = 0
    max_err_streak = 0

    for step in steps:
        if not isinstance(step, dict):
            continue
        metrics = step.get("metrics") or {}
        if isinstance(metrics, dict):
            pt = metrics.get("prompt_tokens")
            if isinstance(pt, int) and pt >= 0:
                max_prompt_tokens = pt if max_prompt_tokens is None else max(max_prompt_tokens, pt)

        tcs = step.get("tool_calls")
        if isinstance(tcs, list):
            if tcs:
                has_tool_events = True
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = str(tc.get("function_name") or tc.get("name") or "").strip()
                if not fn:
                    continue
                total_tool_calls += 1
                has_tool_events = True
                entry = tools_stats.setdefault(fn, {"calls": 0, "errors": 0})
                entry["calls"] += 1

                raw_args = tc.get("arguments")
                try:
                    norm_args = json.dumps(raw_args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError):
                    norm_args = str(raw_args)
                sig = hashlib.sha256(f"{fn}:{norm_args}".encode("utf-8")).hexdigest()[:16]
                if sig == last_sig:
                    cur_repeat += 1
                else:
                    last_sig = sig
                    last_fn = fn
                    cur_repeat = 1
                if cur_repeat > max_repeat:
                    max_repeat = cur_repeat
                    max_repeat_fn = last_fn

                cid = tc.get("tool_call_id") or tc.get("id")
                obs_res = obs_by_call.get(str(cid)) if cid else None
                is_err = bool(obs_res.get("is_error")) if isinstance(obs_res, dict) else False
                if is_err:
                    entry["errors"] += 1
                    cur_err_streak += 1
                    if cur_err_streak > max_err_streak:
                        max_err_streak = cur_err_streak
                else:
                    cur_err_streak = 0

    clues: list[dict[str, Any]] = []
    if max_repeat >= min_repeat:
        clues.append(
            {
                "code": "repeated_identical_tool_call",
                "max_consecutive": max_repeat,
                "function_name": max_repeat_fn,
                "threshold": min_repeat,
            }
        )
    if max_err_streak >= min_consec_errors:
        clues.append(
            {
                "code": "consecutive_tool_errors",
                "max_consecutive": max_err_streak,
                "threshold": min_consec_errors,
            }
        )

    return {
        "has_tool_events": has_tool_events,
        "step_count": len(steps),
        "tool_call_count": total_tool_calls,
        "max_prompt_tokens": max_prompt_tokens,
        "tools": tools_stats,
        "clues": clues,
    }


def _compute_gateway_turn_clue(
    turns: Iterable[Any],
    *,
    msg_drop_pct: float = 30.0,
) -> dict[str, Any] | None:
    discarded_turns = 0
    max_drop_pct = 0.0
    drop_indices: list[int] = []
    prev_len: int | None = None
    for idx, t in enumerate(turns):
        if not isinstance(t, dict):
            continue
        if t.get("discarded") is True:
            discarded_turns += 1
        msgs = t.get("request_messages")
        if not msgs and isinstance(t.get("request"), dict):
            msgs = t["request"].get("messages")
        if isinstance(msgs, list):
            cur_len = len(msgs)
            if prev_len and prev_len > 0 and cur_len < prev_len:
                drop = ((prev_len - cur_len) / prev_len) * 100.0
                if drop > max_drop_pct:
                    max_drop_pct = drop
                if drop > msg_drop_pct and len(drop_indices) < 20:
                    drop_indices.append(idx)
            prev_len = cur_len
    if discarded_turns > 0 or drop_indices:
        return {
            "code": "gateway_discarded_or_msg_drop",
            "discarded_turns": discarded_turns,
            "max_msg_drop_pct": round(max_drop_pct, 2),
            "drop_turn_indices": drop_indices,
            "threshold_pct": msg_drop_pct,
        }
    return None


def compute_gateway_clue(gw_data: dict[str, Any], *, msg_drop_pct: float = 30.0) -> dict[str, Any] | None:
    """Compute the objective gateway clue from a capture (02-5 §2.5)."""
    turns = gw_data.get("turns")
    return _compute_gateway_turn_clue(turns, msg_drop_pct=msg_drop_pct) if isinstance(turns, list) else None


def _evidence_file_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size


GATEWAY_CLUE_SUMMARY_VERSION = 1
ATIF_CLUE_SUMMARY_VERSION = 1


class _GatewayDigestReader:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.root_token = b""

    def read(self, size: int) -> bytes:
        chunk = self.stream.read(size)
        self.digest.update(chunk)
        if not self.root_token:
            self.root_token = chunk.lstrip()[:1]
        return chunk


def read_gateway_clue_for_index(
    path: Path, expected_identity: tuple[int, int, int] | None, expected_digest: str | None,
) -> tuple[str, dict[str, Any]] | None:
    """Derive a bounded summary while binding the capture in the background indexer."""
    with path.open("rb") as fh:
        identity = _evidence_file_identity(os.fstat(fh.fileno()))
        if expected_identity is not None and (identity[0], identity[1], identity[3]) != expected_identity:
            return None
        reader = _GatewayDigestReader(fh)
        valid_json = True
        try:
            clue = _compute_gateway_turn_clue(ijson.items(reader, "turns.item"))
        except (ijson.JSONError, UnicodeError, ValueError):
            valid_json = False
            clue = None
            while reader.read(64 * 1024):
                pass
        if _evidence_file_identity(os.fstat(fh.fileno())) != identity:
            return None
    if _evidence_file_identity(path.stat()) != identity:
        return None
    digest = reader.digest.hexdigest()
    if expected_digest is not None and digest != expected_digest:
        return None
    return digest, {
        "source": "gateway_capture",
        "version": GATEWAY_CLUE_SUMMARY_VERSION,
        "sha256": digest,
        "file_identity": list(identity),
        "valid_json": valid_json and reader.root_token == b"{",
        "clue": clue,
    }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (pct / 100.0) * (len(s) - 1)
    lo = int(math.floor(k))
    hi = min(len(s) - 1, int(math.ceil(k)))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class DiagnosticsService:
    """M2 Deep Diagnostics & Objective Trajectory Evidence Center (v2.2.1 · 02-5 & S-02).
    Strictly enforces:
    - Project RBAC (`sql_project_filter`)
    - Holdout redaction (no holdout task_id / trial_id / per-task rows without `dataset:holdout_read`)
    - Source approval checks before reading ATIF / gateway evidence on disk."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        approval_registry: SourceApprovalRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.approval_registry = approval_registry or SourceApprovalRegistry(settings.source_approval_file)
        self._tool_cache_lock = threading.Lock()
        self._evidence_cache: OrderedDict[tuple[Any, ...], dict[str, Any] | None] = OrderedDict()
        self._evidence_workers = ThreadPoolExecutor(max_workers=12, thread_name_prefix="diagnostics-evidence")

    def _holdout_task_pairs(self) -> set[tuple[str, str]]:
        rows = self.db.fetchall(
            "SELECT dataset_release_id, task_id FROM task_definitions WHERE LOWER(COALESCE(split, '')) = 'holdout'"
        )
        return {(str(r["dataset_release_id"]), str(r["task_id"])) for r in rows}

    def _is_row_holdout(self, row: dict[str, Any], holdout_pairs: set[tuple[str, str]] | None = None) -> bool:
        if is_holdout_dataset_or_batch(
            dataset_release_id=row.get("dataset_release_id"),
            release_split=row.get("rel_split"),
            benchmark_slug=row.get("benchmark_slug"),
            raw_dataset_name=row.get("raw_dataset_name"),
            batch_name=row.get("batch_name"),
        ):
            return True
        rel_id = row.get("dataset_release_id")
        tid = row.get("task_id")
        if rel_id and tid:
            if holdout_pairs is not None:
                return (str(rel_id), str(tid)) in holdout_pairs
            hit = self.db.fetchone(
                """
                SELECT 1 AS hit FROM task_definitions
                WHERE dataset_release_id = ? AND task_id = ? AND LOWER(COALESCE(split, '')) = 'holdout'
                LIMIT 1
                """,
                (str(rel_id), str(tid)),
            )
            return hit is not None
        return False

    def _build_filter_clause(
        self,
        user: AuthUser,
        *,
        harness: str | None = None,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        dataset_release_id: str | None = None,
        batch_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        proj_sql, proj_params = sql_project_filter(user, "b.project_id")
        conds = [proj_sql, "b.is_running_placeholder = 0"]
        params: list[Any] = list(proj_params)
        if harness:
            conds.append("b.harness = ?")
            params.append(harness)
        if runner:
            conds.append("b.runner = ?")
            params.append(runner)
        if benchmark_slug:
            conds.append("b.benchmark_slug = ?")
            params.append(benchmark_slug)
        if dataset_release_id:
            conds.append("b.dataset_release_id = ?")
            params.append(dataset_release_id)
        if batch_id:
            conds.append("b.batch_id = ?")
            params.append(batch_id)
        return " AND ".join(conds), params

    def _outcome_banner(self, where_sql: str, params: list[Any]) -> dict[str, Any]:
        row = self.db.fetchone(
            f"""
            SELECT
                COUNT(*) AS total_trials,
                SUM(CASE WHEN e.status = 'scored' AND e.reward IS NOT NULL AND e.reward > 0 THEN 1 ELSE 0 END) AS scored_positive,
                SUM(CASE WHEN e.status = 'scored' AND e.reward IS NOT NULL AND e.reward = 0 THEN 1 ELSE 0 END) AS scored_zero,
                SUM(CASE WHEN e.status = 'unscored' THEN 1 ELSE 0 END) AS unscored,
                SUM(CASE WHEN e.status NOT IN ('scored', 'unscored') THEN 1 ELSE 0 END) AS fault,
                COUNT(DISTINCT b.batch_id) AS batch_count
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            WHERE {where_sql}
            """,
            params,
        ) or {}
        return {
            "total_trials": int(row.get("total_trials") or 0),
            "batch_count": int(row.get("batch_count") or 0),
            "scored_positive": int(row.get("scored_positive") or 0),
            "scored_zero": int(row.get("scored_zero") or 0),
            "unscored": int(row.get("unscored") or 0),
            "fault": int(row.get("fault") or 0),
        }

    def _filter_facets(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, proj_params = sql_project_filter(user, "project_id")
        rows = self.db.fetchall(
            f"""
            SELECT DISTINCT runner, harness, benchmark_slug, dataset_release_id
            FROM batches
            WHERE {proj_sql} AND is_running_placeholder = 0
            """,
            proj_params,
        )
        return {
            "runners": sorted({r["runner"] for r in rows if r["runner"]}),
            "harnesses": sorted({r["harness"] for r in rows if r["harness"]}),
            "benchmarks": sorted({r["benchmark_slug"] for r in rows if r["benchmark_slug"]}),
            "dataset_releases": sorted({r["dataset_release_id"] for r in rows if r["dataset_release_id"]}),
        }

    # 1. Failure Taxonomy
    def get_failures(
        self,
        user: AuthUser,
        *,
        harness: str | None = None,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        dataset_release_id: str | None = None,
        batch_id: str | None = None,
        failure_kind: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        where_sql, params = self._build_filter_clause(
            user,
            harness=harness,
            runner=runner,
            benchmark_slug=benchmark_slug,
            dataset_release_id=dataset_release_id,
            batch_id=batch_id,
        )
        banner = self._outcome_banner(where_sql, params)
        can_holdout = has_scope(user, "dataset:holdout_read")
        rows = self.db.fetchall(
            f"""
            SELECT e.batch_id, e.trial_id, e.task_id, e.status, e.failure_kind, e.exception_type,
                   e.failure_summary, e.http_status, e.failure_host_redacted, e.failure_log_redacted,
                   e.duration_sec, e.started_at, e.finished_at,
                   b.batch_name, b.runner, b.harness, b.model_canonical, b.benchmark_slug,
                   b.dataset_release_id, b.raw_dataset_name, b.source_host_redacted,
                   r.split AS rel_split
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {where_sql}
              AND (e.status NOT IN ('scored', 'unscored') OR e.failure_kind IS NOT NULL OR e.exception_type IS NOT NULL)
            ORDER BY b.finished_at DESC, e.batch_id, e.trial_id
            """,
            params,
        )

        by_kind: dict[str, dict[str, Any]] = {}
        by_harness_kind: dict[str, Counter[str]] = defaultdict(Counter)
        by_runner_kind: dict[str, Counter[str]] = defaultdict(Counter)
        by_host_kind: dict[str, Counter[str]] = defaultdict(Counter)
        filtered_trials: list[dict[str, Any]] = []
        holdout_trials_redacted = 0

        for r in rows:
            kind = str(r["failure_kind"] or r["exception_type"] or r["status"] or "run_error")
            label, category = FAILURE_KIND_LABELS.get(kind, (kind, "other"))
            bucket = by_kind.setdefault(
                kind,
                {
                    "failure_kind": kind,
                    "label": label,
                    "category": category,
                    "count": 0,
                    "batches": set(),
                    "harnesses": set(),
                    "sample_summary": label,
                },
            )
            bucket["count"] += 1
            bucket["batches"].add(r["batch_id"])
            if r["harness"]:
                bucket["harnesses"].add(r["harness"])
            if not self._is_row_holdout(r) and r["failure_summary"] and bucket["sample_summary"] == label:
                bucket["sample_summary"] = r["failure_summary"]
            by_harness_kind[str(r["harness"] or "unknown")][kind] += 1
            by_runner_kind[str(r["runner"] or "unknown")][kind] += 1
            host_red = r["failure_host_redacted"] or r["source_host_redacted"] or "unknown"
            by_host_kind[str(host_red)][kind] += 1

            if failure_kind is None or failure_kind == kind:
                if not can_holdout and self._is_row_holdout(r):
                    holdout_trials_redacted += 1
                    continue
                if len(filtered_trials) < limit:
                    filtered_trials.append(
                        {
                            "batch_id": r["batch_id"],
                            "batch_name": r["batch_name"],
                            "trial_id": r["trial_id"],
                            "task_id": r["task_id"],
                            "status": r["status"],
                            "failure_kind": kind,
                            "failure_label": label,
                            "exception_type": r["exception_type"],
                            "failure_summary": redact_text(r["failure_summary"]) or label,
                            "http_status": r["http_status"],
                            "host_redacted": redact_host(host_red) if host_red != "unknown" else "unknown",
                            "log_redacted": redact_path(r["failure_log_redacted"]) if r["failure_log_redacted"] else None,
                            "runner": r["runner"],
                            "harness": r["harness"],
                            "model_canonical": r["model_canonical"],
                            "benchmark_slug": r["benchmark_slug"],
                            "dataset_release_id": r["dataset_release_id"],
                            "duration_sec": r["duration_sec"],
                        }
                    )

        total_faults = len(rows)
        clusters = sorted(
            (
                {
                    "failure_kind": v["failure_kind"],
                    "label": v["label"],
                    "category": v["category"],
                    "count": v["count"],
                    "share_pct": round((v["count"] / total_faults) * 100.0, 2) if total_faults else 0.0,
                    "batch_count": len(v["batches"]),
                    "harnesses": sorted(v["harnesses"]),
                    "sample_summary": redact_text(v["sample_summary"]) or v["label"],
                }
                for v in by_kind.values()
            ),
            key=lambda x: (-x["count"], x["failure_kind"]),
        )

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "banner": banner,
            "facets": self._filter_facets(user),
            "total_fault_trials": total_faults,
            "holdout_trials_redacted": holdout_trials_redacted,
            "clusters": clusters,
            "pivot_by_harness": [
                {"harness": h, "total": sum(c.values()), "counts": dict(c)}
                for h, c in sorted(by_harness_kind.items(), key=lambda kv: -sum(kv[1].values()))
            ],
            "pivot_by_runner": [
                {"runner": rn, "total": sum(c.values()), "counts": dict(c)}
                for rn, c in sorted(by_runner_kind.items(), key=lambda kv: -sum(kv[1].values()))
            ],
            "pivot_by_host": [
                {"host_redacted": h, "total": sum(c.values()), "counts": dict(c)}
                for h, c in sorted(by_host_kind.items(), key=lambda kv: -sum(kv[1].values()))
            ],
            "trials": filtered_trials,
        }

    # 2. Multi-Submetric & Subject Heatmap
    def get_submetrics(
        self,
        user: AuthUser,
        *,
        harness: str | None = None,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        dataset_release_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        where_sql, params = self._build_filter_clause(
            user,
            harness=harness,
            runner=runner,
            benchmark_slug=benchmark_slug,
            dataset_release_id=dataset_release_id,
            batch_id=batch_id,
        )
        banner = self._outcome_banner(where_sql, params)
        can_holdout = has_scope(user, "dataset:holdout_read")

        ep_rows = self.db.fetchall(
            f"""
            SELECT e.batch_id, e.trial_id, e.task_id, e.status, e.reward, e.verifier_rewards_json,
                   b.batch_name, b.model_canonical, b.harness, b.runner, b.benchmark_slug,
                   b.dataset_release_id, b.raw_dataset_name, r.split AS rel_split
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {where_sql}
              AND e.verifier_rewards_json IS NOT NULL
              AND e.verifier_rewards_json NOT IN ('', '{{}}')
            """,
            params,
        )

        bench_groups: dict[str, dict[str, Any]] = {}

        for r in ep_rows:
            try:
                vr = json.loads(r["verifier_rewards_json"] or "{}")
            except Exception:
                continue
            if not isinstance(vr, dict):
                continue
            numeric_keys = {
                k: float(v)
                for k, v in vr.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and finite(v)
            }
            for k, v in vr.items():
                if isinstance(v, bool) and k not in numeric_keys:
                    numeric_keys[k] = 1.0 if v else 0.0
            if len(numeric_keys) <= 1 and set(numeric_keys.keys()) <= {"reward"}:
                continue

            is_hold = self._is_row_holdout(r)
            bkey = str(r["benchmark_slug"] or r["raw_dataset_name"] or "other")
            grp = bench_groups.setdefault(
                bkey,
                {
                    "group_key": bkey,
                    "title": r["raw_dataset_name"] or bkey,
                    "source_kind": "verifier_rewards",
                    "submetric_names": set(),
                    "subject_cells": defaultdict(lambda: defaultdict(list)),
                    "subject_meta": {},
                    "task_cells": defaultdict(lambda: defaultdict(list)),
                    "trial_samples": defaultdict(list),
                },
            )
            subj_key = f"{r['model_canonical'] or 'unknown'} | {r['harness'] or r['runner']} | {r['batch_name']}"
            grp["subject_meta"][subj_key] = {
                "batch_id": r["batch_id"],
                "batch_name": r["batch_name"],
                "model_canonical": r["model_canonical"],
                "harness": r["harness"],
                "runner": r["runner"],
                "dataset_release_id": r["dataset_release_id"],
            }
            for mk, val in numeric_keys.items():
                is_count_key = mk.endswith("_total") or mk.endswith("_passed") or mk.endswith("_count")
                grp["submetric_names"].add(mk)
                grp["subject_cells"][subj_key][mk].append((val, is_count_key))
                if can_holdout or not is_hold:
                    grp["task_cells"][r["task_id"]][mk].append((val, is_count_key))
                    if val <= 0.5 and len(grp["trial_samples"][(subj_key, mk)]) < 5:
                        grp["trial_samples"][(subj_key, mk)].append(
                            {"batch_id": r["batch_id"], "trial_id": r["trial_id"], "task_id": r["task_id"], "value": val}
                        )

        vlm_rows = self.db.fetchall(
            f"""
            SELECT b.batch_id, b.batch_name, b.model_canonical, b.harness, b.runner,
                   b.benchmark_slug, b.dataset_release_id, b.raw_dataset_name, b.vlmeval_metrics_json
            FROM batches b
            WHERE {where_sql}
              AND b.vlmeval_metrics_json IS NOT NULL
              AND b.vlmeval_metrics_json NOT IN ('', '{{}}', '[]')
            """,
            params,
        )
        for r in vlm_rows:
            try:
                vm = json.loads(r["vlmeval_metrics_json"] or "null")
            except Exception:
                continue
            records = vm if isinstance(vm, list) else ([vm] if isinstance(vm, dict) else [])
            flat_metrics: dict[str, float] = {}
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                for k, v in rec.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and finite(v):
                        flat_metrics[str(k)] = float(v)
            if not flat_metrics:
                continue
            bkey = f"vlmevalkit:{r['benchmark_slug'] or r['raw_dataset_name'] or r['batch_name']}"
            grp = bench_groups.setdefault(
                bkey,
                {
                    "group_key": bkey,
                    "title": f"VLMEvalKit 学科分项 · {r['raw_dataset_name'] or r['benchmark_slug']}",
                    "source_kind": "vlmevalkit_metrics",
                    "submetric_names": set(),
                    "subject_cells": defaultdict(lambda: defaultdict(list)),
                    "subject_meta": {},
                    "task_cells": defaultdict(lambda: defaultdict(list)),
                    "trial_samples": defaultdict(list),
                },
            )
            subj_key = f"{r['model_canonical'] or 'unknown'} | {r['batch_name']}"
            grp["subject_meta"][subj_key] = {
                "batch_id": r["batch_id"],
                "batch_name": r["batch_name"],
                "model_canonical": r["model_canonical"],
                "harness": r["harness"],
                "runner": r["runner"],
                "dataset_release_id": r["dataset_release_id"],
            }
            for mk, val in flat_metrics.items():
                grp["submetric_names"].add(mk)
                grp["subject_cells"][subj_key][mk].append((val, False))

        matrices: list[dict[str, Any]] = []
        for grp in bench_groups.values():
            sub_cols = sorted(grp["submetric_names"])
            rows_out: list[dict[str, Any]] = []
            for subj_key, meta in sorted(grp["subject_meta"].items()):
                cells: dict[str, Any] = {}
                for mk in sub_cols:
                    vals = grp["subject_cells"][subj_key].get(mk) or []
                    if not vals:
                        cells[mk] = None
                        continue
                    raw_mean = sum(v for v, _ in vals) / len(vals)
                    is_cnt = any(c for _, c in vals)
                    pct_val = None if is_cnt else round(raw_mean * 100.0 if raw_mean <= 1.0 else raw_mean, 2)
                    cells[mk] = {
                        "mean_raw": round(raw_mean, 4),
                        "pct": pct_val,
                        "n": len(vals),
                        "is_count": is_cnt,
                        "low_samples": grp["trial_samples"].get((subj_key, mk), []),
                    }
                rows_out.append({"subject_label": subj_key, **meta, "cells": cells})

            task_rows_out: list[dict[str, Any]] = []
            for tid, tmap in sorted(grp["task_cells"].items())[:60]:
                tcells: dict[str, Any] = {}
                for mk in sub_cols:
                    vals = tmap.get(mk) or []
                    if not vals:
                        tcells[mk] = None
                        continue
                    raw_mean = sum(v for v, _ in vals) / len(vals)
                    is_cnt = any(c for _, c in vals)
                    tcells[mk] = {
                        "mean_raw": round(raw_mean, 4),
                        "pct": None if is_cnt else round(raw_mean * 100.0 if raw_mean <= 1.0 else raw_mean, 2),
                        "n": len(vals),
                    }
                task_rows_out.append({"task_id": tid, "cells": tcells})

            matrices.append(
                {
                    "group_key": grp["group_key"],
                    "title": grp["title"],
                    "source_kind": grp["source_kind"],
                    "submetrics": sub_cols,
                    "subject_rows": rows_out,
                    "task_rows": task_rows_out,
                }
            )

        matrices.sort(key=lambda m: (-len(m["submetrics"]), m["group_key"]))
        return {
            "methodology_version": METHODOLOGY_VERSION,
            "banner": banner,
            "facets": self._filter_facets(user),
            "matrices": matrices,
        }

    # 3. Efficiency, Token Semantics & Pareto
    def get_efficiency(
        self,
        user: AuthUser,
        *,
        harness: str | None = None,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        dataset_release_id: str | None = None,
        batch_id: str | None = None,
        x_metric: str = "duration_sec",
    ) -> dict[str, Any]:
        if x_metric not in ("duration_sec", "agent_execution_s", "total_tokens", "reasoning_tokens", "cost_usd"):
            x_metric = "duration_sec"
        where_sql, params = self._build_filter_clause(
            user,
            harness=harness,
            runner=runner,
            benchmark_slug=benchmark_slug,
            dataset_release_id=dataset_release_id,
            batch_id=batch_id,
        )
        banner = self._outcome_banner(where_sql, params)
        can_holdout = has_scope(user, "dataset:holdout_read")
        rows = self.db.fetchall(
            f"""
            SELECT e.batch_id, e.trial_id, e.task_id, e.status, e.reward,
                   e.duration_sec, e.phase_timings_json, e.input_tokens, e.output_tokens,
                   e.cached_tokens, e.reasoning_tokens, e.reasoning_key_source,
                   e.token_semantics, e.tool_call_count, e.cost_usd, e.cost_source,
                   b.batch_name, b.model_canonical, b.harness, b.runner, b.benchmark_slug,
                   b.dataset_release_id, b.raw_dataset_name, r.split AS rel_split
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {where_sql}
            """,
            params,
        )

        sem_counts = Counter()
        reasoning_key_counts = Counter()
        cached_observed = 0
        reasoning_observed = 0
        cost_recorded_observed = 0
        total_ep = len(rows)

        batch_agg: dict[str, dict[str, Any]] = {}
        trial_points: list[dict[str, Any]] = []

        for r in rows:
            sem_counts[str(r["token_semantics"] or "none")] += 1
            if r["reasoning_key_source"]:
                reasoning_key_counts[str(r["reasoning_key_source"])] += 1
            if r["cached_tokens"] is not None:
                cached_observed += 1
            if r["reasoning_tokens"] is not None:
                reasoning_observed += 1
            if r["cost_usd"] is not None:
                cost_recorded_observed += 1

            agent_exec_s: float | None = None
            if r["phase_timings_json"]:
                try:
                    pt = json.loads(r["phase_timings_json"])
                    if isinstance(pt, dict) and finite(pt.get("agent_execution_s")):
                        agent_exec_s = float(pt["agent_execution_s"])
                except Exception:
                    pass

            tot_tok: float | None = None
            if r["input_tokens"] is not None or r["output_tokens"] is not None:
                tot_tok = float((r["input_tokens"] or 0) + (r["output_tokens"] or 0))

            x_val_map = {
                "duration_sec": float(r["duration_sec"]) if finite(r["duration_sec"]) else None,
                "agent_execution_s": agent_exec_s,
                "total_tokens": tot_tok,
                "reasoning_tokens": float(r["reasoning_tokens"]) if r["reasoning_tokens"] is not None else None,
                "cost_usd": float(r["cost_usd"]) if finite(r["cost_usd"]) else None,
            }
            xv = x_val_map[x_metric]

            b = batch_agg.setdefault(
                r["batch_id"],
                {
                    "batch_id": r["batch_id"],
                    "batch_name": r["batch_name"],
                    "model_canonical": r["model_canonical"],
                    "harness": r["harness"],
                    "runner": r["runner"],
                    "benchmark_slug": r["benchmark_slug"],
                    "dataset_release_id": r["dataset_release_id"],
                    "token_semantics": r["token_semantics"] or "none",
                    "scores": [],
                    "x_vals": [],
                    "tool_calls": [],
                    "in_toks": [],
                    "out_toks": [],
                    "cached_toks": [],
                    "reason_toks": [],
                    "cost_sources": Counter(),
                    "recorded": 0,
                },
            )
            b["recorded"] += 1
            if r["status"] == "scored" and finite(r["reward"]):
                b["scores"].append(float(r["reward"]))
            if xv is not None:
                b["x_vals"].append(xv)
            if r["tool_call_count"] is not None:
                b["tool_calls"].append(int(r["tool_call_count"]))
            if r["input_tokens"] is not None:
                b["in_toks"].append(int(r["input_tokens"]))
            if r["output_tokens"] is not None:
                b["out_toks"].append(int(r["output_tokens"]))
            if r["cached_tokens"] is not None:
                b["cached_toks"].append(int(r["cached_tokens"]))
            if r["reasoning_tokens"] is not None:
                b["reason_toks"].append(int(r["reasoning_tokens"]))
            if r["cost_source"]:
                b["cost_sources"][r["cost_source"]] += 1

            if (
                (can_holdout or not self._is_row_holdout(r))
                and len(trial_points) < 400
                and r["status"] == "scored"
                and finite(r["reward"])
                and xv is not None
            ):
                trial_points.append(
                    {
                        "batch_id": r["batch_id"],
                        "trial_id": r["trial_id"],
                        "task_id": r["task_id"],
                        "model_canonical": r["model_canonical"],
                        "harness": r["harness"],
                        "x_value": round(xv, 4),
                        "reward_pct": round(float(r["reward"]) * 100.0 if float(r["reward"]) <= 1.0 else float(r["reward"]), 2),
                        "tool_call_count": r["tool_call_count"],
                    }
                )

        batch_points: list[dict[str, Any]] = []
        for b in batch_agg.values():
            if not b["scores"] or not b["x_vals"]:
                continue
            mean_score = sum(b["scores"]) / len(b["scores"])
            score_pct = round(mean_score * 100.0 if mean_score <= 1.0 else mean_score, 2)
            x_avg = round(sum(b["x_vals"]) / len(b["x_vals"]), 4)
            x_p50 = round(_percentile(b["x_vals"], 50.0) or x_avg, 4)
            x_cov = round(len(b["x_vals"]) / b["recorded"], 4) if b["recorded"] else 0.0
            batch_points.append(
                {
                    "batch_id": b["batch_id"],
                    "batch_name": b["batch_name"],
                    "model_canonical": b["model_canonical"],
                    "harness": b["harness"],
                    "runner": b["runner"],
                    "benchmark_slug": b["benchmark_slug"],
                    "dataset_release_id": b["dataset_release_id"],
                    "score_pct": score_pct,
                    "x_avg": x_avg,
                    "x_p50": x_p50,
                    "x_observed": len(b["x_vals"]),
                    "recorded": b["recorded"],
                    "x_coverage": x_cov,
                    "eligible_for_pareto": x_cov >= 0.9999,
                    "avg_tool_calls": round(sum(b["tool_calls"]) / len(b["tool_calls"]), 2) if b["tool_calls"] else None,
                    "token_semantics": b["token_semantics"],
                    "tokens": {
                        "input_avg": round(sum(b["in_toks"]) / len(b["in_toks"]), 1) if b["in_toks"] else None,
                        "output_avg": round(sum(b["out_toks"]) / len(b["out_toks"]), 1) if b["out_toks"] else None,
                        "cached_avg": round(sum(b["cached_toks"]) / len(b["cached_toks"]), 1) if b["cached_toks"] else None,
                        "reasoning_avg": round(sum(b["reason_toks"]) / len(b["reason_toks"]), 1) if b["reason_toks"] else None,
                        "can_stack_4seg": b["token_semantics"] == "mutually_exclusive_verified",
                    },
                    "cost_source": b["cost_sources"].most_common(1)[0][0] if b["cost_sources"] else "none",
                }
            )

        pareto_ids: set[str] = set()
        eligible = sorted([p for p in batch_points if p["eligible_for_pareto"]], key=lambda p: (p["x_avg"], -p["score_pct"]))
        best_y = -1.0
        for p in eligible:
            if p["score_pct"] > best_y:
                pareto_ids.add(p["batch_id"])
                best_y = p["score_pct"]
        for p in batch_points:
            p["on_pareto"] = p["batch_id"] in pareto_ids

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "banner": banner,
            "facets": self._filter_facets(user),
            "x_metric": x_metric,
            "honest_latency_notice": "严禁以总 Trial 耗时替代逐请求 TTFT / TPS；成本轴仅展示原生已记录成本 (recorded_native)，无账单对账严禁标为实付。",
            "token_coverage": {
                "total_trials": total_ep,
                "semantics_breakdown": dict(sem_counts),
                "reasoning_key_sources": dict(reasoning_key_counts),
                "cached_tokens_observed": cached_observed,
                "reasoning_tokens_observed": reasoning_observed,
                "cost_recorded_observed": cost_recorded_observed,
            },
            "batch_points": batch_points,
            "pareto_frontier_batch_ids": sorted(pareto_ids),
            "trial_points": trial_points,
        }

    # Shared helper: approval-checked, same-generation SHA-256 verified ATIF + gateway clue scan per harbor_runs batch (I-01)
    def _resolve_evidence_path(self, bdir: Path | None, ref: dict[str, Any] | None) -> Path | None:
        if not isinstance(ref, dict):
            return None
        rel = ref.get("rel_path")
        if not isinstance(rel, str) or not rel:
            return None
        if ref.get("storage_tier") == "evidence_store_0700":
            if self.settings.evidence_store_dir is None:
                return None
            return resolve_within_root(self.settings.evidence_store_dir, rel)
        if bdir is None or not bdir.is_dir():
            return None
        return resolve_within_root(bdir, rel)

    def _verified_evidence_summary(
        self,
        path: Path,
        *,
        kind: str,
        digest: str,
        generation_id: str,
        indexed_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        def mismatch() -> AppError:
            return AppError(
                409,
                "GENERATION_MISMATCH",
                "诊断证据已缺失或与当前索引发布代不一致，请重扫后刷新",
                {"generation_id": generation_id},
            )

        try:
            file_id = _evidence_file_identity(path.stat())
            if (
                indexed_summary
                and indexed_summary.get("sha256") == digest
                and indexed_summary.get("file_identity") == list(file_id)
            ):
                if kind == "atif" and indexed_summary.get("version") == ATIF_CLUE_SUMMARY_VERSION:
                    summary = indexed_summary.get("tool_summary")
                    if "tool_summary" in indexed_summary and (summary is None or isinstance(summary, dict)):
                        return summary
                if kind == "gateway" and indexed_summary.get("version") == GATEWAY_CLUE_SUMMARY_VERSION:
                    clue = indexed_summary.get("clue")
                    if "clue" in indexed_summary and (
                        clue is None or (isinstance(clue, dict) and clue.get("code") == "gateway_discarded_or_msg_drop")
                    ):
                        return {"clue": clue} if indexed_summary.get("valid_json", True) else None
            key = (kind, path, generation_id, digest, file_id)
            with self._tool_cache_lock:
                if key in self._evidence_cache:
                    self._evidence_cache.move_to_end(key)
                    return self._evidence_cache[key]
            with path.open("rb") as fh:
                if _evidence_file_identity(os.fstat(fh.fileno())) != file_id:
                    raise mismatch()
                raw = fh.read()
                if _evidence_file_identity(os.fstat(fh.fileno())) != file_id:
                    raise mismatch()
        except FileNotFoundError as exc:
            raise mismatch() from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise mismatch()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            data = None
        if not isinstance(data, dict):
            summary = None
        elif kind == "gateway":
            summary = {"clue": compute_gateway_clue(data)}
        elif isinstance(data.get("steps"), list):
            # Store the underlying maxima so custom thresholds apply without rescanning.
            summary = compute_atif_tool_and_clues(data, min_repeat=2, min_consec_errors=2)
        else:
            summary = None
        with self._tool_cache_lock:
            self._evidence_cache[key] = summary
            self._evidence_cache.move_to_end(key)
            if len(self._evidence_cache) > 4096:
                self._evidence_cache.popitem(last=False)
        return summary

    def _scan_batch_trajectories(
        self,
        batch_row: dict[str, Any],
        *,
        include_gateway: bool = True,
        user: AuthUser | None = None,
        holdout_pairs: set[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        batch_approval = str(batch_row.get("evidence_approval") or "pending")
        bname = str(batch_row["source_locator"]).partition(":")[2]
        bdir = None
        if bname and batch_approval in ("approved_public", "approved_restricted"):
            try:
                bdir = resolve_within_root(self.settings.harbor_runs_dir, bname)
            except AppError:
                pass
        source_approval = "pending"
        classes: frozenset[str] = frozenset()
        if bdir is not None:
            _, _, source_approval, classes = self.approval_registry.lookup_full(bdir)
        restricted = "approved_restricted" in (batch_approval, source_approval)
        source_approved = source_approval in ("approved_public", "approved_restricted")
        allow_atif = source_approved and "atif_trajectory" in classes and (
            user is None or not restricted or has_scope(user, "trace:read")
        )
        allow_gw = include_gateway and source_approved and "gateway_capture" in classes and (
            user is None or not restricted or has_scope(user, "gateway:read")
        )
        gen_id = str(batch_row.get("generation_id") or "")
        eps = self.db.fetchall(
            """
            SELECT trial_id, task_id, status, reward, tool_call_count, input_tokens, output_tokens,
                   duration_sec, evidence_refs_json, observable_clues_json
            FROM episodes WHERE batch_id = ? AND generation_id = ?
            ORDER BY trial_id
            """,
            (batch_row["batch_id"], gen_id),
        )
        if holdout_pairs is not None:
            rel = str(batch_row.get("dataset_release_id") or "")
            eps = [ep for ep in eps if (rel, str(ep["task_id"])) not in holdout_pairs]

        def summarize(
            ref: Any, kind: str, indexed_summary: dict[str, Any] | None = None,
        ) -> tuple[str, dict[str, Any] | None]:
            if not isinstance(ref, dict) or not ref.get("rel_path"):
                return "absent", None
            if ref.get("generation_id") not in (None, gen_id):
                raise AppError(409, "GENERATION_MISMATCH", "诊断证据引用与当前索引发布代不一致，请重扫后刷新")
            digest = ref.get("sha256")
            if "pending_file_identity" in ref:
                return ("pending" if ref["pending_file_identity"] else "unavailable"), None
            if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                return "pending", None
            try:
                path = self._resolve_evidence_path(bdir, ref)
                if path is None:
                    return "unavailable", None
                summary = self._verified_evidence_summary(
                    path, kind=kind, digest=digest.lower(), generation_id=gen_id, indexed_summary=indexed_summary,
                )
                return ("verified" if summary is not None else "unavailable"), summary
            except AppError as exc:
                if exc.code == "GENERATION_MISMATCH":
                    raise
            except OSError:
                pass
            return "unavailable", None

        def inspect_ep(ep: dict[str, Any]) -> dict[str, Any]:
            refs = json.loads(ep["evidence_refs_json"] or "{}") if allow_atif or allow_gw else {}
            indexed_by_source = {}
            if (allow_atif and refs.get("trajectory")) or (allow_gw and refs.get("gateway")):
                try:
                    indexed_rows = json.loads(ep["observable_clues_json"] or "[]")
                except ValueError:
                    indexed_rows = []
                if isinstance(indexed_rows, list):
                    indexed_by_source = {
                        row["source"]: row for row in indexed_rows
                        if isinstance(row, dict) and isinstance(row.get("source"), str)
                    }
            atif_state, atif = ("absent", None)
            if allow_atif:
                atif_state, atif = summarize(refs.get("trajectory"), "atif", indexed_by_source.get("atif_trajectory"))
            gw_state, gw = ("absent", None)
            if allow_gw:
                gw_state, gw = summarize(refs.get("gateway"), "gateway", indexed_by_source.get("gateway_capture"))
            clues = list(atif["clues"]) if atif else []
            if gw and gw["clue"]:
                clues.append(gw["clue"])
            return {
                **ep,
                "atif_state": atif_state,
                "gateway_state": gw_state,
                "has_tool_events": atif["has_tool_events"] if atif else False,
                "step_count": atif["step_count"] if atif else 0,
                "tool_call_count": atif["tool_call_count"] if atif else ep["tool_call_count"] or 0,
                "max_prompt_tokens": atif["max_prompt_tokens"] if atif else None,
                "tools": atif["tools"] if atif else {},
                "clues": clues,
            }

        if allow_atif or allow_gw:
            return list(self._evidence_workers.map(inspect_ep, eps))
        return [inspect_ep(ep) for ep in eps]

    # 4. Tool Call Ecosystem Profile
    def get_tools_profile(
        self,
        user: AuthUser,
        *,
        harness: str | None = None,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        dataset_release_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        where_sql, params = self._build_filter_clause(
            user,
            harness=harness,
            runner=runner,
            benchmark_slug=benchmark_slug,
            dataset_release_id=dataset_release_id,
            batch_id=batch_id,
        )
        banner = self._outcome_banner(where_sql, params)
        batches = self.db.fetchall(
            f"""
            SELECT b.batch_id, b.batch_name, b.source_namespace, b.source_locator, b.content_digest,
                   b.generation_id, b.trial_digests_sha256,
                   b.evidence_approval, b.runner, b.harness, b.harness_version, b.model_canonical, b.recorded
            FROM batches b
            WHERE {where_sql}
            """,
            params,
        )

        total_trials_in_scope = 0
        tool_recording_trials = 0
        excluded_harnesses: Counter[str] = Counter()
        included_harnesses: Counter[str] = Counter()

        tool_agg: dict[str, dict[str, Any]] = {}
        by_harness_tools: dict[str, Counter[str]] = defaultdict(Counter)

        pass_tool_counts: list[float] = []
        zero_tool_counts: list[float] = []
        pass_step_counts: list[float] = []
        zero_step_counts: list[float] = []
        pass_max_prompt: list[float] = []
        zero_max_prompt: list[float] = []

        for b in batches:
            if b["source_namespace"] != "harbor_runs" or b["runner"] != "harbor":
                rec = int(b["recorded"] or 0)
                total_trials_in_scope += rec
                label = f"{b['runner']}/{b['harness'] or 'default'}"
                excluded_harnesses[label] += rec
                continue

            trials = self._scan_batch_trajectories(b, include_gateway=False)
            h_name = str(b["harness"] or "unknown")
            h_label = f"{h_name}@{b['harness_version']}" if b["harness_version"] else h_name
            batch_has_tools = False
            for tr in trials:
                total_trials_in_scope += 1
                if not tr["has_tool_events"]:
                    excluded_harnesses[h_label] += 1
                    continue
                batch_has_tools = True
                tool_recording_trials += 1
                included_harnesses[h_label] += 1

                for fn, st in tr["tools"].items():
                    row = tool_agg.setdefault(fn, {"function_name": fn, "calls": 0, "errors": 0, "trials": 0})
                    row["calls"] += st["calls"]
                    row["errors"] += st["errors"]
                    row["trials"] += 1
                    by_harness_tools[h_name][fn] += st["calls"]

                if tr["status"] == "scored" and finite(tr["reward"]):
                    if float(tr["reward"]) > 0:
                        pass_tool_counts.append(float(tr["tool_call_count"]))
                        pass_step_counts.append(float(tr["step_count"]))
                        if tr["max_prompt_tokens"] is not None:
                            pass_max_prompt.append(float(tr["max_prompt_tokens"]))
                    elif float(tr["reward"]) == 0:
                        zero_tool_counts.append(float(tr["tool_call_count"]))
                        zero_step_counts.append(float(tr["step_count"]))
                        if tr["max_prompt_tokens"] is not None:
                            zero_max_prompt.append(float(tr["max_prompt_tokens"]))
            if not batch_has_tools and not trials:
                excluded_harnesses[h_label] += int(b["recorded"] or 0)

        def _dist_stats(vals: list[float]) -> dict[str, Any]:
            if not vals:
                return {"n": 0, "min": None, "p25": None, "p50": None, "p75": None, "p95": None, "max": None, "mean": None}
            return {
                "n": len(vals),
                "min": round(min(vals), 2),
                "p25": round(_percentile(vals, 25.0) or 0.0, 2),
                "p50": round(_percentile(vals, 50.0) or 0.0, 2),
                "p75": round(_percentile(vals, 75.0) or 0.0, 2),
                "p95": round(_percentile(vals, 95.0) or 0.0, 2),
                "max": round(max(vals), 2),
                "mean": round(sum(vals) / len(vals), 2),
            }

        tool_ranking = sorted(
            (
                {
                    "function_name": v["function_name"],
                    "calls": v["calls"],
                    "errors": v["errors"],
                    "error_rate_pct": round((v["errors"] / v["calls"]) * 100.0, 2) if v["calls"] else 0.0,
                    "trials": v["trials"],
                }
                for v in tool_agg.values()
            ),
            key=lambda x: (-x["calls"], x["function_name"]),
        )

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "banner": banner,
            "facets": self._filter_facets(user),
            "coverage_guard": {
                "tool_recording_trials": tool_recording_trials,
                "total_trials_in_scope": total_trials_in_scope,
                "coverage_pct": round((tool_recording_trials / total_trials_in_scope) * 100.0, 2)
                if total_trials_in_scope
                else 0.0,
                "included_harnesses": dict(included_harnesses),
                "excluded_harnesses": dict(excluded_harnesses),
                "note": "仅统计实际记录了 ATIF 工具事件的 Harbor Trial；未记录工具事件的 Harness（如 OpenClaw、VLMEvalKit、Direct）已自动排除，避免拉低或失真统计。",
            },
            "tool_ranking": tool_ranking,
            "by_harness": [
                {"harness": h, "total_calls": sum(c.values()), "top_tools": dict(c.most_common(10))}
                for h, c in sorted(by_harness_tools.items(), key=lambda kv: -sum(kv[1].values()))
            ],
            "pass_vs_valid_zero": {
                "tool_calls": {"pass": _dist_stats(pass_tool_counts), "valid_zero": _dist_stats(zero_tool_counts)},
                "step_count": {"pass": _dist_stats(pass_step_counts), "valid_zero": _dist_stats(zero_step_counts)},
                "max_prompt_tokens": {"pass": _dist_stats(pass_max_prompt), "valid_zero": _dist_stats(zero_max_prompt)},
            },
        }

    # 5. 4 Objective Trajectory Clues
    def get_objective_clues(
        self,
        user: AuthUser,
        *,
        harness: str | None = None,
        runner: str | None = None,
        benchmark_slug: str | None = None,
        dataset_release_id: str | None = None,
        batch_id: str | None = None,
        min_repeat: int = 3,
        min_consec_errors: int = 4,
        outlier_pct: float = 95.0,
    ) -> dict[str, Any]:
        where_sql, params = self._build_filter_clause(
            user,
            harness=harness,
            runner=runner,
            benchmark_slug=benchmark_slug,
            dataset_release_id=dataset_release_id,
            batch_id=batch_id,
        )
        banner = self._outcome_banner(where_sql, params)
        can_holdout = has_scope(user, "dataset:holdout_read")
        batches = self.db.fetchall(
            f"""
            SELECT b.batch_id, b.batch_name, b.source_namespace, b.source_locator, b.content_digest,
                   b.generation_id, b.trial_digests_sha256,
                   b.evidence_approval, b.runner, b.harness, b.model_canonical, b.benchmark_slug,
                   b.dataset_release_id, b.raw_dataset_name, r.split AS rel_split
            FROM batches b
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {where_sql}
            """,
            params,
        )

        holdout_pairs = None if can_holdout else self._holdout_task_pairs()
        if not can_holdout:
            batches = [batch for batch in batches if not self._is_row_holdout(batch)]

        def get_trials(batch: dict[str, Any]) -> list[dict[str, Any]]:
            if batch["source_namespace"] == "harbor_runs":
                return self._scan_batch_trajectories(batch, user=user, holdout_pairs=holdout_pairs)
            rows = self.db.fetchall(
                """
                SELECT trial_id, task_id, status, reward, input_tokens, output_tokens
                FROM episodes WHERE batch_id = ? AND generation_id = ? ORDER BY trial_id
                """,
                (batch["batch_id"], batch["generation_id"]),
            )
            rel = str(batch.get("dataset_release_id") or "")
            return [
                {**row, "atif_state": "absent", "gateway_state": "absent", "has_tool_events": False, "step_count": 0, "clues": []}
                for row in rows
                if holdout_pairs is None or (rel, str(row["task_id"])) not in holdout_pairs
            ]

        # Feed the shared file workers across batches so small batches don't serialize cold reads.
        with ThreadPoolExecutor(max_workers=6) as pool:
            trials_by_batch = list(pool.map(get_trials, batches))

        coverage: dict[str, Counter[str]] = {"atif": Counter(), "gateway": Counter()}
        tool_denom = 0
        step_or_tok_denom = 0

        cnt_repeat = cnt_consec_err = cnt_gw_drop = cnt_outlier = 0
        hits_repeat: list[dict[str, Any]] = []
        hits_consec_err: list[dict[str, Any]] = []
        hits_gw_drop: list[dict[str, Any]] = []
        hits_outlier: list[dict[str, Any]] = []

        for b, trials in zip(batches, trials_by_batch):
            batch_steps: list[float] = []
            batch_tokens: list[float] = []
            for tr in trials:
                if tr["step_count"] > 0:
                    batch_steps.append(float(tr["step_count"]))
                if tr["input_tokens"] is not None or tr["output_tokens"] is not None:
                    batch_tokens.append(float((tr["input_tokens"] or 0) + (tr["output_tokens"] or 0)))

            p_step = _percentile(batch_steps, outlier_pct)
            p_tok = _percentile(batch_tokens, outlier_pct)

            for tr in trials:
                base_item = {
                    "batch_id": b["batch_id"],
                    "batch_name": b["batch_name"],
                    "trial_id": tr["trial_id"],
                    "task_id": tr["task_id"],
                    "model_canonical": b["model_canonical"],
                    "harness": b["harness"],
                    "benchmark_slug": b["benchmark_slug"],
                    "dataset_release_id": b["dataset_release_id"],
                    "status": tr["status"],
                    "reward": tr["reward"],
                }
                if tr["has_tool_events"]:
                    tool_denom += 1
                for kind, counts in coverage.items():
                    counts[tr[f"{kind}_state"]] += 1
                has_step_or_tok = tr["step_count"] > 0 or tr["input_tokens"] is not None or tr["output_tokens"] is not None
                if has_step_or_tok:
                    step_or_tok_denom += 1

                for c in tr["clues"]:
                    if c["code"] == "repeated_identical_tool_call" and c.get("max_consecutive", 0) >= min_repeat:
                        cnt_repeat += 1
                        hits_repeat.append({**base_item, "evidence": {**c, "threshold": min_repeat}})
                    elif c["code"] == "consecutive_tool_errors" and c.get("max_consecutive", 0) >= min_consec_errors:
                        cnt_consec_err += 1
                        hits_consec_err.append({**base_item, "evidence": {**c, "threshold": min_consec_errors}})
                    elif c["code"] == "gateway_discarded_or_msg_drop":
                        cnt_gw_drop += 1
                        hits_gw_drop.append({**base_item, "evidence": c})

                if tr["status"] == "scored" and finite(tr["reward"]) and float(tr["reward"]) == 0.0 and has_step_or_tok:
                    tot_tok = (
                        float((tr["input_tokens"] or 0) + (tr["output_tokens"] or 0))
                        if (tr["input_tokens"] is not None or tr["output_tokens"] is not None)
                        else None
                    )
                    step_over = p_step is not None and tr["step_count"] > p_step and tr["step_count"] > 0
                    tok_over = p_tok is not None and tot_tok is not None and tot_tok > p_tok and tot_tok > 0
                    if step_over or tok_over:
                        cnt_outlier += 1
                        hits_outlier.append(
                            {
                                **base_item,
                                "evidence": {
                                    "code": "step_or_token_outlier",
                                    "step_count": tr["step_count"],
                                    "batch_step_threshold": round(p_step, 1) if p_step is not None else None,
                                    "total_tokens": int(tot_tok) if tot_tok is not None else None,
                                    "batch_token_threshold": round(p_tok, 1) if p_tok is not None else None,
                                    "percentile": outlier_pct,
                                },
                            }
                        )

        gw_denom = coverage["gateway"]["verified"]
        cards = [
            {
                "code": "repeated_identical_tool_call",
                "title": "连续重复相同工具调用",
                "formula": f"连续 ≥ {min_repeat} 步 tool_calls 出现相同 function_name 且参数规范化 SHA-256 一致",
                "applicable_denominator": tool_denom,
                "denominator_label": f"{tool_denom} 条含 ATIF 工具记录的 Harbor Trial",
                "hit_count": cnt_repeat,
                "hit_rate_pct": round((cnt_repeat / tool_denom) * 100.0, 2) if tool_denom else 0.0,
                "usage_note": "纯客观复现线索，仅供人工抽检是否陷入重复命令循环，不代表行为定性。",
                "trials": hits_repeat[:100],
            },
            {
                "code": "consecutive_tool_errors",
                "title": "连续工具返回错误",
                "formula": f"连续 ≥ {min_consec_errors} 次配对 observation.results[].is_error == true",
                "applicable_denominator": tool_denom,
                "denominator_label": f"{tool_denom} 条含 ATIF 工具记录的 Harbor Trial",
                "hit_count": cnt_consec_err,
                "hit_rate_pct": round((cnt_consec_err / tool_denom) * 100.0, 2) if tool_denom else 0.0,
                "usage_note": "快速定位连续命令报错或环境依赖缺失样本进入人工复核。",
                "trials": hits_consec_err[:100],
            },
            {
                "code": "gateway_discarded_or_msg_drop",
                "title": "网关轮次丢弃或消息窗口突降",
                "formula": "model-gateway.json 出现 discarded == true，或相邻轮次 len(request_messages) 下降 > 30%",
                "applicable_denominator": gw_denom,
                "denominator_label": f"{gw_denom} 份含 model-gateway.json 的网关捕获 Trial",
                "hit_count": cnt_gw_drop,
                "hit_rate_pct": round((cnt_gw_drop / gw_denom) * 100.0, 2) if gw_denom else 0.0,
                "usage_note": "客观观测上下文压缩、截断或回退分支的真实发生轮次。",
                "trials": hits_gw_drop[:100],
            },
            {
                "code": "step_or_token_outlier",
                "title": "高消耗有效 0 分离群样本",
                "formula": f"status == 'scored' 且 reward == 0，且步数或总 Token 高于同批 P{int(outlier_pct)}",
                "applicable_denominator": step_or_tok_denom,
                "denominator_label": f"{step_or_tok_denom} 条含步数或 Token 记录的 Trial",
                "hit_count": cnt_outlier,
                "hit_rate_pct": round((cnt_outlier / step_or_tok_denom) * 100.0, 2) if step_or_tok_denom else 0.0,
                "usage_note": "定位高消耗但未得分样本直开轨迹核查根因，严禁推断动机。",
                "trials": hits_outlier[:100],
            },
        ]

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "banner": banner,
            "facets": self._filter_facets(user),
            "honest_clue_policy": "仅输出 4 项 100% 客观可统计轨迹线索与精确适用覆盖分母，绝不输出任何无分类器支撑的主观定性标签。",
            "evidence_coverage": {
                kind: {
                    "verified_trials": counts["verified"],
                    "pending_trials": counts["pending"],
                    "unavailable_trials": counts["unavailable"],
                }
                for kind, counts in coverage.items()
            },
            "clues": cards,
        }
