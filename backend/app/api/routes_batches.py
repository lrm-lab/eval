from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from app.api.deps import get_current_user, require_researcher
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])


@router.get("")
def list_batches(
    request: Request,
    runner: str | None = Query(default=None),
    benchmark_slug: str | None = Query(default=None),
    run_scope: str | None = Query(default=None),
    scoring_protocol: str | None = Query(default=None),
    source_namespace: str | None = Query(default=None),
    model_canonical: str | None = Query(default=None),
    harness: str | None = Query(default=None),
    batch_status: str | None = Query(default=None),
    modality: str | None = Query(default=None),
    language: str | None = Query(default=None),
    has_gateway: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.list_batches(
        user,
        runner=runner,
        benchmark_slug=benchmark_slug,
        run_scope=run_scope,
        scoring_protocol=scoring_protocol,
        source_namespace=source_namespace,
        model_canonical=model_canonical,
        harness=harness,
        batch_status=batch_status,
        modality=modality,
        language=language,
        has_gateway=has_gateway,
        q=q,
        page=page,
        page_size=page_size,
    )


@router.get("/{batch_id}")
def get_batch_detail(
    batch_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_batch_detail(user, batch_id)


@router.get("/{batch_id}/tasks")
def get_batch_tasks(
    batch_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_batch_tasks(user, batch_id)


@router.get("/{batch_id}/episodes")
def get_batch_episodes(
    batch_id: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_batch_episodes(user, batch_id, page=page, page_size=page_size)


@router.get("/{batch_id}/trials/{trial_id}")
def get_trial_detail(
    batch_id: str,
    trial_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.get_trial_detail(user, batch_id, trial_id)


@router.get("/{batch_id}/trials/{trial_id}/runner-evidence")
def get_runner_trial_evidence(
    batch_id: str,
    trial_id: str,
    request: Request,
    response: Response,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    job_id, generation = ctx.query.get_runner_trial_binding(user, batch_id, trial_id)
    response.headers["Cache-Control"] = "private, no-store"
    return ctx.execution.get_authorized_trial_evidence(
        user, job_id, trial_id, expected_batch=(batch_id, generation),
    )


@router.get("/{batch_id}/trials/{trial_id}/trajectory")
def get_trial_trajectory(
    batch_id: str,
    trial_id: str,
    request: Request,
    step_offset: int = Query(default=0, ge=0),
    step_limit: int = Query(default=50, ge=1, le=200),
    only_tools: bool = Query(default=False),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.read_trial_trajectory(
        user,
        batch_id,
        trial_id,
        step_offset=step_offset,
        step_limit=step_limit,
        only_tools=only_tools,
    )


@router.get("/{batch_id}/trials/{trial_id}/gateway")
def get_trial_gateway(
    batch_id: str,
    trial_id: str,
    request: Request,
    turn_offset: int = Query(default=0, ge=0),
    turn_limit: int = Query(default=10, ge=1, le=50),
    include_tools: bool = Query(default=False),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.read_trial_gateway(
        user,
        batch_id,
        trial_id,
        turn_offset=turn_offset,
        turn_limit=turn_limit,
        include_tools=include_tools,
    )


@router.get("/{batch_id}/trials/{trial_id}/verifier-output")
def get_trial_verifier_output(
    batch_id: str,
    trial_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.read_trial_verifier_output(user, batch_id, trial_id)


@router.get("/{batch_id}/trials/{trial_id}/artifacts")
def get_trial_artifacts(
    batch_id: str,
    trial_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.query.read_trial_artifacts(user, batch_id, trial_id)


@router.get("/{batch_id}/trials/{trial_id}/training-capture")
def get_trial_training_capture(
    batch_id: str,
    trial_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.query.read_trial_training_capture(user, batch_id, trial_id)


@router.get("/{batch_id}/trials/{trial_id}/media/{idx}")
def get_trial_media(
    batch_id: str,
    trial_id: str,
    idx: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> Response:
    data, mime = request.app.state.ctx.query.read_trial_media(user, batch_id, trial_id, idx)
    return Response(content=data, media_type=mime)
