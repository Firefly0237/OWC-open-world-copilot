from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.lore_rules import (
    CharacterStateContradictionRule,
    EventResultReferencedTooEarlyRule,
    TimelineViolationRule,
)
from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    QuestEventReference,
    QuestEventRefKind,
)


def test_timeline_violation_rule_flags_late_prerequisite() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", timeline_order=1, prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", timeline_order=2),
            }
        )
    )

    issues = list(TimelineViolationRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "TIMELINE_VIOLATION"


def test_timeline_violation_rule_skips_prerequisite_cycles() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", timeline_order=1, prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", timeline_order=2, prerequisites=["q1"]),
            }
        )
    )

    issues = list(TimelineViolationRule().check(ctx))

    assert issues == []


def test_event_result_referenced_too_early_rule_flags_future_event_result() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "event_siege": Entity(
                    id="event_siege",
                    name="Siege",
                    type=EntityType.EVENT,
                    metadata={"timeline_order": 5},
                )
            },
            quests={
                "q1": Quest(
                    id="q1",
                    title="Q1",
                    timeline_order=3,
                    metadata={"references_event_results": ["event_siege"]},
                )
            },
        )
    )

    issues = list(EventResultReferencedTooEarlyRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "EVENT_RESULT_REFERENCED_TOO_EARLY"


def test_event_result_rule_uses_ref_kind_not_any_event_mention() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "event_siege": Entity(
                    id="event_siege",
                    name="Siege",
                    type=EntityType.EVENT,
                    metadata={"timeline_order": 5},
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", timeline_order=3)},
            quest_event_refs={
                "mention": QuestEventReference(
                    id="mention",
                    quest_id="q1",
                    event_id="event_siege",
                    ref_kind=QuestEventRefKind.MENTIONS_EVENT,
                ),
                "result": QuestEventReference(
                    id="result",
                    quest_id="q1",
                    event_id="event_siege",
                    ref_kind=QuestEventRefKind.REFERENCES_RESULT,
                ),
            },
        )
    )

    issues = list(EventResultReferencedTooEarlyRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].evidence[0].path == "quest_event_refs.result"


def test_character_state_contradiction_rule_flags_dead_active_entity() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_dead": Entity(
                    id="npc_dead",
                    name="Dead",
                    type=EntityType.NPC,
                    status="dead",
                    tags=["active"],
                )
            }
        )
    )

    issues = list(CharacterStateContradictionRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "CHARACTER_STATE_CONTRADICTION"
