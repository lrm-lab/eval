from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import enforce_csrf, get_current_user, require_admin
from app.api.errors import AppError
from app.db.migrations import sync_taxonomy_to_db
from app.logging_setup import (
    MAX_JOB_STDIO_BYTES,
    MAX_TOTAL_JOB_STDIO_BYTES,
)
from app.security.auth import AuthUser
from app.security.managed_exec import verify_managed_framework_bundle
from app.security.storage_probe import (
    get_mount_fstype,
    probe_isolated_dir,
    probe_secret_file,
    validate_safe_id,
)
from app.services.execution_service import is_docker_group_attached
from app.services.methodology import METHODOLOGY_REGISTRY, METHODOLOGY_VERSION

router = APIRouter(prefix="/api/v1", tags=["system"])


_DIR_SIZE_LOCK = threading.Lock()
_DIR_SIZE_CACHE: dict[str, tuple[float, int]] = {}
_DIR_SIZE_TTL_SEC = 15.0


def _invalidate_dir_size_cache() -> None:
    with _DIR_SIZE_LOCK:
        _DIR_SIZE_CACHE.clear()


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    key = str(path)
    now = time.monotonic()
    with _DIR_SIZE_LOCK:
        cached = _DIR_SIZE_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _DIR_SIZE_TTL_SEC:
            return cached[1]
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    with _DIR_SIZE_LOCK:
        _DIR_SIZE_CACHE[key] = (time.monotonic(), total)
    return total


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "methodology_version": METHODOLOGY_VERSION}


@router.get("/system/overview")
def system_overview(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.query.get_system_overview(user)


@router.get("/search")
def global_search(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=30, ge=1, le=100),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.search_global(user, q=q, limit=limit)


@router.get("/toolkit/methodology")
def get_methodology(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return {"methodology_version": METHODOLOGY_VERSION, "sections": METHODOLOGY_REGISTRY}


@router.get("/admin/health")
def admin_health(request: Request, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    settings = ctx.settings
    usage_root = shutil.disk_usage("/")
    free_gb = round(usage_root.free / (1024**3), 2)
    used_pct = round((usage_root.used / usage_root.total) * 100.0, 1)

    state_usage = shutil.disk_usage(settings.db_path.parent)
    state_free_gb = round(state_usage.free / (1024**3), 2)
    runtime_mb = round(_dir_size_bytes(settings.runtime_dir) / (1024**2), 2)

    probes: dict[str, Any] = {}
    for label, p in (
        ("state_dir", settings.state_dir),
        ("db_dir", settings.db_path.parent),
        ("staging_dir", settings.staging_dir),
        ("evidence_store_dir", settings.evidence_store_dir),
        ("runner_work_dir", settings.runner_work_dir),
    ):
        try:
            probes[label] = probe_isolated_dir(p, create=False)
        except AppError as exc:
            probes[label] = {"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail}
    try:
        probes["secrets_file"] = probe_secret_file(settings.secrets_file, create_parent=False)
    except AppError as exc:
        probes["secrets_file"] = {"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail}
    if settings.gateways_env_file is not None and (settings.gateways_env_file.exists() or settings.gateways_env_file.is_symlink()):
        try:
            probes["gateways_env_file"] = probe_secret_file(settings.gateways_env_file, create_parent=False)
        except AppError as exc:
            probes["gateways_env_file"] = {"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail}
    else:
        probes["gateways_env_file"] = {"ok": True, "configured": False, "note": "尚未创建 0600 gateways.env（凭据回退进程环境变量）"}
    probes["config_dir_fstype"] = get_mount_fstype(settings.config_dir)
    try:
        probes["managed_framework"] = verify_managed_framework_bundle(settings)
    except AppError as exc:
        probes["managed_framework"] = {"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail}

    disk_guard_ok = True
    disk_guard_error = None
    try:
        ctx.db.check_write_guard()
    except AppError as exc:
        disk_guard_ok = False
        disk_guard_error = exc.message

    runner_counts = ctx.db.fetchall(
        "SELECT runner, COUNT(*) AS batches, COALESCE(SUM(recorded), 0) AS trials FROM batches GROUP BY runner"
    )
    running_placeholders = ctx.db.fetchall(
        """
        SELECT batch_id, batch_name, runner, dataset_release_id, raw_dataset_name,
               planned, recorded, batch_status, indexed_at
        FROM batches
        WHERE is_running_placeholder = 1
        ORDER BY indexed_at DESC
        """
    )
    warnings = ctx.db.fetchall(
        "SELECT warning_id, tier, severity, code, target_path, message, detected_at FROM index_warnings ORDER BY warning_id DESC LIMIT 50"
    )
    cold_cache = ctx.db.fetchall(
        "SELECT cache_kind, status, COUNT(*) AS files, COALESCE(SUM(item_count), 0) AS items FROM cold_file_cache GROUP BY cache_kind, status"
    )
    cold_files = ctx.db.fetchall(
        "SELECT file_path, cache_kind, size_bytes, item_count, status, error_message, updated_at FROM cold_file_cache ORDER BY updated_at DESC LIMIT 60"
    )
    by_source = ctx.coordinator.cold.build_source_coverage_report()
    gateway_profiles = ctx.gateway_profiles.list_public_profiles() if hasattr(ctx, "gateway_profiles") else []
    exec_job_counts = ctx.db.fetchall("SELECT status, COUNT(*) AS c FROM exec_jobs GROUP BY status")
    runner_health = ctx.execution.probe_runner_health() if hasattr(ctx, "execution") else {
        "isolated_runner_ready": False,
        "runner_connected": False,
        "runner_configured": False,
        "docker_group_attached": is_docker_group_attached(),
    }
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "indexer": ctx.coordinator.snapshot_health(),
        "storage": {
            "production_mode": settings.production_mode,
            "root_free_gb": free_gb,
            "root_used_pct": used_pct,
            "state_fs_free_gb": state_free_gb,
            "root_disk_alert": free_gb < settings.root_disk_min_free_gb,
            "write_guard_ok": disk_guard_ok,
            "write_guard_error": disk_guard_error,
            "local_runtime_mb": runtime_mb,
            "local_budget_mb": settings.local_budget_mb,
            "budget_alert": runtime_mb >= 600.0,
            "log_budgets": {
                "daemon_rotating_logs_max_mb": 150,
                "per_job_stdio_max_mb": round(MAX_JOB_STDIO_BYTES / (1024 * 1024), 2),
                "cumulative_job_stdio_max_mb": round(MAX_TOTAL_JOB_STDIO_BYTES / (1024 * 1024), 2),
                "combined_all_logs_max_mb": round((150 * 1024 * 1024 + MAX_TOTAL_JOB_STDIO_BYTES) / (1024 * 1024), 2),
            },
            "isolation_probes": probes,
        },
        "source_coverage": {
            "by_runner": runner_counts,
            "running_placeholders": running_placeholders,
            "cold_cache": cold_cache,
            "cold_files": cold_files,
            "by_source": by_source,
            "warnings": warnings,
        },
        "gateway_profiles": gateway_profiles,
        "runner_status": {
            **runner_health,
            "jobs_by_status": {r["status"]: int(r["c"]) for r in exec_job_counts},
            "docker_group_attached": is_docker_group_attached(),
            "trusted_event_channel": "hmac_authenticated_private_0600_ndjson",
        },
    }


def _reload_all_registries(ctx: Any) -> None:
    _invalidate_dir_size_cache()
    ctx.coordinator.cold.invalidate_source_coverage_cache()
    ctx.coordinator.hot.approval_registry.reload()
    ctx.coordinator.cold.approval_registry.reload()
    ctx.query.approval_registry.reload()
    ctx.compare.approval_registry.reload()
    ctx.profiles.approval_registry.reload()
    if hasattr(ctx, "diagnostics"):
        ctx.diagnostics.approval_registry.reload()
    if hasattr(ctx, "synth"):
        ctx.synth.approval_registry.reload()
    if hasattr(ctx, "toolkit"):
        ctx.toolkit.approval_registry.reload()
    if hasattr(ctx, "gateway_profiles"):
        ctx.gateway_profiles.reload()
    if hasattr(ctx, "direct_lab"):
        ctx.direct_lab.reload_scorers()


@router.post("/admin/reindex", dependencies=[Depends(enforce_csrf)])
def admin_reindex_all(
    request: Request,
    force: bool = Query(default=False),
    include_cold: bool = Query(default=False),
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    _reload_all_registries(ctx)
    hot_res = ctx.coordinator.run_hot_now(force=force)
    cold_res = ctx.coordinator.run_cold_now(force=force) if include_cold else None
    return {"ok": True, "hot": hot_res, "cold": cold_res}


@router.post("/admin/reindex/{batch_id}", dependencies=[Depends(enforce_csrf)])
def admin_reindex_batch(
    batch_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    validate_safe_id(batch_id, "batch_id")
    ctx = request.app.state.ctx
    ctx.write_guard()
    _reload_all_registries(ctx)
    row = ctx.db.fetchone("SELECT source_namespace, source_locator FROM batches WHERE batch_id = ?", (batch_id,))
    if row is None:
        raise AppError(404, "RESOURCE_NOT_FOUND", f"批次不存在: {batch_id}")
    if row["source_namespace"] == "harbor_runs":
        bname = row["source_locator"].split(":", 1)[1]
        status = ctx.coordinator.hot.scan_single_harbor_batch(ctx.settings.harbor_runs_dir / bname, force=True)
        return {"ok": True, "batch_id": batch_id, "status": status}
    if row["source_namespace"] == "runner_published" and ctx.settings.published_batches_dir is not None:
        bname = row["source_locator"].split(":", 1)[1]
        status = ctx.coordinator.hot.scan_single_harbor_batch(
            ctx.settings.published_batches_dir / bname,
            force=True,
            source_namespace="runner_published",
        )
        return {"ok": True, "batch_id": batch_id, "status": status}
    res = ctx.coordinator.run_cold_now(force=True)
    return {"ok": True, "batch_id": batch_id, "cold": res}


@router.post("/admin/reindex-cold-file", dependencies=[Depends(enforce_csrf)])
def admin_reindex_cold_file(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Force-rebuild a single registered cold-tier cache target (02-8 §4)."""
    ctx = request.app.state.ctx
    ctx.write_guard()
    _reload_all_registries(ctx)
    target = str(payload.get("file_path") or "").strip()
    if not target:
        raise AppError(422, "SCHEMA_VALIDATION_FAILED", "必须提供已登记的 file_path")
    row = ctx.db.fetchone("SELECT file_path, cache_kind FROM cold_file_cache WHERE file_path = ?", (target,))
    if row is None:
        raise AppError(404, "RESOURCE_NOT_FOUND", "目标冷缓存文件未登记")
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM cold_file_cache WHERE file_path = ?", (target,))
    p = Path(target)
    kind = row["cache_kind"]
    if kind == "legacy_jsonl":
        res = ctx.coordinator.cold.index_legacy_jsonl_offsets(p.parent.name, force=True)
    elif kind == "legacy_json_dict":
        res = ctx.coordinator.cold.index_legacy_text_json_offsets(p.parent.name, force=True)
    else:
        res = ctx.coordinator.run_cold_now(force=True)
    return {"ok": True, "file_path": target, "cache_kind": kind, "result": res}


@router.post("/admin/taxonomy/reload", dependencies=[Depends(enforce_csrf)])
def admin_reload_taxonomy(request: Request, user: AuthUser = Depends(require_admin)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    counts = sync_taxonomy_to_db(ctx.db, ctx.settings.taxonomy_file)
    return {"ok": True, "synced": counts, "methodology_version": METHODOLOGY_VERSION}
