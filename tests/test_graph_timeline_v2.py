from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.graph.timeline import active_edges_at, is_active


def test_relation_is_active_uses_inclusive_bounds() -> None:
    assert is_active(None, None, 10)
    assert is_active(5, 10, 10)
    assert not is_active(11, None, 10)
    assert not is_active(None, 9, 10)


def test_active_edges_at_filters_timeline_relations() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
            "faction_old": Entity(id="faction_old", name="Old Guard", type=EntityType.FACTION),
            "faction_new": Entity(id="faction_new", name="New Guard", type=EntityType.FACTION),
        },
        relations=[
            Relation(
                source="npc_aldric",
                target="faction_old",
                kind="member_of",
                valid_until=4,
            ),
            Relation(
                source="npc_aldric",
                target="faction_new",
                kind="member_of",
                valid_from=5,
            ),
        ],
    )
    graph = build_content_graph(bundle)

    active = active_edges_at(graph, 5)

    assert [(edge.target, edge.kind) for edge in active] == [
        ("entity:faction_new", "member_of")
    ]
