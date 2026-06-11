from __future__ import annotations

from owcopilot.qa.models import Citation, QAAnswer
from owcopilot.qa.service import parse_qa_answer


def test_qa_answer_defaults_to_non_refusal() -> None:
    answer = QAAnswer(
        answer="Aldric leads caravans.",
        citations=[Citation(ref="entity:npc_aldric")],
    )

    assert answer.refused is False
    assert answer.confidence == 0.0
    assert answer.citations[0].ref == "entity:npc_aldric"


def test_qa_answer_accepts_live_model_shorthand_shapes() -> None:
    answer = parse_qa_answer(
        """
        {
          "answer": "Aldric leads caravans.",
          "citations": ["[entity:npc_aldric] Aldric: Caravan master"],
          "confidence": "high",
          "mentioned_entities": "npc_aldric",
          "unresolved_mentions": null
        }
        """
    )

    assert answer.citations == [Citation(ref="entity:npc_aldric")]
    assert answer.confidence == 0.85
    assert answer.mentioned_entities == ["npc_aldric"]


def test_qa_answer_accepts_null_answer_from_live_model_refusal() -> None:
    answer = parse_qa_answer(
        """
        {
          "answer": null,
          "citations": [],
          "confidence": 0,
          "mentioned_entities": [],
          "unresolved_mentions": ["师父"],
          "refused": true
        }
        """
    )

    assert answer.answer == ""
    assert answer.refused is True
