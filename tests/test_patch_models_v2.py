from __future__ import annotations

from owcopilot.content.models import Origin
from owcopilot.patches.models import PatchCandidate, PatchOp, PatchOperation, PatchStatus


def test_patch_candidate_defaults_to_ai_patch_proposed() -> None:
    candidate = PatchCandidate(
        ops=[PatchOperation(op=PatchOp.REPLACE, path="/entities/npc_aldric/description", value="x")]
    )

    assert candidate.origin is Origin.AI_PATCH
    assert candidate.status is PatchStatus.PROPOSED
    assert candidate.ops[0].op is PatchOp.REPLACE
