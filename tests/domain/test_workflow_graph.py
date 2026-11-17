"""Run the real compiled DAG on a rejected reference, without calling a model provider."""

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.workflow.models import TaskInput, WorkflowSpec
from owcopilot.workflow.runtime import execute_workflow


def test_real_graph_continues_optional_reports_after_bad_required_input(tmp_path):
    root = tmp_path / "project"
    ContentStore(root).save(ContentBundle())
    task_id = create_task_action(
        root,
        "test",
        TaskInput(
            brief="明确测试输入，不是生成结果",
            use_references=True,
            reference_ids=["missing-source"],
        ),
    )["task"]["id"]
    store = open_workflow_store(root)
    run, _ = store.create_run(store.get_task(task_id))
    # A caller-supplied graph is test INPUT. This does not claim the LLM planned this graph.
    spec = WorkflowSpec(
        name="failure-contract",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "audit", "operation": "audit", "optional_dependencies": ["quest"]},
            {"id": "pack", "operation": "package", "optional_dependencies": ["audit", "quest"]},
        ],
    )
    store.update_run(run.id, workflow=spec.model_dump(mode="json"))
    result = execute_workflow(store, run.id, str(root))
    outcomes = store.outcomes(run.id)
    assert result["status"] == "completed_with_skips"
    assert outcomes["events"].status == "skipped_nonretryable"
    assert outcomes["quest"].status == "blocked_dependency"
    assert outcomes["audit"].status == "succeeded"
    assert outcomes["pack"].status == "succeeded"
    assert store.model_calls(run.id) == []
