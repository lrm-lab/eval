from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import enforce_csrf, get_current_user, require_researcher
from app.security.auth import AuthUser
from app.services.execution_service import TERMINAL_STATUSES
from app.services.telemetry_service import execution_telemetry_overview, job_telemetry, normalize_cgroup_telemetry

router = APIRouter(prefix="/api/v1/execution", tags=["execution"])


@router.get("/capabilities")
def execution_capabilities(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    capabilities = request.app.state.ctx.execution.get_capabilities(user)
    telemetry = capabilities["runner_environment"]["telemetry"]
    telemetry["container_cgroup"] = normalize_cgroup_telemetry(telemetry["container_cgroup"])
    return capabilities


@router.get("/telemetry")
def execution_telemetry(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Read-only optional telemetry: web cgroup v2 scope, stdio caps; admin additionally sees log/backup ops status."""
    return execution_telemetry_overview(request.app.state.ctx.settings, include_ops=user.role == "admin")


@router.post("/plan", dependencies=[Depends(enforce_csrf)])
def execution_plan(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.execution.plan_job(user, payload)


@router.post("/jobs", status_code=202, dependencies=[Depends(enforce_csrf)])
def execution_submit_job(
    payload: dict[str, Any],
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> JSONResponse:
    res = request.app.state.ctx.execution.submit_job(user, payload)
    return JSONResponse(status_code=202, content=res)


@router.get("/jobs")
def execution_list_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.execution.list_jobs(user, limit=limit)


@router.get("/notifications")
def execution_notifications(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.execution.list_notifications(user, limit=limit)


@router.get("/jobs/{job_id}")
def execution_get_job(
    job_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    detail = ctx.execution.get_job_detail(user, job_id)
    detail["telemetry"].update(job_telemetry(ctx.settings, job_id, detail["events"]))
    return detail


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(enforce_csrf)])
def execution_cancel_job(
    job_id: str,
    request: Request,
    user: AuthUser = Depends(require_researcher),
) -> dict[str, Any]:
    return request.app.state.ctx.execution.cancel_job(user, job_id)


@router.post("/jobs/{job_id}/resume", status_code=202, dependencies=[Depends(enforce_csrf)])
def execution_resume_job(
    job_id: str,
    request: Request,
    payload: dict[str, Any] | None = None,
    user: AuthUser = Depends(require_researcher),
) -> JSONResponse:
    res = request.app.state.ctx.execution.resume_job(user, job_id, payload or {})
    return JSONResponse(status_code=202, content=res)


@router.get("/jobs/{job_id}/evidence/{trial_id}")
def execution_trial_evidence(
    job_id: str,
    trial_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return request.app.state.ctx.execution.get_authorized_trial_evidence(user, job_id, trial_id)


@router.get("/jobs/{job_id}/stream")
async def execution_stream_job(
    job_id: str,
    request: Request,
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id_query: int | None = Query(default=None, alias="last_event_id", ge=0),
    once: bool = Query(default=False),
    user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE real-time event stream with gapless `Last-Event-ID` replay and strict `(job_id, seq_id)` dedup (I-05)."""
    svc = request.app.state.ctx.execution
    cursor = 0
    if last_event_id_header and last_event_id_header.strip().isdigit():
        cursor = int(last_event_id_header.strip())
    elif last_event_id_query is not None:
        cursor = int(last_event_id_query)

    # Authorize upfront before opening SSE stream
    initial_status, initial_events = svc.read_events_after(user, job_id, cursor)

    async def _gen() -> AsyncIterator[str]:
        seen: set[int] = set()
        last_seq = cursor

        def _format_sse(ev: dict[str, Any]) -> str:
            sid = int(ev["seq_id"])
            etype = str(ev["event_type"])
            data = json.dumps(ev["payload"], ensure_ascii=False)
            return f"id: {sid}\nevent: {etype}\ndata: {data}\n\n"

        for ev in initial_events:
            sid = int(ev["seq_id"])
            if sid in seen or sid <= last_seq:
                continue
            seen.add(sid)
            last_seq = sid
            yield _format_sse(ev)

        if once or initial_status in TERMINAL_STATUSES:
            return

        # Poll live updates until terminal state or client disconnect (max 120s per connection, client auto-reconnects via Last-Event-ID)
        for _ in range(600):
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.2)
            st, more = svc.read_events_after(user, job_id, last_seq)
            for ev in more:
                sid = int(ev["seq_id"])
                if sid in seen or sid <= last_seq:
                    continue
                seen.add(sid)
                last_seq = sid
                yield _format_sse(ev)
            if st in TERMINAL_STATUSES:
                # Drain any final event (e.g. notification) written right as status turned terminal
                _, final_more = svc.read_events_after(user, job_id, last_seq)
                for ev in final_more:
                    sid = int(ev["seq_id"])
                    if sid in seen or sid <= last_seq:
                        continue
                    seen.add(sid)
                    last_seq = sid
                    yield _format_sse(ev)
                break

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
