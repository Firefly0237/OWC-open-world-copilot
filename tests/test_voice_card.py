from __future__ import annotations

from owcopilot.assist.voice import build_voice_card
from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation


def test_build_voice_card_uses_entity_metadata_and_faction_relations() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Caravan master",
                tags=["merchant"],
                metadata={"tone": "dry", "taboo": ["royal secrets"]},
            )
        },
        relations=[Relation(source="npc_aldric", target="faction_merchants", kind="member_of")],
    )

    card = build_voice_card(bundle.entities["npc_aldric"], bundle)

    assert card.entity_id == "npc_aldric"
    assert card.faction_ids == ["faction_merchants"]
    assert card.tone == "dry"
    assert card.taboo == ["royal secrets"]
