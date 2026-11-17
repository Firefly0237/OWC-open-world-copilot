"""Per-node official SqliteSaver checkpoints, joins and immutable run bindings."""

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from owcopilot.workflow.graph_driver import execute_persistent_graph
from owcopilot.workflow.graph_v2 import (
    DataEdge,
    GraphSpecV2,
    NodeSpecV2,
    OutputRef,
    PortSpec,
    RoleContract,
    compile_graph,
    rewrite_graph,
)


def case():
    port = PortSpec(type_name="json")
    registry = {
        "produce": RoleContract(
            operation="produce", inputs={"item": port}, outputs={"value": port}
        ),
        "sum": RoleContract(
            operation="sum", inputs={"left": port, "right": port}, outputs={"value": port}
        ),
    }
    spec = GraphSpecV2(
        name="Persistent two-parent join",
        nodes=[
            NodeSpecV2(
                id="first",
                operation="produce",
                product_key="first",
                problem_key="first/produce",
                context_bindings={"item": "one"},
            ),
            NodeSpecV2(
                id="second",
                operation="produce",
                product_key="second",
                problem_key="second/produce",
                context_bindings={"item": "two"},
            ),
            NodeSpecV2(
                id="total", operation="sum", product_key="total", problem_key="total/produce"
            ),
        ],
        data_edges=[
            DataEdge(
                source_node="first", source_port="value", target_node="total", target_port="left"
            ),
            DataEdge(
                source_node="second", source_port="value", target_node="total", target_port="right"
            ),
        ],
        outputs={"answer": OutputRef(node_id="total", port="value")},
    )
    ports = {"one": port, "two": port}
    return spec, registry, ports


def test_actual_per_node_checkpoints_resume_pending_join_without_repeating_completed_work(tmp_path):
    spec, registry, ports = case()
    compiled = compile_graph(spec, registry, ports)
    path = tmp_path / "checkpoints.sqlite3"
    calls = []
    interrupt_once = [True]
    events = []

    def produce(node, inputs):
        calls.append(node.id)
        return {"value": inputs["item"]}

    def total(node, inputs):
        calls.append(node.id)
        if interrupt_once[0]:
            interrupt_once[0] = False
            raise KeyboardInterrupt("Controlled process interruption before the final result")
        assert inputs == {"left": 2, "right": 3}
        return {"value": inputs["left"] + inputs["right"]}

    handlers = {"produce": produce, "sum": total}
    with pytest.raises(KeyboardInterrupt):
        execute_persistent_graph(compiled, {"one": 2, "two": 3}, handlers, path, "run-one")
    with SqliteSaver.from_conn_string(str(path)) as checkpointer:
        # The official checkpoint has real completed parents, not one wrapper node.
        saved = checkpointer.get_tuple(
            {"configurable": {"thread_id": f"run-one:{compiled.graph_hash}"}}
        )
        assert saved is not None
        assert set(saved.checkpoint["channel_values"]["outcomes"]) == {"first", "second"}
    resumed = execute_persistent_graph(
        compiled,
        {"one": 2, "two": 3},
        handlers,
        path,
        "run-one",
        emit=lambda kind, payload, **kwargs: events.append((kind, payload)),
    )
    assert resumed.status == "completed" and resumed.outputs == {"answer": 5}
    assert calls.count("first") == 1 and calls.count("second") == 1
    assert calls.count("total") == 2
    assert any(kind == "graph.resuming" for kind, _ in events)
    finished_calls = list(calls)
    repeated = execute_persistent_graph(compiled, {"one": 2, "two": 3}, {}, path, "run-one")
    assert repeated.model_dump(mode="json") == resumed.model_dump(mode="json")
    assert calls == finished_calls


def test_same_run_rejects_graph_or_context_change_and_cancellation_discards_late_output(tmp_path):
    spec, registry, ports = case()
    compiled = compile_graph(spec, registry, ports)
    path = tmp_path / "checkpoints.sqlite3"
    handlers = {
        "produce": lambda node, inputs: {"value": inputs["item"]},
        "sum": lambda node, inputs: {"value": inputs["left"] + inputs["right"]},
    }
    execute_persistent_graph(compiled, {"one": 2, "two": 3}, handlers, path, "bound-run")
    next_spec = rewrite_graph(
        spec, nodes_upsert=[spec.nodes[0].model_copy(update={"guidance": "Changed contract"})]
    )
    with pytest.raises(ValueError, match="不同图或上下文"):
        execute_persistent_graph(
            compile_graph(next_spec, registry, ports), {"one": 2, "two": 3}, {}, path, "bound-run"
        )
    with pytest.raises(ValueError, match="不同图或上下文"):
        execute_persistent_graph(compiled, {"one": 5, "two": 3}, {}, path, "bound-run")
    with pytest.raises(ValueError, match="不同图或上下文"):
        execute_persistent_graph(
            compiled,
            {"one": 2, "two": 3},
            {},
            path,
            "bound-run",
            context_identity={"requirements": "changed"},
        )
    cancelled = [False]

    def late(node, inputs):
        cancelled[0] = True
        return {"value": inputs["item"]}

    stopped = execute_persistent_graph(
        compiled,
        {"one": 2, "two": 3},
        {"produce": late},
        path,
        "cancelled-run",
        cancelled=lambda: cancelled[0],
    )
    assert stopped.status == "cancelled" and stopped.artifacts == {} and stopped.outputs == {}
    assert stopped.outcomes["first"].error == "迟到结果未发布"
