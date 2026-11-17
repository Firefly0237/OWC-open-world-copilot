"""Authenticated local narrative studio over durable project-scoped workflow state."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from ...app.actions.workflow import (
    artifact_is_stale,
    create_task_action,
    feedback_action,
    list_workflows_action,
    open_workflow_store,
    restore_artifact_action,
    revise_artifact_action,
    save_workflow_action,
    scoped_task,
    start_preview_action,
    step_preview_action,
    submit_review_action,
    task_detail_action,
    update_workflow_action,
)
from ...content.store import ContentStore
from ...trust.security import PathSecurityError
from ...workflow.models import (
    FeedbackInput,
    LearnMethodInput,
    PreviewInput,
    PreviewStepInput,
    RestoreArtifactInput,
    RevisionInput,
    RunRecord,
    SaveWorkflowInput,
    TaskInput,
    WorkflowPatchInput,
)
from ...workflow.store import WorkflowStore
from ..api import _require_real_allowed
from ..dependencies import _project_root_or_404, _project_sqlite_path
from ..profile import require_local_capability


def _local_studio(request: Request) -> None:
    require_local_capability(request, "creative_workflows")


router = APIRouter(prefix="/projects/{project}/creative", dependencies=[Depends(_local_studio)])
_SUBMISSION_LOCK = threading.Lock()


class ContinueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _call(action, *args, **kwargs):
    """Keep the HTTP error boundary uniform without opening a heavyweight ProjectContext."""
    try:
        return action(*args, **kwargs)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PathSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _store(project: str) -> WorkflowStore:
    return _call(open_workflow_store, _project_root_or_404(project))


def _scoped_run(store: WorkflowStore, project: str, run_id: str) -> RunRecord:
    run = store.get_run(run_id)
    scoped_task(store, project, run.task_id)
    return run


def _job_response(run: RunRecord) -> dict[str, Any]:
    return {"job_id": run.job_id, "run_id": run.id, "status": run.status}


def _submit_run(
    request: Request,
    project: str,
    store: WorkflowStore,
    run: RunRecord,
    *,
    force: bool = False,
) -> dict[str, Any]:
    manager = request.app.state.jobs_manager
    if (
        not force
        and run.job_id
        and (job := manager.get(run.job_id))
        and job.status in {"queued", "running"}
    ):
        return _job_response(run)
    root = _project_root_or_404(project)
    ready = threading.Event()

    def runner(emit) -> dict[str, Any]:
        ready.wait()
        try:
            from ...workflow.runtime import execute_workflow

            return execute_workflow(store, run.id, root, emit=emit)
        except Exception as exc:
            store.update_run(run.id, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise

    job = manager.submit("creative_workflow", runner)
    try:
        run = store.update_run(run.id, job_id=job.id)
    finally:
        ready.set()
    return _job_response(run)


@router.get("/capabilities")
def capabilities(project: str, request: Request) -> dict[str, Any]:
    root = _project_root_or_404(project)
    content = ContentStore(root)
    version = (content.read_head() or {}).get("working") or "v1"
    bundle = _call(content.load_scoped, version=version)
    try:
        _require_real_allowed(request)
        can_run, reason = True, ""
    except HTTPException as exc:
        can_run, reason = False, str(exc.detail)
    return {
        "project": project,
        "model_required": True,
        "can_run": can_run,
        "reason": reason,
        "submit_review_available": True,
        "preview_quests": [
            {"id": quest.id, "title": quest.title, "has_logic": quest.logic is not None}
            for quest in bundle.quests.values()
        ],
    }


@router.get("/tasks")
def list_tasks(project: str) -> dict[str, Any]:
    return {
        "tasks": [
            {key: value for key, value in row.items() if key != "base_bundle"}
            for row in _store(project).list_tasks()
            if row["project"] == project
        ]
    }


@router.post("/tasks", status_code=201)
def create_task(project: str, req: TaskInput) -> dict[str, Any]:
    return _call(create_task_action, _project_root_or_404(project), project, req)


@router.get("/tasks/{task_id}")
def get_task(project: str, task_id: str) -> dict[str, Any]:
    return _call(task_detail_action, _store(project), project, task_id)


@router.post("/tasks/{task_id}/runs", status_code=202)
def create_run(project: str, task_id: str, request: Request) -> dict[str, Any]:
    _require_real_allowed(request)
    store = _store(project)
    with _SUBMISSION_LOCK:
        task = _call(scoped_task, store, project, task_id)
        run, created = _call(store.create_run, task)
        if not created and run.status == "waiting_user":
            return _job_response(run)
        return _call(_submit_run, request, project, store, run, force=created)


@router.post("/runs/{run_id}:cancel")
def cancel_run(project: str, run_id: str) -> dict[str, Any]:
    store = _store(project)
    run = _call(_scoped_run, store, project, run_id)
    if run.status in {"queued", "running", "waiting_user"}:
        run = store.update_run(
            run.id,
            cancel_requested=True,
            status="running" if run.status == "running" else "cancelled",
        )
    return {"run": run.model_dump(mode="json")}


@router.get("/runs/{run_id}/method-learning")
def method_learning(project: str, run_id: str, request: Request) -> dict[str, Any]:
    from ...workflow.learning_runtime import method_learning_detail

    store = _store(project)
    run = _call(_scoped_run, store, project, run_id)
    result = _call(method_learning_detail, store, project, run.id)
    if result["workflow"] is not None:
        return result
    job = request.app.state.jobs_manager.get(run.learning_job_id) if run.learning_job_id else None
    if job is not None:
        if job.status in {"queued", "running"}:
            result["status"] = job.status
        elif job.status == "failed":
            result.update(status="failed", error=job.error or result["error"])
        elif job.result is not None:
            result.update(
                status=job.result.get("status", result["status"]),
                error=job.result.get("error", result["error"]),
            )
    return result


@router.post("/runs/{run_id}:learn-method", status_code=202)
def learn_method(
    project: str, run_id: str, req: LearnMethodInput, request: Request
) -> dict[str, Any]:
    from ...workflow.learning_runtime import execute_method_learning, prepare_method_learning

    _require_real_allowed(request)
    store = _store(project)
    with _SUBMISSION_LOCK:
        run = _call(_scoped_run, store, project, run_id)
        source = _call(
            prepare_method_learning, store, project, run.id, expected_revision=req.expected_revision
        )
        result = method_learning(project, run.id, request)
        if result["workflow"] is not None or result["status"] in {"queued", "running"}:
            return result
        if store.attempts(source["budget_id"]) >= 3:
            raise HTTPException(status_code=409, detail="归纳节点已用尽3次，不能另开额度")
        root = _project_root_or_404(project)
        ready = threading.Event()

        def runner(emit) -> dict[str, Any]:
            ready.wait()
            return execute_method_learning(
                store,
                run.id,
                root,
                expected_revision=req.expected_revision,
                name=req.name,
                emit=emit,
            )

        job = request.app.state.jobs_manager.submit("method_learning", runner)
        try:
            store.update_run(run.id, learning_job_id=job.id)
        finally:
            ready.set()
        return {**result, "job_id": job.id, "status": "queued"}


@router.post("/runs/{run_id}:continue", status_code=202)
def continue_run(project: str, run_id: str, req: ContinueInput, request: Request) -> dict[str, Any]:
    _require_real_allowed(request)
    store = _store(project)
    with _SUBMISSION_LOCK:
        run = _call(_scoped_run, store, project, run_id)
        task = _call(scoped_task, store, project, run.task_id)
        if run.revision != task.revision or run.cancel_requested:
            raise HTTPException(status_code=409, detail="运行版本已过期或已取消")
        if run.status in {"queued", "running"}:
            return _call(_submit_run, request, project, store, run)
        if run.status != "waiting_user":
            raise HTTPException(status_code=409, detail="运行没有等待方向确认")
        run = store.update_run(
            run.id, status="queued", direction_confirmed=True, waiting_question=""
        )
        return _call(_submit_run, request, project, store, run, force=True)


@router.get("/runs/{run_id}/events")
def run_events(
    project: str, run_id: str, after_seq: int = Query(default=0, ge=0)
) -> dict[str, Any]:
    store = _store(project)
    run = _call(_scoped_run, store, project, run_id)
    events = store.events(run.id, after_seq)
    return {
        "events": events,
        "after_seq": events[-1]["seq"] if events else after_seq,
        "run_id": run.id,
        "status": run.status,
    }


@router.get("/runs/{run_id}/events/stream")
def stream_run_events(
    project: str,
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Tail the authoritative business journal; observing never starts a job or model call."""
    store = _store(project)
    _call(_scoped_run, store, project, run_id)
    cursor = after_seq
    if last_event_id:
        try:
            resumed = int(last_event_id)
            if resumed < 0:
                raise ValueError("negative cursor")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID必须为非负事件序号") from exc
        cursor = max(cursor, resumed)

    async def events():
        nonlocal cursor
        loop = asyncio.get_running_loop()
        last_sent = loop.time()
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            rows = await asyncio.to_thread(store.events, run_id, cursor)
            for event in rows:
                cursor = event["seq"]
                data = json.dumps(event, ensure_ascii=False)
                yield f"id: {cursor}\nevent: workflow_event\ndata: {data}\n\n"
                last_sent = loop.time()
            if len(rows) == 500:
                # Drain the journal's page limit before observing a final status.
                continue
            run = await asyncio.to_thread(store.get_run, run_id)
            if run.status not in {"queued", "running"}:
                # Status may have changed after the previous read; its final event must
                # not be lost by closing between the journal read and status read.
                if await asyncio.to_thread(store.events, run_id, cursor):
                    continue
                data = json.dumps(
                    {"run_id": run_id, "status": run.status, "after_seq": cursor},
                    ensure_ascii=False,
                )
                yield f"event: run_state\ndata: {data}\n\n"
                return
            if loop.time() - last_sent >= 10:
                yield ": keep-alive\n\n"
                last_sent = loop.time()
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/model-calls")
def run_model_calls(project: str, run_id: str) -> dict[str, Any]:
    store = _store(project)
    run = _call(_scoped_run, store, project, run_id)
    return {"run_id": run.id, "calls": store.model_calls(run.id)}


@router.post("/tasks/{task_id}/feedback")
def feedback(project: str, task_id: str, req: FeedbackInput) -> dict[str, Any]:
    return _call(feedback_action, _store(project), project, task_id, req)


@router.get("/artifacts/{artifact_id}")
def get_artifact(project: str, artifact_id: str) -> dict[str, Any]:
    store = _store(project)
    artifact = _call(store.get_artifact, artifact_id)
    task = _call(scoped_task, store, project, artifact.task_id)
    return {
        "artifact": {**artifact.model_dump(mode="json"), "stale": artifact_is_stale(task, artifact)}
    }


@router.post("/artifacts/{artifact_id}/revisions", status_code=201)
def revise_artifact(project: str, artifact_id: str, req: RevisionInput) -> dict[str, Any]:
    return _call(revise_artifact_action, _store(project), project, artifact_id, req)


@router.post("/artifacts/{artifact_id}:restore", status_code=201)
def restore_artifact(project: str, artifact_id: str, req: RestoreArtifactInput) -> dict[str, Any]:
    return _call(restore_artifact_action, _store(project), project, artifact_id, req)


@router.get("/workflows")
def list_workflows(project: str, query: str = "", task_id: str | None = None) -> dict[str, Any]:
    return _call(list_workflows_action, _store(project), project, query, task_id)


@router.get("/tasks/{task_id}/evidence")
def task_evidence(project: str, task_id: str) -> dict[str, Any]:
    from ...workflow.evidence import export_task_evidence

    return _call(export_task_evidence, _store(project), project, task_id)


@router.post("/workflows", status_code=201)
def save_workflow(project: str, req: SaveWorkflowInput) -> dict[str, Any]:
    return _call(save_workflow_action, _store(project), project, req)


@router.patch("/workflows/{workflow_id}")
def workflow_availability(
    project: str, workflow_id: str, req: WorkflowPatchInput
) -> dict[str, Any]:
    return _call(update_workflow_action, _store(project), project, workflow_id, req)


@router.get("/preferences")
def preferences(project: str) -> dict[str, Any]:
    return {
        "preferences": [
            row for row in _store(project).preferences() if row.get("project") == project
        ]
    }


@router.delete("/preferences/{preference_id}")
def disable_preference(project: str, preference_id: str) -> dict[str, Any]:
    store = _store(project)
    if not any(
        row["id"] == preference_id and row.get("project") == project for row in store.preferences()
    ):
        raise HTTPException(status_code=404, detail="偏好不存在")
    _call(store.disable_preference, preference_id)
    return {"id": preference_id, "disabled": True}


@router.post("/tasks/{task_id}/preview", status_code=201)
def preview(project: str, task_id: str, req: PreviewInput) -> dict[str, Any]:
    return _call(start_preview_action, _store(project), project, task_id, req)


@router.post("/previews/{preview_id}/steps")
def preview_step(project: str, preview_id: str, req: PreviewStepInput) -> dict[str, Any]:
    return _call(step_preview_action, _store(project), project, preview_id, req)


@router.post("/tasks/{task_id}:submit-review")
def submit_review(project: str, task_id: str) -> dict[str, Any]:
    from pathlib import Path

    root = _project_root_or_404(project)
    return _call(
        submit_review_action,
        root,
        project,
        task_id,
        sqlite_path=_project_sqlite_path(project, Path(root)),
    )
