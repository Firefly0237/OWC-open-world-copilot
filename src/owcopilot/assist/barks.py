# 批量 bark 生成：确定性过滤 + 进审阅队列。
"""Batch bark generation with deterministic filtering and review queue insertion."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Origin, ReviewStatus
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
from .critic import BarkCritic, CritiqueResult
from .industry import BARK_RUBRIC_SOURCES, industry_source_block
from .lint import AssistLintIssue, lint_text
from .refine import run_refine_loop
from .review_queue import ReviewItem, ReviewItemType, ReviewQueue
from .voice import VoiceCard, build_voice_card

#: 素材检索预算：这条链路没有 canon 检索预算旋钮，取与任务草稿默认档一致的固定值
#: （N4.3，同构 grounding 契约）。
_INSPIRATION_BUDGET_TOKENS = 800


class BarkVariant(BaseModel):
    speaker_id: str
    text: str
    origin: Origin = Origin.AI_DRAFT
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    voice_card: VoiceCard
    refine_rounds: int = 0
    auto_review_incomplete: bool = False
    # 灵感库那一侧的溯源（N4.3）：素材开关关掉（默认）时恒为空 dict，产物与接线之前逐键一致。
    # BarkVariant 不是 content 模型（不进 ContentBundle），键名仍与 worldgen 一致
    # （``inspiration_refs``，逗号连接的字符串）以便同一套悬空引用检查覆盖它。
    metadata: dict[str, str] = Field(default_factory=dict)


class RejectedBark(BaseModel):
    speaker_id: str
    text: str
    issues: list[AssistLintIssue]


class BarkBatchResult(BaseModel):
    accepted: list[BarkVariant] = Field(default_factory=list)
    rejected: list[RejectedBark] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    # 整批的溯源：这批 bark 用到的素材 + 每条素材的确定性泄漏重算结论（同一个 pack，批内每条
    # accepted 变体的 metadata 都引用它）。素材开关关掉时恒为空。
    inspiration_refs: list[str] = Field(default_factory=list)
    reference_report: list[ReferenceReportItem] = Field(default_factory=list)


class BarkBatchService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
        critic: BarkCritic | None = None,
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
        # Opt-in critique→refine loop: without a critic this stays the original single shot. Adding
        # the loop to barks was just supplying assess/regenerate around the existing draft call.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0
        # Lessons fetched from the archive by the caller and injected into BOTH the generation
        # prompt and the critic prompt (the same pattern the quest draft path uses). None when no
        # lessons were fetched (injection disabled or empty archive) → the prompts render unchanged.
        self.lessons = lessons

    def generate(
        self,
        *,
        speaker_ids: list[str],
        topic: str,
        variants_per_speaker: int,
        max_chars: int,
        allowed_entity_ids: set[str] | None = None,
        use_references: bool = False,
    ) -> BarkBatchResult:
        """Batch-generate barks. ``use_references`` additionally grounds them on the inspiration
        library (see ``src/owcopilot/inspiration/grounding.py``).
        默认关；库里没有允许用于草稿的素材时照常生成。检索只做一次（topic + 全部 speaker
        名字），整批共用同一份素材。"""
        result = BarkBatchResult()
        allowed = allowed_entity_ids or set(speaker_ids)
        speaker_names = [
            self.bundle.entities[sid].name
            for sid in speaker_ids
            if sid in self.bundle.entities and self.bundle.entities[sid].name.strip()
        ]
        inspiration_query = " ".join(part for part in (topic, *speaker_names) if part.strip())
        inspiration_pack = retrieve_drafting_references(
            self.reference_context_builder,
            inspiration_query,
            enabled=use_references,
            budget_tokens=_INSPIRATION_BUDGET_TOKENS,
        )
        inspiration_metadata = inspiration_refs_metadata(inspiration_pack)
        for speaker_id in speaker_ids:
            entity = self.bundle.entities[speaker_id]
            voice_card = build_voice_card(entity, self.bundle)
            try:
                texts = self._draft_variants(
                    voice_card, topic, variants_per_speaker, max_chars, inspiration_pack
                )
            except ValueError:
                # One speaker's unparseable reply must not lose the whole batch; skip it.
                continue
            refine_rounds = 0
            auto_incomplete = False
            if self.critic is not None:
                texts, refine_rounds, auto_incomplete = self._refine_variants(
                    voice_card,
                    topic,
                    variants_per_speaker,
                    max_chars,
                    allowed,
                    texts,
                    inspiration_pack,
                )
            for text in texts:
                issues = lint_text(
                    text,
                    bundle=self.bundle,
                    max_chars=max_chars,
                    allowed_entity_ids=allowed,
                )
                if issues:
                    result.rejected.append(
                        RejectedBark(speaker_id=speaker_id, text=text, issues=issues)
                    )
                    continue
                variant = BarkVariant(
                    speaker_id=speaker_id,
                    text=text,
                    voice_card=voice_card,
                    refine_rounds=refine_rounds,
                    auto_review_incomplete=auto_incomplete,
                    metadata=inspiration_metadata,
                )
                result.accepted.append(variant)
                if self.review_queue is not None:
                    result.review_items.append(
                        self.review_queue.add(
                            ReviewItem(
                                item_type=ReviewItemType.BARK_VARIANT,
                                object_ref=f"bark:{speaker_id}:{len(result.accepted)}",
                                payload=variant.model_dump(mode="json"),
                            )
                        )
                    )
        result.inspiration_refs = inspiration_pack.refs
        result.reference_report = build_reference_report(
            inspiration_pack,
            # BarkVariant 不是 content 模型，draft_prose 覆盖不到它——bark 文本必须走 extra_prose。
            draft=ContentBundle(),
            canon=self.bundle,
            human_input=topic,
            used_for="bark 台词的灵感参考",
            extra_prose=[variant.text for variant in result.accepted],
        )
        return result

    def _draft_variants(
        self,
        voice_card: VoiceCard,
        topic: str,
        count: int,
        max_chars: int,
        inspiration_pack: ContextPack,
        *,
        feedback: list[str] | None = None,
    ) -> list[str]:
        user = f"Topic: {topic}\nVariants: {count}"
        if feedback:
            user += "\n[REFINE] Address this reviewer feedback:\n" + "\n".join(feedback)
        raw = self.gateway.complete(
            task="barks_batch",
            system=_system_prompt(
                voice_card,
                max_chars=max_chars,
                lessons=self.lessons,
                inspiration_pack=inspiration_pack,
            ),
            user=user,
        )
        return parse_bark_texts(raw)[:count]

    def _refine_variants(
        self,
        voice_card: VoiceCard,
        topic: str,
        count: int,
        max_chars: int,
        allowed: set[str],
        texts: list[str],
        inspiration_pack: ContextPack,
    ) -> tuple[list[str], int, bool]:
        assert self.critic is not None

        def assess(current: list[str]) -> tuple[list[str], CritiqueResult]:
            assert self.critic is not None
            problems = self._lint_problems(current, max_chars, allowed)
            critique = self.critic.critique(
                topic=topic,
                voice_card_json=voice_card.model_dump_json(),
                variants=current,
                lint_problems=problems,
                # Same archive the generator sees: a dimension humans keep rejecting makes the
                # critic stricter on it too. inject guard mirrors the quest draft path.
                lessons=self.lessons,
                inject_lessons=self.lessons is not None,
            )
            return problems, critique

        def regenerate(current: list[str], fixes: list[str]) -> list[str]:
            try:
                return self._draft_variants(
                    voice_card, topic, count, max_chars, inspiration_pack, feedback=fixes
                )
            except ValueError:
                return current  # keep the last good batch if a refine reply won't parse

        outcome = run_refine_loop(
            initial=texts,
            max_rounds=self.max_refine_rounds,
            assess=assess,
            regenerate=regenerate,
        )
        return outcome.artifact, len(outcome.trail), outcome.auto_review_incomplete

    def _lint_problems(self, texts: list[str], max_chars: int, allowed: set[str]) -> list[str]:
        problems: list[str] = []
        for text in texts:
            for issue in lint_text(
                text, bundle=self.bundle, max_chars=max_chars, allowed_entity_ids=allowed
            ):
                problems.append(f"「{text[:20]}」：{issue.message}")
        return problems


# Kept off complete_structured: the output is a list[str] accepted in two shapes (a bare array or
# {"variants": [...]}), not a single BaseModel.
def parse_bark_texts(raw: str) -> list[str]:
    data = extract_json(raw)
    if isinstance(data, dict):
        data = data.get("variants", [])
    if not isinstance(data, list):
        raise ValueError("barks output must be a JSON list or {'variants': [...]}")
    return [str(item) for item in data]


def _system_prompt(
    voice_card: VoiceCard,
    *,
    max_chars: int,
    lessons: list[dict] | None = None,
    inspiration_pack: ContextPack | None = None,
) -> str:
    # Lesson memory block: injected after the rubric, before the voice card (same relative slot as
    # the quest draft prompt). Empty string when lessons is None/empty → the prompt is byte-for-byte
    # unchanged from before this hook existed.
    lesson_section = ""
    if lessons:
        from .lessons import build_lesson_block

        lb = build_lesson_block(lessons, inject_lessons=True)
        lesson_section = f"{lb}\n" if lb else ""
    return (
        "Generate short NPC bark variants as JSON. Return {'variants': [text, ...]}. "
        f"Each variant must be <= {max_chars} characters. Stay within this voice card:\n"
        + industry_source_block(*BARK_RUBRIC_SOURCES)
        + "\n"
        + lesson_section
        + f"{voice_card.model_dump_json()}"
        # 素材是独立的一段，接在 voice card 之后（voice card 是这条链路里对应"正典事实"的那部
        # 分，与对白树的 voice cards 位置同构）。没有素材时是空串，system prompt 与接线之前逐字
        # 节一致。
        + (inspiration_prompt_section(inspiration_pack) if inspiration_pack is not None else "")
    )
