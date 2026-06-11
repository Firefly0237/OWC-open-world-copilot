"""Rank fusion helpers."""

from __future__ import annotations

from collections.abc import Iterable

from .models import RetrievalHit


def reciprocal_rank_fusion(
    result_lists: Iterable[list[RetrievalHit]],
    *,
    k: int = 60,
    source: str = "rrf",
) -> list[RetrievalHit]:
    by_ref: dict[str, RetrievalHit] = {}
    scores: dict[str, float] = {}
    for results in result_lists:
        for rank, hit in enumerate(results, start=1):
            if hit.ref in by_ref:
                by_ref[hit.ref] = _merge_hit(by_ref[hit.ref], hit)
            else:
                by_ref[hit.ref] = hit
            scores[hit.ref] = scores.get(hit.ref, 0.0) + 1.0 / (k + rank)

    fused: list[RetrievalHit] = []
    for ref, hit in by_ref.items():
        fused.append(hit.model_copy(update={"score": scores[ref], "source": source}))
    return sorted(fused, key=lambda hit: (-hit.score, hit.ref))


def _merge_hit(left: RetrievalHit, right: RetrievalHit) -> RetrievalHit:
    body_parts = []
    for body in (left.body, right.body):
        if body and body not in body_parts:
            body_parts.append(body)
    return left.model_copy(
        update={
            "body": " ".join(body_parts),
            "metadata": {**left.metadata, **right.metadata},
        }
    )
