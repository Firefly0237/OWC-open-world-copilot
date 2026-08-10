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
    run_agent_diagnose_action,
    run_build_overview_action,
    run_character_action,
    run_extraction_action,
    run_multi_agent_action,
    run_theme_sweep_action,
    run_world_expand_action,
    run_world_seed_action,
)
from ..api import _require_real_allowed
from ..dependencies import _project_root_or_404
from ..profile import require_local_capability
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
    require_local_capability(request, "async_jobs")
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
        # E1: the agent diagnosis run is the seventh job kind, not a second async mechanism — same
        # submit/poll/SSE contract, and a run PAUSED at the human-approval gate simply ends this job
        # with status="interrupted"; the human's approve/reject is the NEXT job (resume=true).
        "agent_diagnose": run_agent_diagnose_action,
        "multi_agent": run_multi_agent_action,
    }
    action = runners[req.kind]
    params = dict(req.params)
    intake_tokens = request.app.state.intake_tokens

    def runner(emit: Callable[[str, dict[str, Any]], None]) -> dict[str, Any]:
        result = action(content_root, progress=emit, **params)
        if req.kind == "extraction":
            # Bind the token to the declaration accepted by the server-side action, not merely
            # to the untrusted request parameter.  The worker result stays otherwise unchanged.
            result["intake_token"] = intake_tokens.issue(
                project=project,
                draft=result["draft"],
                provenance=result["provenance"],
            )
        return result

    job = request.app.state.jobs_manager.submit(req.kind, runner)
    return JobCreatedResponse(job_id=job.id, kind=job.kind, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def job_status(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JobStatusResponse:
    require_local_capability(request, "async_jobs")
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
    require_local_capability(request, "async_jobs")
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
