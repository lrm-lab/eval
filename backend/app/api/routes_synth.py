from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import enforce_csrf, get_current_user, require_researcher
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/synth", tags=["synth"])


@router.get("/datasets")
def list_synth_datasets(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.synth.list_synth_datasets(user)


@router.post("/generate", dependencies=[Depends(enforce_csrf)])
def generate_synth_dataset(
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.synth.generate_synth_dataset(user, body)


@router.post("/check", dependencies=[Depends(enforce_csrf)])
def check_synth_dataset(
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.synth.check_synth_dataset(user, body)


@router.get("/selection/batches")
def list_selection_batches(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.synth.list_selection_batches(user)


@router.get("/selection/{batch_id}")
def get_selection_board(
    batch_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.synth.get_selection_board(user, batch_id)


@router.post("/selection/{batch_id}/preview", dependencies=[Depends(enforce_csrf)])
def preview_selection(
    batch_id: str,
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.synth.preview_selection(user, batch_id, body)
