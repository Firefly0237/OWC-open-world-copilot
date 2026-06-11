"""Batch bark generation with deterministic filtering and review queue insertion."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Origin, ReviewStatus
from ..llm.gateway import LLMGateway
from .lint import AssistLintIssue, lint_text
from .review_queue import ReviewItem, ReviewItemType, ReviewQueue
from .voice import VoiceCard, build_voice_card


class BarkVariant(BaseModel):
    speaker_id: str
    text: str
    origin: Origin = Origin.AI_DRAFT
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    voice_card: VoiceCard


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
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue

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
            raw = self.gateway.complete(
                task="barks_batch",
                system=_system_prompt(voice_card, max_chars=max_chars),
                user=f"Topic: {topic}\nVariants: {variants_per_speaker}",
            )
            for text in parse_bark_texts(raw)[:variants_per_speaker]:
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
                variant = BarkVariant(speaker_id=speaker_id, text=text, voice_card=voice_card)
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


def parse_bark_texts(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
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
