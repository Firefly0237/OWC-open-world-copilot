"""Input-budget and prompt-injection guardrails for World Bible ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import WorldBible


class WorldBibleSecurityError(ValueError):
    """Raised when a user-provided World Bible exceeds safety budgets."""


@dataclass(frozen=True)
class WorldBibleLimits:
    max_chars: int = 200_000
    max_entities: int = 500
    max_relations: int = 2_000
    max_field_chars: int = 2_000


_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all )?(previous|above) instructions",
        r"system prompt",
        r"developer message",
        r"tool call",
        r"execute (shell|command|code)",
        r"you are now",
        r"jailbreak",
    )
]


def validate_world_bible_text(text: str, limits: WorldBibleLimits) -> list[str]:
    """Validate raw markdown budget and return prompt-injection warnings."""
    if len(text) > limits.max_chars:
        raise WorldBibleSecurityError(
            f"world_bible_md is too large: {len(text)} chars > {limits.max_chars}"
        )
    warnings: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            warnings.append(f"prompt-injection-like text matched: {pattern.pattern}")
    return warnings


def validate_world_bible_model(wb: WorldBible, limits: WorldBibleLimits) -> None:
    """Validate parsed World Bible structure budgets."""
    if len(wb.entities) > limits.max_entities:
        raise WorldBibleSecurityError(
            f"world bible has too many entities: {len(wb.entities)} > {limits.max_entities}"
        )
    if len(wb.relations) > limits.max_relations:
        raise WorldBibleSecurityError(
            f"world bible has too many relations: {len(wb.relations)} > {limits.max_relations}"
        )
    for entity in wb.entities.values():
        fields = [entity.id, entity.name, entity.description, *entity.tags]
        too_long = [value for value in fields if len(value) > limits.max_field_chars]
        if too_long:
            raise WorldBibleSecurityError(
                f"entity {entity.id!r} has a field longer than {limits.max_field_chars} chars"
            )
    for relation in wb.relations:
        fields = [relation.source, relation.target, relation.kind]
        if any(len(value) > limits.max_field_chars for value in fields):
            raise WorldBibleSecurityError(
                f"relation {relation.source!r}->{relation.target!r} exceeds field length limit"
            )
