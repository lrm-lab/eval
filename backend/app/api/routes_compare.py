from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import enforce_csrf, get_current_user, require_researcher
from app.api.errors import AppError
from app.security.auth import AuthUser

router = APIRouter(prefix="/api/v1", tags=["compare"])


class SaveComparisonRequest(BaseModel):
    name: str
    mode: str = "snapshot"
    project_id: str = "default"
    comparison_id: str


class ExportComparisonRequest(BaseModel):
    comparison_id: str
    format: str = "markdown"


class PreviewBatch(BaseModel):
    batch_id: str
    generation_id: str


class PreviewPair(BaseModel):
    id: str
    baseline: PreviewBatch
    candidate: PreviewBatch
    dataset_release_id: str
    scoring_protocol: str


class PreviewComparisons(BaseModel):
    pairs: list[PreviewPair] = Field(min_length=1, max_length=18)


@router.get("/compare/options")
def get_compare_options(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.get_compare_options(user)


@router.get("/compare/verdict")
def get_compare_verdict(
    request: Request,
    baseline: str = Query(..., description="Baseline batch_id or comma-separated legacy model_col(s)"),
    candidate: str = Query(..., description="Candidate batch_id or comma-separated legacy model_col(s)"),
    kind: str = Query(default="batch", description="batch | legacy_model"),
    regression_threshold_pp: float = Query(default=-1.0),
    scoring_protocol: str | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    if kind == "legacy_model":
        b_cols = [c.strip() for c in baseline.split(",") if c.strip()]
        c_cols = [c.strip() for c in candidate.split(",") if c.strip()]
        subjects = [
            {"id": "baseline", "kind": "legacy_model", "model_cols": b_cols, "alias": baseline},
            {"id": "candidate", "kind": "legacy_model", "model_cols": c_cols, "alias": candidate},
        ]
    else:
        subjects = [
            {"id": "baseline", "kind": "batch", "batch_id": baseline},
            {"id": "candidate", "kind": "batch", "batch_id": candidate},
        ]
    res = request.app.state.ctx.compare.query_compare(
        user,
        {
            "subjects": subjects,
            "baseline_subject_id": "baseline",
            "scoring_protocol": scoring_protocol,
            "regression_threshold_pp": regression_threshold_pp,
            "include_verdict": True,
        },
        persist_ephemeral=False,
    )
    return {
        "comparison_id": res["comparison_id"],
        "methodology_version": res["methodology_version"],
        "gain_vs_guardrail_verdict": res["gain_vs_guardrail_verdict"],
        "auto_insights": res.get("auto_insights", []),
    }


@router.post("/compare/query", dependencies=[Depends(enforce_csrf)])
def query_compare(
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.query_compare(user, body)


@router.post("/compare/preview", dependencies=[Depends(enforce_csrf)])
def preview_batch_comparisons(
    body: PreviewComparisons,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Compute short, generation-bound summaries without creating saved comparison sessions."""
    service = request.app.state.ctx.compare
    items = []
    for pair in body.pairs:
        subjects = [
            {
                "id": side, "kind": "batch", "batch_id": batch.batch_id,
                "expected_generation_id": batch.generation_id,
            }
            for side, batch in (("baseline", pair.baseline), ("candidate", pair.candidate))
        ]
        try:
            result = service.query_compare(
                user,
                {
                    "subjects": subjects,
                    "baseline_subject_id": "baseline",
                    "scoring_protocol": pair.scoring_protocol,
                    "scope": {"dataset_release_ids": [pair.dataset_release_id]},
                    "include_verdict": False,
                    "enable_faults_as_zero_protocol": False,
                },
                persist_ephemeral=False,
                exact_batch_context=(pair.dataset_release_id, pair.scoring_protocol),
            )
        except AppError as exc:
            if exc.status_code >= 500:
                raise
            items.append({"id": pair.id, "reason": exc.code, "message": exc.message})
            continue
        row = next(iter(result["rows"]), None)
        if row is None or not row["comparable"]:
            items.append({"id": pair.id, "reason": "INCOMPARABLE_RELEASE"})
            continue
        intersection = row["intersection"]
        baseline = row["cells"]["baseline"]["common_valid"]
        candidate = row["cells"]["candidate"]["common_valid"]
        items.append({
            "id": pair.id,
            "reason": None if intersection["common_valid_scored_tasks"] else "NO_COMMON_VALID_TASKS",
            "common_valid": intersection["common_valid_scored_tasks"],
            "common_covered": intersection["common_covered_tasks"],
            "baseline_score": baseline["score"],
            "candidate_score": candidate["score"],
            "delta_pp": candidate["delta_pp"],
            "metric_name": row["metric_name"],
        })
    return {"items": items}


@router.post("/compare/task-diff", dependencies=[Depends(enforce_csrf)])
def query_task_diff(
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.query_task_diff(user, body)


@router.get("/compare/saved")
def list_saved_comparisons(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.list_saved_comparisons(user)


@router.post("/compare/saved", dependencies=[Depends(enforce_csrf)])
def save_comparison(
    body: SaveComparisonRequest,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.save_comparison(
        user,
        name=body.name,
        mode=body.mode,
        project_id=body.project_id,
        comparison_id=body.comparison_id,
    )


@router.delete("/compare/saved/{short_id}", dependencies=[Depends(enforce_csrf)])
def delete_saved_comparison(
    short_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.delete_saved_comparison(user, short_id)


@router.get("/compare/s/{short_id}")
def open_saved_comparison(
    short_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.open_saved_comparison(user, short_id)


@router.post("/compare/export", dependencies=[Depends(enforce_csrf)])
def export_comparison(
    body: ExportComparisonRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> Response:
    content, media_type, filename = request.app.state.ctx.compare.export_comparison(
        user, body.comparison_id, fmt=body.format
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Custom Sample Sets CRUD ---


@router.get("/sample-sets")
def list_sample_sets(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.list_sample_sets(user)


@router.post("/sample-sets", dependencies=[Depends(enforce_csrf)])
def create_sample_set(
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.create_sample_set(user, body)


@router.put("/sample-sets/{sample_set_id}", dependencies=[Depends(enforce_csrf)])
def update_sample_set(
    sample_set_id: str,
    body: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.update_sample_set(user, sample_set_id, body)


@router.delete("/sample-sets/{sample_set_id}", dependencies=[Depends(enforce_csrf)])
def delete_sample_set(
    sample_set_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.compare.delete_sample_set(user, sample_set_id)
