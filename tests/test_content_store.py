from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation, Term
from owcopilot.content.store import ContentStore


def test_content_store_saves_and_loads_bundle(tmp_path) -> None:
    store = ContentStore(tmp_path / "content")
    bundle = ContentBundle()
    bundle.entities["npc_aldric"] = Entity(
        id="npc_aldric",
        name="Aldric",
        type=EntityType.NPC,
        description="Caravan master",
    )
    bundle.relations.append(
        Relation(source="npc_aldric", target="location_northwatch", kind="located_in")
    )
    bundle.quests["quest_missing_caravan"] = Quest(
        id="quest_missing_caravan",
        title="The Missing Caravan",
        giver_npc="npc_aldric",
        location="location_northwatch",
    )
    bundle.terms["term_iron_vow"] = Term(id="term_iron_vow", canonical="Iron Vow")

    store.save(bundle)
    loaded = store.load()

    assert loaded.entities["npc_aldric"].description == "Caravan master"
    assert loaded.relations[0].kind == "located_in"
    assert loaded.quests["quest_missing_caravan"].title == "The Missing Caravan"
    assert loaded.terms["term_iron_vow"].canonical == "Iron Vow"


def test_content_store_loads_empty_directory(tmp_path) -> None:
    loaded = ContentStore(tmp_path / "missing").load()

    assert loaded == ContentBundle()
