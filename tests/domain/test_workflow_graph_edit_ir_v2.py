"""New model protocol has business edits; stable execution authority stays host-owned."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from owcopilot.content.hash import content_hash
from owcopilot.workflow.evolution import SourceRef
from owcopilot.workflow.evolution_v2 import (
    ReflectionReportV2,
    TypedEvolutionProposalV2,
    bind_method_schema,
    verify_problem_identity,
)
from owcopilot.workflow.graph_delta import (
    GraphEditIR,
    baseline_to_constrained_delta,
    compile_graph_edit,
    graph_edit_context,
    project_legacy_graph_delta,
)
from owcopilot.workflow.graph_v2 import (
    PortSpec,
    create_narrative_graph,
    narrative_role_registry,
)


def _business_edit():
    graph = create_narrative_graph()
    legacy = baseline_to_constrained_delta(graph).model_dump(mode="json")
    projection = project_legacy_graph_delta(legacy)
    return graph, projection["edit"]


def test_business_ir_compiles_real_edges_and_derives_identity_and_context():
    graph, edit = _business_edit()
    frozen = content_hash(graph)
    receipt = compile_graph_edit(
        graph, edit, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    )
    gate = next(
        node for node in receipt.applied.graph.nodes if node.operation == "validate_decisions"
    )
    source = next(node for node in graph.nodes if node.id == "logic")
    assert content_hash(graph) == frozen
    assert (gate.product_key, gate.problem_key) == (source.product_key, source.problem_key)
    assert gate.context_bindings == {}  # validate_decisions has no task input.
    assert receipt.applied.graph_hash != receipt.applied.parent_graph_hash
    assert len(receipt.host_delta.operations) == len(edit["operations"]) == 12
    assert receipt.edit_hash == content_hash(edit)
    context = graph_edit_context(graph, narrative_role_registry())
    assert all(
        "product_key" not in node and "context_bindings" not in node for node in context["nodes"]
    )
    with pytest.raises(ValueError, match="CAS"):
        compile_graph_edit(
            receipt.applied.graph,
            edit,
            narrative_role_registry(),
            {"task": PortSpec(type_name="task_context")},
        )


def test_host_fields_and_wrong_role_source_cannot_be_smuggled_into_business_ir():
    graph, edit = _business_edit()
    insert = next(step for step in edit["operations"] if step["op"] == "insert_registered_role")
    contaminated = deepcopy(edit)
    next(step for step in contaminated["operations"] if step["op"] == insert["op"])[
        "product_key"
    ] = "new-quota"
    with pytest.raises(ValidationError):
        GraphEditIR.model_validate(contaminated)
    insert["source_node"] = "plan"
    with pytest.raises(ValueError, match="继承合同"):
        compile_graph_edit(
            graph, edit, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
        )
    insert["source_node"] = "logic"
    edit["operations"].append({"op": "remove_node", "node_id": "audit"})
    with pytest.raises(ValueError, match="必需产物|强制检查"):
        compile_graph_edit(
            graph, edit, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
        )


def test_guidance_edit_preserves_execution_authority_and_cannot_change_mandatory_checks():
    graph, baseline_edit = _business_edit()
    edit = {
        "expected_graph_hash": baseline_edit["expected_graph_hash"],
        "rationale": "Correct a formally diagnosed output contract",
        "operations": [
            {"op": "update_guidance", "node_id": "logic", "guidance": "Use valid types"}
        ],
    }
    receipt = compile_graph_edit(
        graph, edit, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    )
    old = next(node for node in graph.nodes if node.id == "logic")
    new = next(node for node in receipt.applied.graph.nodes if node.id == "logic")
    assert new.model_dump(exclude={"guidance"}) == old.model_dump(exclude={"guidance"})
    assert receipt.applied.graph.data_edges == graph.data_edges
    assert receipt.applied.graph.control_edges == graph.control_edges
    assert {"logic", "audit"} <= set(receipt.applied.affected_nodes)
    contaminated = deepcopy(edit)
    contaminated["operations"][0]["problem_key"] = "fresh-budget"
    with pytest.raises(ValidationError):
        GraphEditIR.model_validate(contaminated)
    edit["operations"][0]["node_id"] = "audit"
    with pytest.raises(ValueError, match="强制检查"):
        compile_graph_edit(
            graph, edit, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
        )


def test_legacy_invalid_host_claims_are_retained_in_independent_projection_only():
    graph, _ = _business_edit()
    old = baseline_to_constrained_delta(graph).model_dump(mode="json")
    inserted = next(step for step in old["operations"] if step["op"] == "insert_node")
    inserted["node"].update(
        product_key="slice/decisions",
        problem_key="slice/decisions/validate",
        context_bindings={"task": "task"},
    )
    before = content_hash(old)
    projected = project_legacy_graph_delta(old)
    receipt = compile_graph_edit(
        graph,
        projected["edit"],
        narrative_role_registry(),
        {"task": PortSpec(type_name="task_context")},
    )
    assert before == content_hash(old) == projected["legacy_proposal_hash"]
    assert projected["old_status"] == "unchanged"
    assert projected["validation_status"] == "not_performed"
    fields = next(
        row["fields"] for row in projected["stripped_host_fields"] if "product_key" in row["fields"]
    )
    assert fields["product_key"] == "slice/decisions"
    assert fields["context_bindings"] == {"task": "task"}
    assert receipt.derived_fields[-1]["product_key"] == "slice/logic"


def test_problem_identity_is_fixed_while_diagnosis_translation_is_allowed():
    graph, edit = _business_edit()
    problem_id = "evolution:" + "a" * 64
    report = ReflectionReportV2(
        problem_id=problem_id,
        target_description="缺少决策合同交接",
        diagnosis="实际轨迹的机械问题",
        evidence_refs=[SourceRef(kind="run", id="source", content_hash="b" * 64)],
        proposed_change="structure",
    )
    proposal = TypedEvolutionProposalV2(
        problem_id=problem_id,
        target_description="Missing decision contract handoff",
        rationale="Add an actual typed gate",
        change_kind="structure",
        graph_edit=edit,
    )
    verify_problem_identity(problem_id, report, proposal)
    schema = bind_method_schema(
        TypedEvolutionProposalV2.model_json_schema(),
        problem_id=problem_id,
        graph=graph,
        registry=narrative_role_registry(),
        graph_hash=edit["expected_graph_hash"],
    )
    assert schema["properties"]["problem_id"]["const"] == problem_id
    assert (
        schema["$defs"]["GraphEditIR"]["properties"]["expected_graph_hash"]["const"]
        == edit["expected_graph_hash"]
    )
    assert "NodeSpecV2" not in schema["$defs"]
    with pytest.raises(ValueError, match="固定的问题身份"):
        verify_problem_identity(
            problem_id, report, proposal.model_copy(update={"problem_id": "other"})
        )
