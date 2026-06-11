"""Batch flavor-text generation for items / skills / achievements.

Same contract as every assist path: style-guide + term constrained prompts, deterministic
lint filtering, and the only exit is the review queue.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Entity, EntityType, Origin, ReviewStatus
from ..llm.gateway import LLMGateway
from .lint import AssistLintIssue, lint_text
from .review_queue import ReviewItem, ReviewQueue

FLAVOR_CATEGORIES: dict[str, EntityType] = {
    "item": EntityType.ITEM,
    "skill": EntityType.SKILL,
    "achievement": EntityType.ACHIEVEMENT,
}

_SYSTEM_PROMPT = (
    "You write flavor text for a game. Return ONE JSON object only: "
    '{"entries": [{"name": str, "description": str, "flavor": str}]}. '
    "description is functional (what it does / how it is earned); flavor is a short "
    "atmospheric line in the world's voice. One entry per requested name, same order. "
    "Respect the style guide and the character budget."
)


class FlavorEntry(BaseModel):
    name: str
    description: str
    flavor: str = ""


class RejectedFlavor(BaseModel):
    name: str
    text: str
    issues: list[AssistLintIssue]


class FlavorBatchResult(BaseModel):
    batch_id: str
    category: str
    accepted: list[FlavorEntry] = Field(default_factory=list)
    rejected: list[RejectedFlavor] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    review_item: ReviewItem | None = None


class FlavorBatchService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue

    def generate(
        self,
        *,
        category: str,
        names: list[str],
        theme: str = "",
        max_chars: int = 120,
    ) -> FlavorBatchResult:
        if category not in FLAVOR_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(FLAVOR_CATEGORIES)}, got {category!r}"
            )
        cleaned = [name.strip() for name in names if name.strip()]
        if not cleaned:
            raise ValueError("at least one name is required")
        style = self.bundle.style_guides.get("style_guide")
        style_text = style.body[:600] if style is not None else ""
        raw = self.gateway.complete(
            task="flavor_batch",
            system=(
                f"{_SYSTEM_PROMPT}\nCategory: {category}. Character budget: {max_chars}.\n"
                f"Style guide: {style_text or '(none)'}"
            ),
            user=f"Theme: {theme or '(none)'}\nNames: {', '.join(cleaned)}",
        )
        entries = parse_flavor_entries(raw, expected_names=cleaned)
        batch_id = (
            "flavor_"
            + hashlib.sha256(f"{category}|{theme}|{','.join(cleaned)}".encode()).hexdigest()[:10]
        )
        result = FlavorBatchResult(batch_id=batch_id, category=category)
        used_ids = set(self.bundle.entities)
        for entry in entries:
            combined = f"{entry.description} {entry.flavor}".strip()
            issues = lint_text(
                combined, bundle=self.bundle, max_chars=max_chars * 2, allowed_entity_ids=set()
            )
            issues.extend(
                lint_text(entry.flavor, bundle=self.bundle, max_chars=max_chars)
                if entry.flavor
                else []
            )
            if issues:
                result.rejected.append(
                    RejectedFlavor(name=entry.name, text=combined, issues=issues)
                )
                continue
            result.accepted.append(entry)
            entity_id = _unique_id(category, entry.name, used_ids)
            result.entities.append(
                Entity(
                    id=entity_id,
                    name=entry.name,
                    type=FLAVOR_CATEGORIES[category],
                    description=entry.description,
                    tags=[category],
                    metadata={
                        "flavor_text": entry.flavor,
                        "flavor_batch_id": batch_id,
                        "theme": theme,
                    },
                    origin=Origin.AI_DRAFT,
                    review_status=ReviewStatus.PENDING_REVIEW,
                )
            )
        if self.review_queue is not None and result.entities:
            result.review_item = self.review_queue.add_flavor_batch(
                {
                    "id": batch_id,
                    "category": category,
                    "theme": theme,
                    "entities": [
                        entity.model_dump(mode="json", exclude_none=True)
                        for entity in result.entities
                    ],
                }
            )
        return result


def parse_flavor_entries(raw: str, *, expected_names: list[str]) -> list[FlavorEntry]:
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.find("{") : text.rfind("}") + 1]
    payload = json.loads(text)
    entries_raw = payload.get("entries") if isinstance(payload, dict) else None
    entries: list[FlavorEntry] = []
    by_name: dict[str, dict[str, str]] = {}
    for item in entries_raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            by_name[name] = {
                "description": str(item.get("description") or "").strip(),
                "flavor": str(item.get("flavor") or item.get("flavor_text") or "").strip(),
            }
    for name in expected_names:
        slot = by_name.get(name) or {"description": "", "flavor": ""}
        entries.append(
            FlavorEntry(name=name, description=slot["description"], flavor=slot["flavor"])
        )
    return entries


class OfflineFlavorProvider:
    """Deterministic per-name entries so the batch pipeline is testable at $0."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        names_match = re.search(r"Names:\s*(.+)", user)
        theme_match = re.search(r"Theme:\s*(.+)", user)
        names = [
            part.strip()
            for part in (names_match.group(1) if names_match else "").split(",")
            if part.strip()
        ]
        theme = theme_match.group(1).strip() if theme_match else ""
        payload = {
            "entries": [
                {
                    "name": name,
                    "description": f"{name}：与{theme or '本世界'}相关的标准效果说明。",
                    "flavor": f"传闻{name}见证过一段被遗忘的往事。",
                }
                for name in names
            ]
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _unique_id(prefix: str, raw: str, used: set[str]) -> str:
    stem = _slug(raw)
    base = stem if stem.startswith(f"{prefix}_") else f"{prefix}_{stem or 'entry'}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _slug(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9㐀-鿿]+", "_", text)
    return text.strip("_")
