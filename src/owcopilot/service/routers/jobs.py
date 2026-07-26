# 异步作业：提交长任务 + 状态查询 + SSE 事件流。
# Async jobs: submit + status + SSE events.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...app.actions import (
    run_build_overview_action,
    run_character_action,
    run_extraction_action,
    run_theme_sweep_action,
    run_world_expand_action,
    run_world_seed_action,
)
from ..api import _require_real_allowed
from ..dependencies import _project_root_or_404
from ..schemas import _JOB_PARAM_KEYS, JobCreatedResponse, JobCreateRequest, JobStatusResponse

router = APIRouter()


@router.post("/projects/{project}/jobs", response_model=JobCreatedResponse, status_code=202)
def create_job(
    project: str,
    req: JobCreateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JobCreatedResponse:
    """Run a long action asynchronously; progress streams over /jobs/{id}/events."""
    allowed = _JOB_PARAM_KEYS[req.kind]
    unknown = sorted(set(req.params) - allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown params for {req.kind}: {unknown}; allowed: {sorted(allowed)}",
        )
    if req.params.get("llm_mode") == "real":
        _require_real_allowed(request)
    content_root = _project_root_or_404(project)
    runners: dict[str, Callable[..., dict[str, Any]]] = {
        "world_seed": run_world_seed_action,
        "world_expand": run_world_expand_action,
        "extraction": run_extraction_action,
        "theme_sweep": run_theme_sweep_action,
        "character_profile": run_character_action,
        "build_overview": run_build_overview_action,
    }
    action = runners[req.kind]
    params = dict(req.params)

    def runner(emit: Callable[[str, dict[str, Any]], None]) -> dict[str, Any]:
        return action(content_root, progress=emit, **params)

    job = request.app.state.jobs_manager.submit(req.kind, runner)
    return JobCreatedResponse(job_id=job.id, kind=job.kind, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def job_status(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JobStatusResponse:
    job = request.app.state.jobs_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return JobStatusResponse(
        job_id=job.id,
        kind=job.kind,
        status=job.status,
        events=list(job.events),
        result=job.result,
        error=job.error,
    )


@router.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StreamingResponse:
    """SSE: replays buffered events, then tails until the job is terminal."""
    if request.app.state.jobs_manager.get(job_id) is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")

    def stream():
        index = 0
        while True:
            events, index, terminal = request.app.state.jobs_manager.wait_events(
                job_id, index, timeout=10.0
            )
            for event in events:
                payload = json.dumps(event["data"], ensure_ascii=False)
                yield f"event: {event['type']}\ndata: {payload}\n\n"
            if terminal and not events:
                return
            if not events and not terminal:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
