# 排名融合小工具（RRF 等）。
"""Rank fusion helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable

from .models import RetrievalHit


def reciprocal_rank_fusion(
    result_lists: Iterable[list[RetrievalHit]],
    *,
    k: int = 60,
    weights: Iterable[float] | None = None,
    source: str = "rrf",
) -> list[RetrievalHit]:
    lists = list(result_lists)
    if not lists:
        return []
    selected_weights = [1.0] * len(lists) if weights is None else list(weights)
    if len(selected_weights) != len(lists):
        raise ValueError("RRF weights must align one-to-one with result lists")
    if (
        any(not math.isfinite(weight) or weight < 0.0 for weight in selected_weights)
        or not any(selected_weights)
    ):
        raise ValueError("RRF weights must be finite, non-negative and include a positive value")
    by_ref: dict[str, RetrievalHit] = {}
    scores: dict[str, float] = {}
    for weight, results in zip(selected_weights, lists, strict=True):
        for rank, hit in enumerate(results, start=1):
            if hit.ref in by_ref:
                by_ref[hit.ref] = _merge_hit(by_ref[hit.ref], hit)
            else:
                by_ref[hit.ref] = hit
            scores[hit.ref] = scores.get(hit.ref, 0.0) + weight / (k + rank)

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
