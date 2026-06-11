from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.patches.apply import apply_patch_to_store, rollback_patch_in_store
from owcopilot.patches.models import PatchCandidate, PatchOp, PatchOperation


def test_rollback_patch_in_store_restores_previous_value(tmp_path) -> None:
    store = ContentStore(tmp_path / "content")
    store.save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Old",
                )
            }
        )
    )
    applied = apply_patch_to_store(
        store,
        PatchCandidate(
            ops=[
                PatchOperation(
                    op=PatchOp.REPLACE,
                    path="/entities/npc_aldric/description",
                    value="New",
                )
            ]
        ),
        applied_by="tester",
    )

    rollback_patch_in_store(store, applied.rollback_ops)

    assert store.load().entities["npc_aldric"].description == "Old"


def test_content_store_removes_stale_json_after_patch_remove(tmp_path) -> None:
    store = ContentStore(tmp_path / "content")
    store.save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)
            }
        )
    )

    apply_patch_to_store(
        store,
        PatchCandidate(ops=[PatchOperation(op=PatchOp.REMOVE, path="/entities/npc_aldric")]),
        applied_by="tester",
    )

    assert not (tmp_path / "content" / "world" / "entities" / "npc_aldric.json").exists()
    assert store.load().entities == {}


def test_rollback_patch_in_store_restores_list_append_path(tmp_path) -> None:
    store = ContentStore(tmp_path / "content")
    store.save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)
            }
        )
    )
    applied = apply_patch_to_store(
        store,
        PatchCandidate(
            ops=[
                PatchOperation(
                    op=PatchOp.ADD,
                    path="/entities/npc_aldric/tags/-",
                    value="new-tag",
                )
            ]
        ),
        applied_by="tester",
    )

    rollback_patch_in_store(store, applied.rollback_ops)

    assert store.load().entities["npc_aldric"].tags == []
