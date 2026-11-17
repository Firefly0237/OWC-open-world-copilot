"""Background, budgeted execution-grounded method learning; content approval is separate."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from ..config import DEFAULT_CHEAP_MODEL
from ..content.hash import content_hash
from ..llm.execution_policy import SINGLE_DISPATCH_POLICY
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from ..storage.world_lock import world_write_lock
from .learning import build_learning_trace, induce_workflow, learning_eligibility
from .models import RunRecord, TaskRecord, WorkflowSpec
from .runtime import TraceGateway, _error_text, _run_step
from .store import RunInterruptedError, WorkflowStore


def _budget(run: RunRecord, task: TaskRecord) -> str:
    if run.budget_overrides and not run.budget_authorization_note:
        raise ValueError("额外预算缺少明确授权记录")
    return (
        run.budget_overrides.get("learn_method")
        or task.preserved_budgets.get("learn_method")
        or f"{task.id}:{run.revision}:learn_method"
    )


def _exclude_learning(trace: dict[str, Any]) -> dict[str, Any]:
    """Do not learn the learner's own requests when freezing observable production history."""

    def production(row: dict[str, Any]) -> bool:
        return row.get("node_id") != "_learn_method" and row.get("kind") != "learned_method"

    trace["source_outcomes"] = [row for row in trace["source_outcomes"] if production(row)]
    trace["source_artifacts"] = [row for row in trace["source_artifacts"] if production(row)]
    trace["artifacts"] = [row for row in trace["artifacts"] if production(row)]
    for history in trace["history"]:
        history["outcomes"] = [row for row in history["outcomes"] if production(row)]
        history["model_calls"] = [row for row in history["model_calls"] if production(row)]
        history["events"] = [row for row in history["events"] if production(row.get("payload", {}))]
    trace["learning_records_excluded"] = True
    return trace


def prepare_method_learning(
    store: WorkflowStore, project: str, run_id: str, *, expected_revision: int
) -> dict[str, Any]:
    with world_write_lock(store.path.parent.parent):
        run = store.get_run(run_id)
        task = store.get_task(run.task_id)
        if task.project != project:
            raise KeyError("源运行不属于当前项目")
        if expected_revision != task.revision or run.revision != task.revision:
            raise RunInterruptedError("只能从当前任务版本的完成运行归纳")
        if run.status != "completed" or run.cancel_requested:
            raise ValueError("源运行未完整完成")
        source = store.learning_source(run.id)
        budget_id = _budget(run, task)
        if source is None:
            trace = _exclude_learning(build_learning_trace(store, run.id))
            eligibility = learning_eligibility(trace)
            if not eligibility["allowed_to_induce"]:
                raise ValueError("源执行不能归纳方法：" + "；".join(eligibility["reasons"]))
            source = store.freeze_learning_source(
                run.id,
                {
                    "source_run_id": run.id,
                    "revision": run.revision,
                    "project": project,
                    "trace": trace,
                    "trace_hash": content_hash(trace),
                    "budget_id": budget_id,
                    "eligibility": eligibility,
                },
            )
        if source["budget_id"] != budget_id:
            raise ValueError("归纳预算继承已改变，拒绝改用新额度")
        return source


def method_learning_detail(store: WorkflowStore, project: str, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    task = store.get_task(run.task_id)
    if task.project != project:
        raise KeyError("源运行不属于当前项目")
    record = store.induced_workflow(run.id)
    outcome = store.outcomes(run.id).get("_learn_method")
    source = store.learning_source(run.id)
    budget_id = source["budget_id"] if source else _budget(run, task)
    status = "completed" if record else "idle"
    if not record and outcome:
        status = "failed" if outcome.status.startswith("skipped_") else "interrupted"
    elif not record and run.learning_job_id:
        status = "interrupted"
    return {
        "source_run_id": run.id,
        "status": status,
        "job_id": run.learning_job_id,
        "workflow": {**record, "content_hash": content_hash(record)} if record else None,
        "outcome": outcome.model_dump(mode="json") if outcome else None,
        "error": outcome.error if outcome else "",
        "attempts": store.attempts(budget_id),
        "budget_id": budget_id,
    }


def execute_method_learning(
    store: WorkflowStore,
    run_id: str,
    content_root: str,
    *,
    expected_revision: int,
    name: str = "",
    emit: Callable[[str, dict], None] | None = None,
) -> dict[str, Any]:
    notify = emit or (lambda *_: None)
    run = store.get_run(run_id)
    task = store.get_task(run.task_id)
    source = prepare_method_learning(
        store, task.project, run.id, expected_revision=expected_revision
    )
    existing = store.induced_workflow(run.id)
    if existing is not None:
        return method_learning_detail(store, task.project, run.id)
    model = task.request.model or os.getenv("OWCOPILOT_CHEAP_MODEL") or DEFAULT_CHEAP_MODEL
    gateway = TraceGateway(
        store,
        run.id,
        providers={},
        provider_factory=lambda: build_real_provider(
            model=model,
            timeout=120,
            max_output_tokens=6000,
            execution_policy=SINGLE_DISPATCH_POLICY,
        ),
        router=StaticRouter(mapping={"studio_learn_method": "cheap"}),
        execution_policy=SINGLE_DISPATCH_POLICY,
        namespace=f"method-learning:{source['trace_hash']}",
    )
    try:
        outcome = _run_step(
            store,
            run,
            task,
            "_learn_method",
            "learn_method",
            source["trace_hash"],
            lambda error: (
                "learned_method",
                {
                    "spec": induce_workflow(
                        gateway,
                        {
                            **source["trace"],
                            "induction_feedback": error,
                        },
                    ).model_dump(mode="json"),
                    "trace_hash": source["trace_hash"],
                    "source_run_id": run.id,
                },
            ),
            notify,
            kind="learned_method",
        )
        if outcome.status != "succeeded" or not outcome.artifact_id:
            return method_learning_detail(store, task.project, run.id)
        artifact = store.get_artifact(outcome.artifact_id)
        spec = WorkflowSpec.model_validate(artifact.payload["spec"])
        with world_write_lock(content_root):
            record = store.save_induced_workflow(
                run.id,
                artifact.id,
                {
                    "project": task.project,
                    "scope": "project",
                    "storage_scope": "current_project",
                    "name": name.strip() or spec.name,
                    "description": spec.description,
                    "schema_version": spec.schema_version,
                    "spec": spec.model_dump(mode="json"),
                    "memory_kind": "trace_induced",
                    "method_origin": "trace_induced",
                    "technical_status": "validated",
                    "acceptance_status": "pending",
                    "author_acceptance": "unknown",
                    "content_acceptance": "unknown",
                    "source_run_id": run.id,
                    "source_task_id": task.id,
                    "source_revision": run.revision,
                    "source_trace_hash": source["trace_hash"],
                    "source_artifact_id": artifact.id,
                    "budget_id": source["budget_id"],
                    "validation": source["eligibility"],
                    "disabled": False,
                },
            )
        notify("method_learned", {"workflow_id": record["id"], "acceptance_status": "pending"})
        return method_learning_detail(store, task.project, run.id)
    except RunInterruptedError as exc:
        return {
            **method_learning_detail(store, task.project, run.id),
            "status": "interrupted",
            "error": str(exc),
        }
    except Exception as exc:
        notify("method_learning_failed", {"error": _error_text(exc)})
        raise
