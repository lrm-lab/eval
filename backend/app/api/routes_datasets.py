from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from app.api.deps import get_current_user
from app.api.errors import AppError
from app.security.auth import AuthUser
from app.security.authz import require_project_access, require_scope
from app.security.storage_probe import resolve_within_root, validate_safe_id

router = APIRouter(prefix="/api/v1", tags=["datasets"])


@router.get("/datasets/tree")
def get_datasets_tree(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.query.get_datasets_tree(user)


@router.get("/datasets")
def list_datasets(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    rows = ctx.db.fetchall(
        """
        SELECT r.*, bm.display_name, bm.track_slug, bm.track_display_name, bm.goal_type,
               bm.capability, bm.modality, bm.language, bm.status
        FROM dataset_releases r
        JOIN benchmarks bm ON bm.benchmark_slug = r.benchmark_slug
        ORDER BY bm.track_slug, bm.display_name, r.dataset_release_id
        """
    )
    return {"items": rows, "total": len(rows), "methodology_version": ctx.settings.methodology_version}


@router.get("/datasets/{dataset_release_id}/tasks")
def list_dataset_tasks(
    dataset_release_id: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.list_dataset_tasks(user, dataset_release_id, page=page, page_size=page_size)


@router.get("/datasets/{dataset_release_id}/tasks/{task_id:path}")
def get_dataset_task_detail(
    dataset_release_id: str,
    task_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_dataset_task_detail(user, dataset_release_id, task_id)


@router.get("/datasets/{dataset_release_id}/task-attachments/{attachment_name}")
def download_dataset_task_attachment(
    dataset_release_id: str,
    attachment_name: str,
    request: Request,
    task_id: str = Query(min_length=1, max_length=512),
    user: AuthUser = Depends(get_current_user),
) -> Response:
    content = request.app.state.ctx.query.get_dataset_task_attachment(
        user, dataset_release_id, task_id, attachment_name
    )
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(attachment_name)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/legacy/dirs")
def list_legacy_dirs(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.query.list_legacy_dirs(user)


@router.get("/legacy/{benchmark_dir}/samples")
def list_legacy_samples(
    benchmark_dir: str,
    request: Request,
    model_file: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.list_legacy_samples(
        user, benchmark_dir, model_file=model_file, page=page, page_size=page_size
    )


@router.get("/legacy/{benchmark_dir}/samples/{model_file}/{sample_id:path}")
def get_legacy_sample_detail(
    benchmark_dir: str,
    model_file: str,
    sample_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_legacy_sample_detail(user, benchmark_dir, model_file, sample_id)


@router.get("/media/{source}/tsv-inline/{tsv_key}")
def serve_tsv_inline_media(
    source: str,
    tsv_key: str,
    request: Request,
    idx: int = Query(default=0, ge=0, le=32),
    user: AuthUser = Depends(get_current_user),
) -> Response:
    validate_safe_id(source, "source")
    if not tsv_key or "/" in tsv_key or "\\" in tsv_key or ".." in tsv_key:
        raise AppError(400, "INVALID_PATH_OR_ID", "非法 tsv_key")
    require_scope(user, "direct_vlm_qa:read")
    ctx = request.app.state.ctx
    matcher = ctx.coordinator.cold.media_matcher
    root: Path | None = matcher.allowed_sources.get(source)
    if root is None or not root.is_dir():
        raise AppError(404, "RESOURCE_NOT_FOUND", f"未登记的媒体源: {source}")
    proj_id, owner_id, reg_status, reg_classes = ctx.query.approval_registry.lookup_full(root)
    require_project_access(user, proj_id, owner_id=owner_id)
    if reg_status == "pending" or "multimodal_media" not in reg_classes:
        raise AppError(403, "SCOPE_FORBIDDEN", f"媒体源未批准 multimodal_media 证据类: {source}")
    decoded = matcher.read_tsv_inline_image(source, tsv_key, idx=idx)
    if decoded is None:
        raise AppError(404, "RESOURCE_NOT_FOUND", f"TSV 内联图像不存在: {source}/{tsv_key}")
    content, mime = decoded
    return Response(content=content, media_type=mime)


@router.get("/media/{source}/{rel_path:path}")
def serve_registered_media(
    source: str,
    rel_path: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> Response:
    validate_safe_id(source, "source")
    require_scope(user, "direct_vlm_qa:read")
    ctx = request.app.state.ctx
    matcher = ctx.coordinator.cold.media_matcher
    root: Path | None = matcher.allowed_sources.get(source)
    if root is None or not root.is_dir():
        raise AppError(404, "RESOURCE_NOT_FOUND", f"未登记的媒体源: {source}")
    proj_id, owner_id, reg_status, reg_classes = ctx.query.approval_registry.lookup_full(root)
    require_project_access(user, proj_id, owner_id=owner_id)
    if reg_status == "pending" or "multimodal_media" not in reg_classes:
        raise AppError(403, "SCOPE_FORBIDDEN", f"媒体源未批准 multimodal_media 证据类: {source}")
    target = resolve_within_root(root, rel_path)
    t_proj_id, t_owner_id, t_reg_status, t_reg_classes = ctx.query.approval_registry.lookup_full(target)
    require_project_access(user, t_proj_id, owner_id=t_owner_id)
    if t_reg_status == "pending" or "multimodal_media" not in t_reg_classes:
        raise AppError(403, "SCOPE_FORBIDDEN", f"媒体文件未批准 multimodal_media 证据类: {source}")
    if not target.is_file():
        raise AppError(404, "RESOURCE_NOT_FOUND", "媒体文件不存在")
    ext = target.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
    return Response(content=target.read_bytes(), media_type=mime)
