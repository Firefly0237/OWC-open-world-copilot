"""Local skill selection, candidate maintenance, and portable document packages."""

from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...content.hash import content_hash
from ...workflow.skill_cards import SkillCards
from ...workflow.skill_learning import learn_skill
from ...workflow.skill_packages import export_skill_package, import_skill_package, package_zip
from ..api import _require_real_allowed
from ..dependencies import _project_root_or_404
from .workflows import _call, _local_studio, _store

router = APIRouter(prefix="/skill-cards", dependencies=[Depends(_local_studio)])
_JOBS = {}
_LOCK = threading.Lock()


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FromRun(_Input):
    run_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class Update(FromRun):
    expected_card_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_version_id: str = Field(min_length=1)
    feedback: str = Field(min_length=1, max_length=4000)


class Decision(_Input):
    expected_card_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    version_id: str = Field(min_length=1)
    make_default: bool = False


class Enable(_Input):
    expected_card_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    enabled: bool


class PackageFile(_Input):
    path: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=131072)


class PackageInput(_Input):
    files: list[PackageFile] = Field(min_length=3, max_length=3)


def submit_summary(
    request, project, store, root, run_id, *, operation_id, maintenance_decision_id=None, **kwargs
):
    from ...workflow.v2_service import v2_detail

    state = v2_detail(store, project, run_id)
    if state["run"]["status"] not in {"completed", "completed_with_skips", "failed"}:
        raise ValueError("请等待当前制作结束后再整理Skill")
    key = content_hash([project, run_id, kwargs])
    manager = request.app.state.jobs_manager
    with _LOCK:
        previous = manager.get(_JOBS[key]) if key in _JOBS else None
        if previous is not None:
            return {"job_id": previous.id, "status": previous.status}

        def runner(emit):
            from ...workflow.execution_records import ExecutionRecords
            from ...workflow.skill_policy import record_maintenance_outcome

            def record_outcome(value=None):
                if not maintenance_decision_id:
                    return
                try:
                    observed = value
                    for _ in range(3):
                        if not isinstance(observed, dict) or not isinstance(
                            observed.get("result"), dict
                        ):
                            break
                        observed = observed["result"]
                    summary_id = (observed or {}).get("summary_run_id")
                    fresh = value.get("new_dispatch") if value else None
                    if fresh is True and summary_id:
                        ExecutionRecords(store).bind_run(
                            project, maintenance_decision_id, summary_id
                        )
                    record_maintenance_outcome(
                        store,
                        project,
                        maintenance_decision_id,
                        status="completed"
                        if value and value.get("status") in {"candidate", "no_change", "completed"}
                        else "unknown",
                        summary_run_id=summary_id,
                        new_dispatch=fresh,
                        technical_status="completed"
                        if (observed or {}).get("status") in {"candidate", "no_change"}
                        else "unknown",
                    )
                except Exception as exc:
                    from ...workflow.harness import HarnessStore

                    HarnessStore(store).append(
                        run_id,
                        "skill/maintenance_recording_failed",
                        {
                            "decision_id": maintenance_decision_id,
                            "error": str(exc),
                        },
                    )
                    if value is not None:
                        value["maintenance_recording_error"] = str(exc)

            try:
                value = learn_skill(
                    store, root, project, run_id, operation_id=operation_id, emit=emit, **kwargs
                )
            except Exception:
                record_outcome()
                raise
            record_outcome(value)
            return value

        job = manager.submit("skill_summary", runner)
        _JOBS[key] = job.id
        return {"job_id": job.id, "status": job.status}


def schedule_run_summary(request, project, store, root, result):
    """A separate background job never holds up or changes the finished story."""
    from ...workflow.v2_service import finalize_memory_run

    result = finalize_memory_run(store, root, project, result)
    if result["run"]["status"] not in {
        "completed",
        "completed_with_skips",
    }:
        return result
    run_id = result["run_id"]
    task = store.get_task(result["task_id"])
    feedback_refs = [
        "feedback:" + content_hash(row)
        for row in task.feedback
        if row.get("scope") == "workflow" or row.get("target_operation") == "workflow_spec"
    ]
    bindings = result.get("skill_card_bindings", [])
    try:
        from ...workflow.skill_policy import record_maintenance_decision, record_maintenance_outcome

        update = {}
        target = None
        if len(bindings) == 1 and (result.get("repair_contract") or feedback_refs):
            binding = bindings[0]
            card = SkillCards(store).detail(project, binding["card_id"])
            target = {key: binding[key] for key in ("card_id", "version_id", "version_hash")}
            update = {
                "card_id": card["id"],
                "expected_card_hash": card["card_hash"],
                "base_version_id": binding["version_id"],
                "feedback": "根据本次已记录修复或方法反馈判断是否值得更新；没有实质变化则保留。",
            }
        decision = record_maintenance_decision(
            store, project, result, update_target=target, feedback_refs=feedback_refs
        )
        result.pop("skill_summary_error", None)
        result["skill_maintenance_decision_id"] = decision["id"]
        action = decision["selected_action_id"]
        if action in {"retain", "no_change"}:
            record_maintenance_outcome(
                store, project, decision["id"], status="not_dispatched", new_dispatch=False
            )
            result["skill_summary_status"] = (
                "retained_existing" if action == "retain" else "no_change"
            )
            with store.connection() as db:
                row = db.execute(
                    "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                state = json.loads(row[0])
                state.pop("skill_summary_error", None)
                state.update(
                    {
                        key: result[key]
                        for key in ("skill_maintenance_decision_id", "skill_summary_status")
                    }
                )
                db.execute(
                    "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
                    (json.dumps(state, ensure_ascii=False), run_id),
                )
            return result
        scheduled = submit_summary(
            request,
            project,
            store,
            root,
            run_id,
            operation_id="auto:" + run_id,
            maintenance_decision_id=decision["id"],
            **(update if action == "update" else {}),
        )
        with store.connection() as db:
            row = db.execute(
                "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            state = json.loads(row[0])
            state.pop("skill_summary_error", None)
            state["skill_summary_job_id"] = scheduled["job_id"]
            state["skill_maintenance_decision_id"] = decision["id"]
            db.execute(
                "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
                (json.dumps(state, ensure_ascii=False), run_id),
            )
        result["skill_summary_job_id"] = scheduled["job_id"]
    except Exception as exc:
        # Story success remains distinct from a failed or unavailable optional summary.
        result["skill_summary_error"] = str(exc)
        with store.connection() as db:
            row = db.execute(
                "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            state = json.loads(row[0])
            state["skill_summary_error"] = str(exc)
            db.execute(
                "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
                (json.dumps(state, ensure_ascii=False), run_id),
            )
    return result


@router.get("")
def list_cards(project: str):
    return {"cards": _call(SkillCards(_store(project)).list, project)}


@router.post("/from-run", status_code=202)
def from_run(project: str, payload: FromRun, request: Request):
    _require_real_allowed(request)
    return _call(
        submit_summary,
        request,
        project,
        _store(project),
        _project_root_or_404(project),
        payload.run_id,
        operation_id=payload.operation_id,
        name=payload.name,
    )


@router.post("/import")
def import_package(project: str, payload: PackageInput):
    def unpack():
        paths = [item.path for item in payload.files]
        if len(paths) != len(set(paths)):
            raise ValueError("技能包含重复文件路径")
        return {item.path: item.content for item in payload.files}

    result = _call(import_skill_package, _store(project), project, _call(unpack))
    return result["card"]


@router.post("/{card_id}/update", status_code=202)
def update_card(project: str, card_id: str, payload: Update, request: Request):
    _require_real_allowed(request)
    _call(SkillCards(_store(project)).detail, project, card_id)
    return _call(
        submit_summary,
        request,
        project,
        _store(project),
        _project_root_or_404(project),
        payload.run_id,
        operation_id=payload.operation_id,
        card_id=card_id,
        expected_card_hash=payload.expected_card_hash,
        base_version_id=payload.base_version_id,
        feedback=payload.feedback,
        name=payload.name,
    )


@router.post("/{card_id}/adopt")
def adopt(project: str, card_id: str, payload: Decision):
    return _call(
        SkillCards(_store(project)).decide,
        project,
        card_id,
        payload.expected_card_hash,
        payload.version_id,
        make_default=payload.make_default,
    )


@router.post("/{card_id}/enable")
def enable(project: str, card_id: str, payload: Enable):
    return _call(
        SkillCards(_store(project)).enable,
        project,
        card_id,
        payload.expected_card_hash,
        payload.enabled,
    )


@router.post("/{card_id}/rollback")
def rollback(project: str, card_id: str, payload: Decision):
    return _call(
        SkillCards(_store(project)).rollback,
        project,
        card_id,
        payload.expected_card_hash,
        payload.version_id,
    )


def _package(project, card_id, version_id):
    return export_skill_package(_store(project), project, card_id, version_id)


@router.get("/{card_id}/versions/{version_id}/package-manifest")
def package_manifest(project: str, card_id: str, version_id: str):
    package = _call(_package, project, card_id, version_id)
    return {
        "files": [{"path": path, "content": content} for path, content in package["files"].items()]
    }


@router.get("/{card_id}/versions/{version_id}/package")
def download_package(project: str, card_id: str, version_id: str):
    package = _call(_package, project, card_id, version_id)
    return Response(
        package_zip(package["files"]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="' + package["filename"] + '"'},
    )
