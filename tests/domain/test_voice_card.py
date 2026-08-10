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


# ── C4 voice fix tests (H1–H5) ──────────────────────────────────────────────


def test_build_voice_card_profile_voice_h1() -> None:
    """C4-H1: profile_voice equals entity.metadata["profile"]["voice"] (stripped)."""
    entity = Entity(
        id="npc_r1_a",
        name="测试角色",
        type=EntityType.NPC,
        metadata={"profile": {"voice": "言简意赅，惯用船民俚语，句末带'哩'"}},
    )
    card = build_voice_card(entity, ContentBundle())
    assert card.profile_voice == "言简意赅，惯用船民俚语，句末带'哩'"


def test_build_voice_card_no_profile_h2() -> None:
    """C4-H2: no "profile" key in metadata → profile_voice == "" (no KeyError)."""
    entity = Entity(id="e", name="N", type=EntityType.NPC, metadata={})
    card = build_voice_card(entity, ContentBundle())
    assert card.profile_voice == ""


def test_build_voice_card_profile_no_voice_h3() -> None:
    """C4-H3: "profile" present but no "voice" key → profile_voice == ""."""
    entity = Entity(
        id="e", name="N", type=EntityType.NPC, metadata={"profile": {"appearance": "高"}}
    )
    card = build_voice_card(entity, ContentBundle())
    assert card.profile_voice == ""


def test_build_voice_card_profile_voice_empty_h4() -> None:
    """C4-H4: profile.voice is "" or None → profile_voice == ""."""
    entity_empty = Entity(
        id="e", name="N", type=EntityType.NPC, metadata={"profile": {"voice": ""}}
    )
    entity_none = Entity(
        id="e2", name="N2", type=EntityType.NPC, metadata={"profile": {"voice": None}}
    )
    assert build_voice_card(entity_empty, ContentBundle()).profile_voice == ""
    assert build_voice_card(entity_none, ContentBundle()).profile_voice == ""


def test_build_voice_card_model_dump_json_contains_profile_voice_h5() -> None:
    """C4-H5: model_dump_json() includes "profile_voice" field (pydantic serialization)."""
    entity = Entity(
        id="npc_test",
        name="船民",
        type=EntityType.NPC,
        metadata={"profile": {"voice": "粗犷直接"}},
    )
    card = build_voice_card(entity, ContentBundle())
    dumped = card.model_dump_json()
    assert '"profile_voice"' in dumped
    assert "粗犷直接" in dumped


def test_build_voice_card_backward_compat_h6() -> None:
    """C4-H6: existing voice card construction (no profile arg) still works.

    profile_voice defaults to "".
    """
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Caravan master",
                tags=["merchant"],
                metadata={"tone": "dry"},
            )
        }
    )
    card = build_voice_card(bundle.entities["npc_aldric"], bundle)
    assert card.tone == "dry"
    assert card.profile_voice == ""
