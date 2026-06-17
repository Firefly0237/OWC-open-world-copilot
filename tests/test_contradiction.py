"""WS-M · Batch 2 — semantic contradiction detection: recall candidates, judge confirms, never fake.

The autouse conftest pins the hashing-stub embedder, so the semantic layer is off here and these
tests exercise the deterministic relation-pair layer + judge behavior. A real-bge-m3 semantic test
is skip-if-model-absent, like the other semantic suites.
"""

from __future__ import annotations

from owcopilot.assist.contradiction import (
    ContradictionDetector,
    OfflineContradictionJudge,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation


def _world() -> ContentBundle:
    return ContentBundle(
        entities={
            "fac_a": Entity(id="fac_a", name="铁律盟", type=EntityType.FACTION),
            "fac_b": Entity(id="fac_b", name="怒潮帮", type=EntityType.FACTION),
        },
        relations=[
            Relation(source="fac_a", target="fac_b", kind="盟友"),
            Relation(source="fac_a", target="fac_b", kind="死敌"),
        ],
    )


class _Gateway:
    """Wrap a provider so the detector can call .complete(task=, system=, user=)."""

    def __init__(self, provider) -> None:  # noqa: ANN001
        self.provider = provider

    def complete(self, *, task: str, system: str, user: str) -> str:
        return self.provider.complete(system=system, user=user, model="cheap")[0]


def test_relation_pair_is_a_candidate_without_a_judge() -> None:
    report = ContradictionDetector(bundle=_world()).detect(use_llm=False)
    assert report.candidate_count >= 1
    # without a judge nothing is asserted as a contradiction — only surfaced for review
    assert not report.contradictions
    assert any(f.layer == "relation" and "盟友" in f.point for f in report.review_suggested)


def test_offline_judge_confirms_the_antonym_pair() -> None:
    gw = _Gateway(OfflineContradictionJudge())
    report = ContradictionDetector(bundle=_world(), gateway=gw).detect(use_llm=True)
    assert report.llm_used
    assert report.contradictions  # 盟友 vs 死敌 confirmed
    c = report.contradictions[0]
    assert c.verdict == "contradiction" and c.subjects == ["fac_a", "fac_b"]


def test_clean_world_has_no_candidates() -> None:
    bundle = ContentBundle(
        entities={"fac_a": Entity(id="fac_a", name="铁律盟", type=EntityType.FACTION)},
        relations=[Relation(source="fac_a", target="fac_a", kind="盟友")],
    )
    report = ContradictionDetector(bundle=bundle).detect(use_llm=False)
    assert report.candidate_count == 0 and not report.findings


def test_judge_parse_failure_fabricates_nothing() -> None:
    class _Garbage:
        def complete(self, *, task: str, system: str, user: str) -> str:
            return "对不起，我无法用 JSON 回答这个问题。"

    report = ContradictionDetector(bundle=_world(), gateway=_Garbage()).detect(use_llm=True)
    # candidates exist, judge replied unparseably -> degrade to zero confirmed, never invent one
    assert report.candidate_count >= 1
    assert report.contradictions == []


def test_same_kind_relations_are_not_flagged() -> None:
    bundle = ContentBundle(
        entities={
            "a": Entity(id="a", name="A", type=EntityType.FACTION),
            "b": Entity(id="b", name="B", type=EntityType.FACTION),
        },
        relations=[
            Relation(source="a", target="b", kind="盟友"),
            Relation(source="a", target="b", kind="盟友"),
        ],
    )
    report = ContradictionDetector(bundle=bundle).detect(use_llm=False)
    assert report.candidate_count == 0  # same kind twice is not a contradiction
