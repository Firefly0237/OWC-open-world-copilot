# Lore 问答服务：检索→作答→带引用/拒答；忠实性校验挂在后面。
"""Lore QA service."""

from __future__ import annotations

import os
from typing import Any

from pydantic import ValidationError

from ..content.models import ContentBundle
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json, extract_json_object
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack
from .faithfulness import FaithfulnessJudge, judge_qa_faithfulness
from .models import QAAnswer, QAFaithfulness
from .offline_entailment import OfflineEntailmentJudge
from .verify import verify_qa_answer

#: Env var backstop for the faithfulness gate; a constructor argument always wins over it.
QA_FAITHFULNESS_ENV = "OWCOPILOT_QA_FAITHFULNESS"
_FAITHFULNESS_MODES = ("off", "offline", "llm")
#: Default gate threshold — matches the acceptance gate, which fails an answer at faithfulness < 1.0
#: (any unsupported claim trips it). Overridable per service via ``faithfulness_threshold``.
_DEFAULT_FAITHFULNESS_THRESHOLD = 1.0
_FAITHFULNESS_UNAVAILABLE_NOTE = (
    "未能进行忠实性（蕴含级）校验：当前没有可用的模型判官（llm 模式需先接入模型 provider）。"
    "答案已给出但未经蕴含校验，请人工核对。"
)

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
        agentic: bool = False,
        faithfulness: str | None = None,
        faithfulness_threshold: float | None = None,
    ) -> None:
        self.gateway = gateway
        self.context_builder = context_builder
        self.bundle = bundle
        self.expand = expand
        # Opt-in agentic retrieval: a bounded retrieve→assess→re-query loop for multi-hop
        # questions (retrieval/agentic.py). Off by default — single-pass behaviour unchanged.
        self.agentic = agentic
        # Opt-in, layered, default-off faithfulness (entailment) gate. ``off`` (default) leaves the
        # answer path byte-for-byte unchanged; ``offline`` uses the deterministic $0 lexical judge;
        # ``llm`` uses this service's gateway as a live judge. A constructor argument wins over the
        # OWCOPILOT_QA_FAITHFULNESS env backstop. This annotates the answer — it never blocks it.
        self.faithfulness_mode = _resolve_faithfulness_mode(faithfulness)
        self.faithfulness_threshold = (
            _DEFAULT_FAITHFULNESS_THRESHOLD
            if faithfulness_threshold is None
            else faithfulness_threshold
        )

    def ask(self, query: str, *, budget_tokens: int = 800) -> QAAnswer:
        if self.agentic:
            from ..retrieval.agentic import AgenticRetriever  # noqa: PLC0415 — opt-in path

            pack = (
                AgenticRetriever(self.context_builder, gateway=self.gateway)
                .retrieve(query, budget_tokens=budget_tokens)
                .pack
            )
        else:
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
        # Citation-existence grounding passed; layer the opt-in entailment annotation on top. It is
        # a signal only — it never flips ``grounded`` or converts the answer into a refusal.
        faithfulness = self._faithfulness_annotation(answer, pack)
        return answer.model_copy(
            update={
                "grounded": True,
                "verification_errors": [],
                "faithfulness": faithfulness,
            }
        )

    def _faithfulness_annotation(
        self, answer: QAAnswer, pack: ContextPack
    ) -> QAFaithfulness | None:
        """Run the opt-in faithfulness gate over a grounded answer; ``None`` when the gate is off.

        ``offline`` always evaluates via the deterministic lexical judge. ``llm`` uses this
        service's gateway; when that gateway has no usable provider, ``judge_qa_faithfulness``
        skips and this reports ``judge="unavailable"`` rather than inventing a passing score.
        """
        if self.faithfulness_mode == "off":
            return None
        judge = self._faithfulness_judge()
        result = judge_qa_faithfulness(answer, pack=pack, judge=judge)
        return self._build_faithfulness(result)

    def _faithfulness_judge(self) -> FaithfulnessJudge | None:
        if self.faithfulness_mode == "offline":
            return OfflineEntailmentJudge()
        if self.faithfulness_mode == "llm":
            return self.gateway
        return None

    def _build_faithfulness(self, result: dict[str, Any]) -> QAFaithfulness:
        threshold = self.faithfulness_threshold
        if result.get("skipped"):
            # llm mode requested but no usable model judge — an honest non-verification, not a pass.
            return QAFaithfulness(
                judge="unavailable",
                ok=False,
                score=None,
                threshold=threshold,
                unsupported_claims=[],
                note=_FAITHFULNESS_UNAVAILABLE_NOTE,
            )
        score = float(result.get("faithfulness", 0.0))
        unsupported = [
            claim
            for item in result.get("unsupported", [])
            if (claim := str(item.get("claim", "")).strip())
        ]
        ok = score >= threshold
        return QAFaithfulness(
            judge=self.faithfulness_mode,
            ok=ok,
            score=score,
            threshold=threshold,
            unsupported_claims=unsupported,
            note=_faithfulness_note(
                ok=ok, score=score, threshold=threshold, unsupported=unsupported
            ),
        )

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


def _resolve_faithfulness_mode(explicit: str | None) -> str:
    """Resolve the gate mode: explicit constructor argument wins, else the env backstop, else off.

    Raises ``ValueError`` on an unknown value rather than silently defaulting to ``off`` — a typo'd
    ``llm`` must surface, not quietly disable the gate the caller asked for.
    """
    raw = explicit if explicit is not None else os.getenv(QA_FAITHFULNESS_ENV, "off")
    mode = raw.strip().lower()
    if mode not in _FAITHFULNESS_MODES:
        raise ValueError(
            f"未知的忠实性校验模式 {raw!r}；可选值为 off / offline / llm"
            f"（构造参数 faithfulness=... 或环境变量 {QA_FAITHFULNESS_ENV}）。"
        )
    return mode


def _faithfulness_note(*, ok: bool, score: float, threshold: float, unsupported: list[str]) -> str:
    if ok:
        return f"忠实性校验通过：答案的全部论断均可由检索到的证据支持（faithfulness={score:.2f}）。"
    joined = "；".join(unsupported) if unsupported else "（未能定位到具体论断）"
    return (
        f"忠实性校验未通过（faithfulness={score:.2f} < 阈值 {threshold:.2f}）："
        f"以下论断未被检索到的证据支持，建议人工核对：{joined}。"
    )


# Kept off complete_structured: a text→answer alias coercion feeds a real model shape drift into the
# single model, and the caller (``ask``) turns any parse/validation failure into a designed refusal
# ("no source, no answer"), not a raise — both are load-bearing QA-contract behaviour.
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
