"""New business-capability admission, independent of already-passed graph tests."""

import pytest

from owcopilot.workflow.graph_v2 import (
    GraphSpecV2,
    PortSpec,
    compile_graph,
    create_narrative_graph,
    narrative_role_registry,
)
from owcopilot.workflow.v2_planning import validate_requirement_capabilities


@pytest.mark.parametrize(
    "case", ["valid", "missing_gate", "missing_consumption", "explicit_baseline"]
)
def test_planning_checks_real_requirement_capabilities_without_repairing_graph(case):
    baseline = case in {"missing_gate", "explicit_baseline"}
    graph = create_narrative_graph("baseline" if baseline else "constrained")
    if case == "missing_consumption":
        graph = graph.model_copy(
            update={
                "data_edges": [
                    edge
                    for edge in graph.data_edges
                    if not (edge.target_node == "audit" and edge.target_port == "choices")
                ]
            }
        )
    # All four proposals are valid at the generic typed-graph layer. The missing
    # evidence edge is an optional port there, while being a hard business need.
    compile_graph(graph, narrative_role_registry(), {"task": PortSpec(type_name="task_context")})
    before = graph.model_dump(mode="json")
    requirements = (
        {}
        if case == "explicit_baseline"
        else {
            "distinct_outcomes": True,
            "decision_option_count": 2,
        }
    )
    if case in {"missing_gate", "missing_consumption"}:
        with pytest.raises(ValueError, match="明确可玩需求"):
            validate_requirement_capabilities(graph, requirements)
    else:
        admitted = validate_requirement_capabilities(graph, requirements)
        assert admitted["status"] == "admitted"
        assert admitted["playable_coverage_required"] is (case == "valid")
        if case == "valid":
            assert admitted["consuming_nodes"]["gate"] == "decision_gate"
            assert admitted["consuming_nodes"]["final_audit"] == "audit"
    assert GraphSpecV2.model_validate(before).model_dump(mode="json") == graph.model_dump(
        mode="json"
    )
