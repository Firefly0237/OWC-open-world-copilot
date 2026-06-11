from __future__ import annotations

from owcopilot.retrieval.budget import estimate_tokens, trim_hits_to_budget
from owcopilot.retrieval.models import RetrievalHit


def _hit(ref: str, body: str) -> RetrievalHit:
    return RetrievalHit(ref=ref, object_type="entity", title=ref, body=body, score=1.0, source="x")


def test_estimate_tokens_handles_english_and_cjk() -> None:
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("你好") == 2


def test_trim_hits_to_budget_keeps_first_hit_even_if_large() -> None:
    hits = [_hit("entity:a", "one two three four"), _hit("entity:b", "five")]

    trimmed = trim_hits_to_budget(hits, budget_tokens=2)

    assert [hit.ref for hit in trimmed] == ["entity:a"]
