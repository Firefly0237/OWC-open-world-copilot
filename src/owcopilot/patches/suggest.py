"""Patch suggestion: issue -> candidate patches -> shadow validation -> ranked proposals.

The discipline is the same as everywhere else in v2: deterministic fixers run first and are free;
the LLM (when a gateway is supplied) only adds candidates, never decides. Every candidate —
deterministic or model-made — is applied to a shadow copy and re-audited; anything that would
introduce a new open error is silently dropped before a human ever sees it.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.models import Issue, IssueStatus
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..llm.gateway import LLMGateway
from ..retrieval.context_pack import ContextPackBuilder
from .fixers import bundle_pointer_for_ref, deterministic_candidates
from .models import PatchCandidate
from .parser import parse_patch_candidates
from .shadow import apply_patch_shadow

_MAX_RAW_CANDIDATES = 8


class RankedCandidate(BaseModel):
    candidate: PatchCandidate
    target_resolved: bool
    resolved_errors: list[str] = Field(default_factory=list)
    source: str  # "deterministic" | "llm"


class SuggestResult(BaseModel):
    issue: Issue
    candidates: list[RankedCandidate] = Field(default_factory=list)
    rejected_count: int = 0
    parse_failed: bool = False
    used_llm: bool = False
    context_refs: list[str] = Field(default_factory=list)


class PatchSuggestService:
    def __init__(
        self,
        *,
        bundle: ContentBundle,
        audit_runner: AuditRunner,
        gateway: LLMGateway | None = None,
        context_builder: ContextPackBuilder | None = None,
    ) -> None:
        self.bundle = bundle
        self.audit_runner = audit_runner
        self.gateway = gateway
        self.context_builder = context_builder

    def suggest(
        self,
        issue: Issue,
        *,
        max_candidates: int = 3,
        budget_tokens: int = 600,
    ) -> SuggestResult:
        result = SuggestResult(issue=issue)
        raw: list[tuple[PatchCandidate, str]] = [
            (candidate, "deterministic")
            for candidate in deterministic_candidates(issue, self.bundle)
        ]
        if self.gateway is not None:
            result.used_llm = True
            llm_candidates, context_refs, parse_failed = self._llm_candidates(
                issue, budget_tokens=budget_tokens
            )
            result.context_refs = context_refs
            result.parse_failed = parse_failed
            raw.extend((candidate, "llm") for candidate in llm_candidates)

        before = self.audit_runner.run(AuditContext.from_bundle(self.bundle))
        before_errors = {issue_fingerprint(item) for item in before.open_errors}
        target_fingerprint = issue_fingerprint(issue)

        ranked: list[RankedCandidate] = []
        for candidate, source in raw[:_MAX_RAW_CANDIDATES]:
            prepared = self._prepare(candidate, issue)
            try:
                patched = apply_patch_shadow(self.bundle, prepared.ops)
            except Exception:
                result.rejected_count += 1
                continue
            after = self.audit_runner.run(AuditContext.from_bundle(patched))
            after_errors = {issue_fingerprint(item) for item in after.open_errors}
            if after_errors - before_errors:
                result.rejected_count += 1
                continue
            open_fingerprints = {
                issue_fingerprint(item)
                for item in after.issues
                if item.status is IssueStatus.OPEN
            }
            ranked.append(
                RankedCandidate(
                    candidate=prepared,
                    target_resolved=target_fingerprint not in open_fingerprints,
                    resolved_errors=sorted(before_errors - after_errors),
                    source=source,
                )
            )

        ranked.sort(
            key=lambda item: (
                not item.target_resolved,
                -len(item.resolved_errors),
                len(item.candidate.ops),
                item.candidate.id or "",
            )
        )
        result.candidates = _dedupe(ranked)[:max_candidates]
        return result

    def _llm_candidates(
        self, issue: Issue, *, budget_tokens: int
    ) -> tuple[list[PatchCandidate], list[str], bool]:
        context_refs: list[str] = []
        context_lines: list[str] = []
        if self.context_builder is not None:
            pack = self.context_builder.build(
                f"{issue.target_ref} {issue.message}", budget_tokens=budget_tokens
            )
            context_refs = pack.refs
            context_lines = [
                f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits
            ]
        raw = self.gateway.complete(  # type: ignore[union-attr]  # guarded by caller
            task="patch_suggest",
            system=_system_prompt(issue, self.bundle, context_lines),
            user=json.dumps(
                {
                    "issue": issue.model_dump(mode="json", exclude_none=True),
                    "target_object": _target_object(self.bundle, issue.target_ref),
                    "target_pointer": bundle_pointer_for_ref(issue.target_ref),
                },
                ensure_ascii=False,
            ),
        )
        try:
            return parse_patch_candidates(raw), context_refs, False
        except (ValueError, ValidationError, json.JSONDecodeError):
            return [], context_refs, True

    def _prepare(self, candidate: PatchCandidate, issue: Issue) -> PatchCandidate:
        identifier = candidate.id or "patch_" + content_hash(
            {
                "issue": issue_fingerprint(issue),
                "ops": [op.model_dump(mode="json") for op in candidate.ops],
            }
        )[:16]
        return candidate.model_copy(
            update={"id": identifier, "issue_id": issue.id or issue_fingerprint(issue)}
        )


def _dedupe(ranked: list[RankedCandidate]) -> list[RankedCandidate]:
    seen: set[str] = set()
    unique: list[RankedCandidate] = []
    for item in ranked:
        key = content_hash([op.model_dump(mode="json") for op in item.candidate.ops])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _target_object(bundle: ContentBundle, target_ref: str) -> dict | list | None:
    pointer = bundle_pointer_for_ref(target_ref)
    if pointer is None:
        return None
    document = bundle.model_dump(mode="json")
    current: object = document
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            index = int(key)
            if index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
        else:
            return None
    return current if isinstance(current, dict | list) else None


def _system_prompt(issue: Issue, bundle: ContentBundle, context_lines: list[str]) -> str:
    collections = ", ".join(
        f"{name}({len(getattr(bundle, name))})"
        for name in (
            "entities",
            "quests",
            "regions",
            "pois",
            "dialogues",
            "localized_texts",
            "terms",
        )
    )
    context_block = ("\n\nRelated content context:\n" + "\n".join(context_lines)) if (
        context_lines
    ) else ""
    return (
        "You repair structured game-content data. Given one audit issue and its target object, "
        "return ONE JSON object: {\"candidates\": [{\"ops\": [...], \"rationale\": \"...\"}]} "
        "with 1-3 candidates. Each op is a JSON Patch operation: "
        "{\"op\": \"replace\"|\"add\"|\"remove\", \"path\": \"/<collection>/<id>/<field>\", "
        "\"value\": ...}. Paths are absolute JSON pointers into the content bundle document "
        f"whose top-level collections are: {collections}, relations(array, indexed by number). "
        "Rules: only reference ids that exist in the provided context; never invent new ids; "
        "prefer the smallest edit that resolves the issue; do not change unrelated fields; "
        "output JSON only, no prose." + context_block
    )
