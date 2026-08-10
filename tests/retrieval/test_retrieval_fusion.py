from __future__ import annotations

import pytest

from owcopilot.retrieval.fusion import reciprocal_rank_fusion
from owcopilot.retrieval.models import RetrievalHit


def _hit(ref: str, score: float = 1.0, source: str = "test") -> RetrievalHit:
    return RetrievalHit(
        ref=ref,
        object_type=ref.split(":", 1)[0],
        title=ref,
        score=score,
        source=source,
    )


def test_reciprocal_rank_fusion_merges_and_reranks_hits() -> None:
    fused = reciprocal_rank_fusion(
        [
            [_hit("entity:a"), _hit("entity:b")],
            [_hit("entity:b"), _hit("entity:c")],
        ],
        k=10,
    )

    assert [hit.ref for hit in fused] == ["entity:b", "entity:a", "entity:c"]
    assert all(hit.source == "rrf" for hit in fused)


def test_reciprocal_rank_fusion_is_stable_for_ties() -> None:
    fused = reciprocal_rank_fusion([[_hit("entity:b")], [_hit("entity:a")]], k=10)

    assert [hit.ref for hit in fused] == ["entity:a", "entity:b"]


def test_reciprocal_rank_fusion_merges_duplicate_ref_bodies() -> None:
    bm25 = _hit("entity:fac_caobang").model_copy(update={"body": "漕帮"})
    graph = _hit("entity:fac_caobang").model_copy(
        update={"body": "relation entity:fac_caobang enemy_of entity:fac_canglang"}
    )

    fused = reciprocal_rank_fusion([[bm25], [graph]], k=10)

    assert fused[0].body == ("漕帮 relation entity:fac_caobang enemy_of entity:fac_canglang")


def test_weighted_rrf_changes_priority_without_dropping_candidates() -> None:
    lexical = [_hit("lexical-first"), _hit("dense-first")]
    dense = list(reversed(lexical))

    equal = reciprocal_rank_fusion([lexical, dense])
    weighted = reciprocal_rank_fusion([lexical, dense], weights=[0.2, 0.8])

    assert equal[0].ref == "dense-first"  # equal-score tie breaks by stable ref
    assert weighted[0].ref == "dense-first"
    assert {hit.ref for hit in weighted} == {"lexical-first", "dense-first"}


@pytest.mark.parametrize("weights", ([1.0], [-1.0, 1.0], [0.0, 0.0]))
def test_weighted_rrf_rejects_invalid_weight_contract(weights) -> None:
    with pytest.raises(ValueError, match="RRF weights"):
        reciprocal_rank_fusion([[], []], weights=weights)
