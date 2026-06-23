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
from ..llm.jsonio import extract_json
from ..util import unique_id
from .critic import FLAVOR_CRITIQUE_MARKER, CritiqueResult, FlavorCritic
from .industry import FLAVOR_RUBRIC_SOURCES, industry_source_block
from .lint import AssistLintIssue, lint_text
from .offline import _offline_quality_critique
from .refine import run_refine_loop
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
    + "\n"
    + industry_source_block(*FLAVOR_RUBRIC_SOURCES)
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
    refine_rounds: int = 0
    auto_review_incomplete: bool = False


class FlavorBatchService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
        critic: FlavorCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue
        # Opt-in critique→refine loop, identical pattern to characters/dialogue/barks: no critic =
        # the original single shot.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

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
        entries = self._draft_entries(category, cleaned, theme, max_chars, style_text)
        refine_rounds = 0
        auto_incomplete = False
        if self.critic is not None:
            entries, refine_rounds, auto_incomplete = self._refine_entries(
                category, cleaned, theme, max_chars, style_text, entries
            )
        batch_id = (
            "flavor_"
            + hashlib.sha256(f"{category}|{theme}|{','.join(cleaned)}".encode()).hexdigest()[:10]
        )
        result = FlavorBatchResult(
            batch_id=batch_id,
            category=category,
            refine_rounds=refine_rounds,
            auto_review_incomplete=auto_incomplete,
        )
        used_ids = set(self.bundle.entities)
        for entry in entries:
            combined = f"{entry.description} {entry.flavor}".strip()
            if not combined:
                # The model produced nothing usable for this name (e.g. an unparseable reply);
                # surface it as rejected rather than queuing an empty entity for review.
                result.rejected.append(RejectedFlavor(name=entry.name, text="", issues=[]))
                continue
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

    def _draft_entries(
        self,
        category: str,
        names: list[str],
        theme: str,
        max_chars: int,
        style_text: str,
        *,
        feedback: list[str] | None = None,
    ) -> list[FlavorEntry]:
        user = f"Theme: {theme or '(none)'}\nNames: {', '.join(names)}"
        if feedback:
            user += "\n[REFINE] Address this reviewer feedback:\n" + "\n".join(feedback)
        raw = self.gateway.complete(
            task="flavor_batch",
            system=(
                f"{_SYSTEM_PROMPT}\nCategory: {category}. Character budget: {max_chars}.\n"
                f"Style guide: {style_text or '(none)'}"
            ),
            user=user,
        )
        return parse_flavor_entries(raw, expected_names=names)

    def _refine_entries(
        self,
        category: str,
        names: list[str],
        theme: str,
        max_chars: int,
        style_text: str,
        entries: list[FlavorEntry],
    ) -> tuple[list[FlavorEntry], int, bool]:
        assert self.critic is not None

        def assess(current: list[FlavorEntry]) -> tuple[list[str], CritiqueResult]:
            assert self.critic is not None
            problems = self._lint_problems(current, max_chars)
            critique = self.critic.critique(
                category=category,
                theme=theme,
                style_text=style_text,
                entries=[
                    {"name": e.name, "description": e.description, "flavor": e.flavor}
                    for e in current
                ],
                lint_problems=problems,
            )
            return problems, critique

        def regenerate(current: list[FlavorEntry], fixes: list[str]) -> list[FlavorEntry]:
            return self._draft_entries(
                category, names, theme, max_chars, style_text, feedback=fixes
            )

        outcome = run_refine_loop(
            initial=entries,
            max_rounds=self.max_refine_rounds,
            assess=assess,
            regenerate=regenerate,
        )
        return outcome.artifact, len(outcome.trail), outcome.auto_review_incomplete

    def _lint_problems(self, entries: list[FlavorEntry], max_chars: int) -> list[str]:
        problems: list[str] = []
        for entry in entries:
            combined = f"{entry.description} {entry.flavor}".strip()
            if not combined:
                problems.append(f"「{entry.name}」：未产出可用文本")
                continue
            issues = lint_text(
                combined, bundle=self.bundle, max_chars=max_chars * 2, allowed_entity_ids=set()
            )
            if entry.flavor:
                issues.extend(lint_text(entry.flavor, bundle=self.bundle, max_chars=max_chars))
            for issue in issues:
                problems.append(f"「{entry.name}」：{issue.message}")
        return problems


def parse_flavor_entries(raw: str, *, expected_names: list[str]) -> list[FlavorEntry]:
    try:
        data: object = extract_json(raw)
    except ValueError:
        # An unparseable reply must not crash the batch: every name falls through to an empty
        # entry, which generate() then surfaces as rejected (no silent empty content, no crash).
        data = {}
    # tolerate both {"entries": [...]} and a bare top-level array of entries
    if isinstance(data, dict):
        entries_raw = data.get("entries") or []
    elif isinstance(data, list):
        entries_raw = data
    else:
        entries_raw = []
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
    """Deterministic stand-in for the flavor generator AND its refine-loop critic: a critique
    request (marker in the system prompt) returns a verdict, otherwise per-name entries — so one
    provider drives the whole generate→critique→refine loop at $0."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if FLAVOR_CRITIQUE_MARKER in system:
            text = _offline_quality_critique(user)
            return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)
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
    return unique_id(prefix, raw, used, fallback="entry")
