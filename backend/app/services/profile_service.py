from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Any

from app.api.errors import AppError
from app.config import Settings
from app.db.database import Database
from app.indexing.hot_indexer import SourceApprovalRegistry
from app.security.auth import AuthUser
from app.security.authz import (
    authorized_projects_for_user,
    can_access_project,
    has_scope,
    is_holdout_dataset_or_batch,
    sql_task_holdout_exclude_clause,
)
from app.security.storage_probe import validate_safe_id
from app.services.evaluation_time import evaluation_recorded_at
from app.services.methodology import METHODOLOGY_VERSION, finite, normalize_score_to_100, normalize_trial_score_to_100
from app.services.query_service import _project_filter_sql, _sanitize_batch_row


# Known harness recording boundaries (factual Harbor/lrm-eval metadata only — zero fabricated commercial claims)
_HARNESS_BOUNDARY_NOTES: dict[str, str] = {
    "pi": "完整记录 ATIF 轨迹、工具调用与部分网关抓包；支持多轮 Agent 执行。",
    "pi-v5": "历史 2026-11-20 DeepSWE / TB2.1 Pi v5 适配器，含逐题 f2p/p2p 与原生验证日志。",
    "jhpPiV5": "原生 Harbor TB2.1 Pi v5 适配器，保留 4 阶段耗时与 verifier/test-stdout.txt。",
    "openclaw": "OpenClaw 2026.11.5 仅导出会话级消息摘要（no_tool_calls_recorded），不记录逐条工具调用事件。",
    "opencode": "记录完整 ATIF 步骤、工具调用与 reasoning_tokens；支持多尝试评测与网关捕获。",
    "codex": "原生 Harbor / Agentic 适配器，保留步骤与验证器结果。",
    "claude-code": "原生 Harbor / Agentic 适配器，保留步骤、Token 与验证器结果。",
    "vlmevalkit": "VLMEvalKit 多模态运行器（trajectory_status=model_request_response），支持官方汇总、学科分项与内联图像。",
    "direct": "Direct 轻量单轮问答运行器（direct_result），支持白名单评分器与网关抓包。",
    "oracle": "参考解（Reference / Oracle），仅用于校验题目可解性，严禁混入正式主榜。",
    "nop": "空操作基线（Reference / NOP），用于校验环境零通过基线。",
}


def _profile_key(prefix: str, *parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:32]}"


def _recorded_identity(value: str | None) -> str | None:
    return value if value and value.strip().lower() not in ("", "unknown", "unmapped") else None


def _comparison_identity(row: dict[str, Any]) -> dict[str, Any]:
    model = _recorded_identity(row["model_canonical"])
    route = _recorded_identity(row.get("model_route") or row.get("model_col"))
    checkpoint, thinking = row["checkpoint"], row["thinking_mode"]
    harness, version, runner = _recorded_identity(row.get("harness")), row.get("harness_version"), row["runner"]
    # The native Harbor runner and the CSV default fallback do not identify a harness.
    if (harness == runner == "harbor") or (runner == "legacy" and harness == "direct"):
        harness = None
    return {
        "model_key": _profile_key("model", model, checkpoint, thinking) if model else None,
        "harness_key": _profile_key("harness", harness, version, runner) if harness else None,
        "model_canonical": model,
        "model_route": route,
        "checkpoint": checkpoint,
        "thinking_mode": thinking,
        "harness": harness,
        "harness_version": version if harness else None,
        "runner": runner,
    }


def _recorded_batch_settings(row: dict[str, Any]) -> dict[str, int | None]:
    try:
        config = json.loads(row.get("run_config_json") or "{}")
    except ValueError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    attempts = config.get("attempts")
    timeout = config.get("timeout_seconds")
    return {
        "attempts_per_task": attempts if type(attempts) is int and attempts > 0 else None,
        "timeout_seconds": timeout if type(timeout) is int and timeout > 0 else None,
    }


def _visible_episode_filter(can_holdout: bool) -> str:
    return "" if can_holdout else f"AND {sql_task_holdout_exclude_clause('b.dataset_release_id', 'e.task_id')}"


def _observed_task_count_projection(episode_filter: str) -> str:
    return f"""(
        SELECT COUNT(DISTINCT e.task_id)
        FROM episodes e
        WHERE e.batch_id = b.batch_id AND e.generation_id = b.generation_id
          {episode_filter}
    ) AS observed_unique_tasks"""


def _task_details_allowed(row: dict[str, Any], split: str | None, *, can_holdout: bool) -> bool:
    return can_holdout or not is_holdout_dataset_or_batch(
        dataset_release_id=row["dataset_release_id"],
        release_split=split,
        benchmark_slug=row["benchmark_slug"],
        raw_dataset_name=row["raw_dataset_name"],
        batch_name=row["batch_name"],
    )


def _dataset_performance(batches: list[dict[str, Any]], *, can_holdout: bool) -> list[dict[str, Any]]:
    """Each score and denominator come from the authorized batch in this row."""
    rows = []
    for b in batches:
        metric_name = b["metric_name"]
        score, denominator, score_kind = None, None, "unscored"
        if b["dataset_release_id"] and _recorded_identity(b["scoring_protocol"]):
            if finite(b["official_score"]):
                metric_name = b["official_metric"]
                score = normalize_score_to_100(float(b["official_score"]), b["scale_type"])
                denominator, score_kind = b["official_denominator"], "official_summary"
            elif b["scored"] and finite(b["valid_mean"]):
                score = normalize_trial_score_to_100(float(b["valid_mean"]), b["scale_type"], b["scoring_protocol"])
                denominator, score_kind = b["scored"], "trial_valid_mean"
        rows.append({
            **_comparison_identity(b),
            **_recorded_batch_settings(b),
            **{field: b[field] for field in (
                "benchmark_slug", "dataset_release_id", "dataset_version", "split", "known_task_count",
                "scoring_protocol", "run_scope", "batch_status", "scored", "recorded", "planned", "unique_tasks",
                "batch_id", "generation_id", "batch_name", "identity_status", "finished_at", "indexed_at",
            )},
            "display_name": b["bm_display_name"] or b["raw_dataset_name"] or b["benchmark_slug"],
            "metric_name": metric_name,
            "observed_unique_tasks": (
                b["observed_unique_tasks"] if _task_details_allowed(b, b["split"], can_holdout=can_holdout) else None
            ),
            "score": round(score, 2) if score is not None else None,
            "denominator": denominator,
            "score_kind": score_kind,
        })
    return rows


def _grouped_comparison(
    releases: list[dict[str, Any]],
    batches: list[dict[str, Any]],
    historical: list[dict[str, Any]],
    *,
    can_holdout: bool,
) -> dict[str, Any]:
    release_by_id = {r["dataset_release_id"]: r for r in releases}
    contexts: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def context(release: dict[str, Any], protocol: str, metric: str, kind: str, scale: str) -> tuple[str, dict[str, Any]]:
        rel_id = release["dataset_release_id"]
        context_id = _profile_key("ctx", rel_id, protocol, metric, kind, scale)
        if context_id not in contexts:
            contexts[context_id] = {
                "context_id": context_id,
                "dataset_release_id": rel_id,
                "dataset_version": release["dataset_version"],
                "split": release["split"],
                "scoring_protocol": protocol,
                "metric_name": metric,
                "kind": kind,
                "groups": [],
            }
        return context_id, contexts[context_id]

    def group(
        active_context: tuple[str, dict[str, Any]], row: dict[str, Any], source_id: str,
    ) -> dict[str, Any]:
        context_id, entry = active_context
        identity = _comparison_identity(row)
        model_key, harness_key = identity["model_key"], identity["harness_key"]
        # Missing identity does not prove two runs used the same setup.
        group_id = _profile_key(
            "grp", context_id, model_key, harness_key, identity["model_route"],
            None if model_key and harness_key else source_id,
        )
        if group_id not in groups[context_id]:
            grouped = {
                "group_id": group_id,
                **identity,
                "default_batch_id": None,
                "batches": [],
                "historical_scores": [],
            }
            groups[context_id][group_id] = grouped
            entry["groups"].append(grouped)
        return groups[context_id][group_id]

    for row in batches:
        release = release_by_id.get(row["dataset_release_id"])
        protocol = _recorded_identity(row["scoring_protocol"])
        if row["is_running_placeholder"] or release is None or protocol is None:
            continue
        batch = {
            field: row[field]
            for field in (
                "batch_id", "batch_name", "generation_id", "run_scope", "planned", "recorded", "scored",
                "unique_tasks", "batch_status", "finished_at", "indexed_at",
            )
        }
        batch.update(_recorded_batch_settings(row))
        details_allowed = _task_details_allowed(row, release["split"], can_holdout=can_holdout)
        batch["task_details_allowed"] = details_allowed
        batch["observed_unique_tasks"] = row["observed_unique_tasks"] if details_allowed else None
        batch["has_visible_task_scores"] = details_allowed and bool(row["has_visible_task_scores"])
        batch["official_result"] = (
            {"metric": row["official_metric"], "score": row["official_score"], "denominator": row["official_denominator"]}
            if finite(row["official_score"]) else None
        )
        active_context = context(release, protocol, release["metric_name"], "trials", release["scale_type"])
        group(active_context, row, row["batch_id"])["batches"].append(batch)

    for row in historical:
        release = release_by_id.get(row["dataset_release_id"])
        if release is None or not finite(row["norm_score_100"]):
            continue
        active_context = context(release, "legacy-20261120-csv", row["metric"], "historical_summary", row["scale_type"])
        group(active_context, {**row, "runner": "legacy"}, row["model_col"])["historical_scores"].append(
            {
                "model_col": row["model_col"],
                "score": round(float(row["norm_score_100"]), 2),
                "raw_score": row["raw_score"],
                "scale_type": row["scale_type"],
                "denominator": None,
            }
        )

    for entry in contexts.values():
        for grouped in entry["groups"]:
            if grouped["batches"]:
                # Rows arrive newest first. Prefer a finished run with a visible task score.
                default = next(
                    (
                        b for b in grouped["batches"]
                        if b["batch_status"] in (None, "finished", "completed", "succeeded")
                        and b["has_visible_task_scores"]
                    ),
                    grouped["batches"][0],
                )
                grouped["default_batch_id"] = default["batch_id"]
    return {"contexts": list(contexts.values()), "empty_reason": None if contexts else "no_authorized_results"}


_ACTIVE_EVALUATION_STATUSES = frozenset({
    "created", "pending", "queued", "starting", "preparing", "running", "paused", "cancelling", "retrying",
})
_WITHDRAWN_EVALUATION_STATUSES = frozenset({"revoked", "withdrawn", "deleted"})
_EARLIEST_EVALUATION_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _evaluation_run_key(row: dict[str, Any]) -> tuple[str, ...]:
    job_id = row["managed_job_id"] or row["native_job_id"]
    if job_id:
        return "job", row["project_id"], row["owner_id"], row["runner"], job_id
    return "batch", row["batch_id"]


def _evaluation_group_order(batch: dict[str, Any], indexed_at: str) -> tuple[datetime, str, str]:
    recorded_at = batch["recorded_at"]
    instant = datetime.fromisoformat(recorded_at.replace("Z", "+00:00")) if recorded_at else _EARLIEST_EVALUATION_TIME
    return instant, indexed_at, batch["batch_id"]


def _evaluation_batch_projection(
    conn: Connection, row: dict[str, Any], *, can_holdout: bool, active: bool,
) -> dict[str, Any] | None:
    recorded, scored, planned = row["recorded"], row["scored"], row["planned"]
    valid_mean, official_score = row["valid_mean"], row["official_score"]
    recorded_at, recorded_at_source = None, None
    if row["has_hidden_trials"]:
        public_task = sql_task_holdout_exclude_clause("?", "e.task_id")
        visibility = conn.execute(
            f"""
            SELECT COUNT(*) AS recorded,
                   COUNT(CASE WHEN e.reward_valid AND e.reward IS NOT NULL THEN 1 END) AS scored,
                   AVG(CASE WHEN e.reward_valid THEN e.reward END) AS valid_mean
            FROM episodes e
            WHERE e.batch_id = ? AND e.generation_id = ? AND {public_task}
            """,
            (row["batch_id"], row["generation_id"], row["dataset_release_id"]),
        ).fetchone()
        if not visibility["recorded"]:
            return None
        recorded, scored, valid_mean = visibility["recorded"], visibility["scored"], visibility["valid_mean"]
        planned, official_score = None, None
    elif not active:
        recorded_at, recorded_at_source = evaluation_recorded_at(conn, row, can_holdout=can_holdout)

    score, score_kind = None, "unscored"
    if row["scale_type"] and _recorded_identity(row["scoring_protocol"]):
        if finite(official_score):
            score = normalize_score_to_100(official_score, row["scale_type"])
            score_kind = "official_summary"
        elif scored and finite(valid_mean):
            score = normalize_trial_score_to_100(valid_mean, row["scale_type"], row["scoring_protocol"])
            score_kind = "trial_valid_mean"
    return {
        **{key: row[key] for key in ("batch_id", "generation_id", "batch_name", "batch_status", "run_scope")},
        "recorded_at": recorded_at,
        "recorded_at_source": recorded_at_source,
        "score": round(score, 2) if finite(score) else None,
        "score_kind": score_kind,
        "scored": scored,
        "recorded": recorded,
        "planned": planned,
    }


def _evaluation_cell(
    row: dict[str, Any], *, kind: str, source_id: tuple[str, ...] | str,
) -> dict[str, Any]:
    identity = _comparison_identity(row)
    protocol = _recorded_identity(row["scoring_protocol"])
    missing = [
        key for key, value in (
            ("dataset_release_id", row["dataset_release_id"]), ("model_canonical", identity["model_canonical"]),
            ("harness", identity["harness"]), ("scoring_protocol", protocol),
        ) if not value
    ]
    settings = _recorded_batch_settings(row)
    cell_id = _profile_key(
        "cell", kind, row["benchmark_slug"], row["dataset_release_id"], protocol, row["metric_name"],
        row["scale_type"], identity["model_key"], identity["model_route"], identity["harness_key"],
        settings["attempts_per_task"], settings["timeout_seconds"], row.get("fault_policy"),
        source_id if missing else None,
    )
    return {
        "id": cell_id,
        "kind": kind,
        **identity,
        **settings,
        **{key: row[key] for key in ("benchmark_slug", "dataset_release_id", "dataset_version", "split", "metric_name")},
        "display_name": row["release_display_name"] or row["raw_dataset_name"] or row["benchmark_slug"],
        "scoring_protocol": protocol,
        "missing_identity": missing,
        "published_run_count": 0,
        "published_run_counts_by_scope": {},
        "running_count": 0,
        "latest_batch": None,
        "latest_scored_batch": None,
        "historical_scores": [],
    }


def _evaluation_sectors(
    cells: list[dict[str, Any]], releases: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_dimension = {
        "dataset": ("benchmark_slug", "display_name"), "model": ("model_canonical", "model_canonical"),
        "harness": ("harness", "harness"),
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for dimension, (id_key, label_key) in by_dimension.items():
        sectors: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for cell in cells:
            sectors[cell[id_key]].append(cell)
        groups = []
        for sector_id, members in sectors.items():
            release_ids = sorted({cell["dataset_release_id"] for cell in members if cell["dataset_release_id"]})
            weights = [releases[release_id]["registered_task_count"] for release_id in release_ids]
            known = [weight for weight in weights if weight is not None]
            groups.append({
                "id": sector_id or "__unmapped__",
                "label": (members[0][label_key] or sector_id) if sector_id else "未登记",
                "dataset_release_ids": release_ids,
                "registered_task_count": sum(known) if known else None,
                "unknown_release_count": len(weights) - len(known),
                "published_run_count": sum(cell["published_run_count"] for cell in members),
                "cell_ids": [cell["id"] for cell in members],
            })
        result[dimension] = sorted(groups, key=lambda group: (
            group["id"] == "__unmapped__", -(group["registered_task_count"] or 0), group["id"],
        ))
    return result


class ProfileService:
    """Entity Profiles (Models, Harnesses, Benchmarks, Hardest Tasks) & Constraint Recommender (M1).
    Strictly anchored to real evaluated Harbor / lrm-eval / 2026-11-20 fields with auth-before-dedup."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        approval_registry: SourceApprovalRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.approval_registry = approval_registry or SourceApprovalRegistry(settings.source_approval_file)

    def _can_see_legacy(self, user: AuthUser) -> bool:
        proj_id, owner_id, _, _ = self.approval_registry.lookup_full(self.settings.legacy_root_dir / "leaderboard.csv")
        return can_access_project(user, proj_id, owner_id=owner_id)

    def get_evaluation_map(self, user: AuthUser) -> dict[str, Any]:
        can_holdout = has_scope(user, "dataset:holdout_read")
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        public_task = sql_task_holdout_exclude_clause("b.dataset_release_id", "e.task_id")
        has_hidden_trials = "0" if can_holdout else f"""EXISTS (
            SELECT 1 FROM episodes e WHERE e.batch_id = b.batch_id AND e.generation_id = b.generation_id
              AND NOT {public_task}
        )"""
        releases: dict[str, dict[str, Any]] = {}
        cells: dict[str, dict[str, Any]] = {}
        latest: dict[str, tuple[bool, tuple[datetime, str, str]]] = {}
        latest_scored: dict[str, tuple[datetime, str, str]] = {}

        def add_release(row: dict[str, Any]) -> None:
            release_id = row["dataset_release_id"]
            if release_id and release_id not in releases:
                releases[release_id] = {
                    **{key: row[key] for key in ("dataset_release_id", "benchmark_slug", "dataset_version", "split")},
                    "display_name": row["release_display_name"],
                    "registered_task_count": row["known_task_count"],
                }

        with self.db.read_snapshot() as conn:
            rows = conn.execute(
                f"""
                SELECT b.batch_id, b.generation_id, b.batch_name, b.batch_status, b.run_scope,
                       b.project_id, b.owner_id, b.native_job_id, b.evidence_approval, b.is_running_placeholder,
                       b.raw_dataset_name, b.model_canonical, b.model_route, b.checkpoint, b.thinking_mode,
                       b.harness, b.harness_version, b.runner, b.scoring_protocol, b.fault_policy, b.run_config_json,
                       b.finished_at, b.indexed_at, b.recorded, b.scored, b.planned, b.valid_mean,
                       r.dataset_release_id, r.dataset_version, r.split, r.known_task_count, r.scale_type,
                       COALESCE(r.benchmark_slug, b.benchmark_slug) AS benchmark_slug,
                       r.metric_name, o.metric AS official_metric, o.score AS official_score,
                       bm.display_name AS release_display_name,
                       {has_hidden_trials} AS has_hidden_trials,
                       (SELECT j.job_id FROM exec_jobs j
                        WHERE j.batch_id = b.batch_id AND j.project_id = b.project_id
                          AND j.owner_id = b.owner_id AND j.kind = b.runner
                        ORDER BY j.job_id LIMIT 1) AS managed_job_id
                FROM batches b
                LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                LEFT JOIN benchmarks bm ON bm.benchmark_slug = COALESCE(r.benchmark_slug, b.benchmark_slug)
                LEFT JOIN official_results o ON o.batch_id = b.batch_id
                WHERE {proj_sql}
                ORDER BY b.is_running_placeholder, managed_job_id IS NULL, b.indexed_at DESC, b.batch_id
                """,
                params,
            )
            seen_runs = set()
            published = 0
            running = 0
            unmapped = 0
            for record in rows:
                row = dict(record)
                status = str(row["batch_status"] or "").strip().lower()
                if (
                    not can_access_project(user, row["project_id"], owner_id=row["owner_id"])
                    or not _task_details_allowed(row, row["split"], can_holdout=can_holdout)
                    or status in _WITHDRAWN_EVALUATION_STATUSES
                    or row["evidence_approval"] == "revoked"
                ):
                    continue
                run_key = _evaluation_run_key(row)
                if run_key in seen_runs:
                    continue
                active = status in _ACTIVE_EVALUATION_STATUSES
                batch = _evaluation_batch_projection(conn, row, can_holdout=can_holdout, active=active)
                if batch is None:
                    continue
                seen_runs.add(run_key)
                add_release(row)
                if batch["score_kind"] == "official_summary":
                    row["metric_name"] = row["official_metric"]
                cell = _evaluation_cell(row, kind="trials", source_id=run_key)
                cell_id = cell["id"]
                cell = cells.setdefault(cell_id, cell)

                is_published = (
                    not row["is_running_placeholder"] and not active
                    and (batch["recorded"] > 0 or batch["score_kind"] == "official_summary")
                )
                if is_published:
                    scope = row["run_scope"]
                    scope_counts = cell["published_run_counts_by_scope"]
                    scope_counts[scope] = scope_counts.get(scope, 0) + 1
                    cell["published_run_count"] += 1
                    published += 1
                if active:
                    cell["running_count"] += 1
                    running += 1
                unmapped += not row["is_running_placeholder"] and row["dataset_release_id"] is None

                order = _evaluation_group_order(batch, row["indexed_at"])
                display_order = active, order
                if cell_id not in latest or display_order > latest[cell_id]:
                    cell["latest_batch"] = batch
                    latest[cell_id] = display_order
                if is_published and batch["score"] is not None and (
                    cell_id not in latest_scored or order > latest_scored[cell_id]
                ):
                    cell["latest_scored_batch"] = batch
                    latest_scored[cell_id] = order

            historical_count = 0
            if self._can_see_legacy(user):
                historical_rows = conn.execute(
                    """
                    SELECT l.model_col, l.model_canonical, l.checkpoint, l.thinking_mode, l.harness,
                           l.metric AS metric_name, l.raw_score, l.norm_score_100, l.scale_type,
                           l.benchmark AS raw_dataset_name, l.benchmark AS batch_name,
                           COALESCE(r.benchmark_slug, l.benchmark_slug) AS benchmark_slug,
                           r.dataset_release_id, r.dataset_version, r.split, r.known_task_count,
                           bm.display_name AS release_display_name,
                           EXISTS (SELECT 1 FROM task_definitions t WHERE t.dataset_release_id = r.dataset_release_id
                                   AND LOWER(COALESCE(t.split, '')) = 'holdout') AS has_holdout_tasks,
                           'legacy' AS runner, 'legacy-20261120-csv' AS scoring_protocol
                    FROM legacy_leaderboard_rows l
                    LEFT JOIN dataset_releases r ON r.dataset_release_id = l.dataset_release_id
                    LEFT JOIN benchmarks bm ON bm.benchmark_slug = COALESCE(r.benchmark_slug, l.benchmark_slug)
                    ORDER BY l.model_col, l.benchmark, l.metric, l.id
                    """,
                )
                for record in historical_rows:
                    row = dict(record)
                    if (
                        not _task_details_allowed(row, row["split"], can_holdout=can_holdout)
                        or (not can_holdout and row["has_holdout_tasks"])
                        or not (finite(row["norm_score_100"]) and finite(row["raw_score"]))
                    ):
                        continue
                    add_release(row)
                    cell = _evaluation_cell(row, kind="historical_summary", source_id=(
                        row["model_col"], row["raw_dataset_name"], row["metric_name"],
                    ))
                    cell = cells.setdefault(cell["id"], cell)
                    score = {
                        "model_col": row["model_col"], "score": round(row["norm_score_100"], 2),
                        "raw_score": row["raw_score"], "denominator": None,
                    }
                    if score not in cell["historical_scores"]:
                        cell["historical_scores"].append(score)
                        historical_count += 1

            task_counts = conn.execute(
                """
                SELECT dataset_release_id, COUNT(*) AS total,
                       SUM(LOWER(COALESCE(split, '')) = 'holdout') AS holdout
                FROM task_definitions WHERE dataset_release_id IN (SELECT value FROM json_each(?))
                GROUP BY dataset_release_id
                """,
                (json.dumps(list(releases)),),
            )
            for count in task_counts:
                release = releases[count["dataset_release_id"]]
                if not can_holdout and count["holdout"]:
                    release["registered_task_count"] = count["total"] - count["holdout"]
                elif release["registered_task_count"] is None:
                    release["registered_task_count"] = count["total"]

        cell_list = list(cells.values())
        for cell in cell_list:
            release = releases.get(cell["dataset_release_id"])
            cell["registered_task_count"] = release["registered_task_count"] if release else None
        cell_list.sort(key=lambda cell: (-cell["published_run_count"], cell["id"]))
        return {
            "summary": {
                "cell_count": len(cell_list), "batch_count": len(seen_runs),
                "published_run_count": published, "running_count": running,
                "unmapped_batch_count": unmapped, "historical_summary_count": historical_count,
            },
            "releases": [releases[release_id] for release_id in sorted(releases)],
            "groups": _evaluation_sectors(cell_list, releases),
            "cells": cell_list,
        }

    # --- 1. Models ---

    def list_models(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = _project_filter_sql(user, "project_id")
        rows = self.db.fetchall(
            f"""
            SELECT model_canonical, model_route, checkpoint, thinking_mode,
                   harness, runner, benchmark_slug, dataset_release_id, valid_mean, run_scope
            FROM batches
            WHERE {proj_sql} AND model_canonical IS NOT NULL AND is_running_placeholder = 0
            """,
            params,
        )
        models: dict[str, dict[str, Any]] = {}

        def _ensure(mc: str) -> dict[str, Any]:
            if mc not in models:
                models[mc] = {
                    "model_canonical": mc,
                    "model_routes": set(),
                    "checkpoints": set(),
                    "thinking_modes": set(),
                    "harnesses": set(),
                    "runners": set(),
                    "benchmarks": set(),
                    "batch_count": 0,
                    "legacy_bench_count": 0,
                }
            return models[mc]

        for r in rows:
            m = _ensure(str(r["model_canonical"]))
            m["batch_count"] += 1
            if r.get("model_route"):
                m["model_routes"].add(str(r["model_route"]))
            if r.get("checkpoint"):
                m["checkpoints"].add(str(r["checkpoint"]))
            if r.get("thinking_mode"):
                m["thinking_modes"].add(str(r["thinking_mode"]))
            if r.get("harness"):
                m["harnesses"].add(str(r["harness"]))
            if r.get("runner"):
                m["runners"].add(str(r["runner"]))
            if r.get("benchmark_slug"):
                m["benchmarks"].add(str(r["benchmark_slug"]))

        if self._can_see_legacy(user):
            leg_rows = self.db.fetchall(
                "SELECT model_canonical, model_col, checkpoint, thinking_mode, harness, benchmark_slug FROM legacy_leaderboard_rows"
            )
            for lr in leg_rows:
                mc = str(lr["model_canonical"])
                m = _ensure(mc)
                m["legacy_bench_count"] += 1
                if lr.get("model_col"):
                    m["model_routes"].add(str(lr["model_col"]))
                if lr.get("checkpoint"):
                    m["checkpoints"].add(str(lr["checkpoint"]))
                if lr.get("thinking_mode"):
                    m["thinking_modes"].add(str(lr["thinking_mode"]))
                if lr.get("harness"):
                    m["harnesses"].add(str(lr["harness"]))
                m["runners"].add("legacy")
                if lr.get("benchmark_slug"):
                    m["benchmarks"].add(str(lr["benchmark_slug"]))

        items = []
        for mc, data in sorted(models.items(), key=lambda kv: (-kv[1]["batch_count"] - kv[1]["legacy_bench_count"], kv[0])):
            items.append(
                {
                    "model_canonical": mc,
                    "model_routes": sorted(data["model_routes"]),
                    "checkpoints": sorted(data["checkpoints"]),
                    "thinking_modes": sorted(data["thinking_modes"]),
                    "harnesses": sorted(data["harnesses"]),
                    "runners": sorted(data["runners"]),
                    "benchmark_count": len(data["benchmarks"]),
                    "batch_count": data["batch_count"],
                    "legacy_bench_count": data["legacy_bench_count"],
                }
            )
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def get_model_profile(self, user: AuthUser, model_canonical: str) -> dict[str, Any]:
        if not model_canonical or ".." in model_canonical or "\x00" in model_canonical:
            raise AppError(400, "INVALID_PATH_OR_ID", "非法模型标识")
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        can_holdout = has_scope(user, "dataset:holdout_read")
        observed_task_count = _observed_task_count_projection(_visible_episode_filter(can_holdout))
        batch_rows = self.db.fetchall(
            f"""
            SELECT b.*, o.metric AS official_metric, o.score AS official_score,
                   o.denominator AS official_denominator, o.source AS official_source,
                   bm.display_name AS bm_display_name, bm.track_slug, bm.track_display_name,
                   bm.goal_type, bm.capability, r.scale_type, r.dataset_version, r.split,
                   r.metric_name, r.known_task_count, {observed_task_count}
            FROM batches b
            LEFT JOIN official_results o ON o.batch_id = b.batch_id
            LEFT JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {proj_sql} AND b.model_canonical = ? AND b.is_running_placeholder = 0
            ORDER BY COALESCE(b.finished_at, b.indexed_at) DESC, b.batch_id
            """,
            [*params, model_canonical],
        )
        leg_rows: list[dict[str, Any]] = []
        if self._can_see_legacy(user):
            leg_rows = self.db.fetchall(
                """
                SELECT lr.*, bm.display_name AS bm_display_name, bm.track_slug,
                       bm.track_display_name, bm.goal_type, bm.capability,
                       r.dataset_version, r.split
                FROM legacy_leaderboard_rows lr
                LEFT JOIN benchmarks bm ON bm.benchmark_slug = lr.benchmark_slug
                LEFT JOIN dataset_releases r ON r.dataset_release_id = lr.dataset_release_id
                WHERE lr.model_canonical = ?
                ORDER BY bm.track_slug, lr.benchmark
                """,
                (model_canonical,),
            )

        if not batch_rows and not leg_rows:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"模型不存在或无授权批次: {model_canonical}")

        model_routes: set[str] = set()
        checkpoints: set[str] = set()
        harnesses: set[str] = set()
        runners: set[str] = set()
        thinking_modes: set[str] = set()

        # Auth-before-dedup trial scores for this model
        dedup_scores = self.db.fetchall(
            f"""
            WITH auth_tps AS (
                SELECT t.canonical_trial_key, t.scoring_protocol, t.batch_id, t.reward, t.reward_valid,
                       b.benchmark_slug, b.dataset_release_id, b.harness, b.harness_version, b.runner,
                       b.run_scope, b.checkpoint, b.thinking_mode,
                       bm.display_name AS bm_display_name, bm.track_slug, bm.track_display_name,
                       bm.goal_type, bm.capability, r.scale_type, r.dataset_version, r.split, r.metric_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                LEFT JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
                LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                WHERE {proj_sql} AND b.model_canonical = ? AND b.is_running_placeholder = 0
            )
            SELECT benchmark_slug, dataset_release_id, bm_display_name, track_slug, track_display_name,
                   goal_type, capability, scale_type, dataset_version, split, metric_name, harness,
                   harness_version, runner, run_scope,
                   scoring_protocol, checkpoint, thinking_mode,
                   MIN(batch_id) AS sample_batch_id,
                   COUNT(*) AS recorded,
                   SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END) AS scored,
                   AVG(CASE WHEN reward_valid = 1 THEN reward END) AS valid_mean
            FROM auth_tps
            WHERE rn = 1
            GROUP BY benchmark_slug, dataset_release_id, harness, harness_version, runner, run_scope,
                     scoring_protocol, checkpoint, thinking_mode
            """,
            [*params, model_canonical],
        )

        track_scores: dict[str, list[dict[str, Any]]] = defaultdict(list)
        harness_matrix: list[dict[str, Any]] = []
        checkpoint_series_map: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)

        for d in dedup_scores:
            raw_v = float(d["valid_mean"]) if d["valid_mean"] is not None else None
            norm_v = round(
                normalize_trial_score_to_100(raw_v, d["scale_type"] or "ratio_0_1", d["scoring_protocol"]), 2,
            ) if raw_v is not None else None
            entry = {
                "benchmark_slug": d["benchmark_slug"],
                "dataset_release_id": d["dataset_release_id"],
                "dataset_version": d["dataset_version"],
                "split": d["split"],
                "metric_name": d["metric_name"],
                "model_canonical": model_canonical,
                "display_name": d["bm_display_name"] or d["benchmark_slug"] or "Unmapped",
                "track_slug": d["track_slug"] or "custom",
                "track_display_name": d["track_display_name"] or "Custom",
                "goal_type": d["goal_type"] or "extended_baseline",
                "capability": d["capability"] or "Custom",
                "harness": d["harness"],
                "harness_version": d["harness_version"],
                "runner": d["runner"],
                "run_scope": d["run_scope"],
                "scoring_protocol": d["scoring_protocol"],
                "checkpoint": d["checkpoint"],
                "thinking_mode": d["thinking_mode"],
                "score": norm_v,
                "scored": int(d["scored"] or 0),
                "recorded": int(d["recorded"] or 0),
                "batch_id": d["sample_batch_id"],
                "source": "harbor_dedup",
            }
            track_scores[entry["track_slug"]].append(entry)
            harness_matrix.append(entry)
            if d.get("checkpoint") and d.get("dataset_release_id") and norm_v is not None:
                checkpoint_series_map[
                    (d["dataset_release_id"], d["scoring_protocol"], d["harness"] or "", d.get("thinking_mode") or "default")
                ].append(
                    {
                        "checkpoint": d["checkpoint"],
                        "score": norm_v,
                        "scored": int(d["scored"] or 0),
                        "batch_id": d["sample_batch_id"],
                    }
                )

        # Include legacy 2026-11-20 CSV scores (with verified base -> ck checkpoint pairs!)
        leg_by_col: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for lr in leg_rows:
            if lr.get("model_col"):
                model_routes.add(str(lr["model_col"]))
            if lr.get("checkpoint"):
                checkpoints.add(str(lr["checkpoint"]))
            if lr.get("thinking_mode"):
                thinking_modes.add(str(lr["thinking_mode"]))
            if lr.get("harness"):
                harnesses.add(str(lr["harness"]))
            runners.add("legacy")
            leg_by_col[str(lr["model_col"])].append(lr)
            entry = {
                "benchmark_slug": lr["benchmark_slug"],
                "dataset_release_id": lr["dataset_release_id"],
                "dataset_version": lr["dataset_version"],
                "split": lr["split"],
                "metric_name": lr["metric"],
                "display_name": lr["bm_display_name"] or lr["benchmark"],
                "track_slug": lr["track_slug"] or "extended",
                "track_display_name": lr["track_display_name"] or "Extended",
                "goal_type": lr["goal_type"] or "extended_baseline",
                "capability": lr["capability"] or "Legacy",
                "harness": lr["harness"],
                "harness_version": None,
                "runner": "legacy",
                "run_scope": "full_split",
                "scoring_protocol": "legacy-20261120-csv",
                "checkpoint": lr.get("checkpoint") or "base",
                "thinking_mode": lr.get("thinking_mode"),
                "model_col": lr["model_col"],
                "score": round(float(lr["norm_score_100"]), 2),
                "scored": None,
                "recorded": None,
                "batch_id": None,
                "source": "legacy_20261120_csv",
            }
            track_scores[entry["track_slug"]].append(entry)
            if lr.get("dataset_release_id"):
                checkpoint_series_map[
                    (lr["dataset_release_id"], "legacy-20261120-csv", lr["harness"] or "direct", lr.get("thinking_mode") or "default")
                ].append(
                    {
                        "checkpoint": lr.get("checkpoint") or "base",
                        "score": round(float(lr["norm_score_100"]), 2),
                        "model_col": lr["model_col"],
                        "batch_id": None,
                    }
                )

        sanitized_batches = []
        for b in batch_rows:
            if b.get("model_route"):
                model_routes.add(str(b["model_route"]))
            if b.get("checkpoint"):
                checkpoints.add(str(b["checkpoint"]))
            if b.get("harness"):
                harnesses.add(str(b["harness"]))
            if b.get("runner"):
                runners.add(str(b["runner"]))
            if b.get("thinking_mode"):
                thinking_modes.add(str(b["thinking_mode"]))
            off = None
            if b.get("official_score") is not None:
                off = {
                    "metric": b["official_metric"],
                    "score": b["official_score"],
                    "denominator": b["official_denominator"],
                    "source": b["official_source"],
                }
            b_copy = dict(b)
            for k in (
                "official_metric",
                "official_score",
                "official_denominator",
                "official_source",
                "bm_display_name",
                "track_slug",
                "track_display_name",
                "goal_type",
                "capability",
                "scale_type",
                "dataset_version",
                "split",
                "metric_name",
                "known_task_count",
                "observed_unique_tasks",
            ):
                b_copy.pop(k, None)
            dto = _sanitize_batch_row(b_copy)
            dto["official_result"] = off
            sanitized_batches.append(dto)

        # Only emit checkpoint_trend series that have >= 2 verified distinct checkpoints on SAME (release, protocol, harness, thinking_mode)!
        checkpoint_trends = []
        for (rel_id, proto, har, th_mode), pts in sorted(checkpoint_series_map.items()):
            distinct_cks = {p["checkpoint"] for p in pts}
            if len(distinct_cks) >= 2:
                # Deterministic order: base first, then sorted ck*
                pts_sorted = sorted(pts, key=lambda x: (x["checkpoint"] != "base", x["checkpoint"]))
                checkpoint_trends.append(
                    {
                        "dataset_release_id": rel_id,
                        "scoring_protocol": proto,
                        "harness": f"{har} ({th_mode})" if th_mode and th_mode != "default" else har,
                        "thinking_mode": th_mode,
                        "points": pts_sorted,
                    }
                )

        # Suggest 1-click Gain vs Guardrail compare pair if base & trained checkpoint exist in legacy CSV
        verdict_suggestion = None
        cols = sorted(leg_by_col.keys())
        base_col = next((c for c in cols if "_Trained_" not in c and not c.endswith(("_Pi", "_OpenClaw"))), None)
        trained_col = next((c for c in cols if "_Trained_" in c and ("_think" in c) == ("_think" in (base_col or ""))), None)
        if base_col and trained_col:
            verdict_suggestion = {
                "kind": "legacy_model",
                "baseline_col": base_col,
                "candidate_col": trained_col,
                "label": f"{base_col} vs {trained_col}",
            }

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "header": {
                "model_canonical": model_canonical,
                "model_routes": sorted(model_routes),
                "checkpoints": sorted(checkpoints),
                "harnesses": sorted(harnesses),
                "runners": sorted(runners),
                "thinking_modes": sorted(thinking_modes),
                "batch_count": len(sanitized_batches),
                "legacy_score_count": len(leg_rows),
            },
            "verdict_suggestion": verdict_suggestion,
            "track_scores": dict(track_scores),
            "harness_matrix": harness_matrix,
            "dataset_performance": _dataset_performance(batch_rows, can_holdout=can_holdout),
            "checkpoint_trends": checkpoint_trends,
            "batches": sanitized_batches,
        }

    # --- 2. Harnesses ---

    def list_harnesses(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        rows = self.db.fetchall(
            f"""
            SELECT COALESCE(b.harness, b.runner) AS harness_name,
                   b.runner,
                   COUNT(*) AS batch_count,
                   SUM(b.recorded) AS total_recorded,
                   SUM(b.scored) AS total_scored,
                   GROUP_CONCAT(DISTINCT b.harness_version) AS versions_csv,
                   SUM(b.has_gateway) AS gateway_batches
            FROM batches b
            WHERE {proj_sql} AND b.is_running_placeholder = 0
            GROUP BY COALESCE(b.harness, b.runner), b.runner
            ORDER BY batch_count DESC, harness_name ASC
            """,
            params,
        )
        # Count tool-call availability per harness
        tc_rows = self.db.fetchall(
            f"""
            SELECT COALESCE(b.harness, b.runner) AS harness_name,
                   COUNT(e.tool_call_count) AS tool_observed_trials,
                   AVG(e.tool_call_count) AS avg_tool_calls
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            WHERE {proj_sql} AND b.is_running_placeholder = 0
            GROUP BY COALESCE(b.harness, b.runner)
            """,
            params,
        )
        tc_map = {r["harness_name"]: r for r in tc_rows}

        items = []
        for r in rows:
            hname = str(r["harness_name"] or "unknown")
            tc = tc_map.get(hname) or {}
            versions = sorted({v for v in (r.get("versions_csv") or "").split(",") if v})
            items.append(
                {
                    "harness": hname,
                    "runner": r["runner"],
                    "versions": versions,
                    "batch_count": int(r["batch_count"] or 0),
                    "total_recorded": int(r["total_recorded"] or 0),
                    "total_scored": int(r["total_scored"] or 0),
                    "gateway_batches": int(r["gateway_batches"] or 0),
                    "tool_observed_trials": int(tc.get("tool_observed_trials") or 0),
                    "avg_tool_calls": round(float(tc["avg_tool_calls"]), 2) if tc.get("avg_tool_calls") is not None else None,
                    "recording_boundary": _HARNESS_BOUNDARY_NOTES.get(
                        hname,
                        f"前向兼容适配器 ({r['runner']})，按批次实际产物展示可用字段。",
                    ),
                }
            )
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def get_harness_profile(self, user: AuthUser, harness_name: str) -> dict[str, Any]:
        if not harness_name or ".." in harness_name or "\x00" in harness_name:
            raise AppError(400, "INVALID_PATH_OR_ID", "非法 Harness 标识")
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        can_holdout = has_scope(user, "dataset:holdout_read")
        observed_task_count = _observed_task_count_projection(_visible_episode_filter(can_holdout))
        batches = self.db.fetchall(
            f"""
            SELECT b.*, o.metric AS official_metric, o.score AS official_score,
                   o.denominator AS official_denominator, o.source AS official_source,
                   bm.display_name AS bm_display_name, r.scale_type, r.dataset_version, r.split,
                   r.metric_name, r.known_task_count, {observed_task_count}
            FROM batches b
            LEFT JOIN official_results o ON o.batch_id = b.batch_id
            LEFT JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
            LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
            WHERE {proj_sql} AND COALESCE(b.harness, b.runner) = ? AND b.is_running_placeholder = 0
            ORDER BY COALESCE(b.finished_at, b.indexed_at) DESC, b.batch_id
            """,
            [*params, harness_name],
        )
        if not batches:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"Harness 不存在或无授权批次: {harness_name}")

        versions = sorted({str(b["harness_version"]) for b in batches if b.get("harness_version")})
        runners = sorted({str(b["runner"]) for b in batches if b.get("runner")})

        tool_stats = self.db.fetchone(
            f"""
            SELECT COUNT(*) AS total_trials,
                   COUNT(e.tool_call_count) AS tool_observed_trials,
                   AVG(e.tool_call_count) AS avg_tool_calls,
                   MAX(e.tool_call_count) AS max_tool_calls,
                   AVG(e.duration_sec) AS avg_duration_sec,
                   COUNT(e.reasoning_tokens) AS reasoning_observed_trials
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            WHERE {proj_sql} AND COALESCE(b.harness, b.runner) = ? AND b.is_running_placeholder = 0
            """,
            [*params, harness_name],
        ) or {}

        # Auth-before-dedup cross-benchmark score matrix
        score_matrix = self.db.fetchall(
            f"""
            WITH auth_tps AS (
                SELECT t.canonical_trial_key, t.scoring_protocol, t.batch_id, t.reward, t.reward_valid,
                       b.model_canonical, b.benchmark_slug, b.dataset_release_id, b.run_scope,
                       bm.display_name AS bm_display_name, r.scale_type, r.dataset_version, r.split, r.metric_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                LEFT JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
                LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                WHERE {proj_sql} AND COALESCE(b.harness, b.runner) = ? AND b.is_running_placeholder = 0
            )
            SELECT model_canonical, benchmark_slug, dataset_release_id, bm_display_name,
                   run_scope, scoring_protocol, scale_type, dataset_version, split, metric_name,
                   MIN(batch_id) AS sample_batch_id,
                   COUNT(*) AS recorded,
                   SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END) AS scored,
                   AVG(CASE WHEN reward_valid = 1 THEN reward END) AS valid_mean
            FROM auth_tps
            WHERE rn = 1
            GROUP BY model_canonical, benchmark_slug, dataset_release_id, run_scope, scoring_protocol
            ORDER BY benchmark_slug ASC, model_canonical ASC
            """,
            [*params, harness_name],
        )
        matrix_out = []
        for s in score_matrix:
            raw_v = float(s["valid_mean"]) if s["valid_mean"] is not None else None
            matrix_out.append(
                {
                    "model_canonical": s["model_canonical"],
                    "benchmark_slug": s["benchmark_slug"],
                    "dataset_release_id": s["dataset_release_id"],
                    "dataset_version": s["dataset_version"],
                    "split": s["split"],
                    "metric_name": s["metric_name"],
                    "display_name": s["bm_display_name"] or s["benchmark_slug"] or "Unmapped",
                    "run_scope": s["run_scope"],
                    "scoring_protocol": s["scoring_protocol"],
                    "score": round(
                        normalize_trial_score_to_100(raw_v, s["scale_type"] or "ratio_0_1", s["scoring_protocol"]), 2,
                    ) if raw_v is not None else None,
                    "scored": int(s["scored"] or 0),
                    "recorded": int(s["recorded"] or 0),
                    "batch_id": s["sample_batch_id"],
                }
            )

        sanitized_batches = []
        for b in batches:
            off = None
            if b.get("official_score") is not None:
                off = {
                    "metric": b["official_metric"],
                    "score": b["official_score"],
                    "denominator": b["official_denominator"],
                    "source": b["official_source"],
                }
            bc = dict(b)
            for k in (
                "official_metric", "official_score", "official_denominator", "official_source",
                "bm_display_name", "scale_type", "dataset_version", "split", "metric_name", "known_task_count",
                "observed_unique_tasks",
            ):
                bc.pop(k, None)
            dto = _sanitize_batch_row(bc)
            dto["official_result"] = off
            sanitized_batches.append(dto)

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "harness": harness_name,
            "runners": runners,
            "versions": versions,
            "recording_boundary": _HARNESS_BOUNDARY_NOTES.get(
                harness_name,
                f"前向兼容适配器 ({', '.join(runners)})，按批次实际产物展示可用字段。",
            ),
            "tool_profile": {
                "has_tool_events": int(tool_stats.get("tool_observed_trials") or 0) > 0,
                "total_trials": int(tool_stats.get("total_trials") or 0),
                "tool_observed_trials": int(tool_stats.get("tool_observed_trials") or 0),
                "avg_tool_calls": round(float(tool_stats["avg_tool_calls"]), 2)
                if tool_stats.get("avg_tool_calls") is not None
                else None,
                "max_tool_calls": int(tool_stats["max_tool_calls"]) if tool_stats.get("max_tool_calls") is not None else None,
                "avg_duration_sec": round(float(tool_stats["avg_duration_sec"]), 2)
                if tool_stats.get("avg_duration_sec") is not None
                else None,
                "reasoning_observed_trials": int(tool_stats.get("reasoning_observed_trials") or 0),
            },
            "score_matrix": matrix_out,
            "dataset_performance": _dataset_performance(batches, can_holdout=can_holdout),
            "batches": sanitized_batches,
        }

    # --- 3. Benchmarks & Hardest Tasks ---

    def list_benchmarks(self, user: AuthUser) -> dict[str, Any]:
        proj_sql, params = _project_filter_sql(user, "project_id")
        bms = self.db.fetchall("SELECT * FROM benchmarks ORDER BY track_slug, display_name")
        rels = self.db.fetchall("SELECT * FROM dataset_releases ORDER BY dataset_release_id")
        cnts = self.db.fetchall(
            f"""
            SELECT benchmark_slug, COUNT(*) AS batch_count, SUM(recorded) AS recorded_trials
            FROM batches
            WHERE {proj_sql} AND benchmark_slug IS NOT NULL AND is_running_placeholder = 0
            GROUP BY benchmark_slug
            """,
            params,
        )
        cnt_map = {r["benchmark_slug"]: r for r in cnts}
        rels_by_bm: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rel in rels:
            rels_by_bm[rel["benchmark_slug"]].append(rel)

        items = []
        for bm in bms:
            slug = bm["benchmark_slug"]
            meta = json.loads(bm.get("metadata_json") or "{}")
            c = cnt_map.get(slug) or {}
            items.append(
                {
                    "benchmark_slug": slug,
                    "display_name": bm["display_name"],
                    "track_slug": bm["track_slug"],
                    "track_display_name": bm["track_display_name"],
                    "goal_type": bm["goal_type"],
                    "capability": bm["capability"],
                    "modality": bm["modality"],
                    "language": bm["language"],
                    "status": bm["status"],
                    "core_14": bool(meta.get("core_14")),
                    "batch_count": int(c.get("batch_count") or 0),
                    "recorded_trials": int(c.get("recorded_trials") or 0),
                    "releases": rels_by_bm.get(slug, []),
                }
            )
        return {"items": items, "total": len(items), "methodology_version": METHODOLOGY_VERSION}

    def get_benchmark_profile(self, user: AuthUser, benchmark_slug: str) -> dict[str, Any]:
        validate_safe_id(benchmark_slug, "benchmark_slug")
        bm = self.db.fetchone("SELECT * FROM benchmarks WHERE benchmark_slug = ?", (benchmark_slug,))
        if bm is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"基准不存在: {benchmark_slug}")
        bm_out = dict(bm)
        bm_out["metadata"] = json.loads(bm_out.pop("metadata_json") or "{}")

        rels = self.db.fetchall(
            "SELECT * FROM dataset_releases WHERE benchmark_slug = ? ORDER BY dataset_release_id",
            (benchmark_slug,),
        )
        proj_sql, params = _project_filter_sql(user, "b.project_id")
        can_holdout = has_scope(user, "dataset:holdout_read")
        episode_filter = _visible_episode_filter(can_holdout)
        batch_rows = self.db.fetchall(
            f"""
            SELECT b.*, o.metric AS official_metric, o.score AS official_score,
                   o.denominator AS official_denominator, o.source AS official_source,
                   {_observed_task_count_projection(episode_filter)},
                   EXISTS (
                       SELECT 1 FROM episodes e
                       WHERE e.batch_id = b.batch_id AND e.generation_id = b.generation_id
                         AND e.reward_valid = 1 AND e.reward IS NOT NULL
                         {episode_filter}
                   ) AS has_visible_task_scores
            FROM batches b
            LEFT JOIN official_results o ON o.batch_id = b.batch_id
            WHERE {proj_sql} AND b.benchmark_slug = ?
            ORDER BY b.is_running_placeholder DESC, COALESCE(b.finished_at, b.indexed_at) DESC, b.batch_id
            """,
            [*params, benchmark_slug],
        )
        sanitized_batches = []
        scatter_configs: list[dict[str, Any]] = []
        seen_sc: set[tuple[str, str]] = set()
        for b in batch_rows:
            if not b["is_running_placeholder"] and b.get("dataset_release_id") and b.get("scoring_protocol"):
                sc_key = (b["dataset_release_id"], b["scoring_protocol"])
                if sc_key not in seen_sc:
                    seen_sc.add(sc_key)
                    scatter_configs.append(
                        {"dataset_release_id": b["dataset_release_id"], "scoring_protocol": b["scoring_protocol"]}
                    )
            off = None
            if b.get("official_score") is not None:
                off = {
                    "metric": b["official_metric"],
                    "score": b["official_score"],
                    "denominator": b["official_denominator"],
                    "source": b["official_source"],
                }
            bc = dict(b)
            for k in (
                "official_metric", "official_score", "official_denominator", "official_source",
                "has_visible_task_scores", "observed_unique_tasks",
            ):
                bc.pop(k, None)
            dto = _sanitize_batch_row(bc)
            dto["official_result"] = off
            sanitized_batches.append(dto)

        # Mini leaderboard (auth-before-dedup) + legacy CSV rows for this benchmark
        mini_lb: list[dict[str, Any]] = []
        dedup = self.db.fetchall(
            f"""
            WITH auth_tps AS (
                SELECT t.canonical_trial_key, t.scoring_protocol, t.batch_id, t.reward, t.reward_valid,
                       b.model_canonical, b.harness, b.harness_version, b.runner, b.run_scope,
                       b.dataset_release_id, r.scale_type,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                WHERE {proj_sql} AND b.benchmark_slug = ? AND b.is_running_placeholder = 0
            )
            SELECT model_canonical, harness, harness_version, runner, run_scope,
                   dataset_release_id, scoring_protocol, scale_type,
                   MIN(batch_id) AS batch_id,
                   COUNT(*) AS recorded,
                   SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END) AS scored,
                   AVG(CASE WHEN reward_valid = 1 THEN reward END) AS valid_mean
            FROM auth_tps
            WHERE rn = 1
            GROUP BY model_canonical, harness, harness_version, runner, run_scope, dataset_release_id, scoring_protocol
            ORDER BY valid_mean DESC
            """,
            [*params, benchmark_slug],
        )
        for d in dedup:
            rv = float(d["valid_mean"]) if d["valid_mean"] is not None else None
            mini_lb.append(
                {
                    "model_canonical": d["model_canonical"],
                    "harness": d["harness"],
                    "harness_version": d["harness_version"],
                    "runner": d["runner"],
                    "run_scope": d["run_scope"],
                    "dataset_release_id": d["dataset_release_id"],
                    "scoring_protocol": d["scoring_protocol"],
                    "score": round(
                        normalize_trial_score_to_100(rv, d["scale_type"] or "ratio_0_1", d["scoring_protocol"]), 2,
                    ) if rv is not None else None,
                    "scored": int(d["scored"] or 0),
                    "recorded": int(d["recorded"] or 0),
                    "batch_id": d["batch_id"],
                }
            )
        historical = []
        if self._can_see_legacy(user):
            historical = self.db.fetchall(
                "SELECT * FROM legacy_leaderboard_rows WHERE benchmark_slug = ? ORDER BY norm_score_100 DESC, id",
                (benchmark_slug,),
            )
            for lr in historical:
                mini_lb.append(
                    {
                        "model_canonical": lr["model_col"],
                        "harness": lr["harness"],
                        "harness_version": None,
                        "runner": "legacy",
                        "run_scope": "full_split",
                        "dataset_release_id": lr["dataset_release_id"],
                        "scoring_protocol": "legacy-20261120-csv",
                        "score": round(float(lr["norm_score_100"]), 2),
                        "scored": None,
                        "recorded": None,
                        "batch_id": None,
                    }
                )
        mini_lb.sort(key=lambda x: (x["score"] is not None, x["score"] if x["score"] is not None else -1.0), reverse=True)
        release_by_id = {r["dataset_release_id"]: r for r in rels}
        performance_batches = [
            {
                **batch,
                "bm_display_name": bm["display_name"],
                **{
                    field: release_by_id.get(batch["dataset_release_id"], {}).get(field)
                    for field in ("scale_type", "dataset_version", "split", "metric_name", "known_task_count")
                },
            }
            for batch in batch_rows if not batch["is_running_placeholder"]
        ]

        # Task metadata breakdown (category & difficulty counts across non-holdout releases, or all if holdout_read)
        rel_ids = [
            r["dataset_release_id"]
            for r in rels
            if (
                str(r.get("split") or "").lower() != "holdout"
                and "holdout" not in str(r.get("dataset_release_id") or "").lower()
            )
            or has_scope(user, "dataset:holdout_read")
        ]
        domain_breakdown: list[dict[str, Any]] = []
        difficulty_breakdown: list[dict[str, Any]] = []
        task_total = 0
        if rel_ids:
            ph = ",".join("?" for _ in rel_ids)
            task_total = int(
                self.db.fetchone(f"SELECT COUNT(*) AS c FROM task_definitions WHERE dataset_release_id IN ({ph})", rel_ids)["c"]  # type: ignore[index]
            )
            domain_breakdown = self.db.fetchall(
                f"""
                SELECT COALESCE(category, 'unspecified') AS category, COUNT(*) AS count
                FROM task_definitions
                WHERE dataset_release_id IN ({ph})
                GROUP BY COALESCE(category, 'unspecified')
                ORDER BY count DESC
                LIMIT 20
                """,
                rel_ids,
            )
            difficulty_breakdown = self.db.fetchall(
                f"""
                SELECT COALESCE(difficulty, 'unspecified') AS difficulty, COUNT(*) AS count
                FROM task_definitions
                WHERE dataset_release_id IN ({ph})
                GROUP BY COALESCE(difficulty, 'unspecified')
                ORDER BY count DESC
                """,
                rel_ids,
            )

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "benchmark": bm_out,
            "releases": rels,
            "scatter_configs": scatter_configs,
            "mini_leaderboard": mini_lb,
            "dataset_performance": _dataset_performance(performance_batches, can_holdout=can_holdout),
            "grouped_comparison": _grouped_comparison(rels, batch_rows, historical, can_holdout=can_holdout),
            "task_summary": {
                "indexed_tasks": task_total,
                "by_category": domain_breakdown,
                "by_difficulty": difficulty_breakdown,
            },
            "batches": sanitized_batches,
        }

    def get_benchmark_hardest_tasks(
        self,
        user: AuthUser,
        benchmark_slug: str,
        *,
        limit: int = 30,
    ) -> dict[str, Any]:
        validate_safe_id(benchmark_slug, "benchmark_slug")
        bm = self.db.fetchone("SELECT * FROM benchmarks WHERE benchmark_slug = ?", (benchmark_slug,))
        if bm is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", f"基准不存在: {benchmark_slug}")

        proj_sql, params = _project_filter_sql(user, "b.project_id")
        holdout_clause = ""
        if not has_scope(user, "dataset:holdout_read"):
            holdout_clause = (
                "AND LOWER(COALESCE(r.split, '')) != 'holdout' "
                "AND LOWER(COALESCE(t.dataset_release_id, b.dataset_release_id, '')) NOT LIKE '%holdout%' "
                "AND LOWER(COALESCE(b.raw_dataset_name, '')) NOT LIKE '%holdout%' "
                "AND LOWER(COALESCE(b.batch_name, '')) NOT LIKE '%holdout%' "
                f"AND {sql_task_holdout_exclude_clause('COALESCE(t.dataset_release_id, b.dataset_release_id)', 't.task_id')}"
            )

        # Auth-before-dedup on primary batch protocol + paired (batch_id, trial_id) from the exact same episode row!
        rows = self.db.fetchall(
            f"""
            WITH auth_dedup AS (
                SELECT t.task_id, t.batch_id, t.trial_id, t.reward, t.reward_valid,
                       e.duration_sec,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                JOIN episodes e ON e.batch_id = t.batch_id AND e.trial_id = t.trial_id
                LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                WHERE {proj_sql}
                  AND b.benchmark_slug = ?
                  AND b.is_running_placeholder = 0
                  AND t.scoring_protocol = b.scoring_protocol
                  {holdout_clause}
            )
            SELECT task_id,
                   COUNT(*) AS total_attempts,
                   SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END) AS valid_attempts,
                   SUM(CASE WHEN reward_valid = 1 AND reward >= 1.0 THEN 1 ELSE 0 END) AS passed_attempts,
                   SUM(CASE WHEN reward_valid = 0 THEN 1 ELSE 0 END) AS fault_attempts,
                   AVG(CASE WHEN reward_valid = 1 THEN reward END) AS mean_reward,
                   AVG(duration_sec) AS avg_duration_sec,
                   MIN(batch_id || '|' || trial_id) AS sample_pair
            FROM auth_dedup
            WHERE rn = 1
            GROUP BY task_id
            HAVING valid_attempts > 0
            ORDER BY mean_reward ASC, fault_attempts DESC, total_attempts DESC
            LIMIT ?
            """,
            [*params, benchmark_slug, limit],
        )
        items = []
        for r in rows:
            valid_n = int(r["valid_attempts"] or 0)
            pass_n = int(r["passed_attempts"] or 0)
            mr = float(r["mean_reward"]) if r["mean_reward"] is not None else None
            pair_raw = str(r.get("sample_pair") or "")
            s_bid, _, s_tid = pair_raw.partition("|")
            items.append(
                {
                    "task_id": r["task_id"],
                    "total_attempts": int(r["total_attempts"] or 0),
                    "valid_attempts": valid_n,
                    "passed_attempts": pass_n,
                    "fault_attempts": int(r["fault_attempts"] or 0),
                    "pass_rate_pct": round((pass_n / valid_n) * 100.0, 2) if valid_n > 0 else None,
                    "mean_reward": round(mr, 4) if mr is not None else None,
                    "avg_duration_sec": round(float(r["avg_duration_sec"]), 2)
                    if r.get("avg_duration_sec") is not None
                    else None,
                    "sample_batch_id": s_bid,
                    "sample_trial_id": s_tid,
                }
            )
        return {
            "benchmark_slug": benchmark_slug,
            "items": items,
            "total": len(items),
            "methodology_version": METHODOLOGY_VERSION,
        }

    # --- 4. Constraint-Based Recommender (100% real Harbor/historical fields only) ---

    def recommend(self, user: AuthUser, payload: dict[str, Any]) -> dict[str, Any]:
        track_weights: dict[str, float] = payload.get("track_weights") or {
            "agentic": 0.5,
            "llm-text": 0.3,
            "vision-image": 0.2,
        }
        min_score = payload.get("min_score_pct")
        max_duration = payload.get("max_avg_duration_sec")
        max_tokens = payload.get("max_avg_tokens")
        allowed_harnesses = set(payload.get("allowed_harnesses") or [])
        allowed_runners = set(payload.get("allowed_runners") or [])
        formal_only = bool(payload.get("formal_only", False))

        proj_sql, params = _project_filter_sql(user, "b.project_id")
        where = [proj_sql, "b.is_running_placeholder = 0", "b.dataset_release_id IS NOT NULL", "t.scoring_protocol = b.scoring_protocol"]
        eff_where = [proj_sql, "b.is_running_placeholder = 0"]
        if formal_only:
            where.append("b.run_scope = 'full_split'")
            eff_where.append("b.run_scope = 'full_split'")

        rows = self.db.fetchall(
            f"""
            WITH auth_tps AS (
                SELECT t.canonical_trial_key, t.scoring_protocol, t.batch_id, t.reward, t.reward_valid,
                       b.model_canonical, b.harness, b.runner, b.run_scope, b.dataset_release_id,
                       bm.track_slug, r.scale_type,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.canonical_trial_key, t.scoring_protocol
                           ORDER BY t.source_priority_rank ASC, t.batch_id ASC
                       ) AS rn
                FROM trial_protocol_scores t
                JOIN batches b ON b.batch_id = t.batch_id
                JOIN benchmarks bm ON bm.benchmark_slug = b.benchmark_slug
                JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
                WHERE {' AND '.join(where)}
            )
            SELECT model_canonical, harness, runner, track_slug, dataset_release_id, scoring_protocol, scale_type,
                   MIN(batch_id) AS sample_batch_id,
                   SUM(CASE WHEN reward_valid = 1 THEN 1 ELSE 0 END) AS scored,
                   AVG(CASE WHEN reward_valid = 1 THEN reward END) AS valid_mean
            FROM auth_tps
            WHERE rn = 1
            GROUP BY model_canonical, harness, runner, track_slug, dataset_release_id, scoring_protocol
            """,
            params,
        )

        # Gather per (model_canonical, harness, runner) real efficiency metrics (aligned with formal_only scope!)
        eff_rows = self.db.fetchall(
            f"""
            SELECT b.model_canonical, COALESCE(b.harness, b.runner) AS harness, b.runner,
                   AVG(e.duration_sec) AS avg_duration_sec,
                   AVG(CASE WHEN e.input_tokens IS NOT NULL AND e.output_tokens IS NOT NULL
                            THEN e.input_tokens + e.output_tokens END) AS avg_total_tokens
            FROM episodes e
            JOIN batches b ON b.batch_id = e.batch_id
            WHERE {' AND '.join(eff_where)}
            GROUP BY b.model_canonical, COALESCE(b.harness, b.runner), b.runner
            """,
            params,
        )
        eff_map = {(r["model_canonical"], r["harness"], r["runner"]): r for r in eff_rows}

        candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in rows:
            mc = str(r.get("model_canonical") or "unknown")
            har = str(r.get("harness") or r.get("runner") or "unknown")
            run = str(r.get("runner") or "harbor")
            if allowed_harnesses and har not in allowed_harnesses:
                continue
            if allowed_runners and run not in allowed_runners:
                continue
            if r["valid_mean"] is None:
                continue
            key = (mc, har, run)
            if key not in candidates:
                eff = eff_map.get(key) or {}
                candidates[key] = {
                    "model_canonical": mc,
                    "harness": har,
                    "runner": run,
                    "track_buckets": defaultdict(list),
                    "batch_ids": set(),
                    "avg_duration_sec": round(float(eff["avg_duration_sec"]), 2)
                    if eff.get("avg_duration_sec") is not None
                    else None,
                    "avg_total_tokens": round(float(eff["avg_total_tokens"]), 1)
                    if eff.get("avg_total_tokens") is not None
                    else None,
                }
            score100 = normalize_score_to_100(float(r["valid_mean"]), r["scale_type"])
            candidates[key]["track_buckets"][r["track_slug"]].append(score100)
            candidates[key]["batch_ids"].add(r["sample_batch_id"])

        ranked: list[dict[str, Any]] = []
        for c in candidates.values():
            w_num = 0.0
            w_den = 0.0
            track_avgs: dict[str, float] = {}
            for ts, vals in c["track_buckets"].items():
                avg_t = math.fsum(vals) / len(vals)
                track_avgs[ts] = round(avg_t, 2)
                w = float(track_weights.get(ts, 0.1))
                if w > 0:
                    w_num += w * avg_t
                    w_den += w
            if w_den <= 0:
                continue
            weighted_score = round(w_num / w_den, 2)

            if finite(min_score) and weighted_score < float(min_score):
                continue
            if finite(max_duration) and (c["avg_duration_sec"] is None or c["avg_duration_sec"] > float(max_duration)):
                continue
            if finite(max_tokens) and (c["avg_total_tokens"] is None or c["avg_total_tokens"] > float(max_tokens)):
                continue

            notes = []
            if c["avg_duration_sec"] is not None:
                notes.append(f"平均耗时 {c['avg_duration_sec']:.1f}s")
            else:
                notes.append("耗时缺测")
            if c["avg_total_tokens"] is not None:
                notes.append(f"均总 Token {c['avg_total_tokens']:.0f}")
            ranked.append(
                {
                    "subject_id": f"model:{c['model_canonical']}|harness:{c['harness']}",
                    "model_canonical": c["model_canonical"],
                    "harness": c["harness"],
                    "runner": c["runner"],
                    "weighted_score": weighted_score,
                    "track_scores": track_avgs,
                    "covered_tracks": len(track_avgs),
                    "avg_duration_sec": c["avg_duration_sec"],
                    "avg_total_tokens": c["avg_total_tokens"],
                    "sample_batch_ids": sorted(c["batch_ids"]),
                    "pareto_optimal": False,
                    "tradeoff_note": " · ".join(notes),
                }
            )

        ranked.sort(key=lambda x: (-x["weighted_score"], x["avg_duration_sec"] if x["avg_duration_sec"] is not None else 1e9))
        # Mark Pareto frontier over (weighted_score↑, avg_duration_sec↓) for candidates with valid duration
        with_dur = [r for r in ranked if r["avg_duration_sec"] is not None]
        with_dur.sort(key=lambda x: (x["avg_duration_sec"], -x["weighted_score"]))
        best_s = -1e9
        for r in with_dur:
            if r["weighted_score"] > best_s:
                r["pareto_optimal"] = True
                best_s = r["weighted_score"]

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "constraints": {
                "track_weights": track_weights,
                "min_score_pct": min_score,
                "max_avg_duration_sec": max_duration,
                "max_avg_tokens": max_tokens,
                "formal_only": formal_only,
            },
            "items": ranked,
            "total": len(ranked),
        }
