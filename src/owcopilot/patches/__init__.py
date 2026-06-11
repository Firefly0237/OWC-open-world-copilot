"""Patch suggestion, application and rollback package."""

from .apply import AppliedPatch, apply_patch_to_store, rollback_patch_in_store
from .fixers import bundle_pointer_for_ref, deterministic_candidates
from .models import PatchCandidate, PatchOp, PatchOperation, PatchStatus
from .parser import parse_patch_candidates
from .rollback import inverse_operations
from .shadow import apply_patch_shadow
from .suggest import PatchSuggestService, RankedCandidate, SuggestResult
from .validate import PatchValidation, valid_patch_candidates, validate_patch_candidate

__all__ = [
    "AppliedPatch",
    "PatchCandidate",
    "PatchOp",
    "PatchOperation",
    "PatchStatus",
    "PatchSuggestService",
    "PatchValidation",
    "RankedCandidate",
    "SuggestResult",
    "apply_patch_shadow",
    "apply_patch_to_store",
    "bundle_pointer_for_ref",
    "deterministic_candidates",
    "inverse_operations",
    "parse_patch_candidates",
    "rollback_patch_in_store",
    "valid_patch_candidates",
    "validate_patch_candidate",
]
