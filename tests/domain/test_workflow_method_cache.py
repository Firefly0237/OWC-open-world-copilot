"""Method inputs affect production cache decisions, independently of UI invalidation hints."""

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.workflow import runtime
from owcopilot.workflow.models import TaskInput, WorkflowInputSlot, WorkflowSpec


def test_changed_method_slots_invalidate_prior_revision_but_unchanged_slots_reuse(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    ContentStore(root).save(ContentBundle())
    task_id = create_task_action(
        root,
        "test",
        TaskInput(brief="explicit cache input", protected=["explicit protection"]),
    )["task"]["id"]
    store = open_workflow_store(root)
    spec = WorkflowSpec(
        name="method input contract",
        input_slots=[WorkflowInputSlot(name="request", source="brief")],
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
        ],
    )
    seen = []

    def controlled_operation(node, *, inputs, **kwargs):
        # These are explicit technical operator outputs, never claimed as AI generation.
        source = inputs["_method_slots"][0]["source"]
        seen.append((node.operation, source))
        if node.operation == "plan_events":
            return "event_plan", {"plan": {"premise": source, "beats": ["finish"]}}
        quest = Quest(id="q", title=source, stages=[QuestStage(id="s", summary="finish")])
        return "quest", {"quest": quest.model_dump(mode="json")}

    monkeypatch.setattr(runtime, "execute_operator", controlled_operation)
    task = store.get_task(task_id)
    for revision, source in ((1, "brief"), (2, "protected"), (3, "protected")):
        if revision > 1:
            task = task.model_copy(update={"revision": revision})
            store.save_task(task, expected_revision=revision - 1)
        version = spec.model_copy(
            update={"input_slots": [WorkflowInputSlot(name="request", source=source)]},
        )
        run, _ = store.create_run(task)
        store.update_run(run.id, workflow=version.model_dump(mode="json"))
        assert runtime.execute_workflow(store, run.id, str(root))["status"] == "completed"
        assert store.model_calls(run.id) == []
        if revision == 3:
            assert all(outcome.attempts == 0 for outcome in store.outcomes(run.id).values())
    assert seen == [
        ("plan_events", "brief"),
        ("draft_quest", "brief"),
        ("plan_events", "protected"),
        ("draft_quest", "protected"),
    ]
