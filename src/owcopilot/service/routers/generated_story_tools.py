"""Local no-code generated route checker; mounted by creative_v2."""

from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ...workflow.generated_story_tools_v2 import GeneratedStoryTools
from ...workflow.project_v2 import NarrativeProjectStore
from ..api import _require_real_allowed
from .workflows import _call, _store

router = APIRouter(prefix="/generated-story-tools")
_SUBMISSION_LOCK = threading.Lock()


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Check(_Input):
    narrative_project_id: str = Field(min_length=1)
    expected_project_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_reason: str | None = Field(default=None, min_length=1, max_length=4000)


class Revoke(_Input):
    reason: str = Field(min_length=1, max_length=4000)
    actor_kind: Literal["author", "development_operator"] = "development_operator"


class Rollback(Revoke):
    to_version_id: str = Field(min_length=1)


def _response(row):
    return {
        "check_id": row["id"],
        "run_id": row["run_id"],
        "job_id": row["job_id"],
        "status": row["status"],
    }


def _job_lost(request: Request, row: dict) -> bool:
    job = request.app.state.jobs_manager.get(row["job_id"])
    return job is None or job.status in {"done", "failed"}


@router.post("/check", status_code=202)
def check(project: str, req: Check, request: Request):
    _require_real_allowed(request)
    with _SUBMISSION_LOCK:
        service = GeneratedStoryTools(_store(project))
        row, dispatch = _call(
            service.prepare,
            project,
            req.narrative_project_id,
            req.expected_project_hash,
            attempt_reason=req.attempt_reason,
        )
        if not dispatch:
            if row["status"] in {"queued", "running"} and _job_lost(request, row):
                row = _call(service.mark_lost_job, project, row["id"])
            return _response(row)
        ready = threading.Event()
        bound = False

        def runner(emit):
            ready.wait()
            if not bound:
                raise ValueError("job未完成持久绑定，没有调用模型")
            return service.execute(project, row["id"], emit=emit)

        try:
            job = request.app.state.jobs_manager.submit("generated_story_tools", runner)
            row = _call(service.bind_job, project, row["id"], job.id)
            bound = True
        except Exception:
            _call(service.mark_lost_job, project, row["id"])
            raise
        finally:
            ready.set()
        return _response(row)


@router.get("/checks/{check_id}")
def get_check(project: str, check_id: str, request: Request):
    service = GeneratedStoryTools(_store(project))
    row = _call(service.get, project, check_id)
    if row["status"] in {"queued", "running"} and _job_lost(request, row):
        row = _call(service.mark_lost_job, project, check_id)
    return row


@router.get("/versions")
def versions(project: str, narrative_project_id: str | None = None, details: bool = False):
    service = GeneratedStoryTools(_store(project))
    if narrative_project_id:
        _call(NarrativeProjectStore(service.workflow).get, project, narrative_project_id)
    return _call(service.versions, project, details=details)


@router.post("/activations/{activation_id}/revoke")
def revoke(project: str, activation_id: str, req: Revoke):
    return _call(
        GeneratedStoryTools(_store(project)).revoke,
        project,
        activation_id,
        reason=req.reason,
        actor_kind=req.actor_kind,
    )


@router.post("/versions/{version_id}/activate")
def activate(project: str, version_id: str, req: Revoke):
    return _call(
        GeneratedStoryTools(_store(project)).activate,
        project,
        version_id,
        reason=req.reason,
        actor_kind=req.actor_kind,
    )


@router.post("/activations/{activation_id}/rollback")
def rollback(project: str, activation_id: str, req: Rollback):
    return _call(
        GeneratedStoryTools(_store(project)).rollback,
        project,
        activation_id,
        req.to_version_id,
        reason=req.reason,
        actor_kind=req.actor_kind,
    )
