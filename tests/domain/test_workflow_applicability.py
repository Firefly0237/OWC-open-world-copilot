"""Resource eligibility uses actual operator prerequisites, not prose or invented requirements."""

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity
from owcopilot.inspiration.models import ReferenceSource
from owcopilot.workflow.applicability import assess_applicability
from owcopilot.workflow.models import (
    TaskInput,
    TaskRecord,
    WorkflowInputSlot,
    WorkflowNode,
    WorkflowSpec,
)


def workflow(*, dialogue=False):
    nodes = [
        {"id": "events", "operation": "plan_events"},
        {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
        {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
        {"id": "summary", "operation": "package", "optional_dependencies": ["logic"]},
    ]
    if dialogue:
        nodes.append({"id": "dialogue", "operation": "draft_dialogue", "depends_on": ["events"]})
    return WorkflowSpec(name="Resource contract input", nodes=nodes)


def task(bundle=None, **request):
    bundle = bundle or ContentBundle()
    return TaskRecord(
        project="test",
        request=TaskInput(brief="Explicit resource contract input", **request),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )


def source(**changes):
    return ReferenceSource(
        **{
            "id": "selected",
            "title": "Explicit developer reference input",
            "text_hash": "reference-content-hash",
            "created_at": "2026-11-16T00:00:00Z",
            "provenance": "own_ip",
            "allowed_uses": ["drafting"],
            **changes,
        }
    )


def test_empty_project_and_no_selected_references_remain_compatible():
    spec = workflow()
    spec.applicability = ["Requires ten NPCs according to this untrusted prose"]
    for enabled in (False, True):
        assessment = assess_applicability(spec, task(use_references=enabled))
        assert assessment["compatible"] is True
        assert assessment["reasons"] == []
        assert assessment["scope"]["natural_language_applicability"] == "not_evaluated"
    assert (
        assess_applicability(spec.model_copy(update={"schema_version": 2}), task())["compatible"]
        is False
    )
    broken = workflow()
    broken.nodes[2].depends_on = ["events"]
    assert assess_applicability(broken, task())["compatible"] is False
    inconsistent = task().model_copy(update={"base_hash": "not-the-frozen-content"})
    assert assess_applicability(spec, inconsistent)["compatible"] is False


def test_dialogue_requires_one_real_npc_but_not_two_or_an_existing_quest():
    spec = workflow(dialogue=True)
    location = Entity(id="place", name="A place", type="location")
    unavailable = task(ContentBundle(entities={location.id: location}))
    rejected = assess_applicability(spec, unavailable)
    assert rejected["compatible"] is False
    assert any("真实NPC" in reason for reason in rejected["reasons"])
    npc = Entity(id="speaker", name="One speaker", type="npc")
    available = task(ContentBundle(entities={npc.id: npc}))
    assert assess_applicability(spec, available)["compatible"] is True
    assert available.base_bundle["quests"] == {}


def test_selected_sources_require_real_available_and_drafting_permitted_catalog():
    spec = workflow()
    current = task(use_references=True, reference_ids=["selected"])
    assert assess_applicability(spec, current)["compatible"] is False
    assert assess_applicability(spec, current, reference_sources=[])["compatible"] is False
    for unusable in (
        source(id="another"),
        source(allowed_uses=["inspiration"]),
        source(quarantine_status="quarantined"),
    ):
        assert (
            assess_applicability(spec, current, reference_sources=[unusable])["compatible"] is False
        )
    accepted = assess_applicability(spec, current, reference_sources=[source()])
    assert accepted["compatible"] is True
    assert any(item.get("text_hash") == "reference-content-hash" for item in accepted["checks"])


def test_disabled_reference_flag_only_requires_sources_for_consumed_reference_slot():
    current = task(use_references=False, reference_ids=["selected"])
    spec = workflow()
    spec.input_slots = [
        WorkflowInputSlot(
            name="chosen_sources", source="reference_ids", description="Optional source IDs"
        )
    ]
    assert assess_applicability(spec, current)["compatible"] is True
    spec.nodes[0].guidance = "Read the explicitly chosen sources: {{chosen_sources}}"
    assert assess_applicability(spec, current)["compatible"] is False
    assert assess_applicability(spec, current, reference_sources=[source()])["compatible"] is True
    assert assess_applicability(spec, task())["compatible"] is True
    spec.nodes[0].guidance = ""
    spec.nodes.append(
        WorkflowNode(id="audit", operation="audit", guidance="Only a note: {{chosen_sources}}")
    )
    assert assess_applicability(spec, current)["compatible"] is True
