"""Apply and rollback patch candidates against a ContentStore."""

from __future__ import annotations

from dataclasses import dataclass

from ..content.store import ContentStore
from .models import PatchCandidate, PatchOperation, PatchStatus
from .rollback import inverse_operations
from .shadow import apply_patch_shadow


@dataclass
class AppliedPatch:
    candidate: PatchCandidate
    rollback_ops: list[PatchOperation]
    applied_by: str


def apply_patch_to_store(
    store: ContentStore, candidate: PatchCandidate, *, applied_by: str
) -> AppliedPatch:
    before = store.load()
    rollback_ops = inverse_operations(before, candidate.ops)
    after = apply_patch_shadow(before, candidate.ops)
    store.save(after)
    applied = candidate.model_copy(update={"status": PatchStatus.APPLIED})
    return AppliedPatch(candidate=applied, rollback_ops=rollback_ops, applied_by=applied_by)


def rollback_patch_in_store(store: ContentStore, rollback_ops: list[PatchOperation]) -> None:
    before = store.load()
    after = apply_patch_shadow(before, rollback_ops)
    store.save(after)
