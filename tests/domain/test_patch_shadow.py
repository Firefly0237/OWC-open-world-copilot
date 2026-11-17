from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.patches.models import PatchOp, PatchOperation
from owcopilot.patches.shadow import apply_patch_shadow


def test_apply_patch_shadow_replaces_without_mutating_original() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Old",
            )
        }
    )

    patched = apply_patch_shadow(
        bundle,
        [
            PatchOperation(
                op=PatchOp.REPLACE,
                path="/entities/npc_aldric/description",
                value="New",
            )
        ],
    )

    assert patched.entities["npc_aldric"].description == "New"
    assert bundle.entities["npc_aldric"].description == "Old"


def test_apply_patch_shadow_adds_and_removes_values() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                tags=["merchant"],
            )
        }
    )

    patched = apply_patch_shadow(
        bundle,
        [
            PatchOperation(op=PatchOp.ADD, path="/entities/npc_aldric/tags/-", value="scout"),
            PatchOperation(op=PatchOp.REMOVE, path="/entities/npc_aldric/tags/0"),
        ],
    )

    assert patched.entities["npc_aldric"].tags == ["scout"]
