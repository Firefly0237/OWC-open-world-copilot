from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.graph_rules import (
    DuplicateRelationRule,
    FactionConflictRule,
    MissingRelationEndpointRule,
    PrerequisiteCycleRule,
    RelationshipConflictRule,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation, SourceRef


def test_missing_relation_endpoint_rule_flags_unknown_relation_entities() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[Relation(source="npc_missing", target="faction_a", kind="member_of")]
        )
    )

    issues = list(MissingRelationEndpointRule().check(ctx))

    assert [issue.rule_code for issue in issues] == [
        "MISSING_RELATION_ENDPOINT",
        "MISSING_RELATION_ENDPOINT",
    ]


def test_duplicate_relation_rule_flags_duplicate_triples() -> None:
    relation = Relation(source="npc_a", target="faction_a", kind="member_of")
    ctx = AuditContext.from_bundle(ContentBundle(relations=[relation, relation]))

    issues = list(DuplicateRelationRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "DUPLICATE_RELATION"


def test_duplicate_relation_rule_allows_cross_source_repeated_facts() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[
                Relation(
                    source="faction_a",
                    target="faction_b",
                    kind="allied_with",
                    source_ref=SourceRef(path="style.md"),
                ),
                Relation(
                    source="faction_a",
                    target="faction_b",
                    kind="allied_with",
                    source_ref=SourceRef(path="relations.xlsx"),
                ),
            ]
        )
    )

    issues = list(DuplicateRelationRule().check(ctx))

    assert issues == []


def test_relationship_conflict_rule_flags_enemy_and_ally_pair() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            relations=[
                Relation(source="faction_a", target="faction_b", kind="allied_with"),
                Relation(source="faction_b", target="faction_a", kind="enemy_of"),
            ]
        )
    )

    issues = list(RelationshipConflictRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "RELATION_CONFLICT"


def test_prerequisite_cycle_rule_flags_quest_cycles() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "q1": Quest(id="q1", title="Q1", prerequisites=["q2"]),
                "q2": Quest(id="q2", title="Q2", prerequisites=["q1"]),
            }
        )
    )

    issues = list(PrerequisiteCycleRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "PREREQ_CYCLE"


def test_faction_conflict_rule_flags_enemy_controlled_location() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC),
                "loc_b": Entity(id="loc_b", name="B", type=EntityType.LOCATION),
                "faction_a": Entity(id="faction_a", name="A", type=EntityType.FACTION),
                "faction_b": Entity(id="faction_b", name="B", type=EntityType.FACTION),
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_a", location="loc_b")},
            relations=[
                Relation(source="npc_a", target="faction_a", kind="member_of"),
                Relation(source="loc_b", target="faction_b", kind="controlled_by"),
                Relation(source="faction_a", target="faction_b", kind="enemy_of"),
            ],
        )
    )

    issues = list(FactionConflictRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "FACTION_CONFLICT"


def test_faction_conflict_rule_does_not_flag_main_quests() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            entities={
                "npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC),
                "loc_b": Entity(id="loc_b", name="B", type=EntityType.LOCATION),
                "faction_a": Entity(id="faction_a", name="A", type=EntityType.FACTION),
                "faction_b": Entity(id="faction_b", name="B", type=EntityType.FACTION),
            },
            quests={
                "q_main": Quest(
                    id="q_main",
                    title="Main",
                    giver_npc="npc_a",
                    location="loc_b",
                    tags=["main"],
                ),
                "q_side": Quest(
                    id="q_side",
                    title="Side",
                    giver_npc="npc_a",
                    location="loc_b",
                    tags=["side"],
                ),
            },
            relations=[
                Relation(source="npc_a", target="faction_a", kind="member_of"),
                Relation(source="loc_b", target="faction_b", kind="controlled_by"),
                Relation(source="faction_a", target="faction_b", kind="enemy_of"),
            ],
        )
    )

    issues = list(FactionConflictRule().check(ctx))

    assert [issue.target_ref for issue in issues] == ["quest:q_side"]
