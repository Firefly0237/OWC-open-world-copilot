from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.reference_rules import (
    DeprecatedEntityReferenceRule,
    MissingDialogueReferenceRule,
    MissingEntityReferenceRule,
    MissingPrerequisiteRule,
)
from owcopilot.content.models import (
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    Quest,
)


def test_missing_entity_reference_rule_flags_unknown_refs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "quest_missing": Quest(
                    id="quest_missing",
                    title="Missing",
                    giver_npc="npc_missing",
                    location="location_missing",
                )
            }
        )
    )

    issues = list(MissingEntityReferenceRule().check(ctx))

    assert [issue.rule_code for issue in issues] == ["UNKNOWN_ENTITY_REF", "UNKNOWN_ENTITY_REF"]
    assert {issue.evidence[0].path for issue in issues} == {"giver_npc", "location"}


def test_deprecated_entity_reference_rule_flags_deprecated_refs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_old": Entity(
                    id="npc_old",
                    name="Old NPC",
                    type=EntityType.NPC,
                    status="deprecated",
                )
            },
            quests={"quest_old": Quest(id="quest_old", title="Old", giver_npc="npc_old")},
        )
    )

    issues = list(DeprecatedEntityReferenceRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "DEPRECATED_ENTITY_REF"
    assert issues[0].target_ref == "quest:quest_old"


def test_missing_dialogue_reference_rule_flags_dangling_dialogue_refs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={"dlg_existing": DialogueRef(id="dlg_existing", text_key="existing")},
            quests={
                "quest_dialogue": Quest(
                    id="quest_dialogue",
                    title="Dialogue",
                    dialogue_refs=["dlg_missing"],
                )
            },
        )
    )

    issues = list(MissingDialogueReferenceRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "MISSING_DIALOGUE_REF"
    assert issues[0].evidence[0].path == "dialogue_refs.dlg_missing"


def test_missing_prerequisite_rule_flags_unknown_prereqs() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", prerequisites=["q_missing"])})
    )

    issues = list(MissingPrerequisiteRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "PREREQ_MISSING"
    assert issues[0].target_ref == "quest:q1"
