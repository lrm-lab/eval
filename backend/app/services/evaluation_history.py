from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from sqlite3 import Connection
from typing import Any

from app.security.auth import AuthUser
from app.security.authz import (
    can_access_project,
    has_scope,
    is_holdout_dataset_or_batch,
    sql_project_filter,
    sql_task_holdout_exclude_clause,
)
from app.services.evaluation_reference import APPROVED_REFERENCE_EVIDENCE, EVALUATION_REFERENCES
from app.services.evaluation_time import _recorded_instant, evaluation_recorded_at
from app.services.profile_service import _comparison_identity, _recorded_batch_settings, _recorded_identity


_NONCOMPLETED_STATUSES = {
    "created", "pending", "queued", "starting", "preparing", "running", "paused", "cancelling", "retrying",
    "finalizing", "revoked", "withdrawn", "retracted", "deleted",
}
_RUN_SETTINGS = (
    "suite_id", "vlmevalkit_mode", "gateway_profile_id", "temperature", "max_tokens",
    "reasoning_effort", "timeout", "selection_mode", "top_p", "max_output_tokens",
    "max_steps", "api_mode", "wire_api",
)


def _configuration(row: dict[str, Any], identity: dict[str, Any]) -> tuple | None:
    try:
        config = json.loads(row["run_config_json"] or "{}")
    except ValueError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    # A gateway can use different API protocols at the same model route.
    if row["has_gateway"] and not any(config.get(key) for key in ("gateway_profile_id", "api_mode", "wire_api")):
        return None
    settings = json.dumps(
        {
            **_recorded_batch_settings(row),
            **{key: config.get(key) for key in _RUN_SETTINGS},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        identity["model_key"], identity["model_route"], row["model_provider"], identity["harness_key"],
        bool(row["has_gateway"]), row["run_scope"], row["fault_policy"], settings,
    )


def _visible(row, user: AuthUser, *, can_holdout: bool, allow_running: bool = False) -> bool:
    if row["evidence_approval"] == "revoked" or not can_access_project(
        user, row["project_id"], owner_id=row["owner_id"],
    ):
        return False
    status = str(row["batch_status"] or "").strip().lower()
    if status in _NONCOMPLETED_STATUSES and not (allow_running and status == "running"):
        return False
    return can_holdout or not is_holdout_dataset_or_batch(
        dataset_release_id=row["dataset_release_id"],
        release_split=row["split"],
        benchmark_slug=row["benchmark_slug"],
        raw_dataset_name=row["raw_dataset_name"],
        batch_name=row["batch_name"],
    )


def _reference(conn: Connection, row: dict[str, Any], *, can_holdout: bool) -> dict[str, Any]:
    at, source = evaluation_recorded_at(conn, row, can_holdout=can_holdout)
    return {
        **{key: row[key] for key in ("batch_id", "generation_id", "batch_name")},
        "recorded_at": at,
        "recorded_at_source": source,
    }


def _outcome(reason: str | None, **details: Any) -> dict[str, Any]:
    return {
        "reason": reason,
        "candidate": None,
        "baseline": None,
        "dataset_release_id": None,
        "split": None,
        "scoring_protocol": None,
        "metric_name": None,
        "common_valid": None,
        "common_covered": None,
        "baseline_score": None,
        "candidate_score": None,
        "delta_pp": None,
        **details,
    }


def _compare_history_item(
    user: AuthUser,
    conn: Connection,
    current: dict[str, Any],
    peers: list[dict[str, Any]],
    preview: Callable[..., dict[str, Any]],
    *,
    can_holdout: bool,
    allow_references: bool = False,
) -> dict[str, Any]:
    outcome = _outcome(
        None, candidate=_reference(conn, current, can_holdout=can_holdout),
        **{key: current[key] for key in ("dataset_release_id", "split", "scoring_protocol", "metric_name")},
    )
    current_time = _recorded_instant(outcome["candidate"]["recorded_at"])
    if None in current["peer_key"] or not current["dataset_release_id"] or not current["split"]:
        return {**outcome, "reason": "MISSING_IDENTITY"}
    if not _recorded_identity(current["scoring_protocol"]) or not current["metric_name"]:
        return {**outcome, "reason": "INCOMPARABLE_PROTOCOL"}
    if not current["scored"]:
        return {**outcome, "reason": "MISSING_VALID_SCORE"}
    if current_time is None:
        return {**outcome, "reason": "MISSING_RECORDED_TIME"}

    verified_configuration = current["configuration"] is not None
    if not verified_configuration and not allow_references:
        return {**outcome, "reason": "INCOMPARABLE_CONFIGURATION"}
    reasons = set() if verified_configuration else {"INCOMPARABLE_CONFIGURATION"}
    prior = []
    references = []
    for peer in peers:
        if peer["run_key"] == current["run_key"]:
            continue
        reference = _reference(conn, peer, can_holdout=can_holdout)
        when = _recorded_instant(reference["recorded_at"])
        if when is not None and when > current_time:
            continue
        same_configuration = verified_configuration and peer["configuration"] == current["configuration"]
        if not same_configuration and not allow_references:
            reasons.add("INCOMPARABLE_CONFIGURATION")
        elif peer["dataset_release_id"] != current["dataset_release_id"]:
            reasons.add("INCOMPARABLE_RELEASE")
        elif (peer["scoring_protocol"], peer["recorded_metric"]) != (
            current["scoring_protocol"], current["recorded_metric"],
        ):
            reasons.add("INCOMPARABLE_PROTOCOL")
        elif not peer["scored"]:
            reasons.add("MISSING_VALID_SCORE")
        elif when is None or when == current_time:
            reasons.add("MISSING_RECORDED_TIME")
        elif same_configuration:
            prior.append((when, reference))
        else:
            reasons.add("INCOMPARABLE_CONFIGURATION")
            if (
                peer["reference_identity"] == current["reference_identity"]
                and all(row["batch_status"] == "finished" and row["evidence_approval"] in APPROVED_REFERENCE_EVIDENCE
                        for row in (peer, current))
            ):
                references.append((when, reference))

    nearest_result = None
    for candidates, is_reference in ((prior, False), (references, True)):
        for _, reference in sorted(candidates, key=lambda pair: (pair[0], pair[1]["batch_id"]), reverse=True):
            score = preview(
                user,
                {
                    "id": current["batch_id"],
                    "baseline": reference,
                    "candidate": outcome["candidate"],
                    "dataset_release_id": current["dataset_release_id"],
                    "scoring_protocol": current["scoring_protocol"],
                },
                conn=conn,
            )
            score.pop("id")
            compared = {**outcome, **score, "baseline": reference}
            if compared["reason"] is None:
                if is_reference:
                    compared["reference_label"] = "同题批次对照 · 运行配置未确认一致"
                return compared
            if nearest_result is None:
                nearest_result = compared

    if nearest_result is not None:
        return nearest_result
    reason = next((code for code in (
        "MISSING_RECORDED_TIME", "MISSING_VALID_SCORE", "INCOMPARABLE_PROTOCOL",
        "INCOMPARABLE_RELEASE", "INCOMPARABLE_CONFIGURATION",
    ) if code in reasons), "NO_PREVIOUS_RUN")
    return {**outcome, "reason": reason}


def _reviewed_comparison(
    conn: Connection,
    user: AuthUser,
    item: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    preview: Callable[..., dict[str, Any]],
    *,
    can_holdout: bool,
) -> dict[str, Any]:
    candidate = rows.get(item["batch_id"])
    reference = EVALUATION_REFERENCES.get(item["batch_id"])
    if candidate is None or reference is None or item["generation_id"] != reference.candidate_generation:
        return _outcome("BATCH_UNAVAILABLE")
    baseline = rows.get(reference.baseline_id)
    if (
        baseline is None or baseline["generation_id"] != reference.baseline_generation
        or candidate["generation_id"] != reference.candidate_generation
    ):
        return _outcome("BATCH_UNAVAILABLE")

    context = ("dataset_release_id", "split", "scoring_protocol", "recorded_metric")
    if not all(candidate[key] and baseline[key] == candidate[key] for key in context):
        return _outcome("INCOMPARABLE_RELEASE")
    before = _reference(conn, baseline, can_holdout=can_holdout)
    after = _reference(conn, candidate, can_holdout=can_holdout)
    score = preview(
        user,
        {
            "id": item["id"], "baseline": before, "candidate": after,
            "dataset_release_id": candidate["dataset_release_id"], "scoring_protocol": candidate["scoring_protocol"],
        },
        conn=conn,
    )
    if score["reason"]:
        return _outcome(score["reason"])
    score.pop("id")
    outcome = _outcome(
        None, baseline=before, candidate=after,
        dataset_release_id=candidate["dataset_release_id"], split=candidate["split"],
        scoring_protocol=candidate["scoring_protocol"], reference_label=reference.label,
        reference_is_partial=any(row["batch_status"] != "finished" for row in (baseline, candidate)),
    )
    return {**outcome, **score}


def build_evaluation_history(
    conn: Connection,
    user: AuthUser,
    items: list[dict[str, Any]],
    preview: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    can_holdout = has_scope(user, "dataset:holdout_read")
    batch_ids = sorted({item["batch_id"] for item in items})
    placeholders = ",".join("?" for _ in batch_ids)
    project_filter, params = sql_project_filter(user, "b.project_id")
    public_task = sql_task_holdout_exclude_clause("b.dataset_release_id", "e.task_id")
    episode_generation = "e.batch_id = b.batch_id AND e.generation_id = b.generation_id"
    visible_trials = "" if can_holdout else f"""
        AND (
            NOT EXISTS (SELECT 1 FROM episodes e WHERE {episode_generation})
            OR EXISTS (SELECT 1 FROM episodes e WHERE {episode_generation} AND {public_task})
        )
    """
    rows = conn.execute(
        f"""
        SELECT b.batch_id, b.generation_id, b.batch_name, b.benchmark_slug,
               b.project_id, b.owner_id, b.evidence_approval, b.dataset_release_id, r.split,
               COALESCE(o.metric, r.metric_name) AS recorded_metric, r.metric_name,
               b.raw_dataset_name, b.model_canonical, b.model_route, b.model_provider, b.checkpoint,
               b.thinking_mode, b.harness, b.harness_version, b.runner, b.native_job_id,
               b.run_scope, b.run_config_json, b.scoring_protocol, b.fault_policy,
               b.batch_status, b.finished_at, b.recorded, b.scored, b.has_gateway,
               (SELECT j.job_id FROM exec_jobs j
                WHERE j.batch_id = b.batch_id AND j.project_id = b.project_id
                  AND j.owner_id = b.owner_id AND j.kind = b.runner
                ORDER BY j.job_id LIMIT 1) AS managed_job_id
        FROM batches b
        LEFT JOIN dataset_releases r ON r.dataset_release_id = b.dataset_release_id
        LEFT JOIN official_results o ON o.batch_id = b.batch_id
        WHERE {project_filter} AND b.is_running_placeholder = 0 {visible_trials}
          AND (b.batch_id IN ({placeholders}) OR b.benchmark_slug IN (
              SELECT benchmark_slug FROM batches WHERE batch_id IN ({placeholders})
          ))
        """,
        (*params, *batch_ids, *batch_ids),
    )
    by_id = {}
    by_identity = defaultdict(list)
    reviewed_ids = {
        batch_id
        for item in items
        if item.get("mode") == "reviewed" and (reference := EVALUATION_REFERENCES.get(item["batch_id"])) is not None
        for batch_id in (item["batch_id"], reference.baseline_id)
    }
    reviewed_rows = {}
    for raw in rows:
        if (
            raw["batch_id"] in reviewed_ids and raw["evidence_approval"] in APPROVED_REFERENCE_EVIDENCE
            and _visible(raw, user, can_holdout=can_holdout, allow_running=raw["batch_id"] in EVALUATION_REFERENCES)
        ):
            reviewed_rows[raw["batch_id"]] = dict(raw)
        if not _visible(raw, user, can_holdout=can_holdout):
            continue
        row = dict(raw)
        identity = _comparison_identity(row)
        row["configuration"] = _configuration(row, identity)
        row["peer_key"] = row["benchmark_slug"], identity["model_canonical"], identity["harness"]
        row["reference_identity"] = (
            identity["model_key"], identity["model_route"], row["model_provider"], identity["harness_key"],
            row["runner"], row["run_scope"], row["fault_policy"],
        )
        job_id = row["managed_job_id"] or row["native_job_id"]
        row["run_key"] = (
            ("job", row["project_id"], row["owner_id"], row["runner"], job_id)
            if job_id else ("batch", row["batch_id"])
        )
        by_id[row["batch_id"]] = row
        by_identity[row["peer_key"]].append(row)

    results = {}
    for item in items:
        mode = item.get("mode", "strict")
        key = item["batch_id"], item["generation_id"], mode
        if key in results:
            continue
        current = by_id.get(item["batch_id"])
        if mode == "reviewed":
            result = _reviewed_comparison(conn, user, item, reviewed_rows, preview, can_holdout=can_holdout)
        elif current is None:
            result = _outcome("BATCH_UNAVAILABLE")
        elif current["generation_id"] != item["generation_id"]:
            result = _outcome("RELEASE_GENERATION_MISMATCH")
        else:
            result = _compare_history_item(
                user, conn, current, by_identity[current["peer_key"]], preview, can_holdout=can_holdout,
                allow_references=mode == "auto",
            )
        results[key] = result
    return {"items": [
        {"id": item["id"], **results[item["batch_id"], item["generation_id"], item.get("mode", "strict")]}
        for item in items
    ]}
