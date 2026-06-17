from __future__ import annotations

from owcopilot.content.models import POI, ContentBundle, Entity, EntityType, Relation
from owcopilot.graph.graph_view import build_graph_view


def _world() -> ContentBundle:
    return ContentBundle(
        entities={
            "fac_a": Entity(id="fac_a", name="宪章会", type=EntityType.FACTION),
            "fac_b": Entity(id="fac_b", name="自由港", type=EntityType.FACTION),
            "npc_x": Entity(id="npc_x", name="白盐", type=EntityType.NPC),
        },
        pois={"loc_p": POI(id="loc_p", name="余烬矿", controlling_faction="fac_a")},
        relations=[
            Relation(source="fac_a", target="fac_b", kind="enemy_of"),
            Relation(source="npc_x", target="fac_a", kind="member_of"),
        ],
    )


def test_ego_subgraph_collects_focus_and_typed_neighbours() -> None:
    view = build_graph_view(_world(), focus_ref="entity:fac_a", radius=1)

    refs = {n.ref for n in view.nodes}
    assert refs == {"entity:fac_a", "entity:fac_b", "entity:npc_x", "poi:loc_p"}

    kind_by_ref = {n.ref: n.kind for n in view.nodes}
    assert kind_by_ref["entity:fac_a"] == "faction"
    assert kind_by_ref["entity:npc_x"] == "npc"
    assert kind_by_ref["poi:loc_p"] == "poi"

    focus = next(n for n in view.nodes if n.focus)
    assert focus.ref == "entity:fac_a"
    assert (focus.x, focus.y) == (300.0, 220.0)  # focus sits at the layout centre


def test_edges_carry_relation_kinds_and_are_deterministic() -> None:
    view = build_graph_view(_world(), focus_ref="entity:fac_a", radius=1)

    edge_keys = {(e.source, e.target, e.kind) for e in view.edges}
    assert ("entity:fac_a", "entity:fac_b", "enemy_of") in edge_keys
    assert ("entity:npc_x", "entity:fac_a", "member_of") in edge_keys
    assert ("poi:loc_p", "entity:fac_a", "controlled_by") in edge_keys

    # deterministic layout -> identical coordinates on a rerun
    again = build_graph_view(_world(), focus_ref="entity:fac_a", radius=1)
    assert [(n.ref, n.x, n.y) for n in view.nodes] == [(n.ref, n.x, n.y) for n in again.nodes]


def test_impact_overlay_marks_direct_neighbours_must_change() -> None:
    view = build_graph_view(_world(), focus_ref="entity:fac_a", radius=1, impact=True)

    flags = {n.ref: n.flag for n in view.nodes}
    assert flags["entity:fac_b"] == "must_change"
    assert flags["entity:npc_x"] == "must_change"
    assert flags["poi:loc_p"] == "must_change"
    assert flags["entity:fac_a"] == ""  # the focus itself is not a ripple target


def test_missing_focus_returns_empty_view() -> None:
    view = build_graph_view(_world(), focus_ref="entity:nope", radius=1)
    assert view.nodes == []
    assert view.edges == []
