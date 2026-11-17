# 检索分层策略：一处显式决定『语料多大才值得上哪套检索机器』（小语料别启重型 ANN/重排）。
"""Explicit corpus-size thresholds for optional retrieval machinery.

Callers use an approximate corpus count, such as content-graph nodes, to decide whether
to enable community reports, hierarchical partitions, neural reranking and vector
quantisation. These are configuration defaults rather than measured optimal cutoffs.
The vector storage layer independently checks its exact count before selecting ANN.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Enable community-report retrieval at this approximate corpus size.
GRAPHRAG_MIN_SIZE = 800

#: Attempt hierarchical partitions above this size; actual subdivision also depends
#: on whether a community exceeds ``max_cluster_size`` and can be split.
HIERARCHY_MIN_SIZE = 5_000

#: A cross-encoder rerank pays for its latency only once the candidate pool is deep. This gate IS
#: wired into the default pipeline: ``ContextPackBuilder`` runs the neural reranker on the fused
#: candidates (bounded by its candidate window) when the corpus is at/above this size AND the
#: reranker model resolves; below it — or when the model is unavailable — the rerank stage stays
#: the lexical field-weighted re-scorer (logged fallback, never a crash).
RERANK_MIN_SIZE = 2_000

#: Permit lower-precision vectors above this size. Recall and storage changes depend
#: on the corpus and backend; this threshold does not establish a quality guarantee.
VECTOR_INT8_MIN_SIZE = 2_000


@dataclass(frozen=True)
class RetrievalPolicy:
    """Which retrieval machinery is worth running for a corpus of ``corpus_size`` (approx.)."""

    corpus_size: int

    @property
    def use_graphrag(self) -> bool:
        """Whether to fold GraphRAG community/macro reports into QA recall. Off for small corpora —
        the one tier the policy actually drives (see ``ProjectContext.qa_context_builder``)."""
        return self.corpus_size >= GRAPHRAG_MIN_SIZE

    @property
    def use_hierarchical_community(self) -> bool:
        """Whether to attempt hierarchical (multi-level) GraphRAG community detection. Off below
        ``HIERARCHY_MIN_SIZE`` — a mid-size corpus keeps the flat single-level partition."""
        return self.corpus_size >= HIERARCHY_MIN_SIZE

    @property
    def recommends_neural_rerank(self) -> bool:
        """Whether the cross-encoder rerank runs at this size — the size half of the two-part gate
        driving ``ContextPackBuilder``'s neural rerank stage (the other half is model availability
        via ``resolve_reranker``; absent model → lexical rerank fallback, logged)."""
        return self.corpus_size >= RERANK_MIN_SIZE

    @property
    def use_ann(self) -> bool:
        """Whether the corpus is large enough for the on-disk usearch HNSW (sub-linear) index. The
        storage layer applies the same ``USEARCH_MIN_N`` gate on its own persisted-vector count
        (imported lazily to avoid an import cycle, and so a test monkeypatching the storage
        threshold is reflected here too)."""
        from ..storage.sqlite import USEARCH_MIN_N  # noqa: PLC0415 — lazy: avoid import cycle

        return self.corpus_size >= USEARCH_MIN_N

    @property
    def use_int8(self) -> bool:
        """Whether to quantise vectors to int8 (memory win, recall ~0.999). Off for small corpora so
        the exact fp32 scan (and the acceptance recall gate) is never perturbed."""
        return self.corpus_size >= VECTOR_INT8_MIN_SIZE

    @property
    def vector_tier(self) -> str:
        """The vector index tier as a human-readable label: ``usearch`` HNSW at/above the ANN
        threshold, else ``int8`` (quantised exact scan) at/above the int8 threshold, else the
        lossless fp32 ``scan``."""
        if self.use_ann:
            return "usearch"
        return "int8" if self.use_int8 else "scan"

    def describe(self) -> dict[str, object]:
        """A flat, loggable/telemetry-friendly view of every tier decision for this corpus."""
        return {
            "corpus_size": self.corpus_size,
            "vector_tier": self.vector_tier,
            "use_ann": self.use_ann,
            "use_int8": self.use_int8,
            "use_graphrag": self.use_graphrag,
            "use_hierarchical_community": self.use_hierarchical_community,
            "recommends_neural_rerank": self.recommends_neural_rerank,
        }

    @classmethod
    def for_corpus(cls, corpus_size: int) -> RetrievalPolicy:
        """Build a policy for a corpus of the given approximate size (clamped to ``>= 0``)."""
        return cls(corpus_size=max(0, corpus_size))
