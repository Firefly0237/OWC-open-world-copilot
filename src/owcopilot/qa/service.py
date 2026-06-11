"""Lore QA service."""

from __future__ import annotations

import json

from ..content.models import ContentBundle
from ..llm.gateway import LLMGateway
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack
from .models import QAAnswer
from .verify import verify_qa_answer


class LoreQAService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        context_builder: ContextPackBuilder,
        bundle: ContentBundle,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.bundle = bundle

    def ask(self, query: str, *, budget_tokens: int = 800) -> QAAnswer:
        pack = self.context_builder.build(query, budget_tokens=budget_tokens)
        if not pack.hits:
            return refusal_answer(query, unresolved=[query])

        raw = self.gateway.complete(
            task="qa_answer",
            system=_system_prompt(pack),
            user=query,
        )
        answer = parse_qa_answer(raw)
        if _looks_like_refusal(answer):
            return refusal_answer(query, unresolved=answer.unresolved_mentions or [query])
        verification = verify_qa_answer(answer, pack=pack, bundle=self.bundle)
        if not verification.valid:
            return refusal_answer(query, unresolved=verification.unresolved_mentions)
        return answer


def parse_qa_answer(raw: str) -> QAAnswer:
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.find("{") : text.rfind("}") + 1]
    payload = json.loads(text)
    if "answer" not in payload and "text" in payload:
        payload["answer"] = payload["text"]
    return QAAnswer.model_validate(payload)


def refusal_answer(query: str, *, unresolved: list[str] | None = None) -> QAAnswer:
    return QAAnswer(
        answer="No grounded lore answer is available for this question.",
        citations=[],
        confidence=0.0,
        mentioned_entities=[],
        unresolved_mentions=unresolved or [query],
        refused=True,
    )


def _system_prompt(pack: ContextPack) -> str:
    context_lines = [
        f"- [{hit.ref}] {hit.title}: {hit.body}".strip()
        for hit in pack.hits
    ]
    return (
        "Answer using only the cited lore context. Return one JSON object with keys: "
        "answer, citations, confidence, mentioned_entities, unresolved_mentions, refused. "
        "citations must be an array of objects like {\"ref\":\"entity:npc_id\"}; "
        "confidence must be a number from 0 to 1. "
        "Every citation ref must be one of the provided refs. "
        "If the context contains relation_conflict or both allied_with and enemy_of "
        "for the same pair, state that the data is conflicting instead of choosing one. "
        "If the answer is not grounded in the context, set refused=true, citations=[], "
        "confidence=0, and put missing concepts in unresolved_mentions.\n\n"
        "Lore context:\n"
        + "\n".join(context_lines)
    )


def _looks_like_refusal(answer: QAAnswer) -> bool:
    return answer.refused or (
        not answer.citations
        and answer.confidence <= 0
        and bool(answer.unresolved_mentions)
    )
