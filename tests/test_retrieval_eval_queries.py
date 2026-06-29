"""Tests for retrieval_eval_queries() and run_semantic_retrieval_benchmark().

These test the T5 evaluation improvements:
- retrieval_eval_queries() returns paraphrase + indirect + unanswerable queries
- At least 30% of the queries do not contain entity canonical names (P5C-1)
- run_semantic_retrieval_benchmark() is skippable in CI ($0 path)
"""

from __future__ import annotations

from owcopilot.evaluation.acceptance import (
    _EVENTS,
    _FACTIONS,
    _ITEMS,
    retrieval_eval_queries,
    run_semantic_retrieval_benchmark,
)


def _entity_names() -> set[str]:
    """Collect all canonical entity names from the acceptance world for name-presence checking."""
    names: set[str] = set()
    for _, name_cn, name_en in _FACTIONS:
        names.add(name_cn)
        names.add(name_en)
    for _, name_cn, _ in _EVENTS:
        names.add(name_cn)
    for _, name_cn, _ in _ITEMS:
        names.add(name_cn)
    return names


def test_retrieval_eval_queries_returns_expected_count() -> None:
    """Should return paraphrase + indirect + unanswerable = 10 + 3 + 2 = 15 queries."""
    queries = retrieval_eval_queries()
    assert len(queries) == 15


def test_retrieval_eval_queries_has_unanswerable() -> None:
    """At least 1 query must have expected_ref=None (unanswerable test)."""
    queries = retrieval_eval_queries()
    unanswerable = [q for q, ref in queries if ref is None]
    assert len(unanswerable) >= 1


def test_retrieval_eval_queries_has_answerable() -> None:
    """At least 1 query must have a non-None expected_ref."""
    queries = retrieval_eval_queries()
    answerable = [(q, ref) for q, ref in queries if ref is not None]
    assert len(answerable) >= 1


def test_retrieval_eval_queries_30pct_no_entity_name() -> None:
    """P5C-1: at least 30% of queries must NOT contain entity canonical names.

    This validates that the evaluation tests semantic retrieval, not just BM25 name-matching.
    """
    names = _entity_names()
    queries = retrieval_eval_queries()
    answerable_queries = [q for q, ref in queries if ref is not None]

    def contains_entity_name(query: str) -> bool:
        return any(name in query for name in names if len(name) >= 2)

    no_name_count = sum(1 for q in answerable_queries if not contains_entity_name(q))
    ratio = no_name_count / len(answerable_queries) if answerable_queries else 0.0
    assert ratio >= 0.30, (
        f"Only {ratio:.1%} of answerable queries lack entity names (need >=30%). "
        f"Queries with names: {[q for q in answerable_queries if contains_entity_name(q)]}"
    )


def test_retrieval_eval_queries_expected_refs_are_valid_format() -> None:
    """All non-None expected_refs must have the 'type:id' format."""
    queries = retrieval_eval_queries()
    for q, ref in queries:
        if ref is not None:
            assert ":" in ref, f"Invalid ref format {ref!r} for query {q!r}"
            prefix, _ = ref.split(":", 1)
            assert prefix in {"entity", "quest", "poi", "region"}, (
                f"Unexpected ref type {prefix!r} in {ref!r}"
            )


def test_retrieval_eval_queries_are_strings() -> None:
    """All queries must be non-empty strings."""
    queries = retrieval_eval_queries()
    for q, _ref in queries:
        assert isinstance(q, str) and q.strip(), f"Empty or non-string query: {q!r}"


def test_run_semantic_retrieval_benchmark_skips_in_ci(tmp_path) -> None:
    """When bge-m3 is unavailable, skip_if_no_semantic=True must return skipped=True."""
    from unittest.mock import patch

    # If semantic is available, mock it away to simulate CI
    with patch(
        "owcopilot.retrieval.embedding.semantic_available",
        return_value=False,
    ):
        result = run_semantic_retrieval_benchmark(
            str(tmp_path / "ws"),
            skip_if_no_semantic=True,
        )
    assert result.get("skipped") is True
    assert "reason" in result


def test_run_semantic_retrieval_benchmark_returns_correct_keys_when_skipped(tmp_path) -> None:
    """Skipped result must have 'skipped' and 'reason' keys."""
    from unittest.mock import patch

    with patch("owcopilot.retrieval.embedding.semantic_available", return_value=False):
        result = run_semantic_retrieval_benchmark(str(tmp_path / "ws"), skip_if_no_semantic=True)
    assert "skipped" in result
    assert "reason" in result


def test_retrieval_benchmark_queries_still_30_verbatim() -> None:
    """Existing acceptance test compatibility: retrieval_benchmark_queries() still returns 30."""
    from owcopilot.evaluation.acceptance import retrieval_benchmark_queries

    queries = retrieval_benchmark_queries()
    assert len(queries) == 30


# ---------------------------------------------------------------------------
# RT5: indirect query graph-hop validation
# ---------------------------------------------------------------------------


def _faction_canonical_names() -> set[str]:
    """Return all faction canonical names (CN + EN) from the acceptance world."""
    return {name for _, name_cn, name_en in _FACTIONS for name in (name_cn, name_en)}


def _region_canonical_names() -> set[str]:
    """Return all region canonical names (CN + EN) from the acceptance world fixture."""
    from owcopilot.evaluation.acceptance import _REGIONS

    return {name for name_cn, name_en, *_ in _REGIONS for name in (name_cn, name_en)}


def test_indirect_queries_contain_no_faction_canonical_names() -> None:
    """RT5: indirect queries must NOT contain faction canonical names (graph-hop test).

    If a query contains the answer entity's own name (e.g. '铁卫军团'), BM25 can answer it
    directly without traversing any graph edges.  True graph-hop queries describe the answer
    entity only via its *relationships* (e.g. quest→giver→faction, loc→controlled_by→faction).
    """
    queries = retrieval_eval_queries()
    # indirect queries are indices 10-12 (after 10 paraphrase + before 2 unanswerable)
    indirect = queries[10:13]
    assert len(indirect) == 3, f"Expected 3 indirect queries, got {len(indirect)}"

    faction_names = _faction_canonical_names()
    for query_text, _ref in indirect:
        for name in faction_names:
            if len(name) >= 2:
                assert name not in query_text, (
                    f"Indirect query {query_text!r} contains faction canonical name {name!r}. "
                    "Indirect (graph-hop) queries must describe entities by relationship chain, "
                    "not by name. Replace with a relationship-chain description."
                )


def test_indirect_queries_contain_no_region_canonical_names() -> None:
    """RT5: indirect queries must NOT contain region canonical names.

    '雾脊山道' and 'Mistridge Pass' identify specific locations by name; a BM25 retriever
    with the region index can answer without traversing the graph.
    """
    queries = retrieval_eval_queries()
    indirect = queries[10:13]

    region_names = _region_canonical_names()
    for query_text, _ref in indirect:
        for name in region_names:
            if len(name) >= 3:  # skip very short names to avoid false positives
                assert name not in query_text, (
                    f"Indirect query {query_text!r} contains region name {name!r}. "
                    "Use generic positional/relationship descriptions instead."
                )


def test_retrieval_eval_queries_docstring_mentions_hit_rate_includes_rerank() -> None:
    """RT5: retrieval_eval_queries docstring must note hit_rate includes the rerank stage."""
    import inspect

    from owcopilot.evaluation import acceptance as acc_mod

    source = inspect.getsource(acc_mod.retrieval_eval_queries)
    # The fix adds a note that hit_rate is post-rerank, not pure recall
    assert "rerank" in source.lower(), (
        "retrieval_eval_queries docstring should mention that hit_rate is measured after "
        "the rerank stage (not pure recall). Add an honest annotation clarifying this."
    )
