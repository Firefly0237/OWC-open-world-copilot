from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    add_relation_action,
    create_entity_action,
    remove_relation_action,
    set_object_position_action,
    update_dialogue_tree_action,
    update_quest_action,
)
from owcopilot.content.models import (
    POI,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
    Relation,
    Term,
)
from owcopilot.content.relation_kinds import is_symmetric_kind, relation_kind_catalog
from owcopilot.content.store import ContentStore
from owcopilot.graph.graph_view import build_graph_overview, build_graph_view


@pytest.fixture()
def root(tmp_path) -> str:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "fac_a": Entity(id="fac_a", name="宪章会", type=EntityType.FACTION),
                "npc_x": Entity(id="npc_x", name="白盐", type=EntityType.NPC),
            },
            pois={"loc_p": POI(id="loc_p", name="余烬矿", controlling_faction="fac_a")},
            quests={"q1": Quest(id="q1", title="任务一"), "q2": Quest(id="q2", title="任务二")},
            relations=[Relation(source="npc_x", target="fac_a", kind="member_of")],
            dialogue_trees={
                "dt": DialogueTree(
                    id="dt",
                    title="树",
                    root_node="n1",
                    nodes={
                        "n1": DialogueNode(
                            id="n1",
                            text="hi",
                            choices=[DialogueChoice(text="a", next_node="n2")],
                        ),
                        "n2": DialogueNode(id="n2", text="bye"),
                    },
                )
            },
        )
    )
    return str(content_root)


def test_create_entity_persists_with_typed_id(root: str) -> None:
    result = create_entity_action(root, name="灰渡", entity_type="npc", description="向导")
    new_id = result["entity"]["id"]
    assert new_id.startswith("npc_")
    bundle = ContentStore(root).load()
    assert bundle.entities[new_id].name == "灰渡"


def test_update_quest_sets_clears_order_and_filters_prereqs(root: str) -> None:
    update_quest_action(root, quest_id="q1", timeline_order=5, set_timeline_order=True)
    update_quest_action(
        root,
        quest_id="q2",
        timeline_order=8,
        set_timeline_order=True,
        prerequisites=["q1", "ghost"],
    )
    bundle = ContentStore(root).load()
    assert bundle.quests["q1"].timeline_order == 5
    assert bundle.quests["q2"].prerequisites == ["q1"]  # unknown prereq dropped

    update_quest_action(root, quest_id="q1", timeline_order=None, set_timeline_order=True)
    assert ContentStore(root).load().quests["q1"].timeline_order is None


def test_relations_dedupe_symmetric_flag_and_custom_kind(root: str) -> None:
    add_relation_action(root, source="fac_a", target="npc_x", kind="ally_of")
    add_relation_action(root, source="fac_a", target="npc_x", kind="ally_of")  # dedup
    add_relation_action(root, source="npc_x", target="loc_p", kind="守护")  # custom, directed
    bundle = ContentStore(root).load()

    ally = [r for r in bundle.relations if r.kind == "ally_of"]
    assert len(ally) == 1 and ally[0].metadata.get("symmetric") is True
    custom = [r for r in bundle.relations if r.kind == "守护"]
    assert len(custom) == 1 and not custom[0].metadata.get("symmetric")

    remove_relation_action(root, source="fac_a", target="npc_x", kind="ally_of")
    assert not [r for r in ContentStore(root).load().relations if r.kind == "ally_of"]


def test_dialogue_tree_edit_replaces_nodes_and_validates_root(root: str) -> None:
    update_dialogue_tree_action(
        root,
        tree_id="dt",
        nodes={
            "n1": {"id": "n1", "text": "改过的", "choices": [{"text": "走", "next_node": "n3"}]},
            "n3": {"id": "n3", "text": "新节点"},
        },
        root_node="n1",
    )
    tree = ContentStore(root).load().dialogue_trees["dt"]
    assert set(tree.nodes) == {"n1", "n3"}
    assert tree.nodes["n1"].text == "改过的"

    with pytest.raises(ValueError):
        update_dialogue_tree_action(root, tree_id="dt", root_node="missing")


def test_dialogue_tree_edit_rejects_malformed_nodes_as_valueerror(root: str) -> None:
    # a node whose `choices` is the wrong shape is bad input, not a server fault: it must surface
    # as a clean ValueError (→ 4xx), never a raw pydantic ValidationError (→ 500).
    with pytest.raises(ValueError):
        update_dialogue_tree_action(
            root,
            tree_id="dt",
            nodes={"n1": {"id": "n1", "text": "hi", "choices": "not-a-list"}},
        )


def test_dialogue_tree_edit_rejects_orphaned_root_when_nodes_replaced(root: str) -> None:
    # Regression: replacing the node map so the existing root id disappears, WITHOUT passing a new
    # root_node, must not silently persist a tree whose root points at a node that no longer exists.
    with pytest.raises(ValueError, match="根节点"):
        update_dialogue_tree_action(
            root,
            tree_id="dt",
            nodes={"n9": {"id": "n9", "text": "只剩这个节点，原根 n1 被删了"}},
        )
    # the rejected edit left the tree untouched
    tree = ContentStore(root).load().dialogue_trees["dt"]
    assert set(tree.nodes) == {"n1", "n2"} and tree.root_node == "n1"


def test_add_relation_allows_term_endpoint(tmp_path) -> None:
    # Regression: terms are real graph objects, so a relation to/from a term must be accepted, not
    # rejected with "both ends must already exist".
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            entities={"npc_x": Entity(id="npc_x", name="白盐", type=EntityType.NPC)},
            terms={"term_pact": Term(id="term_pact", canonical="炉心公约")},
        )
    )
    root = str(content_root)
    add_relation_action(root, source="npc_x", target="term_pact", kind="知晓")
    bundle = ContentStore(root).load()
    assert any(r.source == "npc_x" and r.target == "term_pact" for r in bundle.relations)


def test_position_persists_and_layout_uses_override(root: str) -> None:
    set_object_position_action(root, ref="entity:npc_x", x=123.4, y=56.7)
    bundle = ContentStore(root).load()
    assert bundle.entities["npc_x"].metadata["graph_pos"] == [123.4, 56.7]

    view = build_graph_view(bundle, focus_ref="entity:fac_a", radius=1)
    npc = next(n for n in view.nodes if n.ref == "entity:npc_x")
    assert (npc.x, npc.y) == (123.4, 56.7)


def test_overview_is_multi_hub_with_symmetric_edges(root: str) -> None:
    add_relation_action(root, source="fac_a", target="npc_x", kind="ally_of")
    overview = build_graph_overview(ContentStore(root).load())

    assert overview.overview is True
    refs = {n.ref for n in overview.nodes}
    assert {"entity:fac_a", "entity:npc_x", "poi:loc_p"} <= refs
    ally = [e for e in overview.edges if e.kind == "ally_of"]
    assert ally and ally[0].symmetric is True


def test_relation_catalog_covers_common_kinds() -> None:
    ids = {kind.id for kind in relation_kind_catalog()}
    assert {"ally_of", "enemy_of", "member_of", "kin_of", "borders"} <= ids
    assert is_symmetric_kind("ally_of") and not is_symmetric_kind("member_of")
