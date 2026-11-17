"""Local, project-scoped API for typed graphs, harness traces and skill evolution."""

from __future__ import annotations

import threading
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ...app.actions.workflow import (
    create_task_action,
    feedback_action,
    scoped_task,
    task_summary,
)
from ...content.hash import content_hash
from ...workflow import v2_service
from ...workflow.collaboration_v2 import CollaborationPolicy, CollaborationStore
from ...workflow.edit_continuity import ApplicationContinuation
from ...workflow.graph_v2 import create_narrative_graph
from ...workflow.harness import HarnessStore
from ...workflow.intent_application import apply_intent_review
from ...workflow.models import (
    FeedbackInput,
    PreviewStepInput,
    RunRecord,
    TaskCreateInput,
    TaskRecord,
)
from ...workflow.requirements_v2 import MachineRequirements
from ...workflow.store import WorkflowStore
from ..api import _require_real_allowed
from ..dependencies import _project_root_or_404
from .generated_story_tools import router as generated_story_tools_router
from .narrative_projects import router as narrative_projects_router
from .workflows import _call, _local_studio, _store

router = APIRouter(prefix="/projects/{project}/creative/v2", dependencies=[Depends(_local_studio)])
_SUBMISSION_LOCK = threading.Lock()
_EVOLUTION_JOBS: dict[str, str] = {}
_PRODUCTION_JOBS: dict[str, str] = {}
_INTENT_PROPOSAL_JOBS: dict[str, str] = {}


def _invalid(message: str):
    raise HTTPException(status_code=409, detail=message)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunInput(_Input):
    variant: Literal["baseline", "constrained"] = "constrained"
    selection: Literal["template", "matching", "planned"] = "template"
    skill_version_id: str | None = Field(default=None, min_length=1)
    trial_scope_id: str | None = Field(default=None, min_length=1)
    expected_trial_scope_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    prior_run_id: str | None = Field(default=None, min_length=1)
    plan_guidance: dict[str, str] = Field(default_factory=dict, max_length=32)
    machine_requirements: MachineRequirements | None = None
    collaboration_policy: CollaborationPolicy | None = None
    narrative_project_id: str | None = Field(default=None, min_length=1)
    narrative_scene_id: str | None = Field(default=None, min_length=1)
    narrative_preview_id: str | None = Field(default=None, min_length=1)
    narrative_transition_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def paired_trial_scope(self):
        if bool(self.trial_scope_id) != bool(self.expected_trial_scope_hash):
            raise ValueError("试用许可ID和版本校验必须同时提供")
        if self.trial_scope_id and not self.skill_version_id:
            raise ValueError("试用许可必须与明确选择的流程版本一起使用")
        return self


class PauseInput(_Input):
    expected_policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResumeInput(_Input):
    expected_gate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Literal["continue", "revise", "cancel"] = "continue"
    guidance: dict[str, str] = Field(default_factory=dict, max_length=32)
    actor_kind: Literal["author", "development_operator"] = "author"
    expected_policy_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    collaboration_policy: CollaborationPolicy | None = None


class CreatorSourceInput(_Input):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=250000)
    allowed_uses: list[Literal["drafting", "inspiration"]] = Field(default=["drafting"])
    purpose: Literal["reference", "project_material"] = "reference"
    canon_status: Literal["unconfirmed", "author_supplied"] = "unconfirmed"


class SaveSkillInput(_Input):
    name: str = Field(min_length=1, max_length=200)


class TrialInput(_Input):
    expected_family_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    allowed: StrictBool
    note: str = Field(default="", max_length=4000)
    actor_kind: Literal["author", "development_operator"] = "author"


class TaskTrialInput(_Input):
    expected_revision: int = Field(ge=1)
    version_ids: list[str] = Field(min_length=1, max_length=16)
    machine_requirements: MachineRequirements = Field(default_factory=MachineRequirements)
    operation_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)
    actor_kind: Literal["author", "development_operator"] = "author"


class RevokeTaskTrialInput(_Input):
    expected_permit_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=4000)


class DecisionInput(_Input):
    expected_family_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["accepted", "rejected"]
    make_default: StrictBool = False
    note: str = Field(default="", max_length=4000)


class RollbackInput(_Input):
    expected_family_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    version_id: str = Field(min_length=1)


class EvolveInput(_Input):
    source_run_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)
    source_kind: Literal["author_feedback", "development_input"] = "author_feedback"
    attempt_reason: str | None = Field(default=None, min_length=1, max_length=4000)


class ApplyIntentInput(_Input):
    expected_review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_decision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    continuation: ApplicationContinuation | None = None


class ContinueEditedPreviewInput(_Input):
    preview_id: str = Field(min_length=1, max_length=128)
    expected_seq: int = Field(ge=0, strict=True)


class ContinueOriginalPreviewInput(_Input):
    expected_seq: int = Field(ge=0, strict=True)


class ProposePatchInput(_Input):
    expected_revision: int = Field(ge=1)
    expected_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def _run_response(run: RunRecord) -> dict[str, Any]:
    return {"run_id": run.id, "job_id": run.job_id, "status": run.status}


def _submit_run(
    request: Request, project: str, store: WorkflowStore, run_id: str
) -> dict[str, Any]:
    """Bind the durable job ID before a worker can dispatch any model request."""
    root = _project_root_or_404(project)
    ready = threading.Event()
    bound = False

    def runner(emit) -> dict[str, Any]:
        ready.wait()
        try:
            if not bound:
                raise ValueError("任务未成功绑定后台执行器，未调用模型")
            return v2_service.execute_v2_run(store, root, project, run_id, emit=emit)
        except Exception as exc:
            store.update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise

    try:
        job = request.app.state.jobs_manager.submit("creative_v2", runner)
    except Exception as exc:
        store.update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    try:
        run = store.update_run(run_id, job_id=job.id)
        bound = True
    finally:
        ready.set()
    return _run_response(run)


def _family(store: WorkflowStore, project: str, family_id: str) -> dict[str, Any]:
    evolution = v2_service.evolution_store(store)
    family = evolution.get_family(project, family_id)
    return {**family, "versions": evolution.versions(project, family_id)}


def _prepare_production(store, root, task, req):
    narrative_context = v2_service.narrative_generation_context(
        store,
        task,
        req.narrative_project_id,
        req.narrative_scene_id,
        req.narrative_preview_id,
        req.narrative_transition_id,
        req.prior_run_id,
    )
    extra = {
        "prior_run_id": req.prior_run_id,
        "plan_guidance": req.plan_guidance,
        "machine_requirements": req.machine_requirements.model_dump(mode="json")
        if req.machine_requirements
        else None,
        "collaboration_policy": req.collaboration_policy.model_dump(mode="json")
        if req.collaboration_policy
        else None,
        "narrative_project_id": req.narrative_project_id,
        "narrative_scene_id": req.narrative_scene_id,
        "narrative_preview_id": req.narrative_preview_id,
        "narrative_transition_id": req.narrative_transition_id,
    }
    if req.skill_version_id:
        return v2_service.prepare_skill_run(
            store, root, task, req.skill_version_id,
            trial_scope_id=req.trial_scope_id,
            expected_trial_scope_hash=req.expected_trial_scope_hash,
            **extra,
        )
    if req.trial_scope_id or req.expected_trial_scope_hash:
        raise ValueError("试用许可必须与明确选择的流程版本一起使用")
    if req.selection == "matching":
        from ...workflow.v2_skills import prepare_auto_run

        return prepare_auto_run(store, root, task, req.variant, **extra)
    if req.selection == "planned":
        from ...workflow.v2_skills import plan_initial_graph

        planned = plan_initial_graph(
            store,
            root,
            task,
            requirements=extra["machine_requirements"],
            narrative_context=narrative_context,
        )
        return v2_service.prepare_v2_run(store, root, task, planned["graph"], **extra)
    return v2_service.prepare_v2_run(
        store, root, task, create_narrative_graph(req.variant).model_dump(mode="json"), **extra
    )


@router.get("/creator/context")
def creator_context(project: str) -> dict[str, Any]:
    from ...content.store import ContentStore
    from ...inspiration.store import ReferenceStore
    from ...workflow.project_v2 import NarrativeProjectStore

    root = _project_root_or_404(project)
    bundle = _call(ContentStore(root).load)
    return {
        "entities": [
            {"id": row.id, "name": row.name, "kind": row.type.value}
            for row in bundle.entities.values()
        ],
        "sources": [row.model_dump(mode="json") for row in ReferenceStore(root).list_sources()],
        "narrative_projects": NarrativeProjectStore(_store(project)).list(project),
    }


@router.post("/creator/sources", status_code=201)
def add_creator_source(project: str, req: CreatorSourceInput) -> dict[str, Any]:
    from ...inspiration.store import ReferenceStore

    if not req.allowed_uses:
        raise ValueError("请选择至少一种资料用途")
    result = _call(
        ReferenceStore(_project_root_or_404(project)).add_text,
        title=req.title,
        text=req.text,
        allowed_uses=req.allowed_uses,
        source_type="author_workspace",
        metadata={
            "purpose": req.purpose,
            "upload_declaration": req.canon_status,
            "canon_status": "unconfirmed",
        },
    )
    return {
        "source": result.source.model_dump(mode="json"),
        "injection_flagged_chunks": result.injection_flagged_chunks,
    }


@router.post("/tasks/{task_id}/production-jobs", status_code=202)
def production_job(project: str, task_id: str, req: RunInput, request: Request) -> dict[str, Any]:
    _require_real_allowed(request)
    store, root = _store(project), _project_root_or_404(project)
    manager = request.app.state.jobs_manager
    with _SUBMISSION_LOCK:
        task = _call(scoped_task, store, project, task_id)
        if req.skill_version_id:
            from ...workflow.v2_skills import assert_skill_admission

            _call(
                assert_skill_admission, store, task, req.skill_version_id,
                machine_requirements=req.machine_requirements.model_dump(mode="json")
                if req.machine_requirements else None,
                trial_scope_id=req.trial_scope_id,
                expected_trial_scope_hash=req.expected_trial_scope_hash,
            )
        key = content_hash([project, task_id, task.revision])
        previous = manager.get(_PRODUCTION_JOBS[key]) if key in _PRODUCTION_JOBS else None
        if previous and previous.status in {"queued", "running"}:
            return {"job_id": previous.id, "status": previous.status}
        if any(
            run.status in {"queued", "running", "waiting_user"} for run in store.list_runs(task_id)
        ):
            return _invalid("此任务已有在途或暂停的运行，请继续、修订或取消当前运行")
        ready = threading.Event()
        job_id = ""

        def runner(emit):
            ready.wait()
            state = _prepare_production(store, root, task, req)
            store.update_run(state["run_id"], job_id=job_id)
            return v2_service.execute_v2_run(store, root, project, state["run_id"], emit=emit)

        job = manager.submit("creative_v2_production", runner)
        job_id = job.id
        _PRODUCTION_JOBS[key] = job.id
        ready.set()
        return {"job_id": job.id, "status": "queued"}


@router.get("/tasks")
def list_tasks(project: str) -> dict[str, Any]:
    store = _store(project)
    return {
        "tasks": [
            task_summary(TaskRecord.model_validate(row), store)
            for row in store.list_tasks()
            if row["project"] == project
        ]
    }


@router.post("/tasks", status_code=201)
def create_task(project: str, req: TaskCreateInput) -> dict[str, Any]:
    return _call(create_task_action, _project_root_or_404(project), project, req)


@router.get("/tasks/{task_id}")
def get_task(project: str, task_id: str) -> dict[str, Any]:
    store = _store(project)
    task = _call(scoped_task, store, project, task_id)
    v2_service.initialize_v2(store)
    with store.connection() as db:
        rows = db.execute(
            "SELECT v.run_id FROM creative_v2_runs AS v "
            "JOIN runs AS r ON r.id=v.run_id "
            "WHERE v.project=? AND r.task_id=? ORDER BY v.rowid DESC",
            (project, task_id),
        ).fetchall()
    return {
        "task": task_summary(task, store),
        "runs": [_call(v2_service.v2_detail, store, project, row[0]) for row in rows],
    }


@router.get("/graphs/{variant}")
def get_graph(project: str, variant: Literal["baseline", "constrained"]) -> dict[str, Any]:
    _project_root_or_404(project)
    return {"graph": create_narrative_graph(variant).model_dump(mode="json")}


@router.post("/tasks/{task_id}/runs", status_code=202)
def create_run(project: str, task_id: str, req: RunInput, request: Request) -> dict[str, Any]:
    _require_real_allowed(request)
    if req.selection == "planned":
        return production_job(project, task_id, req, request)
    store = _store(project)
    root = _project_root_or_404(project)
    with _SUBMISSION_LOCK:
        task = _call(scoped_task, store, project, task_id)
        extra = {
            "machine_requirements": req.machine_requirements.model_dump(mode="json")
            if req.machine_requirements
            else None,
            "collaboration_policy": req.collaboration_policy.model_dump(mode="json")
            if req.collaboration_policy
            else None,
            "narrative_project_id": req.narrative_project_id,
            "narrative_scene_id": req.narrative_scene_id,
            "narrative_preview_id": req.narrative_preview_id,
            "narrative_transition_id": req.narrative_transition_id,
        }
        if req.skill_version_id:
            state = _call(
                v2_service.prepare_skill_run,
                store,
                root,
                task,
                req.skill_version_id,
                prior_run_id=req.prior_run_id,
                plan_guidance=req.plan_guidance,
                trial_scope_id=req.trial_scope_id,
                expected_trial_scope_hash=req.expected_trial_scope_hash,
                **extra,
            )
        else:
            if req.selection == "matching":
                from ...workflow.v2_skills import prepare_auto_run

                state = _call(
                    prepare_auto_run,
                    store,
                    root,
                    task,
                    req.variant,
                    prior_run_id=req.prior_run_id,
                    plan_guidance=req.plan_guidance,
                    **extra,
                )
                return _call(_submit_run, request, project, store, state["run_id"])
            state = _call(
                v2_service.prepare_v2_run,
                store,
                root,
                task,
                create_narrative_graph(req.variant).model_dump(mode="json"),
                prior_run_id=req.prior_run_id,
                plan_guidance=req.plan_guidance,
                **extra,
            )
        return _call(_submit_run, request, project, store, state["run_id"])


@router.get("/runs/{run_id}")
def get_run(project: str, run_id: str) -> dict[str, Any]:
    return _call(v2_service.v2_detail, _store(project), project, run_id)


@router.get("/runs/{run_id}/events")
def run_events(
    project: str,
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    store = _store(project)
    state = _call(v2_service.v2_detail, store, project, run_id)
    events = _call(HarnessStore(store).events, run_id, after_seq=after_seq, limit=limit)
    return {
        "events": events,
        "after_seq": events[-1]["seq"] if events else after_seq,
        "run_id": run_id,
        "status": state["run"]["status"],
    }


@router.get("/runs/{run_id}/requests")
def run_requests(project: str, run_id: str) -> dict[str, Any]:
    store = _store(project)
    _call(v2_service.v2_detail, store, project, run_id)
    return {"requests": _call(HarnessStore(store).requests, run_id)}


@router.post("/runs/{run_id}/cancel")
def cancel_run(project: str, run_id: str) -> dict[str, Any]:
    store = _store(project)
    _call(v2_service.v2_detail, store, project, run_id)
    run = store.get_run(run_id)
    if run.status in {"queued", "running", "waiting_user"}:
        run = _call(
            store.update_run,
            run_id,
            cancel_requested=True,
            status="running" if run.status == "running" else "cancelled",
        )
    return {"run": run.model_dump(mode="json")}


@router.post("/runs/{run_id}/preview")
def create_preview(project: str, run_id: str) -> dict[str, Any]:
    return _call(v2_service.create_v2_preview, _store(project), project, run_id)


@router.post("/runs/{run_id}/pause")
def pause_run(project: str, run_id: str, req: PauseInput) -> dict[str, Any]:
    store = _store(project)
    _call(v2_service.v2_detail, store, project, run_id)
    return _call(
        CollaborationStore(store).request_pause,
        project,
        run_id,
        expected_policy_hash=req.expected_policy_hash,
    )


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(project: str, run_id: str, req: ResumeInput, request: Request) -> dict[str, Any]:
    store, root = _store(project), _project_root_or_404(project)
    with _SUBMISSION_LOCK:
        _call(v2_service.v2_detail, store, project, run_id)
        collaboration = CollaborationStore(store)
        detail = _call(collaboration.detail, project, run_id)
        if req.action == "revise":
            _require_real_allowed(request)
            next_state = _call(
                v2_service.revise_stage_run,
                store, root, project, run_id,
                expected_gate_hash=req.expected_gate_hash,
                expected_policy_hash=req.expected_policy_hash,
                guidance=req.guidance,
                collaboration_policy=req.collaboration_policy.model_dump(mode="json")
                if req.collaboration_policy is not None else None,
                actor_kind=req.actor_kind,
            )
            successor = store.get_run(next_state["run_id"])
            job = request.app.state.jobs_manager.get(successor.job_id) if successor.job_id else None
            task = scoped_task(store, project, successor.task_id)
            if successor.status == "queued" and job is None and task.revision == successor.revision:
                result = _call(_submit_run, request, project, store, successor.id)
            else:
                result = _run_response(successor)
            return {**result, "stage_change": next_state.get("stage_change")}
        if (
            req.expected_policy_hash is not None
            and req.expected_policy_hash != detail["policy_hash"]
        ):
            return _invalid("后续约定版本已变化，请刷新")
        if req.guidance or req.collaboration_policy is not None:
            return _invalid("包含指导或约定变更时请使用revise")
        pending = detail["pending_gate"]
        if pending is None or pending["gate_hash"] != req.expected_gate_hash:
            return _invalid("待决门已变化，请刷新")
        if req.action == "cancel":
            return cancel_run(project, run_id)
        _require_real_allowed(request)
        _call(
            collaboration.authorize_resume,
            project,
            run_id,
            expected_gate_hash=req.expected_gate_hash,
            actor_kind=req.actor_kind,
        )
        _call(store.update_run, run_id, status="queued")
        return _call(_submit_run, request, project, store, run_id)


@router.post("/previews/{preview_id}/step")
def step_preview(project: str, preview_id: str, req: PreviewStepInput) -> dict[str, Any]:
    return _call(
        v2_service.step_v2_preview,
        _store(project),
        project,
        preview_id,
        action_id=req.action_id,
        expected_seq=req.expected_seq,
        choice_id=req.choice_id,
    )


@router.post("/tasks/{task_id}/feedback")
def submit_feedback(project: str, task_id: str, req: FeedbackInput) -> dict[str, Any]:
    return _call(feedback_action, _store(project), project, task_id, req)


@router.get("/families")
def list_families(project: str) -> dict[str, Any]:
    store = _store(project)
    evolution = v2_service.evolution_store(store)
    return {
        "families": [
            {**family, "versions": _call(evolution.versions, project, family["id"])}
            for family in _call(evolution.families, project)
        ]
    }


@router.get("/families/{family_id}")
def get_family(project: str, family_id: str) -> dict[str, Any]:
    return _call(_family, _store(project), project, family_id)


@router.get("/families/{family_id}/versions")
def family_versions(project: str, family_id: str) -> dict[str, Any]:
    evolution = v2_service.evolution_store(_store(project))
    return {"versions": _call(evolution.versions, project, family_id)}


@router.get("/versions/{version_id}")
def get_version(project: str, version_id: str) -> dict[str, Any]:
    evolution = v2_service.evolution_store(_store(project))
    return _call(evolution.get_version, project, version_id)


@router.post("/versions/{version_id}/trial")
def authorize_trial(project: str, version_id: str, req: TrialInput) -> dict[str, Any]:
    evolution = v2_service.evolution_store(_store(project))
    return _call(
        evolution.authorize_trial,
        project,
        version_id,
        expected_family_hash=req.expected_family_hash,
        allowed=req.allowed,
        authority="explicit_author_action"
        if req.actor_kind == "author" else "development_operator",
        note=req.note,
    )


@router.get("/tasks/{task_id}/trial-scopes")
def list_task_trials(project: str, task_id: str) -> dict[str, Any]:
    from ...workflow.method_trial_scope_v2 import task_trial_scopes

    return {"scopes": _call(task_trial_scopes, _store(project), project, task_id)}


@router.post("/tasks/{task_id}/trial-scopes", status_code=201)
def grant_task_trial(project: str, task_id: str, req: TaskTrialInput) -> dict[str, Any]:
    from ...workflow.method_trial_scope_v2 import get_trial_scope, grant_trial_scope

    store = _store(project)
    task = _call(scoped_task, store, project, task_id)
    if task.revision != req.expected_revision:
        return _invalid("任务版本已变化，请重新读取后许可试用")
    permit = _call(
        grant_trial_scope, store, project, task_id, req.version_ids,
        machine_requirements=req.machine_requirements.model_dump(mode="json"),
        authority="explicit_author_action"
        if req.actor_kind == "author" else "development_technical_trial",
        reason=req.reason, operation_id=req.operation_id,
    )
    return _call(get_trial_scope, store, project, permit["id"])


@router.post("/trial-scopes/{scope_id}/revoke")
def revoke_task_trial(project: str, scope_id: str, req: RevokeTaskTrialInput) -> dict[str, Any]:
    from ...workflow.method_trial_scope_v2 import revoke_trial_scope

    return _call(
        revoke_trial_scope, _store(project), project, scope_id,
        expected_permit_hash=req.expected_permit_hash, reason=req.reason,
    )


@router.post("/versions/{version_id}/decision")
def decide_version(project: str, version_id: str, req: DecisionInput) -> dict[str, Any]:
    evolution = v2_service.evolution_store(_store(project))
    return _call(
        evolution.decide_version,
        project,
        version_id,
        expected_family_hash=req.expected_family_hash,
        decision=req.decision,
        make_default=req.make_default,
        note=req.note,
    )


@router.post("/families/{family_id}/rollback")
def rollback_family(project: str, family_id: str, req: RollbackInput) -> dict[str, Any]:
    evolution = v2_service.evolution_store(_store(project))
    return _call(
        lambda: evolution.set_default(
            project,
            family_id,
            req.version_id,
            expected_family_hash=req.expected_family_hash,
            action="rollback",
        )
    )


@router.get("/families/{family_id}/episodes")
def family_episodes(project: str, family_id: str) -> dict[str, Any]:
    from ...workflow.v2_skills import method_episode_detail

    store = _store(project)
    evolution = v2_service.evolution_store(store)
    _call(evolution.get_family, project, family_id)
    with store.connection() as db:
        rows = db.execute(
            "SELECT id FROM evolution_episodes WHERE project=? AND family_id=? ORDER BY rowid DESC",
            (project, family_id),
        ).fetchall()
    return {
        "episodes": [_call(method_episode_detail, store, project, row[0]) for row in rows],
        "experiences": _call(evolution.experiences, project, family_id),
    }


@router.post("/runs/{run_id}/skills", status_code=201)
def save_run_skill(project: str, run_id: str, req: SaveSkillInput) -> dict[str, Any]:
    return _call(v2_service.save_run_skill, _store(project), project, run_id, name=req.name)


@router.post("/families/{family_id}/evolve", status_code=202)
def evolve_family(
    project: str, family_id: str, req: EvolveInput, request: Request
) -> dict[str, Any]:
    _require_real_allowed(request)
    store = _store(project)
    root = _project_root_or_404(project)
    manager = request.app.state.jobs_manager
    key = content_hash(
        [
            project,
            family_id,
            req.source_run_id,
            req.text,
            req.source_kind,
            req.attempt_reason,
        ]
    )
    with _SUBMISSION_LOCK:
        _call(v2_service.evolution_store(store).get_family, project, family_id)
        _call(v2_service.v2_detail, store, project, req.source_run_id)
        prior_id = _EVOLUTION_JOBS.get(key)
        prior = manager.get(prior_id) if prior_id else None
        if prior is not None:
            return {
                "job_id": prior.id,
                "status": prior.status,
                "result": prior.result,
                "error": prior.error,
            }
        ready = threading.Event()

        def runner(emit) -> dict[str, Any]:
            ready.wait()
            return v2_service.evolve_family(
                store,
                root,
                project,
                family_id,
                req.source_run_id,
                req.text,
                emit=emit,
                source_kind=req.source_kind,
                attempt_reason=req.attempt_reason,
            )

        job = manager.submit("creative_v2_evolution", runner)
        _EVOLUTION_JOBS[key] = job.id
        ready.set()
        return {"job_id": job.id, "status": "queued"}


@router.get("/runs/{run_id}/evidence")
def run_evidence(project: str, run_id: str) -> dict[str, Any]:
    return _call(
        v2_service.export_v2_evidence,
        _store(project),
        _project_root_or_404(project),
        project,
        run_id,
    )


@router.post("/intent-reviews/{review_id}/apply")
def apply_review(project: str, review_id: str, req: ApplyIntentInput) -> dict[str, Any]:
    return _call(
        apply_intent_review,
        _project_root_or_404(project),
        project,
        review_id,
        expected_review_hash=req.expected_review_hash,
        expected_decision_hash=req.expected_decision_hash,
        continuation=req.continuation.model_dump(exclude_none=True) if req.continuation else None,
    )


@router.get("/tasks/{task_id}/edit-applications")
def edit_applications(project: str, task_id: str) -> dict[str, Any]:
    from ...workflow.edit_continuity import list_applications

    return _call(list_applications, _store(project), project, task_id)


@router.get("/intent-applications/{application_id}")
def edit_application(project: str, application_id: str) -> dict[str, Any]:
    from ...workflow.edit_continuity import read_application

    return _call(read_application, _store(project), project, application_id)


@router.post("/intent-applications/{application_id}/continue-preview")
def continue_edited_preview(project: str, application_id: str, req: ContinueEditedPreviewInput):
    from ...workflow.edit_continuity import continue_application_preview

    return _call(
        continue_application_preview, _store(project), project, application_id,
        preview_id=req.preview_id, expected_seq=req.expected_seq,
    )


@router.get("/previews/{preview_id}")
def read_edit_preview(project: str, preview_id: str):
    from ...workflow.edit_continuity import read_task_preview

    return _call(read_task_preview, _store(project), project, preview_id)


@router.post("/previews/{preview_id}/continue-original")
def continue_original_edit_preview(
    project: str, preview_id: str, req: ContinueOriginalPreviewInput
):
    from ...workflow.edit_continuity import continue_original_task_preview

    return _call(
        continue_original_task_preview, _store(project), project, preview_id,
        expected_seq=req.expected_seq,
    )


@router.post("/previews/{preview_id}/historical-step")
def step_original_edit_preview(project: str, preview_id: str, req: PreviewStepInput):
    from ...workflow.edit_continuity import step_original_task_preview

    return _call(step_original_task_preview, _store(project), project, preview_id, req)


@router.post("/intent-contracts/{contract_id}/propose-patch", status_code=202)
def propose_patch(project: str, contract_id: str, req: ProposePatchInput, request: Request):
    from ...workflow.intent_proposal_v2 import propose_intent_patch

    _require_real_allowed(request)
    store, root = _store(project), _project_root_or_404(project)
    manager = request.app.state.jobs_manager
    key = content_hash([project, contract_id, req.expected_revision, req.expected_contract_hash])
    with _SUBMISSION_LOCK:
        previous = manager.get(_INTENT_PROPOSAL_JOBS[key]) if key in _INTENT_PROPOSAL_JOBS else None
        if previous is not None:
            return {"job_id": previous.id, "status": previous.status}
        ready = threading.Event()
        bound_job_id = ""

        def runner(emit):
            ready.wait()
            return propose_intent_patch(
                store,
                root,
                project,
                contract_id,
                expected_revision=req.expected_revision,
                expected_contract_hash=req.expected_contract_hash,
                emit=emit,
                job_id=bound_job_id,
            )

        job = manager.submit("creative_v2_intent_proposal", runner)
        bound_job_id = job.id
        _INTENT_PROPOSAL_JOBS[key] = job.id
        ready.set()
        return {"job_id": job.id, "status": "queued"}


# Child routes inherit project scope and the local-workspace capability gate.

router.include_router(narrative_projects_router)
router.include_router(generated_story_tools_router)
