"""RetrievalPolicy — the single explicit, corpus-size-tiered retrieval decision (deepening T4).

Unit-tests the tier boundaries, and proves the one tier the policy actually drives: the QA context
builder folds in the GraphRAG community retriever only when the corpus is large enough.
"""

from __future__ import annotations

from pathlib import Path

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.project import ProjectContext
from owcopilot.retrieval.policy import (
    GRAPHRAG_MIN_SIZE,
    HIERARCHY_MIN_SIZE,
    RERANK_MIN_SIZE,
    VECTOR_INT8_MIN_SIZE,
    RetrievalPolicy,
)


# --------------------------------------------------------------------------- tier boundaries
def test_graphrag_gate_boundary() -> None:
    assert RetrievalPolicy.for_corpus(GRAPHRAG_MIN_SIZE - 1).use_graphrag is False
    assert RetrievalPolicy.for_corpus(GRAPHRAG_MIN_SIZE).use_graphrag is True


def test_hierarchical_community_gate_boundary() -> None:
    # A far higher gate than plain GraphRAG: hierarchy only pays off at very large scale, so a
    # mid-size corpus (well past the GraphRAG gate) still keeps the flat single-level partition.
    assert HIERARCHY_MIN_SIZE > GRAPHRAG_MIN_SIZE
    assert RetrievalPolicy.for_corpus(HIERARCHY_MIN_SIZE - 1).use_hierarchical_community is False
    assert RetrievalPolicy.for_corpus(HIERARCHY_MIN_SIZE).use_hierarchical_community is True


def test_neural_rerank_recommendation_boundary() -> None:
    assert RetrievalPolicy.for_corpus(RERANK_MIN_SIZE - 1).recommends_neural_rerank is False
    assert RetrievalPolicy.for_corpus(RERANK_MIN_SIZE).recommends_neural_rerank is True


def test_vector_tier_has_three_bands() -> None:
    from owcopilot.storage.sqlite import USEARCH_MIN_N

    # fp32 exact scan below the int8 threshold; int8 in the middle band; HNSW at/above the ANN one.
    assert RetrievalPolicy.for_corpus(VECTOR_INT8_MIN_SIZE - 1).vector_tier == "scan"
    assert RetrievalPolicy.for_corpus(VECTOR_INT8_MIN_SIZE).vector_tier == "int8"
    assert RetrievalPolicy.for_corpus(USEARCH_MIN_N - 1).vector_tier == "int8"
    assert RetrievalPolicy.for_corpus(USEARCH_MIN_N).vector_tier == "usearch"


def test_int8_and_ann_gates() -> None:
    from owcopilot.storage.sqlite import USEARCH_MIN_N

    assert RetrievalPolicy.for_corpus(VECTOR_INT8_MIN_SIZE - 1).use_int8 is False
    assert RetrievalPolicy.for_corpus(VECTOR_INT8_MIN_SIZE).use_int8 is True
    assert RetrievalPolicy.for_corpus(USEARCH_MIN_N - 1).use_ann is False
    assert RetrievalPolicy.for_corpus(USEARCH_MIN_N).use_ann is True


def test_for_corpus_clamps_negative_and_describe_is_flat() -> None:
    policy = RetrievalPolicy.for_corpus(-5)
    assert policy.corpus_size == 0
    described = policy.describe()
    assert set(described) == {
        "corpus_size",
        "vector_tier",
        "use_ann",
        "use_int8",
        "use_graphrag",
        "use_hierarchical_community",
        "recommends_neural_rerank",
    }


# --------------------------------------------------------------------------- retriever auto-tier
def test_vector_retriever_auto_tier_resolves_from_corpus_size(monkeypatch) -> None:
    """The product default (``auto_tier=True``) maps corpus size → (quantized, ann) via the policy;
    a small corpus stays lossless fp32 exact so the acceptance recall gate is never perturbed."""
    from owcopilot.retrieval.vector import VectorRetriever
    from owcopilot.storage import SQLiteStore

    store = SQLiteStore()
    try:
        retriever = VectorRetriever(store, auto_tier=True)
        # Small corpus → fp32 exact scan.
        retriever._rows = [object()] * 10  # type: ignore[list-item]
        assert retriever._resolve_tier() == (False, False)
        # Mid corpus → int8, still exact-scan (no ANN).
        retriever._rows = [object()] * VECTOR_INT8_MIN_SIZE  # type: ignore[list-item]
        assert retriever._resolve_tier() == (True, False)
        # Large corpus (above both thresholds) → int8 + HNSW ANN.
        monkeypatch.setattr("owcopilot.storage.sqlite.USEARCH_MIN_N", 4)
        retriever._rows = [object()] * VECTOR_INT8_MIN_SIZE  # type: ignore[list-item]
        assert retriever._resolve_tier() == (True, True)
    finally:
        store.close()


def test_vector_retriever_static_flags_bypass_policy() -> None:
    """Without auto_tier the explicit constructor flags win verbatim (backward compatible)."""
    from owcopilot.retrieval.vector import VectorRetriever
    from owcopilot.storage import SQLiteStore

    store = SQLiteStore()
    try:
        retriever = VectorRetriever(store, quantized=True, ann=False)  # auto_tier defaults off
        retriever._rows = [object()] * 100_000  # type: ignore[list-item]
        assert retriever._resolve_tier() == (True, False)  # policy ignored
    finally:
        store.close()


# --------------------------------------------------------------------------- the wired tier
def _small_world(root: Path) -> None:
    ContentStore(root).save(
        ContentBundle(
            entities={
                f"npc_{i}": Entity(
                    id=f"npc_{i}", name=f"N{i}", type=EntityType.NPC, description="x"
                )
                for i in range(5)
            }
        )
    )


def test_qa_builder_omits_graphrag_on_small_corpus(tmp_path: Path) -> None:
    root = tmp_path / "content"
    _small_world(root)
    project = ProjectContext.open(root, sqlite_path=tmp_path / "p.sqlite")
    try:
        # A handful of nodes is far below GRAPHRAG_MIN_SIZE → community retriever omitted.
        assert project.qa_context_builder().community is None
    finally:
        project.close()


def test_qa_builder_includes_graphrag_when_corpus_large_enough(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "content"
    _small_world(root)
    # Drop the threshold so the tiny test corpus counts as "large enough": proves the gate wires
    # the community retriever back on above the threshold (not that it is hard-coded off).
    monkeypatch.setattr("owcopilot.retrieval.policy.GRAPHRAG_MIN_SIZE", 1)
    project = ProjectContext.open(root, sqlite_path=tmp_path / "p.sqlite")
    try:
        assert project.qa_context_builder().community is not None
    finally:
        project.close()
