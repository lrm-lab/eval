from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from app.api.deps import enforce_csrf, get_current_user, require_admin, require_researcher
from app.api.errors import AppError
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/toolkit", tags=["toolkit"])


@router.post("/verify/{batch_id}", dependencies=[Depends(enforce_csrf)])
def verify_existing_batch(
    batch_id: str,
    request: Request,
    payload: dict[str, Any] | None = None,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    body = payload or {}
    return request.app.state.ctx.toolkit.verify_existing_batch(
        user,
        batch_id,
        check_smoke=bool(body.get("check_smoke", False)),
        check_gateway_capture=bool(body.get("check_gateway_capture", False)),
        check_training_captures=bool(body.get("check_training_captures", False)),
        pass_threshold=body.get("pass_threshold"),
    )


@router.post("/offline-calc", dependencies=[Depends(enforce_csrf)])
async def run_offline_calc(
    request: Request,
    file: UploadFile | None = File(default=None),
    report_name: str | None = Form(default=None),
    project_id: str = Form(default="default"),
    dataset_name: str | None = Form(default=None),
    model_name: str | None = Form(default=None),
    pass_threshold: float | None = Form(default=None),
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        body = await request.json()
        r_name = str(body.get("report_name") or "").strip()
        fname = str(body.get("filename") or "upload.json").strip()
        if body.get("content_base64") is not None:
            try:
                raw_bytes = base64.b64decode(str(body["content_base64"]), validate=True)
            except Exception as exc:
                raise AppError(422, "SCHEMA_VALIDATION_FAILED", "content_base64 编码无效") from exc
        else:
            raw_bytes = str(body.get("content") or "").encode("utf-8")
        return ctx.toolkit.run_offline_calc(
            user,
            report_name=r_name,
            filename=fname,
            raw_bytes=raw_bytes,
            project_id=str(body.get("project_id") or "default"),
            dataset_name=body.get("dataset_name"),
            model_name=body.get("model_name"),
            pass_threshold=body.get("pass_threshold"),
        )

    if file is None:
        raise AppError(422, "SCHEMA_VALIDATION_FAILED", "请上传离线评测结果文件或 JSON 负载")
    max_b = ctx.settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_b:
            raise AppError(
                413,
                "PAYLOAD_TOO_LARGE",
                f"离线上传包超过 {max_b // (1024 * 1024)} MB 上限",
                {"max_bytes": max_b},
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    fname = file.filename or "upload.json"
    return ctx.toolkit.run_offline_calc(
        user,
        report_name=(report_name or fname).strip(),
        filename=fname,
        raw_bytes=raw,
        project_id=project_id,
        dataset_name=dataset_name,
        model_name=model_name,
        pass_threshold=pass_threshold,
    )


@router.get("/offline-reports")
def list_offline_reports(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.toolkit.list_offline_reports(user)


@router.get("/offline-reports/{report_id}")
def get_offline_report(
    report_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.toolkit.get_offline_report(user, report_id)


@router.delete("/offline-reports/{report_id}", dependencies=[Depends(enforce_csrf)])
def delete_offline_report(
    report_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    return ctx.toolkit.delete_offline_report(user, report_id)


@router.put("/official-result/{batch_id}", dependencies=[Depends(enforce_csrf)])
def attach_official_result(
    batch_id: str,
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    return ctx.toolkit.attach_official_result(user, batch_id, payload)


@router.get("/params")
def list_param_groups(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.toolkit.list_param_groups(user)


@router.post("/params", dependencies=[Depends(enforce_csrf)])
def create_param_group(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    return ctx.toolkit.create_param_group(user, payload)


@router.delete("/params/{group_id}", dependencies=[Depends(enforce_csrf)])
def delete_param_group(
    group_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    return ctx.toolkit.delete_param_group(user, group_id)


@router.get("/manuals")
def list_manuals(
    request: Request,
    q: str | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.toolkit.list_manuals(q=q)


@router.get("/manuals/{doc_id}")
def get_manual(
    doc_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.toolkit.get_manual(doc_id)
