# 检索分层策略：一处显式决定『语料多大才值得上哪套检索机器』（小语料别启重型 ANN/重排）。
"""Retrieval tiering policy — one explicit place that decides which retrieval machinery is worth
running for a given corpus size, instead of that decision being scattered across the vector-backend
selector, the QA context builder (GraphRAG), and the reranker.

Why a policy: heavy retrieval machinery is not free, and on a small corpus it is not worth its cost.
The project's own product notes flag GraphRAG community summarisation as over-engineered at small
scale, and an on-disk ANN index / cross-encoder rerank only pay off once the corpus is large. So the
policy tiers by size and turns each tier on only past a defensible threshold. The default cold-start
/ demo / test corpus lands in the cheapest tier, so nothing heavy runs on the ``$0`` path.

``corpus_size`` is an approximate scale signal (the caller passes the natural count for its layer —
the QA builder passes the content-graph node count). The storage layer still applies its own
``USEARCH_MIN_N`` gate on its exact persisted-vector count; ``vector_tier`` here mirrors that
threshold as the single human-readable statement of the tiering.
"""

from __future__ import annotations

from dataclasses import dataclass

#: GraphRAG community/macro reports only help once there are enough entities to form clusters that a
#: flat retrieval would miss. Below this the acceptance-scale world (~375 graph nodes) does not
#: benefit, so the QA builder omits the community retriever entirely.
GRAPHRAG_MIN_SIZE = 800

#: Hierarchical (multi-level) community detection — graspologic's recursive ``hierarchical_leiden``
#: (Edge et al. 2024) — only earns its extra index-time cost on a very large corpus, the
#: "几十万条 long-form narrative" case. Well above ``GRAPHRAG_MIN_SIZE``: a mid-size world keeps
#: the flat single-level partition (byte-identical to today). This gate only decides whether to
#: *attempt* the hierarchy; whether level ≥ 1 layers actually appear is decided independently by
#: ``max_cluster_size`` (a single level-0 community must exceed it before it is subdivided).
HIERARCHY_MIN_SIZE = 5_000

#: A cross-encoder rerank pays for its latency only once the candidate pool is deep. This gate IS
#: wired into the default pipeline: ``ContextPackBuilder`` runs the neural reranker on the fused
#: candidates (bounded by its candidate window) when the corpus is at/above this size AND the
#: reranker model resolves; below it — or when the model is unavailable — the rerank stage stays
#: the lexical field-weighted re-scorer (logged fallback, never a crash).
RERANK_MIN_SIZE = 2_000

#: int8 vector quantisation (~4× less memory, recall ~0.999) earns its slight recall cost only once
#: the corpus is large enough that the fp32 blob set actually strains memory — the "本地受限硬件 +
#: 几十万条" case. Below it, the auto tier stays on lossless fp32 so a small / eval corpus keeps
#: exact recall (the acceptance gate never sees quantisation). Well under ``USEARCH_MIN_N`` so a
#: mid-size corpus gets the memory win before it needs the sub-linear ANN index.
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
