"""Tests for the deterministic ``$0`` offline faithfulness judge.

Unlike ``test_qa_faithfulness.py`` (which feeds a *fixed* verdict through a fake gateway), these
exercise the judge's OWN decision: given the claim and the evidence, ``OfflineEntailmentJudge``
computes support with no model. This is what lets the ``qa_faithfulness_entailment`` gate run
deterministically in ``$0`` CI and catch the "in-canon entity / out-of-canon fact" hallucination.
"""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.evaluation import run_acceptance_evaluation
from owcopilot.qa.faithfulness import (
    FAITHFULNESS_JUDGE_TASK,
    _judge_available,
    judge_qa_faithfulness,
)
from owcopilot.qa.models import Citation, QAAnswer
from owcopilot.qa.offline_entailment import OfflineEntailmentJudge, _parse_claim_evidence
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


def test_offline_judge_is_available() -> None:
    """A truthy ``providers`` makes it a usable judge (unlike ``judge=None``)."""
    assert _judge_available(OfflineEntailmentJudge()) is True


def test_complete_returns_supported_json_for_grounded_claim() -> None:
    judge = OfflineEntailmentJudge()
    user = (
        "QUESTION:\n铁卫军团是什么\n\n"
        "CLAIM:\n铁卫军团是雾脊行省的重要势力之一\n\n"
        "EVIDENCE:\n[entity:fac_iron] 铁卫军团 铁卫军团，雾脊行省的重要势力之一。"
    )
    payload = json.loads(judge.complete(task=FAITHFULNESS_JUDGE_TASK, system="", user=user))
    assert payload["supported"] is True
    assert isinstance(payload["reason"], str) and payload["reason"]


def test_complete_returns_unsupported_json_for_fabricated_fact() -> None:
    judge = OfflineEntailmentJudge()
    user = (
        "QUESTION:\n铁卫军团的军歌歌词\n\n"
        "CLAIM:\n铁卫军团的军歌歌词是「铁血长风，永镇北疆」\n\n"
        "EVIDENCE:\n[entity:fac_iron] 铁卫军团 铁卫军团，雾脊行省的重要势力之一。"
    )
    payload = json.loads(judge.complete(task=FAITHFULNESS_JUDGE_TASK, system="", user=user))
    assert payload["supported"] is False


def test_extractive_answer_scores_full_faithfulness() -> None:
    """An answer copied from the evidence (the offline QA path) is fully faithful."""
    judge = OfflineEntailmentJudge()
    body = "铁卫军团（Iron Ward Legion），雾脊行省的重要势力之一。"
    answer = QAAnswer(answer=body, citations=[Citation(ref="entity:fac_iron")])

    result = judge_qa_faithfulness(answer, pack=_pack(body), judge=judge)

    assert result["skipped"] is False
    assert result["faithfulness"] == 1.0
    assert result["unsupported"] == []


def test_in_canon_entity_but_fact_not_in_evidence_is_caught_deterministically() -> None:
    """The 军歌 case: the judge DECIDES (no fed verdict) that the fabricated song is unsupported."""
    judge = OfflineEntailmentJudge()
    answer = QAAnswer(
        answer="铁卫军团的军歌歌词是「铁血长风，永镇北疆」。",
        citations=[Citation(ref="entity:fac_iron")],
    )
    pack = _pack(
        "铁卫军团（Iron Ward Legion），雾脊行省的重要势力之一。",
        query="铁卫军团的军歌歌词",
    )

    result = judge_qa_faithfulness(answer, pack=pack, judge=judge)

    assert result["skipped"] is False
    assert result["faithfulness"] < 1.0
    assert result["unsupported"]
    assert all(not entry["supported"] for entry in result["unsupported"])


def test_parse_claim_evidence_extracts_both_sections() -> None:
    claim, evidence = _parse_claim_evidence(
        "QUESTION:\nq\n\nCLAIM:\nthe claim text\n\nEVIDENCE:\nthe evidence text"
    )
    assert claim == "the claim text"
    assert evidence == "the evidence text"


def test_parse_claim_evidence_is_defensive_on_missing_sections() -> None:
    claim, evidence = _parse_claim_evidence("no markers here")
    assert claim == ""
    assert evidence == ""


def test_acceptance_faithfulness_gate_runs_and_passes_with_offline_judge(tmp_path: Path) -> None:
    """With the offline judge (what the CLI/CI now passes) the entailment gate is ENFORCED, not
    skipped, and still green — the extractive offline QA answers are faithful to their evidence."""
    report = run_acceptance_evaluation(tmp_path, faithfulness_judge=OfflineEntailmentJudge())
    by_name = {check.name: check for check in report.checks}
    gate = by_name["qa_faithfulness_entailment"]
    assert gate.details["skipped"] is False  # actually ran (not the default skip)
    assert gate.passed is True
    assert report.passed is True
