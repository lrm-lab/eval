from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import enforce_csrf, get_current_user
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1", tags=["profiles"])


@router.get("/models")
def list_models(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.profiles.list_models(user)


@router.get("/models/{model_canonical:path}")
def get_model_profile(
    model_canonical: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.profiles.get_model_profile(user, model_canonical)


@router.get("/harnesses")
def list_harnesses(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.profiles.list_harnesses(user)


@router.get("/harnesses/{harness_name:path}")
def get_harness_profile(
    harness_name: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.profiles.get_harness_profile(user, harness_name)


@router.get("/benchmarks")
def list_benchmarks(request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    return request.app.state.ctx.profiles.list_benchmarks(user)


@router.get("/benchmarks/{benchmark_slug}/hardest-tasks")
def get_benchmark_hardest_tasks(
    benchmark_slug: str,
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.profiles.get_benchmark_hardest_tasks(user, benchmark_slug, limit=limit)


@router.get("/benchmarks/{benchmark_slug}")
def get_benchmark_profile(
    benchmark_slug: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.profiles.get_benchmark_profile(user, benchmark_slug)


@router.post("/recommend", dependencies=[Depends(enforce_csrf)])
def recommend_candidates(
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.profiles.recommend(user, body)
