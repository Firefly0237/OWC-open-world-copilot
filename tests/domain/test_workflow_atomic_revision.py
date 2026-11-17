"""Controlled claim/revision interleavings and truthful manual-memory provenance."""

from __future__ import annotations

import pytest

from owcopilot.app.actions.workflow import (
    feedback_action,
    list_workflows_action,
    save_workflow_action,
    update_workflow_action,
)
from owcopilot.content.hash import content_hash
from owcopilot.workflow.models import (
    FeedbackInput,
    NodeOutcome,
    SaveWorkflowInput,
    TaskInput,
    TaskRecord,
    WorkflowPatchInput,
    WorkflowSpec,
)
from owcopilot.workflow.store import RunInterruptedError, WorkflowStore


def setup_workflow(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    task = TaskRecord(
        project="demo",
        request=TaskInput(brief="A bounded narrative task"),
        base_version="v1",
        base_hash=content_hash({}),
        base_bundle={},
    )
    store.save_task(task)
    run, _ = store.create_run(task)
    spec = WorkflowSpec(
        name="Narrative preparation",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
            {"id": "package", "operation": "package", "optional_dependencies": ["logic"]},
        ],
    )
    run = store.update_run(run.id, workflow=spec.model_dump(mode="json"), status="running")
    return store, task, run


@pytest.mark.parametrize("claim_timing", ["before_action", "after_action_snapshot"])
def test_revision_inherits_claim_before_runtime_can_publish_running(
    tmp_path,
    monkeypatch,
    claim_timing,
):
    store, task, run = setup_workflow(tmp_path)
    budget = f"{task.id}:1:draft_quest"
    commit_revision = store.commit_revision

    def claim():
        # Legacy callers infer the canonical operation from this run's graph.
        assert store.claim_attempt(budget, run.id, "quest") == 1
        claimed = store.outcomes(run.id)["quest"]
        assert claimed.operation == "draft_quest"
        assert claimed.status == "running"
        # Deliberately do not call complete_node: this was the vulnerable window.

    if claim_timing == "before_action":
        claim()
    else:

        def commit_after_claim(revised, **kwargs):
            claim()
            return commit_revision(revised, **kwargs)

        monkeypatch.setattr(store, "commit_revision", commit_after_claim)
    response = feedback_action(
        store,
        task.project,
        task.id,
        FeedbackInput(
            text="Keep the same conflict", expected_revision=1, target_operation="draft_quest"
        ),
    )
    revised = store.get_task(task.id)
    assert response["task"]["preserved_budgets"]["draft_quest"] == budget
    assert revised.preserved_budgets["draft_quest"] == budget
    assert store.attempts(budget) == 1
    with pytest.raises(RunInterruptedError):
        store.claim_attempt(budget, run.id, "renamed-quest", operation="draft_quest")
    assert store.attempts(budget) == 1


def test_later_success_clears_old_carry_and_manual_save_does_not_claim_learning(tmp_path):
    store, task, run = setup_workflow(tmp_path)
    budget = f"{task.id}:1:draft_quest"
    store.claim_attempt(budget, run.id, "quest", operation="draft_quest")
    task.preserved_budgets = {"draft_quest": budget, "unknown-operation": "old-unknown"}
    store.save_task(task)
    store.complete_node(
        run.id,
        NodeOutcome(
            node_id="quest",
            operation="draft_quest",
            status="succeeded",
            attempts=1,
            budget_id=budget,
        ),
    )
    assert store.preserved_failure_budgets(task) == {"unknown-operation": "old-unknown"}
    run = store.update_run(run.id, status="completed")
    template = save_workflow_action(
        store,
        task.project,
        SaveWorkflowInput(name="An explicitly retained template", run_id=run.id),
    )["workflow"]
    assert template["memory_kind"] == "manual_template"
    assert template["method_origin"] == "manual"
    assert template["validation"]["source_run_status"] == "completed"
    assert template["validation"]["template_execution"] == "not_verified"
    assert template["validation"]["content_acceptance"] == "not_recorded"
    assert store.model_calls(run.id) == []
    response = feedback_action(
        store,
        task.project,
        task.id,
        FeedbackInput(
            text="Keep the known constraints", expected_revision=1, target_operation="draft_quest"
        ),
    )
    assert response["task"]["preserved_budgets"] == {"unknown-operation": "old-unknown"}


def test_legacy_memory_defaults_do_not_change_cas_hash(tmp_path):
    store, task, run = setup_workflow(tmp_path)
    record = store.save_workflow(
        {
            "project": task.project,
            "name": "Legacy template",
            "scope": "project",
            "schema_version": 1,
            "spec": run.workflow.model_dump(mode="json"),
            "disabled": False,
        }
    )
    view = list_workflows_action(store, task.project)["workflows"][0]
    assert view["memory_kind"] == "unknown"
    assert view["content_hash"] == content_hash(record)
    updated = update_workflow_action(
        store,
        task.project,
        record["id"],
        WorkflowPatchInput(expected_hash=view["content_hash"], name="Explicitly renamed"),
    )["workflow"]
    assert updated["id"] != record["id"]
