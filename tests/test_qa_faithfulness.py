"""Tests for the opt-in, $0-skippable LLM-judge faithfulness (entailment) verifier.

These tests never call a real model: the judge path is exercised with a fake gateway that returns
a fixed JSON verdict, and the skip path is exercised by passing ``judge=None``.
"""

from __future__ import annotations

from typing import Any

from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.qa.faithfulness import (
    FAITHFULNESS_JUDGE_TASK,
    _judge_available,
    judge_qa_faithfulness,
)
from owcopilot.qa.models import Citation, QAAnswer
from owcopilot.retrieval.models import ContextPack, RetrievalHit


def _pack(body: str, *, query: str = "铁卫军团是什么") -> ContextPack:
    return ContextPack(
        query=query,
        budget_tokens=200,
        hits=[
            RetrievalHit(
                ref="entity:fac_iron",
                object_type="entity",
                title="铁卫军团",
                body=body,
                score=1.0,
                source="test",
            )
        ],
    )


class _FixedJudgeProvider:
    """Offline provider returning a fixed JSON verdict for every claim. Records the prompts."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.reply, 1, 1


def _gateway(reply: str) -> tuple[LLMGateway, _FixedJudgeProvider]:
    provider = _FixedJudgeProvider(reply)
    gateway = LLMGateway(
        providers={"judge": provider},
        router=StaticRouter(mapping={FAITHFULNESS_JUDGE_TASK: "judge"}),
    )
    return gateway, provider


# --- skip path -------------------------------------------------------------------------------


def test_judge_none_skips_without_error() -> None:
    answer = QAAnswer(
        answer="铁卫军团是雾脊行省的重要势力。",
        citations=[Citation(ref="entity:fac_iron")],
    )
    result = judge_qa_faithfulness(answer, pack=_pack("铁卫军团是雾脊行省的重要势力。"), judge=None)

    assert result["skipped"] is True
    assert "reason" in result and result["reason"]
    # No faithfulness score is fabricated when skipped.
    assert "faithfulness" not in result


def test_empty_provider_gateway_is_treated_as_unavailable() -> None:
    answer = QAAnswer(answer="x", citations=[Citation(ref="entity:fac_iron")])
    empty = LLMGateway(providers={})
    assert _judge_available(empty) is False
    result = judge_qa_faithfulness(answer, pack=_pack("body"), judge=empty)
    assert result["skipped"] is True


# --- supported path --------------------------------------------------------------------------


def test_supported_claim_scores_full_faithfulness() -> None:
    gateway, provider = _gateway('{"supported": true, "reason": "evidence states this"}')
    answer = QAAnswer(
        answer="铁卫军团是雾脊行省的重要势力之一。",
        citations=[Citation(ref="entity:fac_iron")],
    )
    pack = _pack("铁卫军团（Iron Ward Legion），雾脊行省的重要势力之一。")

    result = judge_qa_faithfulness(answer, pack=pack, judge=gateway)

    assert result["skipped"] is False
    assert result["faithfulness"] == 1.0
    assert result["unsupported"] == []
    assert len(result["claims"]) >= 1
    # The judge was actually called with the entailment task label.
    assert provider.calls
    assert FAITHFULNESS_JUDGE_TASK  # task label is exported
    # Evidence body must reach the judge prompt.
    assert "雾脊行省的重要势力之一" in provider.calls[0]["user"]


# --- unsupported path (the 军歌 example: entity in canon, fact not in evidence) ---------------


def test_in_canon_entity_but_fact_not_in_evidence_is_unsupported() -> None:
    # The judge sees that the evidence (faction description) says nothing about a song, so it
    # returns supported=false — exactly the hallucination the existence check lets through.
    gateway, _ = _gateway('{"supported": false, "reason": "evidence has no military song"}')
    answer = QAAnswer(
        answer="铁卫军团的军歌歌词是「铁血长风」。",
        citations=[Citation(ref="entity:fac_iron")],
    )
    pack = _pack(
        "铁卫军团（Iron Ward Legion），雾脊行省的重要势力之一。",
        query="铁卫军团的军歌歌词",
    )

    result = judge_qa_faithfulness(answer, pack=pack, judge=gateway)

    assert result["skipped"] is False
    assert result["faithfulness"] < 1.0
    assert result["unsupported"]
    assert all(not entry["supported"] for entry in result["unsupported"])


# --- fail-closed parsing ---------------------------------------------------------------------


def test_unparseable_judge_reply_fails_closed_as_unsupported() -> None:
    gateway, _ = _gateway("I think this is probably fine, yes.")  # no JSON at all
    answer = QAAnswer(
        answer="铁卫军团是一个势力。",
        citations=[Citation(ref="entity:fac_iron")],
    )

    result = judge_qa_faithfulness(answer, pack=_pack("铁卫军团是一个势力。"), judge=gateway)

    assert result["skipped"] is False
    assert result["faithfulness"] < 1.0
    assert result["unsupported"]
    assert result["unsupported"][0].get("parse_error") is True
    # Crucially: an unparseable verdict was NOT silently counted as supported.
    assert result["unsupported"][0]["supported"] is False


def test_missing_supported_field_fails_closed() -> None:
    gateway, _ = _gateway('{"reason": "forgot the supported field"}')
    answer = QAAnswer(answer="铁卫军团是一个势力。", citations=[Citation(ref="entity:fac_iron")])

    result = judge_qa_faithfulness(answer, pack=_pack("body"), judge=gateway)

    assert result["faithfulness"] < 1.0
    assert result["unsupported"][0]["parse_error"] is True


def test_non_boolean_supported_fails_closed() -> None:
    # "supported": "true" (a string, not a bool) must not be coerced into a pass.
    gateway, _ = _gateway('{"supported": "true", "reason": "stringly typed"}')
    answer = QAAnswer(answer="铁卫军团是一个势力。", citations=[Citation(ref="entity:fac_iron")])

    result = judge_qa_faithfulness(answer, pack=_pack("body"), judge=gateway)

    assert result["faithfulness"] < 1.0
    assert result["unsupported"][0]["parse_error"] is True


def test_judge_exception_fails_closed_without_crashing() -> None:
    class _Boom:
        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            raise RuntimeError("provider down")

    gateway = LLMGateway(
        providers={"judge": _Boom()},
        router=StaticRouter(mapping={FAITHFULNESS_JUDGE_TASK: "judge"}),
    )
    answer = QAAnswer(answer="铁卫军团是一个势力。", citations=[Citation(ref="entity:fac_iron")])

    result = judge_qa_faithfulness(answer, pack=_pack("body"), judge=gateway)

    assert result["skipped"] is False
    assert result["faithfulness"] < 1.0
    assert result["unsupported"][0]["parse_error"] is True


# --- refusal / empty answer ------------------------------------------------------------------


def test_refused_answer_is_vacuously_faithful() -> None:
    gateway, provider = _gateway('{"supported": true, "reason": "x"}')
    refusal = QAAnswer(answer="无法作答。", refused=True)

    result = judge_qa_faithfulness(refusal, pack=_pack("body"), judge=gateway)

    assert result["skipped"] is False
    assert result["faithfulness"] == 1.0
    assert result["claims"] == []
    # A refusal makes no claims → the judge is never called.
    assert provider.calls == []


# --- mixed claims -> partial faithfulness ----------------------------------------------------


def test_multi_claim_partial_faithfulness() -> None:
    # One supported sentence + one unsupported sentence; alternate the verdict per call.
    class _Alternating:
        def __init__(self) -> None:
            self.n = 0

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            self.n += 1
            ok = self.n % 2 == 1
            return f'{{"supported": {str(ok).lower()}, "reason": "r"}}', 1, 1

    gateway = LLMGateway(
        providers={"judge": _Alternating()},
        router=StaticRouter(mapping={FAITHFULNESS_JUDGE_TASK: "judge"}),
    )
    answer = QAAnswer(
        answer="铁卫军团是势力。它的军歌叫铁血长风。",
        citations=[Citation(ref="entity:fac_iron")],
    )

    result: dict[str, Any] = judge_qa_faithfulness(answer, pack=_pack("body"), judge=gateway)

    assert result["skipped"] is False
    assert 0.0 < result["faithfulness"] < 1.0
    assert len(result["claims"]) == 2
    assert len(result["unsupported"]) == 1
