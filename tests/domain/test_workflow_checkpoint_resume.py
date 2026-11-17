"""Native SQLite checkpoints and the domain ledger cooperate without provider calls."""

import sqlite3

import pytest

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.workflow import runtime
from owcopilot.workflow.models import TaskInput, WorkflowSpec


def test_native_checkpoint_resumes_same_run_and_does_not_repeat_completed_work(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    ContentStore(root).save(ContentBundle())
    task_id = create_task_action(root, "test", TaskInput(brief="explicit checkpoint test input"))[
        "task"
    ]["id"]
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    run, _ = store.create_run(task)
    quest = Quest(id="q", title="fixture", stages=[QuestStage(id="s", summary="finish")])
    spec = WorkflowSpec(
        name="checkpoint contract",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "audit", "operation": "audit", "optional_dependencies": ["quest"]},
            {"id": "package", "operation": "package", "optional_dependencies": ["audit", "quest"]},
        ],
    )
    store.update_run(run.id, workflow=spec.model_dump(mode="json"))
    executed = []
    original = runtime.execute_operator

    def record_operator(node, **kwargs):
        executed.append(node.operation)
        if node.operation == "plan_events":
            return "event_plan", {"plan": {"premise": "fixture", "beats": ["finish"]}}
        if node.operation == "draft_quest":
            return "quest", {"quest": quest.model_dump(mode="json")}
        return original(node, **kwargs)

    def no_provider(**kwargs):
        raise AssertionError("Checkpoint testing never authorizes a provider request")

    def interrupt_after_committed_output(event, data):
        if event == "node_outcome" and data.get("node_id") == "audit":
            raise KeyboardInterrupt("explicit stop after domain commit, before graph checkpoint")

    monkeypatch.setattr(runtime, "execute_operator", record_operator)
    monkeypatch.setattr(runtime, "build_real_provider", no_provider)
    with pytest.raises(KeyboardInterrupt, match="explicit stop"):
        runtime.execute_workflow(store, run.id, str(root), emit=interrupt_after_committed_output)
    path = store.path.with_name("creative_graph_checkpoints.sqlite3")
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (run.id,)).fetchone()[
                0
            ]
            > 0
        )
    assert store.outcomes(run.id)["audit"].status == "succeeded"
    assert "package" not in store.outcomes(run.id)
    assert runtime.execute_workflow(store, run.id, str(root))["status"] == "completed"
    attempts = {key: value.attempts for key, value in store.outcomes(run.id).items()}
    assert runtime.execute_workflow(store, run.id, str(root))["status"] == "completed"
    assert {key: value.attempts for key, value in store.outcomes(run.id).items()} == attempts
    assert executed == ["plan_events", "draft_quest", "audit", "package"]
    assert store.model_calls(run.id) == []
