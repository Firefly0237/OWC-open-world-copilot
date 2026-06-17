"""Batch bark generation with deterministic filtering and review queue insertion."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Origin, ReviewStatus
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json
from .critic import BarkCritic, CritiqueResult
from .lint import AssistLintIssue, lint_text
from .refine import run_refine_loop
from .review_queue import ReviewItem, ReviewItemType, ReviewQueue
from .voice import VoiceCard, build_voice_card


class BarkVariant(BaseModel):
    speaker_id: str
    text: str
    origin: Origin = Origin.AI_DRAFT
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    voice_card: VoiceCard
    refine_rounds: int = 0
    auto_review_incomplete: bool = False


class RejectedBark(BaseModel):
    speaker_id: str
    text: str
    issues: list[AssistLintIssue]


class BarkBatchResult(BaseModel):
    accepted: list[BarkVariant] = Field(default_factory=list)
    rejected: list[RejectedBark] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)


class BarkBatchService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
        critic: BarkCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue
        # Opt-in critique→refine loop: without a critic this stays the original single shot. Adding
        # the loop to barks was just supplying assess/regenerate around the existing draft call.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

    def generate(
        self,
        *,
        speaker_ids: list[str],
        topic: str,
        variants_per_speaker: int,
        max_chars: int,
        allowed_entity_ids: set[str] | None = None,
    ) -> BarkBatchResult:
        result = BarkBatchResult()
        allowed = allowed_entity_ids or set(speaker_ids)
        for speaker_id in speaker_ids:
            entity = self.bundle.entities[speaker_id]
            voice_card = build_voice_card(entity, self.bundle)
            try:
                texts = self._draft_variants(voice_card, topic, variants_per_speaker, max_chars)
            except ValueError:
                # One speaker's unparseable reply must not lose the whole batch; skip it.
                continue
            refine_rounds = 0
            auto_incomplete = False
            if self.critic is not None:
                texts, refine_rounds, auto_incomplete = self._refine_variants(
                    voice_card, topic, variants_per_speaker, max_chars, allowed, texts
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
        return result

    def _draft_variants(
        self,
        voice_card: VoiceCard,
        topic: str,
        count: int,
        max_chars: int,
        *,
        feedback: list[str] | None = None,
    ) -> list[str]:
        user = f"Topic: {topic}\nVariants: {count}"
        if feedback:
            user += "\n[REFINE] Address this reviewer feedback:\n" + "\n".join(feedback)
        raw = self.gateway.complete(
            task="barks_batch",
            system=_system_prompt(voice_card, max_chars=max_chars),
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
            )
            return problems, critique

        def regenerate(current: list[str], fixes: list[str]) -> list[str]:
            try:
                return self._draft_variants(voice_card, topic, count, max_chars, feedback=fixes)
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


def parse_bark_texts(raw: str) -> list[str]:
    data = extract_json(raw)
    if isinstance(data, dict):
        data = data.get("variants", [])
    if not isinstance(data, list):
        raise ValueError("barks output must be a JSON list or {'variants': [...]}")
    return [str(item) for item in data]


def _system_prompt(voice_card: VoiceCard, *, max_chars: int) -> str:
    return (
        "Generate short NPC bark variants as JSON. Return {'variants': [text, ...]}. "
        f"Each variant must be <= {max_chars} characters. Stay within this voice card:\n"
        f"{voice_card.model_dump_json()}"
    )
