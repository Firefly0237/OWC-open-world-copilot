"""Observable trace induction contracts; controlled responses are not model-effect evidence."""

from __future__ import annotations

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.llm.gateway import LLMGateway
from owcopilot.workflow.budget_policy import BudgetLedger
from owcopilot.workflow.learning import (
    build_learning_trace,
    induce_workflow,
    learning_eligibility,
    validate_induced_workflow,
)
from owcopilot.workflow.memory import accepted_workflow_context, recall_workflows
from owcopilot.workflow.models import Artifact, NodeOutcome, TaskInput, TaskRecord, WorkflowSpec
from owcopilot.workflow.operators import OPERATION_KINDS
from owcopilot.workflow.store import WorkflowStore


def method() -> WorkflowSpec:
    guidance = "Use {{task_input}} and {{current_entities}}; preserve {{constraints}}."
    return WorkflowSpec.model_validate(
        {
            "name": "Branching task method",
            "description": "Create and check a playable task",
            "applicability": ["A request needs one task with explicit choice consequences"],
            "input_slots": [
                {"name": "task_input", "source": "brief", "description": "Current request"},
                {"name": "constraints", "source": "protected", "description": "Author constraints"},
                {"name": "current_entities", "source": "entities", "description": "Current cast"},
            ],
            "nodes": [
                {"id": "events", "operation": "plan_events", "guidance": guidance},
                {
                    "id": "quest",
                    "operation": "draft_quest",
                    "depends_on": ["events"],
                    "guidance": guidance,
                },
                {
                    "id": "logic",
                    "operation": "draft_logic",
                    "depends_on": ["quest"],
                    "guidance": guidance,
                },
                {
                    "id": "audit",
                    "operation": "audit",
                    "optional_dependencies": ["logic"],
                    "guidance": guidance,
                },
                {
                    "id": "package",
                    "operation": "package",
                    "optional_dependencies": ["audit"],
                    "guidance": guidance,
                },
            ],
        }
    )


def source_trace(tmp_path):
    store = WorkflowStore(tmp_path / "learning.sqlite3")
    task = TaskRecord(
        project="demo",
        request=TaskInput(brief="Create a task with old_npc"),
        base_version="v1",
        base_hash=content_hash({}),
        base_bundle={
            "entities": {"old_npc": {"id": "old_npc", "name": "OldCast", "type": "npc"}},
            "quests": {},
        },
    )
    store.save_task(task)
    run, _ = store.create_run(task)
    run = store.update_run(run.id, status="running", workflow=method().model_dump(mode="json"))
    for node in method().nodes:
        budget_id = f"{task.id}:{task.revision}:{node.operation}"
        attempt = store.claim_attempt(budget_id, run.id, node.id, operation=node.operation)
        payload = {"status": "checked", "issues": []} if node.operation == "audit" else {}
        if node.operation == "draft_logic":
            payload = {
                "quest": {
                    "id": "old_quest",
                    "title": "OldQuest",
                    "stages": [],
                    "logic": {"variables": [{"id": "old_choice"}]},
                }
            }
        artifact = Artifact(
            task_id=task.id,
            revision=1,
            run_id=run.id,
            node_id=node.operation,
            kind=OPERATION_KINDS[node.operation],
            payload=payload,
            input_hash="input",
            content_hash=content_hash(payload),
        )
        if node.operation == "draft_logic":
            call_id = store.begin_model_call(
                run.id,
                budget_id,
                node.id,
                attempt,
                {"task": "draft_quest_logic", "user": "observable input"},
            )
            store.finish_model_call(call_id, response="observable output")
        store.complete_node(
            run.id,
            NodeOutcome(
                node_id=node.id,
                operation=node.operation,
                status="succeeded",
                budget_id=budget_id,
                attempts=attempt,
                artifact_id=artifact.id,
            ),
            artifact,
        )
        if node.operation != "draft_logic":
            store.settle_attempt(
                budget_id,
                attempt,
                status="completed",
                evidence={"host_artifact_id": artifact.id, "provider_dispatched": False},
            )
    store.update_run(run.id, status="completed")
    return store, run, build_learning_trace(store, run.id)


class ResponseGateway(LLMGateway):
    def __init__(self, response: str):
        super().__init__(providers={})
        self.response = response
        self.requests = []

    def complete(self, *, task: str, system: str, user: str, tier: str | None = None) -> str:
        self.requests.append({"task": task, "system": system, "user": user})
        return self.response


def test_observable_source_and_single_dispatch_induction(tmp_path):
    store, run, trace = source_trace(tmp_path)
    assert trace["source"]["run_id"] == run.id
    assert trace["history"][0]["model_calls"][0]["response"] == "observable output"
    logic = store.outcomes(run.id)["logic"]
    (receipt,) = BudgetLedger(store).dispatches(logic.budget_id)
    assert receipt["status"] == "completed"
    assert trace["history"][0]["model_calls"][0]["budget_receipt_id"] == receipt["id"]
    assert learning_eligibility(trace)["author_acceptance"] == "unknown"
    gateway = ResponseGateway(method().model_dump_json())
    learned = induce_workflow(gateway, trace)
    assert learned.nodes[0].guidance
    assert len(gateway.requests) == 1
    assert "observable output" in gateway.requests[0]["user"]
    next(item for item in trace["source_artifacts"] if item["kind"] == "audit_report")["payload"][
        "issues"
    ] = [{"severity": "error"}]
    with pytest.raises(ValueError, match="源执行"):
        induce_workflow(gateway, trace)
    assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    "invalid", ["old_npc", "OldCast", "old_quest", "old_choice", "{{not_declared}}"]
)
def test_induction_rejects_instance_leakage_and_unknown_slots(tmp_path, invalid):
    _, _, trace = source_trace(tmp_path)
    data = method().model_dump(mode="json")
    data["nodes"][0]["guidance"] += " " + invalid
    with pytest.raises(ValueError):
        validate_induced_workflow(WorkflowSpec.model_validate(data), trace)


def test_only_accepted_induced_library_enters_automatic_context(tmp_path):
    store, run, _ = source_trace(tmp_path)
    common = {
        "name": "Branching task method",
        "description": "branching task",
        "project": "demo",
        "schema_version": 1,
        "scope": "project",
        "spec": method().model_dump(mode="json"),
        "source_run_id": run.id,
        "disabled": False,
    }
    store.save_workflow({**common, "method_origin": "manual"})
    store.save_workflow(
        {
            **common,
            "method_origin": "trace_induced",
            "technical_status": "validated",
            "acceptance_status": "pending",
        }
    )
    accepted = store.save_workflow(
        {
            **common,
            "method_origin": "trace_induced",
            "technical_status": "validated",
            "acceptance_status": "accepted",
        }
    )
    store.save_workflow(
        {
            **common,
            "project": "other",
            "method_origin": "trace_induced",
            "technical_status": "validated",
            "acceptance_status": "accepted",
        }
    )
    assert [row["id"] for row in accepted_workflow_context(store, project="demo")] == [
        accepted["id"]
    ]
    assert accepted_workflow_context(store, project="demo", max_chars=1) == []
    malformed = method().model_dump(mode="json")
    malformed["nodes"][1]["depends_on"] = []
    store.save_workflow({**common, "spec": malformed})
    assert len(recall_workflows(store, "branching task", project="demo")) == 3


def test_induction_rejects_unescaped_instance_paths(tmp_path):
    _, _, trace = source_trace(tmp_path)
    trace["task_request"]["brief"] += r" Source C:\projects\old_story"
    spec = method()
    spec.nodes[0].guidance += r" Copy from C:\projects\old_story"
    with pytest.raises(ValueError, match="旧实例信息"):
        validate_induced_workflow(spec, trace)
