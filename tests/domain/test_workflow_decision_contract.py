"""Decision coverage is checked against real Branch.effects, selectors and production models."""

from copy import deepcopy

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import DialogueTree, Quest
from owcopilot.workflow.decision_contract import (
    validate_choice_coverage,
    validate_contract_snapshot,
    validate_decisions,
)
from owcopilot.workflow.models import EventPlan


def contract_case():
    plan = EventPlan.model_validate(
        {
            "premise": "A guard collecting a ransom, with two costly choices",
            "beats": ["Learn the guard's motive"],
            "events": [{"id": "gate", "summary": "Decide how to help"}],
            "decisions": [
                {
                    "id": "help",
                    "event_id": "gate",
                    "options": ["Pay", "Refuse"],
                    "consequences": ["Trust rises", "Trust falls"],
                }
            ],
        }
    )
    quest = Quest.model_validate(
        {
            "id": "q",
            "title": "Guard at the gate",
            "stages": [{"id": "gate", "summary": "Talk"}],
            "logic": {
                "variables": [
                    {
                        "id": "route",
                        "type": "enum",
                        "enum_values": ["unset", "pay", "refuse"],
                        "default": "unset",
                    },
                    {"id": "trust", "type": "int", "default": 0},
                ],
                "branches": [
                    {
                        "id": "pay",
                        "from_stage": "gate",
                        "condition": "route == 'pay'",
                        "outcome": "paid",
                        "effects": [{"var": "trust", "op": "inc", "value": 1}],
                    },
                    {
                        "id": "refuse",
                        "from_stage": "gate",
                        "condition": "route == 'refuse'",
                        "outcome": "refused",
                        "effects": [{"var": "trust", "op": "dec", "value": 1}],
                    },
                ],
            },
        }
    )
    mapping = {
        "quest_id": "q",
        "options": [
            {"decision_id": "help", "option_index": 0, "branch_id": "pay", "effect_indices": [0]},
            {
                "decision_id": "help",
                "option_index": 1,
                "branch_id": "refuse",
                "effect_indices": [0],
            },
        ],
    }
    tree = DialogueTree.model_validate(
        {
            "id": "tree",
            "quest_id": "q",
            "root_node": "gate",
            "nodes": {
                "gate": {
                    "id": "gate",
                    "text": "Will you help?",
                    "choices": [
                        {"text": "Pay", "effects": [{"var": "route", "value": "pay"}]},
                        {"text": "Refuse", "effects": [{"var": "route", "value": "refuse"}]},
                    ],
                }
            },
        }
    )
    choices = {
        "quest_id": "q",
        "tree_id": "tree",
        "choices": [
            {"decision_id": "help", "option_index": index, "node_id": "gate", "choice_index": index}
            for index in range(2)
        ],
    }
    return plan, quest, mapping, tree, choices


def test_real_effects_and_selectors_bind_without_claiming_semantic_proof():
    plan, quest, mapping, tree, choices = contract_case()
    before = content_hash({"plan": plan, "quest": quest, "tree": tree})
    contract = validate_decisions(plan, quest, mapping, require_distinct=True)
    assert contract["status"] == "validated" and contract["semantic_status"] == "withheld"
    assert validate_contract_snapshot(contract) == contract
    report = validate_choice_coverage(plan, quest, tree, mapping, choices, require_distinct=True)
    assert report["covered_choice_count"] == 2 and report["mechanical_status"] == "validated"
    assert report["semantic_status"] == "withheld"
    assert content_hash({"plan": plan, "quest": quest, "tree": tree}) == before


def test_missing_real_references_invalid_types_and_tampered_contract_cannot_pass():
    plan, quest, mapping, tree, choices = contract_case()
    for changes in [
        {"branch_id": "nonexistent"},
        {"effect_indices": []},
        {"effect_indices": [0, 0]},
        {"effect_indices": [True]},
        {"option_index": 20},
    ]:
        invalid = deepcopy(mapping)
        invalid["options"][0].update(changes)
        with pytest.raises(ValueError):
            validate_decisions(plan, quest, invalid)
    altered = quest.model_dump(mode="json")
    altered["logic"]["branches"][0]["effects"][0]["var"] = "undeclared"
    with pytest.raises(ValueError, match="未声明"):
        validate_decisions(plan, altered, mapping)
    contract = validate_decisions(plan, quest, mapping)
    contract["quest"]["title"] = "Mutated source"
    with pytest.raises(ValueError, match="hash"):
        validate_contract_snapshot(contract)
    with pytest.raises(ValueError, match="覆盖"):
        validate_choice_coverage(
            plan, quest, tree, mapping, {**choices, "choices": choices["choices"][:1]}
        )


def test_distinctness_is_explicit_and_outcome_duplication_or_wrong_selectors_are_rejected():
    plan, quest, mapping, tree, choices = contract_case()
    converging = quest.model_dump(mode="json")
    converging["logic"]["branches"][1]["effects"] = deepcopy(
        converging["logic"]["branches"][0]["effects"]
    )
    assert validate_decisions(plan, converging, mapping)["status"] == "validated"
    with pytest.raises(ValueError, match="不同状态后果"):
        validate_decisions(plan, converging, mapping, require_distinct=True)
    duplicated = tree.model_dump(mode="json")
    duplicated["nodes"]["gate"]["choices"][0]["effects"].append(
        {"var": "trust", "op": "inc", "value": 1}
    )
    with pytest.raises(ValueError):
        validate_choice_coverage(plan, quest, duplicated, mapping, choices)
    wrong_selector = tree.model_dump(mode="json")
    wrong_selector["nodes"]["gate"]["choices"][0]["effects"][0]["value"] = "refuse"
    with pytest.raises(ValueError, match="所映射分支"):
        validate_choice_coverage(plan, quest, wrong_selector, mapping, choices)
