from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


@router.get("/failures")
def diagnostics_failures(
    request: Request,
    harness: str | None = Query(default=None),
    runner: str | None = Query(default=None),
    benchmark_slug: str | None = Query(default=None),
    dataset_release_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    failure_kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.diagnostics.get_failures(
        user,
        harness=harness,
        runner=runner,
        benchmark_slug=benchmark_slug,
        dataset_release_id=dataset_release_id,
        batch_id=batch_id,
        failure_kind=failure_kind,
        limit=limit,
    )


@router.get("/submetrics")
def diagnostics_submetrics(
    request: Request,
    harness: str | None = Query(default=None),
    runner: str | None = Query(default=None),
    benchmark_slug: str | None = Query(default=None),
    dataset_release_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.diagnostics.get_submetrics(
        user,
        harness=harness,
        runner=runner,
        benchmark_slug=benchmark_slug,
        dataset_release_id=dataset_release_id,
        batch_id=batch_id,
    )


@router.get("/efficiency")
def diagnostics_efficiency(
    request: Request,
    harness: str | None = Query(default=None),
    runner: str | None = Query(default=None),
    benchmark_slug: str | None = Query(default=None),
    dataset_release_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    x_metric: str = Query(default="duration_sec"),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.diagnostics.get_efficiency(
        user,
        harness=harness,
        runner=runner,
        benchmark_slug=benchmark_slug,
        dataset_release_id=dataset_release_id,
        batch_id=batch_id,
        x_metric=x_metric,
    )


@router.get("/tools")
def diagnostics_tools(
    request: Request,
    harness: str | None = Query(default=None),
    runner: str | None = Query(default=None),
    benchmark_slug: str | None = Query(default=None),
    dataset_release_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.diagnostics.get_tools_profile(
        user,
        harness=harness,
        runner=runner,
        benchmark_slug=benchmark_slug,
        dataset_release_id=dataset_release_id,
        batch_id=batch_id,
    )


@router.get("/clues")
def diagnostics_clues(
    request: Request,
    harness: str | None = Query(default=None),
    runner: str | None = Query(default=None),
    benchmark_slug: str | None = Query(default=None),
    dataset_release_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    min_repeat: int = Query(default=3, ge=2, le=20),
    min_consec_errors: int = Query(default=4, ge=2, le=20),
    outlier_pct: float = Query(default=95.0, ge=50.0, le=99.9),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.diagnostics.get_objective_clues(
        user,
        harness=harness,
        runner=runner,
        benchmark_slug=benchmark_slug,
        dataset_release_id=dataset_release_id,
        batch_id=batch_id,
        min_repeat=min_repeat,
        min_consec_errors=min_consec_errors,
        outlier_pct=outlier_pct,
    )
