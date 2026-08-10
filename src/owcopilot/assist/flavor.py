# 批量 flavor 文本生成（道具/技能/成就）。
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
from ..inspiration.grounding import (
    build_reference_report,
    inspiration_prompt_section,
    inspiration_refs_metadata,
    retrieve_drafting_references,
)
from ..inspiration.report import ReferenceReportItem
from ..inspiration.retrieval import ReferenceContextBuilder
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json
from ..retrieval.models import ContextPack
from ..util import unique_id
from .critic import FLAVOR_CRITIQUE_MARKER, CritiqueResult, FlavorCritic
from .industry import FLAVOR_RUBRIC_SOURCES, industry_source_block
from .lint import AssistLintIssue, lint_text
from .offline import _offline_quality_critique
from .refine import run_refine_loop
from .review_queue import ReviewItem, ReviewQueue

#: 素材检索预算：这条链路没有 canon 检索预算旋钮，取与任务草稿默认档一致的固定值
#: （N4.3，同构 grounding 契约）。
_INSPIRATION_BUDGET_TOKENS = 800

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
    # 整批的溯源（N4.3）：这批风味文本用到的素材 + 每条素材的确定性泄漏重算结论。素材开关关掉
    # （默认）时恒为空。
    inspiration_refs: list[str] = Field(default_factory=list)
    reference_report: list[ReferenceReportItem] = Field(default_factory=list)


class FlavorBatchService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
        critic: FlavorCritic | None = None,
        max_refine_rounds: int = 0,
        lessons: list[dict] | None = None,  # cross-session lesson archive (get_lessons_for_type)
        # 灵感库检索器（N4.3）。与 QuestDraft/CharacterProfile 同构：检索器是装配期基础设施，
        # "这次要不要吃素材"是每次调用时人自己的选择（``generate(use_references=...)``，默认关）。
        # None = 这个调用方还没接素材，开关怎么传都是关的。
        reference_context_builder: ReferenceContextBuilder | None = None,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue
        self.reference_context_builder = reference_context_builder
        # Opt-in critique→refine loop, identical pattern to characters/dialogue/barks: no critic =
        # the original single shot.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0
        # Lessons injected into BOTH the generation prompt and the critic prompt (same pattern as
        # the quest draft path). None when injection is disabled or the archive is empty → prompts
        # render byte-for-byte unchanged.
        self.lessons = lessons

    def generate(
        self,
        *,
        category: str,
        names: list[str],
        theme: str = "",
        max_chars: int = 120,
        use_references: bool = False,
    ) -> FlavorBatchResult:
        """Batch-generate flavor text. ``use_references`` additionally grounds it on the
        inspiration library (see
        ``src/owcopilot/inspiration/grounding.py``).
        默认关；库里没有允许用于草稿的素材时照常生成。"""
        if category not in FLAVOR_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(FLAVOR_CATEGORIES)}, got {category!r}"
            )
        cleaned = [name.strip() for name in names if name.strip()]
        if not cleaned:
            raise ValueError("at least one name is required")
        style = self.bundle.style_guides.get("style_guide")
        style_text = style.body[:600] if style is not None else ""
        # 检索查询：category + theme + names（契约 §三），整批只算一次。
        inspiration_query = " ".join(part for part in (category, theme, *cleaned) if part.strip())
        inspiration_pack = retrieve_drafting_references(
            self.reference_context_builder,
            inspiration_query,
            enabled=use_references,
            budget_tokens=_INSPIRATION_BUDGET_TOKENS,
        )
        entries = self._draft_entries(
            category, cleaned, theme, max_chars, style_text, inspiration_pack
        )
        refine_rounds = 0
        auto_incomplete = False
        if self.critic is not None:
            entries, refine_rounds, auto_incomplete = self._refine_entries(
                category, cleaned, theme, max_chars, style_text, entries, inspiration_pack
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
            inspiration_refs=inspiration_pack.refs,
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
                        # 空 dict when no material was recalled → metadata 与接线之前一致。
                        **inspiration_refs_metadata(inspiration_pack),
                    },
                    origin=Origin.AI_DRAFT,
                    review_status=ReviewStatus.PENDING_REVIEW,
                )
            )
        result.reference_report = build_reference_report(
            inspiration_pack,
            # entity.description 已经在 draft_prose 的 _iter_prose 里覆盖；flavor_text 住在
            # metadata 里，draft_prose 查不到，必须走 extra_prose。
            draft=ContentBundle(entities={entity.id: entity for entity in result.entities}),
            canon=self.bundle,
            human_input=f"{theme} {' '.join(cleaned)}".strip(),
            used_for="风味文本的灵感参考",
            extra_prose=[entry.flavor for entry in result.accepted if entry.flavor],
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
        inspiration_pack: ContextPack,
        *,
        feedback: list[str] | None = None,
    ) -> list[FlavorEntry]:
        user = f"Theme: {theme or '(none)'}\nNames: {', '.join(names)}"
        if feedback:
            user += "\n[REFINE] Address this reviewer feedback:\n" + "\n".join(feedback)
        # Lesson memory block after the rubric, before the category/style context. Empty when no
        # lessons → the system prompt is byte-for-byte unchanged from before this hook.
        lesson_section = ""
        if self.lessons:
            from .lessons import build_lesson_block

            lb = build_lesson_block(self.lessons, inject_lessons=True)
            lesson_section = f"{lb}\n" if lb else ""
        raw = self.gateway.complete(
            task="flavor_batch",
            system=(
                f"{_SYSTEM_PROMPT}\n{lesson_section}"
                f"Category: {category}. Character budget: {max_chars}.\n"
                f"Style guide: {style_text or '(none)'}"
                # 素材是独立的一段，接在 style guide 之后。没有素材时是空串，system prompt 与
                # 接线之前逐字节一致。
                + inspiration_prompt_section(inspiration_pack)
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
        inspiration_pack: ContextPack,
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
                # Same archive the generator sees; inject guard mirrors the quest draft path.
                lessons=self.lessons,
                inject_lessons=self.lessons is not None,
            )
            return problems, critique

        def regenerate(current: list[FlavorEntry], fixes: list[str]) -> list[FlavorEntry]:
            return self._draft_entries(
                category, names, theme, max_chars, style_text, inspiration_pack, feedback=fixes
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
