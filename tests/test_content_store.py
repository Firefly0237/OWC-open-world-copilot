from __future__ import annotations

from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    Relation,
    StyleGuide,
    Term,
)
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


def test_style_guides_round_trip_with_full_fidelity(tmp_path) -> None:
    # regression: the store used to write only the "style_guide" key's body to a .md file, silently
    # dropping `rules` (consumed by the lorebook export) and any guide keyed by a different id.
    store = ContentStore(tmp_path / "content")
    store.save(
        ContentBundle(
            style_guides={
                "style_guide": StyleGuide(
                    id="style_guide", body="市井气。", rules=["不用网络梗", "多用古语"]
                ),
                "sg_alt": StyleGuide(id="sg_alt", body="史诗庄重。", rules=["引经据典"]),
            }
        )
    )
    loaded = store.load()
    assert set(loaded.style_guides) == {"style_guide", "sg_alt"}  # custom-id guide not dropped
    assert loaded.style_guides["style_guide"].rules == ["不用网络梗", "多用古语"]  # rules survive
    assert loaded.style_guides["sg_alt"].body == "史诗庄重。"


def test_legacy_style_guide_md_still_loads(tmp_path) -> None:
    # a world saved before the JSON format must keep loading (body-only is acceptable for old data).
    world = tmp_path / "content" / "world"
    world.mkdir(parents=True)
    (world / "style_guide.md").write_text("旧世界的文风。", encoding="utf-8")
    loaded = ContentStore(tmp_path / "content").load()
    assert loaded.style_guides["style_guide"].body == "旧世界的文风。"
