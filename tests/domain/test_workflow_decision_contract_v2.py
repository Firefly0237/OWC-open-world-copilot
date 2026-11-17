"""New explicit-selector checks; legacy V1 tests and historical records stay intact."""

from copy import deepcopy

import pytest

from owcopilot.workflow.decision_contract import (
    DecisionEffectMapV2,
    validate_choice_coverage,
    validate_contract_snapshot,
    validate_decisions,
)


def case():
    plan = {
        "premise": "Development input",
        "beats": ["Choose"],
        "events": [{"id": "e", "summary": "Choose"}],
        "decisions": [
            {
                "id": "d",
                "event_id": "e",
                "options": ["Help", "Refuse"],
                "consequences": ["Trust rises", "Trust falls"],
            }
        ],
    }
    quest = {
        "id": "q",
        "title": "Development fixture",
        "stages": [{"id": "s", "summary": "Choose"}],
        "logic": {
            "variables": [
                {
                    "id": "route",
                    "type": "enum",
                    "enum_values": ["unset", "help", "refuse"],
                    "default": "unset",
                },
                {"id": "trust", "type": "int", "default": 0},
            ],
            "branches": [
                {
                    "id": route,
                    "from_stage": "s",
                    "condition": f"route == '{route}' and trust >= 0",
                    "outcome": route,
                    "effects": [
                        {"var": "route", "value": route},
                        {"var": "trust", "op": "inc", "value": delta},
                    ],
                }
                for route, delta in (("help", 1), ("refuse", -1))
            ],
        },
    }
    mapping = {
        "schema_version": 2,
        "quest_id": "q",
        "selector_variables": ["route"],
        "options": [
            {
                "decision_id": "d",
                "option_index": index,
                "branch_id": route,
                "effect_indices": [0, 1],
            }
            for index, route in enumerate(("help", "refuse"))
        ],
    }
    tree = {
        "id": "t",
        "quest_id": "q",
        "root_node": "n",
        "nodes": {
            "n": {
                "id": "n",
                "text": "Choose",
                "choices": [
                    {"text": route, "effects": [{"var": "route", "value": route}]}
                    for route in ("help", "refuse")
                ],
            }
        },
    }
    choices = {
        "quest_id": "q",
        "tree_id": "t",
        "choices": [
            {"decision_id": "d", "option_index": index, "node_id": "n", "choice_index": index}
            for index in range(2)
        ],
    }
    return plan, quest, mapping, tree, choices


def test_v2_idempotent_selector_reset_passes_with_real_distinct_game_consequences():
    plan, quest, mapping, tree, choices = case()
    assert DecisionEffectMapV2.model_validate(mapping).selector_variables == ["route"]
    contract = validate_decisions(plan, quest, mapping, require_distinct=True)
    assert contract["schema_version"] == 2
    assert validate_contract_snapshot(contract) == contract
    report = validate_choice_coverage(plan, quest, tree, mapping, choices, require_distinct=True)
    assert report["covered_choice_count"] == 2
    assert report["semantic_status"] == "withheld"


def test_v2_condition_state_cannot_be_reclassified_as_selector_or_applied_before_branch():
    plan, quest, mapping, tree, choices = case()
    changed = deepcopy(tree)
    changed["nodes"]["n"]["choices"][0]["effects"].append({"var": "trust", "op": "inc", "value": 1})
    with pytest.raises(ValueError, match="selector"):
        validate_choice_coverage(plan, quest, changed, mapping, choices)
    invalid = {**mapping, "selector_variables": ["route", "trust"]}
    with pytest.raises(ValueError, match="bool／enum"):
        validate_decisions(plan, quest, invalid)
    changed = deepcopy(quest)
    changed["logic"]["branches"][0]["effects"][0]["value"] = "refuse"
    with pytest.raises(ValueError, match="重设selector"):
        validate_choice_coverage(plan, changed, tree, mapping, choices)


def test_v2_only_changing_selector_is_not_a_distinct_game_outcome():
    plan, quest, mapping, _, _ = case()
    for branch in quest["logic"]["branches"]:
        branch["effects"] = branch["effects"][:1]
    for option in mapping["options"]:
        option["effect_indices"] = [0]
    with pytest.raises(ValueError, match="不同状态后果"):
        validate_decisions(plan, quest, mapping, require_distinct=True)
