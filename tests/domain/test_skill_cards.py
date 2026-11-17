"""Skill selection and maintenance contracts, using synthetic protocol evidence only."""

from __future__ import annotations

from copy import deepcopy

import pytest

from owcopilot.workflow.evolution_v2 import render_parameterized_guidance
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.skill_cards import SkillCards, SkillDefinition
from owcopilot.workflow.store import WorkflowStore


@pytest.fixture
def cards(tmp_path):
    return SkillCards(WorkflowStore(tmp_path / "skills.sqlite"))


def definition(
    *,
    name="Ask before disclosure",
    instruction="Keep the motivation implicit.",
    step_id="consider",
    slots=None,
    operation="draft_quest",
):
    return SkillDefinition.model_validate(
        {
            "name": name,
            "purpose": "A bounded scene-planning procedure",
            "steps": [
                {
                    "id": step_id,
                    "title": "Consider the current role",
                    "instruction": instruction,
                    "operation": operation,
                    "slots": slots or [],
                }
            ],
            "applies_to": ["A scene with a meaningful choice"],
            "does_not_apply_to": ["Changing established canon without permission"],
        }
    )


def candidate(cards, spec=None):
    card, created = cards.add_candidate(
        "local",
        spec or definition(),
        {
            "kind": "run",
            "run_id": "controlled-source",
            "execution_origin": "controlled_transport",
            "collection_condition": "synthetic_protocol",
            "author_acceptance": "unknown",
        },
    )
    assert created
    return card


def selection(card, version_id=None):
    version_id = version_id or card["current_version_id"]
    version = next(v for v in card["versions"] if v["id"] == version_id)
    return {
        "card_id": card["id"],
        "version_id": version_id,
        "version_hash": version["version_hash"],
    }


def test_candidate_requires_explicit_task_selection_and_never_becomes_default(cards):
    card = candidate(cards)
    assert card["active_version_id"] is None
    assert card["versions"][0]["status"] == "candidate"
    assert cards.resolve("local") == []
    graph, bindings = cards.bind_graph(
        "local",
        create_narrative_graph(),
        [selection(card)],
        include_defaults=False,
    )
    context = {"entities": [], "machine_requirements": {}}
    quest = next(node for node in graph.nodes if node.operation == "draft_quest")
    assert "Keep the motivation implicit." in render_parameterized_guidance(quest, context)
    assert bindings[0]["selection"] == "explicit_task"
    assert cards.detail("local", card["id"])["active_version_id"] is None
    assert cards.resolve("local") == []


def test_distinct_skills_with_same_slot_name_bind_their_own_current_parameters(cards):
    first = candidate(
        cards,
        definition(
            name="Focus speaker",
            instruction="Focus {{actor}}.",
            slots=[
                {
                    "name": "actor",
                    "kind": "entity",
                    "binding_key": "participant:0",
                    "source_value": "previous-speaker",
                }
            ],
        ),
    )
    second = candidate(
        cards,
        definition(
            name="Contrast listener",
            instruction="Contrast {{actor}}.",
            slots=[
                {
                    "name": "actor",
                    "kind": "entity",
                    "binding_key": "participant:1",
                    "source_value": "previous-listener",
                }
            ],
        ),
    )
    graph, _ = cards.bind_graph(
        "local",
        create_narrative_graph(),
        [selection(first), selection(second)],
        include_defaults=False,
    )
    node = next(node for node in graph.nodes if node.operation == "draft_quest")
    rendered = render_parameterized_guidance(
        node,
        {
            "entities": [{"id": "current-speaker"}, {"id": "current-listener"}],
            "machine_requirements": {"participant_ids": ["current-speaker", "current-listener"]},
        },
    )
    assert "Focus current-speaker." in rendered
    assert "Contrast current-listener." in rendered
    assert "previous-speaker" not in rendered and "previous-listener" not in rendered


def test_successor_rebinding_is_idempotent_and_deselection_removes_old_skill(cards):
    card = candidate(cards)
    first, _ = cards.bind_graph(
        "local", create_narrative_graph(), [selection(card)], include_defaults=False
    )
    second, _ = cards.bind_graph("local", first, [selection(card)], include_defaults=False)
    for original, rebound in zip(first.nodes, second.nodes, strict=True):
        assert rebound.guidance == original.guidance
        assert rebound.parameters.get("skill_entry_templates", []) == original.parameters.get(
            "skill_entry_templates", []
        )
    unselected, bindings = cards.bind_graph("local", second, [], include_defaults=False)
    assert bindings == []
    context = {"entities": [], "machine_requirements": {}}
    assert all(
        "Keep the motivation implicit." not in render_parameterized_guidance(node, context)
        for node in unselected.nodes
    )


def test_version_decisions_disable_and_rollback_are_cas_guarded(cards):
    card = candidate(cards)
    v1 = card["current_version_id"]
    old_hash = card["card_hash"]
    adopted = cards.decide("local", card["id"], old_hash, v1, make_default=True)
    with pytest.raises(ValueError, match="变化"):
        cards.enable("local", card["id"], old_hash, False)
    explicit_old = cards.resolve("local", [selection(adopted)], include_defaults=False)
    default_old = cards.resolve("local")
    updated, created = cards.add_candidate(
        "local",
        definition(instruction="Check the consequence before drafting."),
        {"kind": "run", "run_id": "controlled-update", "author_acceptance": "unknown"},
        card_id=card["id"],
        expected_card_hash=adopted["card_hash"],
        parent_version_id=v1,
    )
    assert created and updated["active_version_id"] == v1
    v2 = updated["current_version_id"]
    with pytest.raises(ValueError, match="明确采用"):
        cards.rollback("local", card["id"], updated["card_hash"], v2)
    changed_default = cards.decide("local", card["id"], updated["card_hash"], v2, make_default=True)
    cards.assert_bindings("local", explicit_old)
    with pytest.raises(ValueError, match="变化"):
        cards.assert_bindings("local", default_old)
    restored = cards.rollback("local", card["id"], changed_default["card_hash"], v1)
    assert restored["active_version_id"] == v1
    disabled = cards.enable("local", card["id"], restored["card_hash"], False)
    assert cards.resolve("local") == []
    with pytest.raises(ValueError, match="停用"):
        cards.resolve("local", [selection(disabled, v1)], include_defaults=False)
    with pytest.raises(ValueError, match="变化"):
        cards.assert_bindings("local", explicit_old)
    with pytest.raises(ValueError):
        cards.resolve("another-project", [selection(disabled, v1)], include_defaults=False)


def test_exact_duplicate_and_no_change_keep_one_version_and_preserve_unknown_quality(cards):
    spec = definition(
        instruction="Focus {{person}}.",
        slots=[
            {
                "name": "person",
                "kind": "entity",
                "binding_key": "participant:0",
                "source_value": "old",
            }
        ],
    )
    card = candidate(cards, spec)
    renamed_step = deepcopy(spec.model_dump(mode="json"))
    renamed_step["steps"][0]["id"] = "another_identifier"
    renamed_step["steps"][0]["slots"][0]["source_value"] = "different-source-instance"
    duplicate, created = cards.add_candidate(
        "local", renamed_step, {"kind": "run", "run_id": "two"}
    )
    assert not created and duplicate["id"] == card["id"]
    unchanged, created = cards.add_candidate(
        "local",
        renamed_step,
        {"kind": "run", "run_id": "three"},
        card_id=card["id"],
        expected_card_hash=duplicate["card_hash"],
        parent_version_id=card["current_version_id"],
    )
    assert not created and len(unchanged["versions"]) == 1
    bindings = cards.resolve("local", [selection(unchanged)], include_defaults=False)
    cards.record_outcome("local", "failed-protocol-run", bindings, "failed")
    cards.record_outcome("local", "failed-protocol-run", bindings, "failed")
    cards.record_outcome("local", "unknown-protocol-run", bindings, "interrupted")
    observed = cards.detail("local", card["id"])
    assert observed["observed_outcomes"] == 2
    assert observed["technical_failures"] == 1
    assert observed["author_quality"] == "unknown"
    assert observed["active_version_id"] is None


def test_skill_cannot_replace_machine_checks_or_claim_unavailable_stage(cards):
    with pytest.raises(ValueError, match="系统检查"):
        definition(operation="coverage_audit")
    card = candidate(cards, definition(operation="logic_contract"))
    with pytest.raises(ValueError, match="没有Skill需要"):
        cards.bind_graph(
            "local", create_narrative_graph("baseline"), [selection(card)], include_defaults=False
        )
