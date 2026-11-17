from __future__ import annotations

from owcopilot.retrieval.models import RetrievalHit
from owcopilot.retrieval.rerank import LexicalReScorer, rerank_hits


def _hit(ref: str, title: str, body: str = "", *, score: float = 0.0) -> RetrievalHit:
    return RetrievalHit(
        ref=ref,
        object_type=ref.split(":", 1)[0],
        title=title,
        body=body,
        score=score,
        source="rrf",
    )


def test_rerank_promotes_on_topic_hit_over_graph_flood() -> None:
    # The fused order (by RRF score) puts a weakly-related graph neighbour first, even
    # though it shares no query terms; the actual answer sits below it. Reranking must
    # surface the document whose title covers the query.
    flood = _hit("entity:loc_market", "Market Square", "a busy plaza", score=0.9)
    answer = _hit("quest:patrol", "Patrol the Beacon Towers", "report to the warden", score=0.4)
    fused = [flood, answer]

    ranked = rerank_hits("patrol the beacon towers quest", fused)

    assert [hit.ref for hit in ranked] == ["quest:patrol", "entity:loc_market"]
    assert ranked[0].source == "reranked"


def test_rerank_title_match_beats_body_match() -> None:
    body_only = _hit("entity:a", "Unrelated", "the dragon sleeps here", score=0.5)
    title_match = _hit("entity:b", "The Dragon", "a guardian", score=0.5)

    ranked = rerank_hits("dragon", [body_only, title_match])

    assert ranked[0].ref == "entity:b"


def test_rerank_breadth_prefers_full_query_coverage() -> None:
    partial = _hit("entity:a", "Salt", "trade goods", score=0.5)
    full = _hit("entity:b", "Salt Caravan Escort", "guards on the road", score=0.5)

    ranked = rerank_hits("salt caravan escort", [partial, full])

    assert ranked[0].ref == "entity:b"


def test_rerank_handles_cjk_terms() -> None:
    other = _hit("entity:a", "无关词条", "随便的描述", score=0.5)
    target = _hit("entity:b", "漕运", "古代的水路运输制度", score=0.5)

    ranked = rerank_hits("漕运是什么", [other, target])

    assert ranked[0].ref == "entity:b"


def test_rerank_preserves_order_for_untokenizable_query() -> None:
    hits = [_hit("entity:b", "B", score=0.9), _hit("entity:a", "A", score=0.4)]

    # Punctuation/emoji-only queries yield no terms; recall order must be left untouched.
    assert rerank_hits("!!! ???", hits) == hits
    assert rerank_hits("🙂", hits) == hits


def test_rerank_is_noop_for_fewer_than_two_hits() -> None:
    single = [_hit("entity:a", "Aldric")]
    assert rerank_hits("aldric", single) == single
    assert rerank_hits("aldric", []) == []


def test_rerank_is_deterministic_and_breaks_ties_stably() -> None:
    # Two hits with identical relevance and equal fused score must order by ref, so the
    # result is reproducible regardless of input order (golden-testable).
    a = _hit("entity:a", "Twin", "same", score=0.5)
    b = _hit("entity:b", "Twin", "same", score=0.5)

    forward = [hit.ref for hit in rerank_hits("twin", [a, b])]
    backward = [hit.ref for hit in rerank_hits("twin", [b, a])]

    assert forward == backward == ["entity:a", "entity:b"]


def test_rerank_does_not_drop_or_invent_candidates() -> None:
    hits = [
        _hit("entity:a", "Alpha", score=0.9),
        _hit("entity:b", "Beta", score=0.5),
        _hit("entity:c", "Gamma", score=0.1),
    ]

    ranked = rerank_hits("alpha", hits)

    assert {hit.ref for hit in ranked} == {"entity:a", "entity:b", "entity:c"}
    assert len(ranked) == len(hits)


# ---------------------------------------------------------------------------
# Terminology / docstring regression tests (RT5)
# ---------------------------------------------------------------------------


def test_rerank_module_docstring_uses_lexical_re_scorer_not_reranker() -> None:
    """RT5: rerank.py module docstring must say 'LexicalReScorer' not 'lexical reranker'."""
    import inspect

    import owcopilot.retrieval.rerank as rerank_mod

    doc = rerank_mod.__doc__ or ""
    # The old erroneous phrasing used 'reranker' to describe the lexical scorer
    assert "lexical reranker" not in doc.lower(), (
        "Module docstring should not use 'lexical reranker'. "
        "The correct term is 'LexicalReScorer' or 'lexical re-scorer'."
    )
    # The correct name should appear somewhere in the module docstring or class
    combined = doc + inspect.getsource(rerank_mod)
    assert "LexicalReScorer" in combined, (
        "LexicalReScorer class/name must appear in the rerank module."
    )


def test_rerank_module_has_no_duplicate_bullet_block() -> None:
    """RT5: the module docstring must not contain the duplicated signals bullet block."""
    import owcopilot.retrieval.rerank as rerank_mod

    doc = rerank_mod.__doc__ or ""
    # The first bullet item marker for the block appears exactly once
    count = doc.count("* breadth  --")
    assert count == 1, (
        f"Module docstring contains the signals bullet block {count} time(s); expected exactly 1. "
        "Remove the duplicate block introduced in the original source."
    )


def test_lexical_rescorer_class_is_importable_and_named_correctly() -> None:
    """LexicalReScorer must be importable as the canonical class name."""
    scorer = LexicalReScorer()
    assert hasattr(scorer, "rerank"), "LexicalReScorer must have a .rerank() method."


def test_lexical_rescorer_rerank_delegates_to_rerank_hits() -> None:
    """LexicalReScorer.rerank must produce the same output as the standalone rerank_hits()."""
    hits = [
        _hit("entity:a", "Iron Ward", score=0.9),
        _hit("entity:b", "Caravan Guild", score=0.4),
    ]
    scorer = LexicalReScorer()
    via_class = scorer.rerank("iron ward patrol", hits)
    via_fn = rerank_hits("iron ward patrol", hits)

    assert [h.ref for h in via_class] == [h.ref for h in via_fn]
