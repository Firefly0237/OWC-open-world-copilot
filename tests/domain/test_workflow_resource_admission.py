"""Production admission paths over explicit workflow/memory inputs; no models or author claims."""

from owcopilot.app.actions.workflow import (
    create_task_action,
    open_workflow_store,
    save_workflow_action,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.workflow import runtime
from owcopilot.workflow.memory import accepted_workflow_context
from owcopilot.workflow.models import SaveWorkflowInput, TaskInput, WorkflowSpec


def workflow(*, dialogue):
    nodes = [
        {"id": "events", "operation": "plan_events"},
        {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
        {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
    ]
    if dialogue:
        nodes.append({"id": "dialogue", "operation": "draft_dialogue", "depends_on": ["events"]})
    nodes.append({"id": "summary", "operation": "package", "optional_dependencies": ["logic"]})
    return WorkflowSpec(name="Explicit resource-admission workflow input", nodes=nodes)


def task(store, root, **request):
    task_id = create_task_action(
        root,
        "admission",
        TaskInput(brief="Explicit resource-admission test request", **request),
    )["task"]["id"]
    return store.get_task(task_id)


def test_selected_dialogue_method_is_rejected_before_interpretation_without_new_budget(
    tmp_path, monkeypatch
):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    store = open_workflow_store(root)
    source = task(store, root)
    source_run, _ = store.create_run(source)
    spec = workflow(dialogue=True)
    # A caller-supplied graph is manual input, never an AI-generated or successful story.
    store.update_run(source_run.id, workflow=spec.model_dump(mode="json"), status="completed")
    saved = save_workflow_action(
        store,
        source.project,
        SaveWorkflowInput(name="Manual dialogue method input", run_id=source_run.id),
    )["workflow"]
    selected_task = task(store, root, workflow_id=saved["id"])
    run, _ = store.create_run(selected_task)
    provider_constructions = []

    def forbidden_provider(**kwargs):
        provider_constructions.append(kwargs)
        raise AssertionError("Resource rejection must precede any model/provider access")

    monkeypatch.setattr(runtime, "build_real_provider", forbidden_provider)
    result = runtime.execute_workflow(store, run.id, str(root))
    assert result["run_id"] == run.id
    assert result["status"] == "completed_with_skips"
    assert "真实NPC" in result["error"]
    persisted = store.get_run(run.id)
    assert persisted.task_id == selected_task.id
    assert persisted.workflow is None  # No silent replacement with a different graph.
    assert store.get_task(selected_task.id).request.workflow_id == saved["id"]
    outcomes = store.outcomes(run.id)
    assert set(outcomes) == {"_plan"}
    assert outcomes["_plan"].status == "skipped_nonretryable"
    assert outcomes["_plan"].attempts == 0
    assert outcomes["_plan"].artifact_id is None
    for operation in ("_interpret", "_plan", *(node.operation for node in spec.nodes)):
        assert store.attempts(f"{selected_task.id}:{selected_task.revision}:{operation}") == 0
    assert store.model_calls(run.id) == []
    assert store.artifacts(selected_task.id) == []
    assert provider_constructions == []


def test_accepted_method_context_filters_current_frozen_resources_but_legacy_read_does_not(
    tmp_path,
):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    store = open_workflow_store(root)
    records = {}
    for needs_dialogue in (False, True):
        spec = workflow(dialogue=needs_dialogue)
        # These states are explicit technical fixture inputs, not real induction or author consent.
        records[needs_dialogue] = store.save_workflow(
            {
                "project": "admission",
                "scope": "project",
                "name": f"Technical accepted-method input, dialogue={needs_dialogue}",
                "description": "Only a resource-admission fixture",
                "schema_version": 1,
                "spec": spec.model_dump(mode="json"),
                "memory_kind": "trace_induced",
                "method_origin": "trace_induced",
                "technical_status": "validated",
                "acceptance_status": "accepted",
                "author_acceptance": "unknown",
                "content_acceptance": "unknown",
            }
        )
    empty_task = task(store, root)
    context = accepted_workflow_context(
        store, project="admission", task=empty_task, reference_sources=[]
    )
    assert {item["id"] for item in context} == {records[False]["id"]}
    # Omitting task preserves the old context read; it does not assert resources were checked.
    legacy = accepted_workflow_context(store, project="admission")
    assert {item["id"] for item in legacy} == {record["id"] for record in records.values()}
    npc = Entity(id="current_speaker", name="Current speaker", type=EntityType.NPC)
    ContentStore(root).save(ContentBundle(entities={npc.id: npc}))
    with_npc = task(store, root)
    admitted = accepted_workflow_context(
        store, project="admission", task=with_npc, reference_sources=[]
    )
    assert {item["id"] for item in admitted} == {record["id"] for record in records.values()}
    assert empty_task.base_bundle["entities"] == {}
    assert with_npc.base_bundle["entities"][npc.id]["type"] == "npc"
    assert store.artifacts(empty_task.id) == [] and store.artifacts(with_npc.id) == []
