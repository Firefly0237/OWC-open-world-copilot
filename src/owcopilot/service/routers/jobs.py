"""Observe narrative task jobs through status and SSE events."""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..profile import require_local_capability
from ..schemas import JobStatusResponse

router = APIRouter()


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
