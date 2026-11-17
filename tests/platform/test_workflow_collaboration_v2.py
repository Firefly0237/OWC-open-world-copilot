"""Real native checkpoints and author-control contracts, never a user study."""

from __future__ import annotations

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle
from owcopilot.workflow.collaboration_v2 import CollaborationPolicy, CollaborationStore
from owcopilot.workflow.graph_driver import execute_persistent_graph
from owcopilot.workflow.graph_v2 import (
    DataEdge,
    GraphSpecV2,
    NodeSpecV2,
    OutputRef,
    PortSpec,
    RoleContract,
    compile_graph,
)
from owcopilot.workflow.models import TaskInput, TaskRecord
from owcopilot.workflow.store import WorkflowStore


def _case(tmp_path, mode):
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    bundle = ContentBundle()
    task = TaskRecord(
        project="dev",
        request=TaskInput(brief="Control-contract development input"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    store.save_task(task)
    run, _ = store.create_run(task)
    store.update_run(run.id, status="running")
    controls = CollaborationStore(store)
    controls.initialize("dev", run.id, {"mode": mode})
    port = PortSpec(type_name="json")
    registry = {
        operation: RoleContract(operation=operation, inputs={"item": port}, outputs={"value": port})
        for operation in ["plan_events", "draft_quest", "logic_contract"]
    }
    nodes = [
        NodeSpecV2(
            id=name,
            operation=operation,
            product_key=name,
            problem_key=name + "/produce",
            context_bindings={"item": "seed"} if index == 0 else {},
        )
        for index, (name, operation) in enumerate(
            [("plan", "plan_events"), ("quest", "draft_quest"), ("logic", "logic_contract")]
        )
    ]
    spec = GraphSpecV2(
        name="Real participation boundaries",
        nodes=nodes,
        data_edges=[
            DataEdge(source_node=left, source_port="value", target_node=right, target_port="item")
            for left, right in [("plan", "quest"), ("quest", "logic")]
        ],
        outputs={"answer": OutputRef(node_id="logic", port="value")},
    )
    compiled = compile_graph(spec, registry, {"seed": port})
    calls = []

    def handler(node, inputs):
        calls.append(node.id)
        return {"value": inputs["item"] + [node.id]}

    def execute(resume=None):
        return execute_persistent_graph(
            compiled,
            {"seed": []},
            {key: handler for key in registry},
            tmp_path / "checkpoints.sqlite3",
            run.id,
            before_node=lambda node, state: controls.before_node(
                "dev", run.id, node, state, graph_hash=compiled.graph_hash
            ),
            consume_resume=lambda gate, decision: controls.consume_resume(
                "dev", run.id, gate, decision
            ),
            resume=resume,
        )

    return store, task, run, controls, calls, execute


@pytest.mark.parametrize(
    "mode,expected_gates",
    [("delegate", []), ("direction", ["quest"]), ("edit", ["quest", "logic"])],
)
def test_policy_changes_actual_native_stop_points_and_resume_does_not_replay(
    tmp_path, mode, expected_gates
):
    store, task, run, controls, calls, execute = _case(tmp_path, mode)
    result = execute()
    observed = []
    while result.status == "waiting_user":
        store.update_run(run.id, status="waiting_user")
        pending = controls.detail("dev", run.id)["pending_gate"]
        observed.append(pending["node_id"])
        assert pending["node_id"] not in calls
        before = list(calls)
        assert execute().status == "waiting_user"  # restart/read is not approval
        assert calls == before
        with pytest.raises(ValueError, match="过期"):
            controls.authorize_resume("dev", run.id, expected_gate_hash="0" * 64)
        decision = controls.authorize_resume("dev", run.id, expected_gate_hash=pending["gate_hash"])
        store.update_run(run.id, status="running")
        result = execute(decision)
    assert observed == expected_gates
    assert result.status == "completed"
    assert calls == ["plan", "quest", "logic"]
    assert result.outputs["answer"] == calls


def test_development_resume_is_not_recorded_as_an_author_decision(tmp_path):
    store, _, run, controls, calls, execute = _case(tmp_path, "direction")
    assert execute().status == "waiting_user"
    store.update_run(run.id, status="waiting_user")
    pending = controls.detail("dev", run.id)["pending_gate"]
    decision = controls.authorize_resume(
        "dev", run.id, expected_gate_hash=pending["gate_hash"], actor_kind="development_operator"
    )
    assert decision["authority"] == "development_operator_action"
    assert decision["actor_kind"] == "development_operator"
    with pytest.raises(ValueError, match="主体"):
        controls.authorize_resume("dev", run.id, expected_gate_hash=pending["gate_hash"])
    with pytest.raises(ValueError, match="决定"):
        controls.consume_resume("dev", run.id, pending, {**decision, "actor_kind": "author"})
    store.update_run(run.id, status="running")
    assert execute(decision).status == "completed"
    assert calls == ["plan", "quest", "logic"]


def test_requested_pause_stops_next_dispatch_and_revision_invalidates_approval(tmp_path):
    store, task, run, controls, calls, execute = _case(tmp_path, "delegate")
    policy_hash = controls.detail("dev", run.id)["policy_hash"]
    controls.request_pause("dev", run.id, expected_policy_hash=policy_hash)
    result = execute()
    assert result.status == "waiting_user" and calls == []
    store.update_run(run.id, status="waiting_user")
    pending = controls.detail("dev", run.id)["pending_gate"]
    task.revision += 1
    store.save_task(task)
    with pytest.raises(ValueError, match="任务已变化"):
        controls.authorize_resume("dev", run.id, expected_gate_hash=pending["gate_hash"])


@pytest.mark.parametrize("operation", ["coverage_audit", "validate_decisions"])
def test_system_checks_ignore_author_allow_and_review_toggles(operation):
    legacy = {"allowed_operations": [], "review_operations": [operation]}
    assert CollaborationStore.operation_allowed(legacy, operation)
    assert not CollaborationStore.operation_review(legacy, operation)
    normalized = CollaborationPolicy.model_validate(legacy).normalized()
    assert operation in normalized["allowed_operations"]
    assert operation not in normalized["review_operations"]
