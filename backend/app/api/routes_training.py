from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import enforce_csrf, require_researcher
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/training", tags=["training"])


@router.get("/runs")
def list_training_runs(
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.training.list_runs(user)


@router.get("/runs/{run_id}")
def get_training_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.training.get_run_detail(user, run_id)


@router.post("/plan", dependencies=[Depends(enforce_csrf)])
def plan_training_cli(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.training.plan_training_cli(user, payload)
