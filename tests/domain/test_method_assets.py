"""Fragments alter executable typed nodes while preserving host guards and identities."""

from copy import deepcopy

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.method_assets import bind_asset, compose_asset, derive_assets, extract_asset
from owcopilot.workflow.method_selection import (
    JointRulePolicy,
    MethodAction,
    StagedLookaheadPolicy,
    enumerate_actions,
)


def version(variant="constrained"):
    spec = create_narrative_graph(variant)
    spec = spec.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"guidance": "consume-current-input-" + node.id})
                for node in spec.nodes
            ]
        }
    )
    return {
        "id": "registered-method",
        "version_hash": "f" * 64,
        "graph": spec.model_dump(mode="json"),
    }


def test_fragment_boundaries_are_typed_and_only_selected_instructions_are_reused():
    source = version()
    assets = derive_assets(source)
    assert {a.kind for a in assets} == {"graph", "subgraph", "skill"}
    skill = next(a for a in assets if a.kind == "skill")
    graph, binding = compose_asset(source, skill)
    actual = [node for node in graph.nodes if node.parameters.get("asset_binding")]
    assert len(actual) == 1
    assert actual[0].guidance.startswith("consume-current-input-")
    assert all(not node.guidance for node in graph.nodes if node not in actual)
    assert {"coverage_audit", "validate_decisions", "bind_dialogue"} <= {
        node.operation for node in graph.nodes
    }
    assert actual[0].product_key == "slice/dialogue"
    assert binding["composition"] == "typed_fragment_in_registered_host"
    changed = deepcopy(source)
    changed["graph"]["nodes"][0]["guidance"] = "changed source"
    with pytest.raises(ValueError, match="变化"):
        compose_asset(changed, skill)
    with pytest.raises(ValueError, match="技术门禁"):
        extract_asset(source, "skill", ["audit"])


def test_explicit_capability_change_produces_real_local_graph_adaptation():
    source = version("baseline")
    asset = extract_asset(source, "graph")
    graph, binding = bind_asset(source, asset, "decision_contract")
    assert "validate_decisions" in {node.operation for node in graph.nodes}
    assert "draft_logic" not in {node.operation for node in graph.nodes}
    assert {"logic", "dialogue", "bind", "audit"} <= set(binding["adaptation"]["affected_nodes"])
    same_graph, same_binding = bind_asset(source, asset, "decision_contract")
    assert content_hash(same_graph) == content_hash(graph)
    assert same_binding == binding


def action(identity, cost, mode="reuse"):
    return MethodAction(identity, mode, None, None, None, {"required": ["guard"]}, cost, {}, 2)


def test_cost_and_rescue_changes_affect_choice_without_removing_required_checks():
    policy = JointRulePolicy()
    first, second = action("repair", 80), action("fresh", 100, "fresh")
    budget = {"remaining_dispatches": 4}
    assert policy.select({}, [first, second], budget).id == "repair"
    assert policy.select({}, [action("repair", 120), second], budget).id == "fresh"
    unknown = action("unknown", None)
    assert policy.select({}, [unknown, second], budget).id == "fresh"
    assert policy.select({}, [first], {"remaining_dispatches": 1}) is None
    # An information-complete staged control is allowed to tie the joint policy.
    assert StagedLookaheadPolicy().select({}, [first, second], budget).id == "repair"


def test_actions_retain_rejected_variants_and_unknown_costs():
    source = version("baseline")
    matches = [
        {"version": source, "version_id": source["id"], "eligible_to_execute": True, "score": 1.0}
    ]
    actions = enumerate_actions(matches, {"distinct_outcomes": True}, {})
    assert any(a.adaptation == "decision_contract" and a.eligible for a in actions)
    assert any(not a.eligible for a in actions)
    assert all(a.estimated_tokens is None for a in actions)
    assert len(actions) <= 13
