"""Lore QA service."""

from __future__ import annotations

from pydantic import ValidationError

from ..content.models import ContentBundle
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json, extract_json_object
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack
from .models import QAAnswer
from .verify import verify_qa_answer

# Sentinel marking a query-expansion call so the deterministic offline provider returns no
# variants (a no-op); real models use it to widen recall for phrasing-sensitive questions.
QA_EXPAND_MARKER = "[[QA_EXPAND]]"
_EXPAND_SYSTEM = (
    "Rewrite the user's lore question into 3 alternative search queries that a knowledge base "
    "might phrase differently (synonyms, the relationship form, the entity-centric form). Return "
    "ONLY a JSON array of 3 short strings, no prose. " + QA_EXPAND_MARKER
)


class LoreQAService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        context_builder: ContextPackBuilder,
        bundle: ContentBundle,
        expand: bool = False,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.bundle = bundle
        self.expand = expand

    def ask(self, query: str, *, budget_tokens: int = 800) -> QAAnswer:
        variants = self._expand_query(query) if self.expand else []
        pack = (
            self.context_builder.build_expanded(query, variants, budget_tokens=budget_tokens)
            if variants
            else self.context_builder.build(query, budget_tokens=budget_tokens)
        )
        if not pack.hits:
            return refusal_answer(
                query,
                unresolved=[query],
                had_context=False,
                verification_errors=["empty_context_pack"],
            )

        raw = self.gateway.complete(
            task="qa_answer",
            system=_system_prompt(pack),
            user=query,
        )
        try:
            answer = parse_qa_answer(raw)
        except (ValueError, ValidationError):
            # The model returned something we cannot parse as an answer. We refuse — a QA system
            # that can't trust its own output must not crash, and must not fabricate. Refusal is
            # the designed behaviour ("no source, no answer"), not a degradation hiding a bug.
            return refusal_answer(
                query,
                unresolved=[query],
                had_context=True,
                verification_errors=["unparseable_model_output"],
            )
        if _looks_like_refusal(answer):
            return refusal_answer(
                query,
                unresolved=answer.unresolved_mentions or [query],
                had_context=True,
                verification_errors=answer.verification_errors or ["model_refused"],
            )
        verification = verify_qa_answer(answer, pack=pack, bundle=self.bundle)
        if not verification.valid:
            return refusal_answer(
                query,
                unresolved=verification.unresolved_mentions,
                had_context=True,
                verification_errors=verification.errors,
            )
        return answer.model_copy(update={"grounded": True, "verification_errors": []})

    def _expand_query(self, query: str) -> list[str]:
        """Best-effort alternate phrasings to widen recall; failure just means no expansion.

        The variants only feed retrieval (reranked against the original query and gated by the
        grounded-or-refuse contract), so a bad variant can add a candidate but never the answer."""
        try:
            raw = self.gateway.complete(task="qa_expand", system=_EXPAND_SYSTEM, user=query)
            value = extract_json(raw)
        except Exception:  # noqa: BLE001 - expansion is best-effort; never fail the question
            return []
        if not isinstance(value, list):
            return []
        variants = [str(item).strip() for item in value if str(item).strip()]
        return variants[:3]


def parse_qa_answer(raw: str) -> QAAnswer:
    """Parse the model's answer JSON, tolerating markdown fences and surrounding prose.

    Raises ``ValueError`` when there is no usable JSON object (or it isn't an object), and lets
    pydantic's ``ValidationError`` propagate on a bad shape — the service catches both and refuses,
    so an unparseable answer never crashes the request and never becomes a fabricated answer."""
    payload = extract_json_object(raw)
    if "answer" not in payload and "text" in payload:
        payload["answer"] = payload["text"]
    return QAAnswer.model_validate(payload)


# Two honestly-different refusals: nothing relevant exists in the world at all, vs. relevant lore
# was found but it does not record the specific point asked (e.g. no direct relation between two
# factions that both exist). The second is the common "the tool seems not to know its own world"
# trap — phrase it as "not recorded" rather than a blanket "no answer", and the UI styles it as a
# calm note, not an error.
REFUSAL_NO_CONTEXT = "这个世界里暂时没有与该问题相关的设定，因此无法作答。"
REFUSAL_UNGROUNDED = (
    "在已有设定里没有找到足以回答该问题的依据——可能所问的具体关系或细节尚未在这个世界中记载。"
)


def refusal_answer(
    query: str,
    *,
    unresolved: list[str] | None = None,
    had_context: bool = False,
    verification_errors: list[str] | None = None,
) -> QAAnswer:
    return QAAnswer(
        answer=REFUSAL_UNGROUNDED if had_context else REFUSAL_NO_CONTEXT,
        citations=[],
        confidence=0.0,
        mentioned_entities=[],
        unresolved_mentions=unresolved or [query],
        refused=True,
        grounded=False,
        verification_errors=verification_errors or [],
    )


def _system_prompt(pack: ContextPack) -> str:
    context_lines = [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in pack.hits]
    return (
        "Answer using only the cited lore context. Return one JSON object with keys: "
        "answer, citations, confidence, mentioned_entities, unresolved_mentions, refused. "
        'citations must be an array of objects like {"ref":"entity:npc_id"}; '
        "confidence must be a number from 0 to 1. "
        "Every citation ref must be one of the provided refs. "
        "If the context contains relation_conflict or both allied_with and enemy_of "
        "for the same pair, state that the data is conflicting instead of choosing one. "
        "If the question asks how two entities relate and the context records no direct relation "
        "between them, do NOT refuse: say no direct relation is recorded, then state what each "
        "entity is and its known relations from the context (cite those). "
        "Answer the part of the question you CAN ground in the context; if some of it is not "
        "covered, answer what is grounded and name the missing pieces in unresolved_mentions "
        "rather than refusing the whole question. Only set refused=true (citations=[], "
        "confidence=0) when NOTHING in the question is grounded in the context.\n\n"
        "Lore context:\n" + "\n".join(context_lines)
    )


def _looks_like_refusal(answer: QAAnswer) -> bool:
    return answer.refused or (
        not answer.citations and answer.confidence <= 0 and bool(answer.unresolved_mentions)
    )
