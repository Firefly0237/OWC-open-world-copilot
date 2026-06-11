"""Deterministic assist lint checks."""

from __future__ import annotations

from pydantic import BaseModel

from ..content.models import ContentBundle


class AssistLintIssue(BaseModel):
    code: str
    message: str
    target: str | None = None


def lint_text(
    text: str,
    *,
    bundle: ContentBundle,
    max_chars: int,
    allowed_entity_ids: set[str] | None = None,
) -> list[AssistLintIssue]:
    issues: list[AssistLintIssue] = []
    if len(text) > max_chars:
        issues.append(
            AssistLintIssue(
                code="TEXT_TOO_LONG_FOR_UI",
                message=f"Text length {len(text)} exceeds limit {max_chars}",
            )
        )
    issues.extend(_term_issues(text, bundle))
    if allowed_entity_ids is not None:
        issues.extend(_entity_reference_issues(text, bundle, allowed_entity_ids))
    return issues


def _term_issues(text: str, bundle: ContentBundle) -> list[AssistLintIssue]:
    lowered = text.lower()
    issues: list[AssistLintIssue] = []
    for term in bundle.terms.values():
        for forbidden in term.forbidden:
            if forbidden.lower() in lowered:
                issues.append(
                    AssistLintIssue(
                        code="TERM_INCONSISTENT",
                        message=(
                            f"Forbidden term '{forbidden}' used; use canonical '{term.canonical}'"
                        ),
                        target=term.id,
                    )
                )
    return issues


def _entity_reference_issues(
    text: str, bundle: ContentBundle, allowed_entity_ids: set[str]
) -> list[AssistLintIssue]:
    lowered = text.lower()
    issues: list[AssistLintIssue] = []
    for entity in bundle.entities.values():
        if entity.id in allowed_entity_ids:
            continue
        names = [entity.name, *entity.aliases]
        if any(name and name.lower() in lowered for name in names):
            issues.append(
                AssistLintIssue(
                    code="FORBIDDEN_ENTITY_REF",
                    message=f"Text references entity '{entity.id}' outside allowed context",
                    target=entity.id,
                )
            )
    return issues
