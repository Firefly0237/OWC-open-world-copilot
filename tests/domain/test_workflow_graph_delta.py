"""Structural edits change actual data flow while preserving execution obligations."""

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.graph_delta import (
    ConnectControl,
    ConnectData,
    DisconnectControl,
    DisconnectData,
    GraphDelta,
    InsertNode,
    RemoveNode,
    ReplaceRole,
    apply_graph_delta,
    baseline_to_constrained_delta,
)
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
)


def test_baseline_upgrade_is_explicit_patch_and_keeps_original_budget_and_snapshot():
    graph = create_narrative_graph()
    before = content_hash(graph)
    roles = narrative_role_registry()
    context_ports = {"task": PortSpec(type_name="task_context")}
    delta = baseline_to_constrained_delta(graph)
    serialized = delta.model_dump(mode="json")
    result = apply_graph_delta(graph, serialized, roles, context_ports)
    by_id = {node.id: node for node in result.graph.nodes}
    assert content_hash(graph) == before
    assert result.graph.parent_graph_id == graph.id and result.graph.id != graph.id
    assert result.graph.version == graph.version + 1
    assert result.parent_graph_hash == delta.expected_graph_hash != result.graph_hash
    assert len(result.changes) == 12
    assert by_id["logic"].operation == "logic_contract"
    assert by_id["dialogue"].operation == "dialogue_constrained"
    assert by_id["decision_gate"].operation == "validate_decisions"
    for old in graph.nodes:
        assert (by_id[old.id].product_key, by_id[old.id].problem_key) == (
            old.product_key,
            old.problem_key,
        )
    assert by_id["decision_gate"].problem_key == by_id["logic"].problem_key
    assert (
        DataEdge(
            source_node="decision_gate",
            source_port="contract",
            target_node="dialogue",
            target_port="contract",
        )
        in result.graph.data_edges
    )
    assert result.graph.outputs == graph.outputs
    assert "audit" in result.affected_nodes and "plan" not in result.affected_nodes
    with pytest.raises(ValueError, match="CAS"):
        apply_graph_delta(result.graph, delta, roles, context_ports)


def test_insert_and_remove_optional_node_rewire_real_consumed_values_and_control_edges():
    port = PortSpec(type_name="json")
    roles = {
        operation: RoleContract(
            operation=operation, inputs={"value": port}, outputs={"value": port}
        )
        for operation in ("source", "transform", "sink")
    }
    first = NodeSpecV2(
        id="source",
        operation="source",
        product_key="source-product",
        problem_key="source-problem",
        context_bindings={"value": "initial"},
    )
    last = NodeSpecV2(
        id="sink", operation="sink", product_key="final-product", problem_key="final-problem"
    )
    direct = DataEdge(
        source_node="source", source_port="value", target_node="sink", target_port="value"
    )
    initial_control = ControlEdge(source_node="source", target_node="sink")
    graph = GraphSpecV2(
        name="Minimal typed pipeline",
        nodes=[first, last],
        data_edges=[direct],
        control_edges=[initial_control],
        outputs={"answer": OutputRef(node_id="sink", port="value")},
    )
    inserted = NodeSpecV2(
        id="adapt",
        operation="transform",
        product_key=first.product_key,
        problem_key=first.problem_key,
        required=False,
    )
    left = DataEdge(
        source_node="source", source_port="value", target_node="adapt", target_port="value"
    )
    right = DataEdge(
        source_node="adapt", source_port="value", target_node="sink", target_port="value"
    )
    final_control = ControlEdge(source_node="adapt", target_node="sink")
    context_ports = {"initial": port}
    expanded = apply_graph_delta(
        graph,
        GraphDelta(
            expected_graph_hash=compile_graph(graph, roles, context_ports).graph_hash,
            rationale="Insert a registered transformation with explicit data consumption",
            operations=[
                InsertNode(node=inserted, budget_source_node="source"),
                DisconnectData(edge=direct),
                ConnectData(edge=left),
                ConnectData(edge=right),
                DisconnectControl(edge=initial_control),
                ConnectControl(edge=final_control),
            ],
        ),
        roles,
        context_ports,
    )
    consumed = []

    def transform(_node, inputs):
        consumed.append(inputs["value"])
        return {"value": inputs["value"] * 3}

    handlers = {
        "source": lambda _node, inputs: inputs,
        "transform": transform,
        "sink": lambda _node, inputs: {"value": inputs["value"] + 1},
    }
    execution = execute_graph(
        compile_graph(expanded.graph, roles, context_ports), {"initial": 2}, handlers
    )
    assert execution.outputs["answer"] == 7 and consumed == [2]
    reduced = apply_graph_delta(
        expanded.graph,
        GraphDelta(
            expected_graph_hash=expanded.graph_hash,
            rationale="Remove an optional transform, explicitly restoring each consumed edge",
            operations=[
                DisconnectData(edge=left),
                DisconnectData(edge=right),
                DisconnectControl(edge=final_control),
                RemoveNode(node_id="adapt"),
                ConnectData(edge=direct),
                ConnectControl(edge=initial_control),
            ],
        ),
        roles,
        context_ports,
    )
    restored = execute_graph(
        compile_graph(reduced.graph, roles, context_ports), {"initial": 2}, handlers
    )
    assert restored.outputs["answer"] == 3 and consumed == [2]


@pytest.mark.parametrize("invalid", ["required", "role", "port", "cycle", "budget"])
def test_invalid_edits_are_rejected_without_mutating_source(invalid):
    graph = create_narrative_graph()
    roles = narrative_role_registry()
    context_ports = {"task": PortSpec(type_name="task_context")}
    compiled = compile_graph(graph, roles, context_ports)
    before = content_hash(graph)
    if invalid == "required":
        operations = [RemoveNode(node_id="plan")]
    elif invalid == "role":
        operations = [ReplaceRole(node_id="logic", operation="unregistered")]
    elif invalid == "port":
        operations = [
            ConnectData(
                edge=DataEdge(
                    source_node="plan",
                    source_port="missing",
                    target_node="logic",
                    target_port="plan",
                )
            )
        ]
    elif invalid == "cycle":
        operations = [ConnectControl(edge=ControlEdge(source_node="audit", target_node="plan"))]
    else:
        operations = [
            InsertNode(
                node=NodeSpecV2(
                    id="extra",
                    operation="draft_logic",
                    product_key="renamed-product",
                    problem_key="fresh-quota",
                ),
                budget_source_node="logic",
            )
        ]
    with pytest.raises(ValueError):
        apply_graph_delta(
            graph,
            {
                "expected_graph_hash": compiled.graph_hash,
                "rationale": "Invalid candidate",
                "operations": [operation.model_dump(mode="json") for operation in operations],
            },
            roles,
            context_ports,
        )
    assert content_hash(graph) == before


@pytest.mark.parametrize("bypass", ["drop_evidence", "replace_gate", "weaken_consumer"])
def test_mandatory_gates_and_their_existing_consumption_cannot_be_weakened(bypass):
    graph = create_narrative_graph("constrained")
    roles = narrative_role_registry()
    context_ports = {"task": PortSpec(type_name="task_context")}
    if bypass == "drop_evidence":
        edge = next(
            edge
            for edge in graph.data_edges
            if edge.target_node == "audit" and edge.target_port == "choices"
        )
        operations = [DisconnectData(edge=edge)]
    elif bypass == "replace_gate":
        operations = [ReplaceRole(node_id="decision_gate", operation="draft_logic")]
    else:
        edge = next(
            edge
            for edge in graph.data_edges
            if edge.target_node == "dialogue" and edge.target_port == "contract"
        )
        operations = [
            DisconnectData(edge=edge),
            ReplaceRole(node_id="dialogue", operation="draft_dialogue"),
        ]
    with pytest.raises(ValueError):
        apply_graph_delta(
            graph,
            {
                "expected_graph_hash": compile_graph(graph, roles, context_ports).graph_hash,
                "rationale": "Attempted weakening",
                "operations": [op.model_dump(mode="json") for op in operations],
            },
            roles,
            context_ports,
        )


def test_registered_checker_can_be_inserted_without_template_switch_or_fresh_budget():
    graph = create_narrative_graph("constrained")
    roles = narrative_role_registry()
    context_ports = {"task": PortSpec(type_name="task_context")}
    logic = next(node for node in graph.nodes if node.id == "logic")
    checker = NodeSpecV2(
        id="extra_gate",
        operation="validate_decisions",
        product_key=logic.product_key,
        problem_key=logic.problem_key,
    )
    operations = [InsertNode(node=checker, budget_source_node="logic")]
    for source_node, source_port, target_port in [
        ("plan", "plan", "plan"),
        ("logic", "logic", "logic"),
        ("logic", "decisions", "decisions"),
    ]:
        operations.append(
            ConnectData(
                edge=DataEdge(
                    source_node=source_node,
                    source_port=source_port,
                    target_node=checker.id,
                    target_port=target_port,
                )
            )
        )
    operations.append(ConnectControl(edge=ControlEdge(source_node=checker.id, target_node="bind")))
    result = apply_graph_delta(
        graph,
        {
            "expected_graph_hash": compile_graph(graph, roles, context_ports).graph_hash,
            "rationale": "Add a host-registered validation gate",
            "operations": [op.model_dump(mode="json") for op in operations],
        },
        roles,
        context_ports,
    )
    compiled = compile_graph(result.graph, roles, context_ports)
    assert compiled.order.index("extra_gate") < compiled.order.index("bind")
    assert result.graph_hash != result.parent_graph_hash
    assert next(node for node in result.graph.nodes if node.id == "extra_gate").problem_key == (
        logic.problem_key
    )
