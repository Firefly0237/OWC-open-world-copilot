from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.impact.analyzer import ImpactAnalyzer
from owcopilot.impact.models import Change, ChangeSet, ChangeType, ImpactLevel


def test_impact_analyzer_marks_direct_refs_as_must_change() -> None:
    graph = build_content_graph(
        ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)},
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_aldric")},
        )
    )

    result = ImpactAnalyzer(graph).analyze(
        ChangeSet(
            changes=[Change(change_type=ChangeType.ENTITY_RENAME, target_ref="entity:npc_aldric")]
        )
    )

    assert result.items[0].target_ref == "quest:q1"
    assert result.items[0].level is ImpactLevel.MUST_CHANGE


def test_impact_analyzer_marks_two_hop_refs_as_suggest_check() -> None:
    graph = build_content_graph(
        ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "faction_guard": Entity(
                    id="faction_guard",
                    name="Guard",
                    type=EntityType.FACTION,
                ),
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_aldric")},
            relations=[Relation(source="npc_aldric", target="faction_guard", kind="member_of")],
        )
    )

    result = ImpactAnalyzer(graph).analyze(
        ChangeSet(
            changes=[
                Change(
                    change_type=ChangeType.ENTITY_FIELD_CHANGE,
                    target_ref="entity:faction_guard",
                )
            ]
        )
    )

    levels = {item.target_ref: item.level for item in result.items}
    assert levels["entity:npc_aldric"] is ImpactLevel.MUST_CHANGE
    assert levels["quest:q1"] is ImpactLevel.SUGGEST_CHECK


def test_impact_analyzer_reaches_beyond_two_hops_when_depth_allows() -> None:
    # a chain a → b → c → d; the old hand-rolled distance helper capped at 2 hops and silently
    # dropped d. With max_depth=3 the BFS must reach it (a missed ripple is the worst failure mode).
    graph = build_content_graph(
        ContentBundle(
            entities={
                eid: Entity(id=eid, name=eid, type=EntityType.NPC) for eid in ("a", "b", "c", "d")
            },
            relations=[
                Relation(source="a", target="b", kind="knows"),
                Relation(source="b", target="c", kind="knows"),
                Relation(source="c", target="d", kind="knows"),
            ],
        )
    )
    result = ImpactAnalyzer(graph).analyze(
        ChangeSet(changes=[Change(change_type=ChangeType.ENTITY_DELETE, target_ref="entity:a")]),
        max_depth=3,
    )
    by_ref = {item.target_ref: item for item in result.items}
    assert by_ref["entity:b"].distance == 1 and by_ref["entity:b"].level is ImpactLevel.MUST_CHANGE
    assert by_ref["entity:c"].distance == 2
    assert by_ref["entity:d"].distance == 3  # the previously-dropped node
    assert by_ref["entity:d"].level is ImpactLevel.SUGGEST_CHECK


def test_impact_analyzer_catches_delete_ripple_through_stage_entities() -> None:
    # a quest that only touches an npc through a STAGE's required_entities (a derived reference, not
    # a relation) must still be flagged when that npc is deleted — recall over every edge type.
    from owcopilot.content.models import QuestStage

    graph = build_content_graph(
        ContentBundle(
            entities={"npc_seer": Entity(id="npc_seer", name="Seer", type=EntityType.NPC)},
            quests={
                "q_rite": Quest(
                    id="q_rite",
                    title="The Rite",
                    stages=[QuestStage(id="s1", summary="speak", required_entities=["npc_seer"])],
                )
            },
        )
    )
    result = ImpactAnalyzer(graph).analyze(
        ChangeSet(
            changes=[Change(change_type=ChangeType.ENTITY_DELETE, target_ref="entity:npc_seer")]
        )
    )
    by_ref = {item.target_ref: item.level for item in result.items}
    assert by_ref.get("quest:q_rite") is ImpactLevel.MUST_CHANGE


def test_impact_analyzer_ignores_unknown_change_target() -> None:
    graph = build_content_graph(ContentBundle())

    result = ImpactAnalyzer(graph).analyze(
        ChangeSet(
            changes=[Change(change_type=ChangeType.ENTITY_DELETE, target_ref="entity:missing")]
        )
    )

    assert result.items == []
