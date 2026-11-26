from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import enforce_csrf, require_researcher
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/direct-lab", tags=["direct-lab"])


@router.get("/capabilities")
def direct_lab_capabilities(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.direct_lab.get_capabilities(user)


@router.get("/suites")
def direct_lab_suites(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    caps = request.app.state.ctx.direct_lab.get_capabilities(user)
    return {
        "active_adapter_variant": caps.get("active_adapter_variant"),
        "extended_adapter_status": caps.get("extended_adapter_status"),
        "items": caps.get("suites") or [],
    }


@router.get("/scorers")
def direct_lab_scorers(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    caps = request.app.state.ctx.direct_lab.get_capabilities(user)
    return {"items": caps.get("scorers") or []}


@router.post("/validate", dependencies=[Depends(enforce_csrf)])
@router.post("/validate-cases", dependencies=[Depends(enforce_csrf)])
def direct_lab_validate(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    dlab = request.app.state.ctx.direct_lab
    images_map = dlab.decode_images_b64(payload.get("images_b64"))
    res = dlab.validate_cases_jsonl(
        str(payload.get("cases_jsonl") or ""),
        scorer_id=str(payload.get("scorer_id") or "exact_match"),
        images_map=images_map,
    )
    # Strip raw cases body from validation echo so response stays lightweight
    return {k: v for k, v in res.items() if k != "cases"}


@router.post("/runs", dependencies=[Depends(enforce_csrf)])
def direct_lab_create_run(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    return ctx.direct_lab.run_direct_batch(user, payload)


@router.get("/runs")
def direct_lab_list_runs(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.direct_lab.list_runs(user)


@router.get("/runs/{run_id}")
def direct_lab_get_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.direct_lab.get_run_detail(user, run_id)


@router.delete("/runs/{run_id}", dependencies=[Depends(enforce_csrf)])
def direct_lab_delete_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    ctx.write_guard()
    return ctx.direct_lab.delete_run(user, run_id)


@router.post("/compare", dependencies=[Depends(enforce_csrf)])
def direct_lab_compare_runs(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.direct_lab.compare_runs(
        user,
        str(payload.get("run_a_id") or ""),
        str(payload.get("run_b_id") or ""),
    )


@router.get("/runs/{run_id}/diff/{other_id}")
def direct_lab_diff_runs(
    run_id: str,
    other_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.direct_lab.compare_runs(user, run_id, other_id)
