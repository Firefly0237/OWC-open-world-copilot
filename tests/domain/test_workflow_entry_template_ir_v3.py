"""Parameters are selected from frozen bindings and rendered by the host grammar."""

import pytest
from pydantic import ValidationError

from owcopilot.workflow.evolution import SourceRef
from owcopilot.workflow.evolution_v2 import (
    EntryTemplateIR,
    ReflectionDraftV3,
    TemplateText,
    compile_entry_templates,
    compile_reflection_draft,
    verify_grounding,
)
from owcopilot.workflow.graph_v2 import narrative_role_registry


def test_entry_parts_compile_slots_without_relaxing_grounding_or_role_contract():
    ref = SourceRef(kind="development_input", id="source", content_hash="a" * 64)
    snapshot = {
        "machine_requirements": {"participant_ids": ["baoyu", "daiyu"], "stage_count": 1},
        "entities": {"baoyu": {"name": "贾宝玉"}, "daiyu": {"name": "林黛玉"}},
    }
    raw = {
        "id": "participants",
        "operation": "draft_dialogue",
        "evidence_kind": "hypothesis",
        "parts": [
            {"kind": "text", "text": "只使用已确认参与者"},
            {"kind": "binding", "binding_kind": "entity", "binding_key": "participant:0"},
            {"kind": "text", "text": "和"},
            {"kind": "binding", "binding_kind": "entity", "binding_key": "participant:1"},
            {"kind": "text", "text": "；阶段数按当前合同"},
            {"kind": "binding", "binding_kind": "requirement", "binding_key": "stage_count"},
        ],
        "support_refs": [ref.model_dump(mode="json")],
    }
    entry = EntryTemplateIR.model_validate(raw)
    report, receipt = compile_reflection_draft(
        ReflectionDraftV3(
            problem_id="fixed-problem",
            target_description="Participant handoff",
            diagnosis="A hypothesis",
            evidence_refs=[ref],
            proposed_change="guidance",
            reusable_entries=[entry],
        ),
        snapshot,
        narrative_role_registry(),
    )
    compiled = report.reusable_entries[0]
    assert compiled.slots[0].name == "entity_participant_0"
    assert "{{entity_participant_0}}" in compiled.text and "baoyu" not in compiled.text
    assert compiled.slots[0].source_value == "baoyu"
    assert receipt["semantic_repair"] == "none" and len(receipt["derivations"]) == 3
    verify_grounding(
        report,
        None,
        [ref],
        set(snapshot["entities"]),
        snapshot["machine_requirements"],
        author_feedback=False,
    )
    with pytest.raises(ValidationError, match="手写占位符"):
        TemplateText(text="Use {{slot}}")
    with pytest.raises(ValueError, match="单个已注册角色"):
        compile_entry_templates(
            [entry.model_copy(update={"operation": "draft_logic / draft_dialogue"})],
            snapshot,
            narrative_role_registry(),
        )
    unknown = EntryTemplateIR.model_validate(
        {
            **raw,
            "parts": [
                {"kind": "binding", "binding_kind": "entity", "binding_key": "participant:9"},
            ],
        }
    )
    with pytest.raises(ValueError, match="冻结表之外"):
        compile_entry_templates([unknown], snapshot, narrative_role_registry())
