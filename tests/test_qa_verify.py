from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.qa.models import Citation, QAAnswer
from owcopilot.qa.verify import verify_qa_answer
from owcopilot.retrieval.models import ContextPack, RetrievalHit


def _pack() -> ContextPack:
    return ContextPack(
        query="Aldric",
        budget_tokens=100,
        hits=[
            RetrievalHit(
                ref="entity:npc_aldric",
                object_type="entity",
                title="Aldric",
                score=1.0,
                source="test",
            )
        ],
    )


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
    )


def test_verify_qa_answer_accepts_pack_citation_and_known_entity() -> None:
    answer = QAAnswer(
        answer="Aldric is a caravan master.",
        citations=[Citation(ref="entity:npc_aldric")],
        mentioned_entities=["Aldric"],
    )
    result = verify_qa_answer(
        answer,
        pack=_pack(),
        bundle=_bundle(),
    )

    assert result.valid
    assert result.errors == []
    assert answer.citations[0].text == "Aldric"


def test_verify_qa_answer_rejects_non_refusal_without_citations() -> None:
    result = verify_qa_answer(
        QAAnswer(answer="Aldric is a caravan master."),
        pack=_pack(),
        bundle=_bundle(),
    )

    assert not result.valid
    assert "non-refusal answer must cite" in result.errors[0]


def test_verify_qa_answer_rejects_citation_outside_pack() -> None:
    result = verify_qa_answer(
        QAAnswer(answer="x", citations=[Citation(ref="entity:missing")]),
        pack=_pack(),
        bundle=_bundle(),
    )

    assert not result.valid
    assert "citation 'entity:missing'" in result.errors[0]


def test_verify_qa_answer_rejects_unknown_mentioned_entity() -> None:
    result = verify_qa_answer(
        QAAnswer(answer="Mara leads the caravan.", mentioned_entities=["Mara"]),
        pack=_pack(),
        bundle=_bundle(),
    )

    assert not result.valid
    assert result.unresolved_mentions == ["Mara"]


def test_verify_qa_answer_ignores_unknown_metadata_mentions_not_in_answer() -> None:
    result = verify_qa_answer(
        QAAnswer(
            answer="Aldric is a caravan master.",
            citations=[Citation(ref="entity:npc_aldric")],
            mentioned_entities=["Mara"],
        ),
        pack=_pack(),
        bundle=_bundle(),
    )

    assert result.valid


def test_verify_qa_answer_canonicalizes_bare_ids_from_live_models() -> None:
    answer = QAAnswer(
        answer="Aldric is a caravan master.",
        citations=[Citation(ref="npc_aldric")],
        mentioned_entities=["npc_aldric"],
    )

    result = verify_qa_answer(answer, pack=_pack(), bundle=_bundle())

    assert result.valid
    assert answer.citations[0].ref == "entity:npc_aldric"


def test_verify_qa_answer_canonicalizes_wrong_object_prefix_when_id_is_unique() -> None:
    pack = ContextPack(
        query="Q1",
        budget_tokens=100,
        hits=[
            RetrievalHit(
                ref="quest:q1",
                object_type="quest",
                title="Q1",
                score=1.0,
                source="test",
            )
        ],
    )
    bundle = ContentBundle(quests={"q1": Quest(id="q1", title="Q1")})
    answer = QAAnswer(answer="Quest answer.", citations=[Citation(ref="entity:q1")])

    result = verify_qa_answer(answer, pack=pack, bundle=bundle)

    assert result.valid
    assert answer.citations[0].ref == "quest:q1"


def test_verify_qa_answer_does_NOT_catch_unsupported_fact_entailment_gap() -> None:
    """Documents the honest limitation: this is *citation-existence* grounding, NOT entailment.

    Scenario = the classic dangerous hallucination: the entity exists in canon and is retrieved,
    but the *specific fact* the answer asserts is nowhere in the cited text ("Aldric's favourite
    food"). Because the cited ref is real and in the pack, ``verify_qa_answer`` returns
    ``valid=True`` — it never checks whether the citation *supports* the claim.

    This test pins that gap on purpose. If a future entailment/NLI verifier is added, it would make
    this case invalid; this assertion should then be moved to that verifier, not silently deleted —
    the point is to keep the over-claim impossible to make accidentally. See qa/verify.py docstring.
    """
    pack = ContextPack(
        query="阿尔德里克最喜欢吃什么",
        budget_tokens=100,
        hits=[
            RetrievalHit(
                ref="entity:npc_aldric",
                object_type="entity",
                title="Aldric",
                body="Aldric is the caravan master of the iron road. He leads escort missions.",
                score=1.0,
                source="test",
            )
        ],
    )
    bundle = ContentBundle(
        entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
    )
    # The answer fabricates a fact the cited body does not support at all.
    answer = QAAnswer(
        answer="Aldric's favourite food is roasted lamb.",
        citations=[Citation(ref="entity:npc_aldric")],
        mentioned_entities=["Aldric"],
        confidence=0.75,
    )

    result = verify_qa_answer(answer, pack=pack, bundle=bundle)

    # KNOWN LIMITATION (not the desired behaviour): existence grounding passes the unsupported
    # answer. No entailment check exists, so this is accepted today.
    assert result.valid, (
        "existence-grounding accepts a real-but-unsupporting citation; "
        "entailment is out of scope (documented limitation)"
    )
    assert result.errors == []


def test_verify_qa_answer_accepts_wrong_prefix_mentions_when_id_is_unique() -> None:
    pack = ContextPack(
        query="Q1",
        budget_tokens=100,
        hits=[
            RetrievalHit(
                ref="quest:q1",
                object_type="quest",
                title="Q1",
                score=1.0,
                source="test",
            )
        ],
    )
    bundle = ContentBundle(quests={"q1": Quest(id="q1", title="Q1")})
    answer = QAAnswer(
        answer="Quest answer.",
        citations=[Citation(ref="quest:q1")],
        mentioned_entities=["entity:q1"],
    )

    result = verify_qa_answer(answer, pack=pack, bundle=bundle)

    assert result.valid
