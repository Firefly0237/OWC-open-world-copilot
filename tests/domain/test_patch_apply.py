from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.patches.apply import apply_patch_to_store
from owcopilot.patches.models import PatchCandidate, PatchOp, PatchOperation, PatchStatus


def test_apply_patch_to_store_writes_bundle_and_returns_rollback_ops(tmp_path) -> None:
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
    candidate = PatchCandidate(
        ops=[
            PatchOperation(
                op=PatchOp.REPLACE,
                path="/entities/npc_aldric/description",
                value="New",
            )
        ]
    )

    applied = apply_patch_to_store(store, candidate, applied_by="tester")

    assert applied.candidate.status is PatchStatus.APPLIED
    assert applied.applied_by == "tester"
    assert applied.rollback_ops[0].value == "Old"
    assert store.load().entities["npc_aldric"].description == "New"
