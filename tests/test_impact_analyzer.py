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
            changes=[
                Change(change_type=ChangeType.ENTITY_RENAME, target_ref="entity:npc_aldric")
            ]
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


def test_impact_analyzer_ignores_unknown_change_target() -> None:
    graph = build_content_graph(ContentBundle())

    result = ImpactAnalyzer(graph).analyze(
        ChangeSet(
            changes=[
                Change(change_type=ChangeType.ENTITY_DELETE, target_ref="entity:missing")
            ]
        )
    )

    assert result.items == []
