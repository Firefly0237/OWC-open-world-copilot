"""Typed graph instances, actual data consumption, strict reuse and cancellation contracts."""

from copy import deepcopy

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.graph_v2 import (
    ControlEdge,
    DataEdge,
    GraphSpecV2,
    NodeSpecV2,
    OutputRef,
    PortSpec,
    RoleContract,
    compile_graph,
    create_narrative_graph,
    execute_graph,
    narrative_role_registry,
    rewrite_graph,
)


def graph_case():
    port = PortSpec(type_name="json")
    roles = {
        "produce": RoleContract(
            operation="produce", inputs={"item": port}, outputs={"value": port}
        ),
        "sum": RoleContract(
            operation="sum", inputs={"left": port, "right": port}, outputs={"value": port}
        ),
    }
    graph = GraphSpecV2(
        name="Two instances preserve distinct products",
        nodes=[
            NodeSpecV2(
                id="first",
                operation="produce",
                product_key="a",
                problem_key="a/fix",
                context_bindings={"item": "one"},
            ),
            NodeSpecV2(
                id="second",
                operation="produce",
                product_key="b",
                problem_key="b/fix",
                context_bindings={"item": "two"},
            ),
            NodeSpecV2(id="total", operation="sum", product_key="sum", problem_key="sum/fix"),
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
    return graph, roles, {"one": port, "two": port}


def test_same_operation_has_distinct_ports_and_rewrite_consumes_only_invalidated_products():
    graph, roles, context_ports = graph_case()
    calls = []
    events = []

    def produce(node, inputs):
        calls.append((node.id, deepcopy(inputs)))
        return {"value": inputs["item"]}

    def total(node, inputs):
        calls.append((node.id, deepcopy(inputs)))
        return {"value": inputs["left"] + inputs["right"]}

    handlers = {"produce": produce, "sum": total}
    first = execute_graph(
        compile_graph(graph, roles, context_ports),
        {"one": 2, "two": 3},
        handlers,
        emit=lambda kind, payload, **kw: events.append((kind, payload, kw)),
    )
    before = content_hash(first)
    assert first.status == "completed" and first.outputs["answer"] == 5
    assert calls[-1] == ("total", {"left": 2, "right": 3})
    assert first.outcomes["first"].problem_key != first.outcomes["second"].problem_key
    assert (
        first.outcomes["total"].input_refs["left"]["artifact_id"]
        != first.outcomes["total"].input_refs["right"]["artifact_id"]
    )
    changed = rewrite_graph(
        graph,
        nodes_upsert=[
            graph.nodes[0].model_copy(update={"guidance": "New version affects this producer only"})
        ],
    )
    calls.clear()
    second = execute_graph(
        compile_graph(changed, roles, context_ports), {"one": 2, "two": 3}, handlers, prior=first
    )
    assert second.outputs["answer"] == 5
    assert second.outcomes["second"].status == "reused"
    assert [node for node, _ in calls] == ["first", "total"]
    assert second.outcomes["total"].signature != first.outcomes["total"].signature
    assert content_hash(first) == before
    assert changed.parent_graph_id == graph.id and changed.version == graph.version + 1
    assert any(
        kind == "graph.node.started" and payload["input_refs"] for kind, payload, _ in events
    )


def test_real_narrative_rewrite_adds_consumed_contract_and_invalid_graphs_fail_before_execution():
    context_ports = {"task": PortSpec(type_name="task_context")}
    registry = narrative_role_registry()
    baseline = compile_graph(create_narrative_graph("baseline"), registry, context_ports)
    constrained = compile_graph(create_narrative_graph("constrained"), registry, context_ports)
    assert "decision_gate" not in baseline.order and "decision_gate" in constrained.order
    assert (
        constrained.order.index("logic")
        < constrained.order.index("decision_gate")
        < constrained.order.index("dialogue")
    )
    assert any(
        edge.source_node == "decision_gate" and edge.target_port == "contract"
        for edge in constrained.spec.data_edges
    )
    assert (
        registry["dialogue_constrained"].inputs["contract"].type_name
        == "validated_decision_contract"
    )
    graph, roles, ports = graph_case()
    invalid = [
        graph.model_copy(
            update={"control_edges": [ControlEdge(source_node="total", target_node="first")]}
        ),
        graph.model_copy(
            update={
                "data_edges": [
                    graph.data_edges[0].model_copy(update={"source_port": "absent"}),
                    graph.data_edges[1],
                ]
            }
        ),
        graph.model_copy(update={"data_edges": [*graph.data_edges, graph.data_edges[0]]}),
        graph.model_copy(
            update={
                "nodes": [
                    graph.nodes[0].model_copy(update={"operation": "unregistered"}),
                    *graph.nodes[1:],
                ]
            }
        ),
    ]
    for candidate in invalid:
        with pytest.raises(ValueError):
            compile_graph(candidate, roles, ports)
    unconsumed = {**roles, "sum": roles["sum"].model_copy(update={"consumed_inputs": ["left"]})}
    with pytest.raises(ValueError, match="未被消费"):
        compile_graph(graph, unconsumed, ports)
    with pytest.raises(ValueError, match="问题身份"):
        rewrite_graph(
            graph, nodes_upsert=[graph.nodes[0].model_copy(update={"problem_key": "fresh-budget"})]
        )


def test_failure_is_branch_local_corrupted_cache_rejected_and_late_outputs_are_discarded():
    graph, roles, ports = graph_case()
    compiled = compile_graph(graph, roles, ports)
    calls = []

    def fail_first(node, inputs):
        calls.append(node.id)
        if node.id == "first":
            raise ValueError("Observed failure, no graph retry")
        return {"value": inputs["item"]}

    result = execute_graph(
        compiled, {"one": 2, "two": 3}, {"produce": fail_first, "sum": lambda n, i: {"value": 5}}
    )
    assert result.outcomes["second"].status == "succeeded"
    assert result.outcomes["total"].status == "blocked_dependency"
    assert calls.count("first") == 1 and result.outputs == {}
    ok = execute_graph(
        compiled,
        {"one": 2, "two": 3},
        {
            "produce": lambda n, i: {"value": i["item"]},
            "sum": lambda n, i: {"value": i["left"] + i["right"]},
        },
    )
    corrupted = ok.model_dump(mode="json")
    corrupted["outcomes"]["first"]["outputs"]["value"]["value"] = 99
    rejected = execute_graph(compiled, {"one": 2, "two": 3}, {}, prior=corrupted)
    assert rejected.outcomes["first"].status == "failed"
    assert rejected.outcomes["second"].status == "reused"
    assert "first.value" not in rejected.artifacts
    cancelled = [False]

    def late(node, inputs):
        cancelled[0] = True
        return {"value": inputs["item"]}

    stopped = execute_graph(
        compiled, {"one": 2, "two": 3}, {"produce": late}, cancelled=lambda: cancelled[0]
    )
    assert stopped.status == "cancelled" and stopped.artifacts == {} and stopped.outputs == {}
    assert stopped.outcomes["first"].error == "迟到结果未发布"
