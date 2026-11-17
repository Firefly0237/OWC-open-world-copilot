"""Project-scoped creative tasks, immutable revisions and deterministic previews."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
from ...content.hash import content_hash
from ...content.models import ContentBundle, DialogueTree, Quest
from ...content.store import ContentStore
from ...inspiration.store import ReferenceStore
from ...logic.stepper import advance_preview, preview_view, start_preview
from ...storage import SQLiteStore
from ...storage.world_lock import world_write_lock
from ...trust.identifiers import validate_path_segment
from ...trust.security import resolve_under_root
from ...workflow.anchors import resolve_feedback_anchor
from ...workflow.applicability import assess_applicability
from ...workflow.memory import recall_workflows
from ...workflow.models import (
    Artifact,
    EventPlan,
    FeedbackInput,
    PreviewInput,
    PreviewStepInput,
    RestoreArtifactInput,
    RevisionInput,
    SaveWorkflowInput,
    TaskCreateInput,
    TaskInput,
    TaskRecord,
    WorkflowPatchInput,
    WorkflowSpec,
)
from ...workflow.planning import validate_bindings
from ...workflow.revision import affected_operations
from ...workflow.store import WorkflowStore

_KIND_OPERATIONS = {
    "event_plan": "plan_events",
    "quest": "draft_quest",
    "dialogue_tree": "draft_dialogue",
    "quest_with_logic": "draft_logic",
    "bound_dialogue": "bind_dialogue",
    "audit_report": "audit",
    "behavior_report": "compare",
    "package": "package",
    "interpretation": "interpretation",
    "workflow_spec": "workflow_spec",
}


def open_workflow_store(content_root: str | Path) -> WorkflowStore:
    root = Path(content_root).expanduser().resolve()
    return WorkflowStore(resolve_under_root(root, root / ".owcopilot/creative_workflows.sqlite3"))


def scoped_task(store: WorkflowStore, project: str, task_id: str) -> TaskRecord:
    task = store.get_task(task_id)
    if task.project != project:
        raise KeyError("任务不属于当前项目")
    return task


def task_summary(task: TaskRecord, store: WorkflowStore | None = None) -> dict[str, Any]:
    result = task.model_dump(mode="json", exclude={"base_bundle"})
    title = task.display_title.strip()
    synopsis = task.display_synopsis.strip()
    if not title and store is not None:
        for artifact in reversed(store.artifacts(task.id)):
            if artifact_is_stale(task, artifact):
                continue
            value = artifact.payload
            candidate = value.get("quest", value.get("logic", value))
            if not isinstance(candidate, dict) or not candidate.get("stages"):
                continue
            title = str(candidate.get("title") or "").strip()
            if title:
                synopsis = synopsis or str(candidate.get("objective") or "").strip()
                break
    result["display_title"] = title or "未命名故事"
    result["display_synopsis"] = synopsis
    return result


def artifact_is_stale(task: TaskRecord, artifact: Artifact) -> bool:
    """Unaffected immutable outputs may be reused; invalidated output stays historical."""
    if artifact.task_id != task.id or artifact.revision > task.revision:
        return True
    if artifact.revision == task.revision:
        return False
    operation = _KIND_OPERATIONS.get(artifact.kind)
    changes = [row for row in task.feedback if row.get("revision", 0) > artifact.revision]
    if not changes or operation is None:
        return True
    return any(
        operation in row.get("affected_operations", _KIND_OPERATIONS.values()) for row in changes
    )


def create_task_action(
    content_root: str | Path,
    project: str,
    request: TaskInput | TaskCreateInput,
) -> dict[str, Any]:
    content_root = Path(content_root).expanduser().resolve()
    content = ContentStore(content_root)
    version = request.version or (content.read_head() or {}).get("working") or "v1"
    # ContentStore's overlay loader expects a trusted version. Check the entire derived path
    # here before accepting a version from HTTP, including existing symlinks/junctions.
    validate_path_segment(version, label="版本")
    validate_path_segment(request.world_id, label="世界")
    resolve_under_root(content_root, Path(content_root) / "versions" / version)
    bundle = content.load_scoped(world_id=request.world_id, version=version)
    store = open_workflow_store(content_root)
    inherited = [
        {
            "text": row["text"],
            "scope": "project",
            "revision": 0,
            "affected_operations": [],
            "inherited_preference_id": row["id"],
        }
        for row in store.preferences()
        if row.get("project") == project and not row.get("disabled")
    ]
    presentation = (
        request.model_dump(include={"display_title", "display_synopsis", "origin_kind"})
        if isinstance(request, TaskCreateInput)
        else {}
    )
    # Keep request hashes and sealed requirements stable for historical tasks. Naming
    # and origin belong to the record, not the model's creative instructions.
    creative_request = TaskInput.model_validate(
        request.model_dump(exclude={"display_title", "display_synopsis", "origin_kind"})
    )
    task = TaskRecord(
        project=project,
        request=creative_request,
        base_version=version,
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
        feedback=inherited,
        **presentation,
    )
    store.save_task(task)
    return {"task": task_summary(task)}


def task_detail_action(store: WorkflowStore, project: str, task_id: str) -> dict[str, Any]:
    task = scoped_task(store, project, task_id)
    runs = store.list_runs(task.id)
    run = runs[0] if runs else None
    outcomes = store.outcomes(run.id) if run else {}
    nodes = []
    if run and run.workflow:
        for node in run.workflow.nodes:
            outcome = outcomes.get(node.id)
            nodes.append(
                {
                    **node.model_dump(mode="json"),
                    "node_id": node.id,
                    "status": "pending",
                    "attempts": 0,
                    **(outcome.model_dump(mode="json") if outcome else {}),
                }
            )
    nodes.extend(
        outcome.model_dump(mode="json")
        for key, outcome in outcomes.items()
        if not run or run.workflow is None or key not in {node.id for node in run.workflow.nodes}
    )
    return {
        "task": task_summary(task, store),
        "runs": [item.model_dump(mode="json") for item in runs],
        "run": run.model_dump(mode="json") if run else None,
        "nodes": nodes,
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes.values()],
        "artifacts": [
            {**artifact.model_dump(mode="json"), "stale": artifact_is_stale(task, artifact)}
            for artifact in store.artifacts(task.id)
        ],
    }


def _revision_feedback(
    store: WorkflowStore,
    task: TaskRecord,
    *,
    text: str,
    scope: str,
    target: str | None,
) -> dict[str, Any]:
    runs = store.list_runs(task.id)
    spec = next((run.workflow for run in runs if run.workflow is not None), None)
    target = target or ("plan_events" if scope == "workflow" else "draft_quest")
    if spec is None:
        if target not in _KIND_OPERATIONS.values():
            raise ValueError("反馈目标操作无效")
        affected = list(_KIND_OPERATIONS.values())
    else:
        affected = affected_operations(spec, target)
        if scope == "workflow":
            affected = ["interpretation", "workflow_spec", *affected]
    return {
        "text": text,
        "scope": scope,
        "target_operation": target,
        "revision": task.revision + 1,
        "affected_operations": affected,
    }


def _preserved_failure_budgets(store: WorkflowStore, task: TaskRecord) -> dict[str, str]:
    """A revision cannot replenish an unresolved logical node's dispatch allowance."""
    return store.preserved_failure_budgets(task)


def feedback_action(
    store: WorkflowStore,
    project: str,
    task_id: str,
    request: FeedbackInput,
) -> dict[str, Any]:
    with world_write_lock(store.path.parent.parent):
        return _feedback_locked(store, project, task_id, request)


def _feedback_locked(
    store: WorkflowStore,
    project: str,
    task_id: str,
    request: FeedbackInput,
) -> dict[str, Any]:
    task = scoped_task(store, project, task_id)
    if request.expected_revision != task.revision:
        raise ValueError("任务版本已改变，请读取最新版本")
    anchor_result = None
    if request.anchor is not None:
        if request.scope != "content":
            raise ValueError("锚定反馈仅作用于本次内容，不能静默扩大为项目或工作流要求")
        source = store.get_artifact(request.anchor.artifact_id)
        if artifact_is_stale(task, source):
            raise ValueError("反馈来源已过期，请读取当前正文")
        preview = (
            store.get_preview(request.anchor.preview_id) if request.anchor.preview_id else None
        )
        anchor_result = resolve_feedback_anchor(task, source, request.anchor, preview)
        if request.target_operation and request.target_operation != anchor_result["operation"]:
            raise ValueError("反馈目标步骤与原文锚点不一致")
        request = request.model_copy(update={"target_operation": anchor_result["operation"]})
    feedback = _revision_feedback(
        store,
        task,
        text=request.text,
        scope=request.scope,
        target=request.target_operation,
    )
    if anchor_result is not None:
        feedback["anchor"] = anchor_result
        feedback["edit_scope_status"] = "location_only_not_a_field_edit_permission"
    revised = task.model_copy(
        update={
            "revision": task.revision + 1,
            "feedback": [*task.feedback, feedback],
            "preserved_budgets": _preserved_failure_budgets(store, task),
        }
    )
    preference = {"project": project, "text": request.text, "source_task_id": task.id}
    store.commit_revision(
        revised,
        expected_revision=task.revision,
        preference=preference if request.scope == "project" else None,
    )
    return {
        "task": task_summary(revised),
        "revision": revised.revision,
        "feedback": feedback,
        "affected_operations": feedback["affected_operations"],
    }


def _editable_workflow_spec(
    payload: dict[str, Any],
    *,
    base_bundle: dict[str, Any] | None = None,
    original_payload: dict[str, Any] | None = None,
) -> WorkflowSpec:
    spec = validate_bindings(WorkflowSpec.model_validate(payload))
    original_parameters = (
        {
            node.operation: node.parameters
            for node in WorkflowSpec.model_validate(original_payload).nodes
        }
        if original_payload is not None
        else {}
    )
    limits = {
        "plan_events": {"budget_tokens": 8000},
        "draft_quest": {"budget_tokens": 8000},
        "draft_dialogue": {"max_nodes": 24, "max_chars": 600},
    }
    for node in spec.nodes:
        for key, value in node.parameters.items():
            if (
                key == "baseline_quest_id"
                and node.operation == "compare"
                and base_bundle is not None
            ):
                if not isinstance(value, str) or value not in base_bundle.get("quests", {}):
                    raise ValueError("比较基线任务必须存在于当前冻结项目")
                continue
            maximum = limits.get(node.operation, {}).get(key)
            if maximum is None:
                # A task-local graph can retain the planner's existing read-only notes.
                # This does not permit new/changed unknown inputs or portable library bindings.
                original = original_parameters.get(node.operation, {})
                if (
                    base_bundle is not None
                    and key in original
                    and content_hash(value) == content_hash(original[key])
                ):
                    continue
                raise ValueError(f"步骤{node.operation}不支持可复用参数{key}")
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"参数{key}需要1到{maximum}的整数")
    return spec


def _validated_artifact_payload(
    kind: str,
    payload: dict[str, Any],
    *,
    base_bundle: dict[str, Any] | None = None,
    original_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind == "workflow_spec":
        return _editable_workflow_spec(
            payload,
            base_bundle=base_bundle,
            original_payload=original_payload,
        ).model_dump(mode="json")
    data = dict(payload)
    if kind == "event_plan":
        data["plan"] = EventPlan.model_validate(data.get("plan")).model_dump(mode="json")
        return data
    if kind in {"quest", "quest_with_logic", "bound_dialogue"}:
        data["quest"] = Quest.model_validate(data.get("quest")).model_dump(mode="json")
    if kind in {"dialogue_tree", "bound_dialogue"}:
        data["tree"] = DialogueTree.model_validate(data.get("tree")).model_dump(mode="json")
    if kind not in {"quest", "quest_with_logic", "dialogue_tree", "bound_dialogue"}:
        raise ValueError("该类产物不支持直接编辑")
    return data


def revise_artifact_action(
    store: WorkflowStore,
    project: str,
    artifact_id: str,
    request: RevisionInput,
) -> dict[str, Any]:
    with world_write_lock(store.path.parent.parent):
        return _revise_artifact_locked(store, project, artifact_id, request)


def _revise_artifact_locked(
    store: WorkflowStore,
    project: str,
    artifact_id: str,
    request: RevisionInput,
) -> dict[str, Any]:
    old = store.get_artifact(artifact_id)
    task = scoped_task(store, project, old.task_id)
    if artifact_is_stale(task, old) or request.expected_hash != old.content_hash:
        raise ValueError("产物版本已改变，请读取当前版本")
    return _commit_artifact_revision(store, task, old, request.payload, restoring=False)


def _commit_artifact_revision(
    store: WorkflowStore,
    task: TaskRecord,
    old: Artifact,
    payload: dict[str, Any],
    *,
    restoring: bool,
) -> dict[str, Any]:
    payload = _validated_artifact_payload(
        old.kind,
        payload,
        base_bundle=task.base_bundle,
        original_payload=old.payload,
    )
    if old.kind == "workflow_spec":
        active = next(
            (
                item
                for item in reversed(store.artifacts(task.id))
                if item.kind == "workflow_spec" and not artifact_is_stale(task, item)
            ),
            old,
        )
        before = WorkflowSpec.model_validate(active.payload)
        after = WorkflowSpec.model_validate(payload)
        before_nodes = {node.operation: node.model_dump(mode="json") for node in before.nodes}
        after_nodes = {node.operation: node.model_dump(mode="json") for node in after.nodes}
        changed = {
            op
            for op in before_nodes.keys() | after_nodes.keys()
            if before_nodes.get(op) != after_nodes.get(op)
        }
        affected = {"workflow_spec"}
        for spec in (before, after):
            for op in changed & {node.operation for node in spec.nodes}:
                affected.update(affected_operations(spec, op))
        feedback = {
            "text": "用户恢复历史工作图" if restoring else "用户直接编辑工作图",
            "scope": "content",
            "target_operation": "workflow_spec",
            "revision": task.revision + 1,
            "affected_operations": sorted(affected),
        }
    else:
        feedback = _revision_feedback(
            store,
            task,
            text="用户恢复历史产物" if restoring else "用户直接编辑产物",
            scope="content",
            target=_KIND_OPERATIONS.get(old.kind),
        )
    feedback.update({"edited_artifact_id": old.id})
    if restoring:
        feedback["restored_artifact_id"] = old.id
    revised = task.model_copy(
        update={
            "revision": task.revision + 1,
            "feedback": [*task.feedback, feedback],
            "preserved_budgets": _preserved_failure_budgets(store, task),
        }
    )
    artifact = Artifact(
        task_id=task.id,
        revision=revised.revision,
        run_id=old.run_id,
        node_id=old.node_id,
        kind=old.kind,
        payload=payload,
        input_hash=old.input_hash,
        content_hash=content_hash(payload),
    )
    store.commit_revision(revised, expected_revision=task.revision, artifact=artifact)
    return {
        "task": task_summary(revised),
        "artifact": {**artifact.model_dump(mode="json"), "stale": False},
        "affected_operations": feedback["affected_operations"],
    }


def restore_artifact_action(
    store: WorkflowStore, project: str, artifact_id: str, request: RestoreArtifactInput
) -> dict[str, Any]:
    with world_write_lock(store.path.parent.parent):
        old = store.get_artifact(artifact_id)
        task = scoped_task(store, project, old.task_id)
        if task.revision != request.expected_revision or old.content_hash != request.source_hash:
            raise ValueError("任务或历史来源版本已改变，请重新读取")
        if old.revision >= task.revision:
            raise ValueError("请选择早于当前版本的历史产物")
        return _commit_artifact_revision(store, task, old, old.payload, restoring=True)


def _workflow_view(record: dict[str, Any]) -> dict[str, Any]:
    stable = {
        key: value
        for key, value in record.items()
        if key not in {"content_hash", "score", "retrieval"}
    }
    return {
        **record,
        "memory_kind": record.get("memory_kind", "unknown"),
        "method_origin": record.get("method_origin", "unknown"),
        "validation": record.get("validation", {"source_run_status": "unknown"}),
        # Defaults above are presentation only. Old records retain their persisted
        # hash so viewing them cannot invalidate the existing version/CAS contract.
        "content_hash": content_hash(stable),
    }


def list_workflows_action(
    store: WorkflowStore, project: str, query: str = "", task_id: str | None = None
) -> dict[str, Any]:
    records = (
        recall_workflows(store, query, project=project)
        if query
        else [row for row in store.workflows() if row.get("project") == project]
    )
    views = [_workflow_view(row) for row in records]
    if task_id is not None:
        task = scoped_task(store, project, task_id)
        sources = ReferenceStore(store.path.parent.parent).list_sources()
        for view in views:
            view["applicability_check"] = assess_applicability(
                WorkflowSpec.model_validate(view["spec"]),
                task,
                reference_sources=sources,
            )
    return {
        "workflows": views,
        "storage_scope": "current_project",
    }


def save_workflow_action(
    store: WorkflowStore,
    project: str,
    request: SaveWorkflowInput,
) -> dict[str, Any]:
    run = store.get_run(request.run_id)
    task = scoped_task(store, project, run.task_id)
    if run.revision != task.revision or run.workflow is None:
        raise ValueError("只能保存当前任务版本已生成的工作图")
    # Project-bound model inputs are supplied afresh by the runtime. A saved graph carries
    # operations/dependencies rather than copying arbitrary prior model parameters or IDs.
    spec = WorkflowSpec.model_validate(run.workflow.model_dump(mode="json"))
    spec = spec.model_copy(
        update={"nodes": [node.model_copy(update={"parameters": {}}) for node in spec.nodes]}
    )
    outcomes = store.outcomes(run.id)
    calls = store.model_calls(run.id)
    record = store.save_workflow(
        {
            "project": project,
            "name": request.name,
            "scope": request.scope,
            "storage_scope": "current_project",
            "schema_version": spec.schema_version,
            "description": task.request.brief,
            "spec": spec.model_dump(mode="json"),
            "source_run_id": run.id,
            "memory_kind": "manual_template",
            "method_origin": "manual",
            "validation": {
                "schema_valid": True,
                "source_revision": run.revision,
                "source_run_status": run.status,
                "source_operation_statuses": {
                    outcome.operation: outcome.status for outcome in outcomes.values()
                },
                "source_model_response_count": sum(
                    call.get("status") == "done" and call.get("response") is not None
                    for call in calls
                ),
                "template_execution": "not_verified",
                "content_acceptance": "not_recorded",
                "research_efficacy": "not_evaluated",
            },
            "disabled": False,
        }
    )
    return {"workflow": _workflow_view(record)}


def update_workflow_action(
    store: WorkflowStore, project: str, workflow_id: str, request: WorkflowPatchInput
) -> dict[str, Any]:
    with world_write_lock(store.path.parent.parent):
        old = next(
            (
                row
                for row in store.workflows()
                if row["id"] == workflow_id and row.get("project") == project
            ),
            None,
        )
        if old is None:
            raise KeyError("流程不存在")
        if request.acceptance_status is not None:
            if not request.expected_hash:
                raise ValueError("确认方法接受状态需要当前内容hash")
            if request.name is not None or request.spec is not None or request.disabled is not None:
                raise ValueError("方法接受、编辑和启停请分别提交")
            record = store.set_method_acceptance(
                workflow_id,
                project=project,
                expected_hash=request.expected_hash,
                status=request.acceptance_status,
            )
        elif request.name is None and request.spec is None:
            if request.disabled is None:
                raise ValueError("请提供流程名称、步骤或启用状态")
            record = store.update_workflow(workflow_id, disabled=request.disabled)
        else:
            if not request.expected_hash:
                raise ValueError("编辑流程需要当前内容hash")
            if request.disabled is not None:
                raise ValueError("流程版本编辑与启用状态请分别提交")
            spec = _editable_workflow_spec(
                request.spec.model_dump(mode="json") if request.spec else old["spec"]
            )
            name = (request.name if request.name is not None else old["name"]).strip()
            if not name:
                raise ValueError("流程名称不能为空")
            changes: dict[str, Any] = {"name": name, "spec": spec.model_dump(mode="json")}
            if old.get("memory_kind") == "trace_induced":
                changes.update(
                    {
                        "memory_kind": "manual_template",
                        "method_origin": "manual",
                        "technical_status": "schema_validated",
                        "acceptance_status": "pending",
                        "acceptance_record": None,
                        "source_method_id": old["id"],
                        "validation": {
                            "schema_valid": True,
                            "template_execution": "not_verified",
                            "content_acceptance": "not_recorded",
                        },
                    }
                )
            record = store.revise_workflow(
                workflow_id,
                expected_hash=request.expected_hash,
                changes=changes,
            )
        return {"workflow": _workflow_view(record)}


def submit_review_action(
    content_root: str | Path,
    project: str,
    task_id: str,
    *,
    sqlite_path: str | Path,
) -> dict[str, Any]:
    """Queue one guarded candidate per task revision; accepting remains a separate action."""
    root = Path(content_root).expanduser().resolve()
    store = open_workflow_store(root)
    with world_write_lock(root):
        task = scoped_task(store, project, task_id)
        review_id = f"creative_{task.id}_{task.revision}"
        runtime = SQLiteStore(
            sqlite_path, world_id=task.request.world_id, version=task.base_version
        )
        try:
            queue = ReviewQueue(runtime)
            try:
                existing = queue.get(review_id)
            except KeyError:
                existing = None
            if existing is not None:
                return {"review_item_id": existing.id, "status": existing.status}
            current = ContentStore(root).load_scoped(
                world_id=task.request.world_id,
                version=task.base_version,
            )
            if content_hash(current) != task.base_hash:
                raise ValueError("项目基线已改变，旧任务不能送审；请基于当前内容创建任务")
            artifacts = [
                item for item in store.artifacts(task.id) if not artifact_is_stale(task, item)
            ]
            incoming = ContentBundle()
            # A bound dialogue carries its final quest. Prefer that resolved output over the
            # earlier prose-only quest, while retaining direct edits in the current revision.
            priority = {"quest": 0, "dialogue_tree": 0, "quest_with_logic": 1, "bound_dialogue": 2}
            for artifact in sorted(
                artifacts,
                key=lambda item: (
                    item.revision,
                    priority.get(item.kind, -1),
                    item.created_at,
                ),
            ):
                if artifact.kind not in priority:
                    continue
                if "quest" in artifact.payload:
                    quest = Quest.model_validate(artifact.payload["quest"])
                    incoming.quests[quest.id] = quest
                if "tree" in artifact.payload:
                    tree = DialogueTree.model_validate(artifact.payload["tree"])
                    incoming.dialogue_trees[tree.id] = tree
            if not incoming.quests and not incoming.dialogue_trees:
                raise ValueError("没有当前版本的任务或对白草稿可以送审")
            payload = {
                "id": review_id,
                "bundle": incoming.model_dump(mode="json"),
                "baseline_bundle": task.base_bundle,
                "creative_scope": {
                    "task_id": task.id,
                    "revision": task.revision,
                    "project": project,
                    "world_id": task.request.world_id,
                    "version": task.base_version,
                    "base_hash": task.base_hash,
                    "candidate_hash": content_hash(incoming),
                    "content_root_hash": content_hash(str(root)),
                },
            }
            item = queue.add(
                ReviewItem(
                    id=review_id,
                    item_type=ReviewItemType.SYNC_DRAFT,
                    object_ref=f"creative:{task.id}:v{task.revision}",
                    payload=payload,
                )
            )
            return {"review_item_id": item.id, "status": item.status}
        finally:
            runtime.close()


def start_preview_action(
    store: WorkflowStore,
    project: str,
    task_id: str,
    request: PreviewInput,
) -> dict[str, Any]:
    task = scoped_task(store, project, task_id)
    bundle = ContentBundle.model_validate(task.base_bundle)
    quest, tree, artifact_id = None, None, None
    if request.quest_id:
        quest = bundle.quests.get(request.quest_id)
        if quest is None:
            raise KeyError("任务基线中不存在该任务内容")
    if request.dialogue_tree_id:
        tree = bundle.dialogue_trees.get(request.dialogue_tree_id)
        if tree is None:
            raise KeyError("任务基线中不存在该对白")
    if request.artifact_id and (request.quest_id or request.dialogue_tree_id):
        raise ValueError("请选择产物或既有内容之一")
    if not request.quest_id and not request.dialogue_tree_id:
        priority = {"quest": 0, "dialogue_tree": 1, "quest_with_logic": 2, "bound_dialogue": 3}
        candidates = (
            [store.get_artifact(request.artifact_id)]
            if request.artifact_id
            else sorted(
                store.artifacts(task.id),
                key=lambda item: (item.revision, priority.get(item.kind, -1), item.created_at),
                reverse=True,
            )
        )
        artifact = next(
            (
                item
                for item in candidates
                if item.kind
                in {
                    "bound_dialogue",
                    "quest_with_logic",
                    "quest",
                    "dialogue_tree",
                }
                and not artifact_is_stale(task, item)
            ),
            None,
        )
        if artifact is None:
            raise ValueError("没有当前版本可预演的内容产物")
        artifact_id = artifact.id
        quest = (
            Quest.model_validate(artifact.payload["quest"]) if "quest" in artifact.payload else None
        )
        tree = (
            DialogueTree.model_validate(artifact.payload["tree"])
            if "tree" in artifact.payload
            else None
        )
    if quest and quest.logic is None:
        raise ValueError("该任务没有显式逻辑，不可作为可玩任务预演")
    if tree is None and quest and request.compose_dialogue:
        tree = next(
            (item for item in bundle.dialogue_trees.values() if item.quest_id == quest.id), None
        )
    if not request.compose_dialogue and quest:
        tree = None
    session = start_preview(
        quest=quest, dialogue_tree=tree, initial_state=request.initial_state, presentation="player"
    )
    record = {
        "task_id": task.id,
        "project": project,
        "revision": task.revision,
        "artifact_id": artifact_id,
        "session": session.model_dump(mode="json"),
    }
    store.save_preview(session.id, record, task_id=task.id, expected_revision=task.revision)
    return {
        **preview_view(session),
        "preview_id": session.id,
        **{key: value for key, value in record.items() if key != "session"},
    }


def step_preview_action(
    store: WorkflowStore,
    project: str,
    preview_id: str,
    request: PreviewStepInput,
) -> dict[str, Any]:
    record = store.get_preview(preview_id)
    task = scoped_task(store, project, record["task_id"])
    if record.get("project") != project or record["revision"] != task.revision:
        raise ValueError("预演源内容版本已改变，请重新开始预演")
    session = advance_preview(record["session"], **request.model_dump())
    old_seq = record["session"]["action_seq"]
    record["session"] = session.model_dump(mode="json")
    store.save_preview(
        preview_id, record, expected_seq=old_seq, task_id=task.id, expected_revision=task.revision
    )
    return {
        **preview_view(session),
        "preview_id": preview_id,
        **{key: value for key, value in record.items() if key != "session"},
    }
