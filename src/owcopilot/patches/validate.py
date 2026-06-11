"""Patch candidate validation."""

from __future__ import annotations

from dataclasses import dataclass

from ..audit.context import AuditContext
from ..audit.runner import AuditRunner
from ..content.models import ContentBundle
from .models import PatchCandidate
from .shadow import apply_patch_shadow


@dataclass
class PatchValidation:
    candidate: PatchCandidate
    introduced_errors: list[str]
    resolved_errors: list[str]

    @property
    def valid(self) -> bool:
        return not self.introduced_errors


def validate_patch_candidate(
    bundle: ContentBundle, candidate: PatchCandidate, runner: AuditRunner
) -> PatchValidation:
    before = runner.run(AuditContext.from_bundle(bundle))
    patched = apply_patch_shadow(bundle, candidate.ops)
    after = runner.run(AuditContext.from_bundle(patched))
    before_errors = {issue.fingerprint or "" for issue in before.open_errors}
    after_errors = {issue.fingerprint or "" for issue in after.open_errors}
    return PatchValidation(
        candidate=candidate,
        introduced_errors=sorted(after_errors - before_errors),
        resolved_errors=sorted(before_errors - after_errors),
    )


def valid_patch_candidates(
    bundle: ContentBundle, candidates: list[PatchCandidate], runner: AuditRunner
) -> list[PatchValidation]:
    validations = [validate_patch_candidate(bundle, candidate, runner) for candidate in candidates]
    return [validation for validation in validations if validation.valid]
