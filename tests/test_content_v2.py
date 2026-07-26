from __future__ import annotations

import json

from owcopilot.content.hash import content_hash
from owcopilot.content.importers.csv import CSVImporter
from owcopilot.content.importers.json import JSONImporter
from owcopilot.content.importers.markdown import parse_markdown
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Origin,
    Quest,
    QuestStage,
    Relation,
    ReviewStatus,
)
from owcopilot.content.normalize import normalize_raw_objects
from owcopilot.content.store import ContentStore


def test_content_bundle_carries_provenance_defaults() -> None:
    entity = Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)

    assert entity.origin is Origin.HUMAN
    assert entity.review_status is ReviewStatus.APPROVED

    bundle = ContentBundle()
    bundle.add_entity(entity)

    assert bundle.has_entity("npc_aldric")


def test_content_hash_is_stable_for_equivalent_payloads() -> None:
    left = {"b": [2, 1], "a": {"name": "Aldric"}}
    right = {"a": {"name": "Aldric"}, "b": [2, 1]}

    assert content_hash(left) == content_hash(right)


# --------------------------------------------------------------------------- relation order is an
# assembly artifact, not content: it must not reach content_hash (and ordered lists must still)


def _relations() -> list[Relation]:
    return [
        Relation(source="npc_a", target="fac_x", kind="member_of"),
        Relation(source="npc_a", target="loc_y", kind="located_in"),
        Relation(source="fac_x", target="fac_z", kind="enemy_of"),
    ]


def test_content_hash_ignores_relation_order() -> None:
    """Relations are a set (every dedup/removal site keys on the source/kind/target triple), so a
    world assembled in a different order is the SAME world and must hash identically. Before the
    canonical ordering this drifted, silently costing cache hits and drifting audit fingerprints."""
    rels = _relations()
    forward = ContentBundle(relations=list(rels))
    reversed_ = ContentBundle(relations=list(reversed(rels)))
    shuffled = ContentBundle(relations=[rels[1], rels[2], rels[0]])

    assert content_hash(forward) == content_hash(reversed_) == content_hash(shuffled)


def test_content_hash_ignores_order_of_same_triple_relations() -> None:
    """Two relations may share the identity triple and differ elsewhere (DUPLICATE_RELATION is a
    real audit finding). The tiebreaker must order them totally, or the primary key alone would
    leave the sort unstable and reintroduce the drift."""
    a = Relation(source="npc_a", target="fac_x", kind="member_of", metadata={"note": "alpha"})
    b = Relation(source="npc_a", target="fac_x", kind="member_of", metadata={"note": "beta"})

    assert content_hash(ContentBundle(relations=[a, b])) == content_hash(
        ContentBundle(relations=[b, a])
    )


def test_content_hash_still_distinguishes_relation_content() -> None:
    """Canonical ordering must not collapse genuinely different relation sets."""
    base = ContentBundle(relations=_relations())
    dropped = ContentBundle(relations=_relations()[:2])
    retargeted = ContentBundle(
        relations=[*_relations()[:2], Relation(source="fac_x", target="fac_q", kind="enemy_of")]
    )

    assert content_hash(base) != content_hash(dropped)
    assert content_hash(base) != content_hash(retargeted)


def test_content_hash_respects_quest_stage_order() -> None:
    """The red line: ``Quest.stages`` is authored sequence, not a set. Reordering stages IS a
    content change and must move the hash — proof the relation fix did not sort lists at large."""
    stages = [QuestStage(id="s1", summary="找到线索"), QuestStage(id="s2", summary="返回复命")]
    forward = ContentBundle(quests={"q": Quest(id="q", title="Q", stages=list(stages))})
    backward = ContentBundle(quests={"q": Quest(id="q", title="Q", stages=list(reversed(stages)))})

    assert content_hash(forward) != content_hash(backward)


def test_content_hash_respects_dialogue_choice_order() -> None:
    """Same red line for dialogue: choice order is what the player sees on screen."""
    choices = [
        DialogueChoice(text="接受委托", next_node="n_yes"),
        DialogueChoice(text="拒绝", next_node="n_no"),
    ]

    def tree(items: list[DialogueChoice]) -> ContentBundle:
        node = DialogueNode(id="root", text="要接吗？", choices=items)
        return ContentBundle(
            dialogue_trees={"t": DialogueTree(id="t", root_node="root", nodes={"root": node})}
        )

    assert content_hash(tree(choices)) != content_hash(tree(list(reversed(choices))))


def test_store_writes_relations_in_canonical_order(tmp_path) -> None:
    """Storage guarantee: the file records the relation set, not the order it was assembled in, so
    the same content saved from two orders is byte-identical on disk (clean git diffs) and reloads
    in a stable order (positional relation refs stay put across saves)."""
    rels = _relations()
    forward_root, reversed_root = tmp_path / "forward", tmp_path / "reversed"
    ContentStore(forward_root).save(ContentBundle(relations=list(rels)))
    ContentStore(reversed_root).save(ContentBundle(relations=list(reversed(rels))))

    jsonl = forward_root / "world" / "relations.jsonl"
    assert jsonl.read_bytes() == (reversed_root / "world" / "relations.jsonl").read_bytes()

    loaded = ContentStore(forward_root).load()
    assert [(r.source, r.kind, r.target) for r in loaded.relations] == [
        ("fac_x", "enemy_of", "fac_z"),
        ("npc_a", "located_in", "loc_y"),
        ("npc_a", "member_of", "fac_x"),
    ]
    assert content_hash(loaded) == content_hash(ContentBundle(relations=rels))


def test_markdown_importer_parses_world_bible_shape() -> None:
    raw = parse_markdown(
        """
## NPCs
- Aldric - Caravan master [merchant, quest_giver]
## Locations
- Northwatch - Fortified northern pass
## Relations
- Aldric -> Northwatch : located_in
""".strip()
    )

    bundle = normalize_raw_objects(raw)

    assert set(bundle.entities) == {"npc_aldric", "location_northwatch"}
    assert bundle.entities["npc_aldric"].source_ref is not None
    assert bundle.entities["npc_aldric"].tags == ["merchant", "quest_giver"]
    assert bundle.relations[0].source == "npc_aldric"
    assert bundle.relations[0].target == "location_northwatch"
    assert bundle.relations[0].kind == "located_in"


def test_json_importer_reads_native_object_list(tmp_path) -> None:
    path = tmp_path / "entities.json"
    path.write_text(
        json.dumps(
            [
                {
                    "kind": "entity",
                    "id": "npc_mara",
                    "name": "Mara",
                    "type": "npc",
                    "description": "Scout",
                }
            ]
        ),
        encoding="utf-8",
    )

    raw = JSONImporter().parse(path)
    bundle = normalize_raw_objects(raw)

    assert list(bundle.entities) == ["npc_mara"]
    assert bundle.entities["npc_mara"].description == "Scout"


def test_csv_importer_preserves_row_source_ref(tmp_path) -> None:
    path = tmp_path / "entities.csv"
    path.write_text(
        "kind,id,name,type,description,tags\n"
        'entity,npc_aldric,Aldric,npc,Caravan master,"merchant,quest_giver"\n',
        encoding="utf-8",
    )

    raw = CSVImporter().parse(path)
    bundle = normalize_raw_objects(raw)

    entity = bundle.entities["npc_aldric"]
    assert entity.tags == ["merchant", "quest_giver"]
    assert entity.source_ref is not None
    assert entity.source_ref.row == 2
