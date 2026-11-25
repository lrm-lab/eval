from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/public-baselines", tags=["public-baselines"])


@router.get("")
def list_public_baselines(
    request: Request, user: AuthUser = Depends(get_current_user), include_text_leaders: bool = False,
) -> dict[str, Any]:
    return request.app.state.ctx.public_baselines.catalog(include_text_leaders=include_text_leaders)


@router.get("/compare")
def compare_public_baseline(
    request: Request,
    baseline_id: Annotated[str, Query(min_length=1, max_length=100)],
    batch_id: Annotated[list[str], Query(min_length=1, max_length=100)],
    user: AuthUser = Depends(get_current_user),
    require_same_model: bool = False,
) -> dict[str, Any]:
    return request.app.state.ctx.public_baselines.compare(
        user, baseline_id, batch_id, require_same_model=require_same_model,
    )


@router.get("/default")
def get_default_public_baseline(
    request: Request,
    batch_id: Annotated[list[str], Query(min_length=1, max_length=100)],
    user: AuthUser = Depends(get_current_user),
    require_same_model: bool = False,
) -> dict[str, str | None]:
    return request.app.state.ctx.public_baselines.default(user, batch_id, require_same_model=require_same_model)
