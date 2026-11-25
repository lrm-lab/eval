from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user
from app.security.auth import AuthUser
from app.services.methodology import METHODOLOGY_VERSION

router = APIRouter(prefix="/api/v1", tags=["leaderboard"])


@router.get("/leaderboard")
def get_leaderboard(
    request: Request,
    track_slug: str | None = Query(default=None),
    run_scope: str | None = Query(default=None),
    include_legacy: bool = Query(default=True),
    protocol: str | None = Query(default=None),
    harness: str | None = Query(default=None),
    runner: str | None = Query(default=None),
    source_namespace: str | None = Query(default=None),
    thinking_mode: str | None = Query(default=None),
    trained_only: bool = Query(default=False),
    has_atif: bool = Query(default=False),
    has_gateway: bool = Query(default=False),
    model_family: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_leaderboard(
        user,
        track_slug=track_slug,
        run_scope=run_scope,
        include_legacy=include_legacy,
        protocol=protocol,
        harness=harness,
        runner=runner,
        source_namespace=source_namespace,
        thinking_mode=thinking_mode,
        trained_only=trained_only,
        has_atif=has_atif,
        has_gateway=has_gateway,
        model_family=model_family,
        page=page,
        page_size=page_size,
    )


@router.get("/scores/deduplicated")
def get_deduplicated_scores(
    request: Request,
    scoring_protocol: str | None = Query(default=None),
    dataset_release_id: str | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Direct HTTP route exercising auth-before-dedup trial_protocol_scores window query (S-02 & A-13)."""
    rows = request.app.state.ctx.query.query_authorized_protocol_scores(
        user,
        scoring_protocol=scoring_protocol,
        dataset_release_id=dataset_release_id,
    )
    return {"items": rows, "total": len(rows), "methodology_version": METHODOLOGY_VERSION}


@router.get("/leaderboard/efficiency-scatter")
def get_efficiency_scatter(
    request: Request,
    dataset_release_id: str = Query(...),
    scoring_protocol: str = Query(default="harbor-native-reward-valid-v1"),
    x_axis: str = Query(default="duration_p50_sec"),
    task_set_policy: str = Query(default="fixed_y_common"),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_efficiency_scatter(
        user,
        dataset_release_id=dataset_release_id,
        scoring_protocol=scoring_protocol,
        x_axis=x_axis,
        task_set_policy=task_set_policy,
    )
