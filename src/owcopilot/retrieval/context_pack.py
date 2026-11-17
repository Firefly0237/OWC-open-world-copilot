# context pack 组装：多路召回融合后拼成喂给模型的最终检索包。
"""Context pack assembly."""

from __future__ import annotations

import sqlite3

from .bm25 import BM25Retriever
from .budget import trim_hits_to_budget
from .community_reports import CommunityReportRetriever
from .fusion import reciprocal_rank_fusion
from .graph_expand import GraphExpansionRetriever
from .models import ContextPack, RetrievalHit
from .neural_rerank import DEFAULT_RERANK_CANDIDATE_WINDOW, NeuralReranker
from .rerank import rerank_hits
from .vector import QueryBundle, VectorRetriever


class ContextPackBuilder:
    def __init__(
        self,
        *,
        bm25: BM25Retriever,
        vector: VectorRetriever | None = None,
        graph: GraphExpansionRetriever | None = None,
        community: CommunityReportRetriever | None = None,
        rerank: bool = True,
        neural_reranker: NeuralReranker | None = None,
        rerank_candidate_window: int = DEFAULT_RERANK_CANDIDATE_WINDOW,
    ) -> None:
        self.bm25 = bm25
        self.vector = vector
        self.graph = graph
        # GraphRAG macro-overview reports, surfaced only for QA so draft grounding stays on
        # specific canon rows. A no-op until an overview index has been built.
        self.community = community
        self.rerank = rerank
        # The neural cross-encoder precision stage. When None (the default, both size/env gates
        # off) the rerank stage stays the lexical re-scorer, so the default path is unchanged.
        self.neural_reranker = neural_reranker
        # Bounded top-K window the cross-encoder scores (canonical "e.g. 100" recall→rerank pool);
        # candidates past it keep their fused order (reorder, never drop).
        self.rerank_candidate_window = rerank_candidate_window

    def build(self, query: str, *, budget_tokens: int = 800, limit: int = 10) -> ContextPack:
        return self._assemble(query, [query], budget_tokens=budget_tokens, limit=limit)

    def build_many(
        self, queries: list[str], *, budget_tokens: int = 800, limit: int = 10
    ) -> list[ContextPack]:
        """Batch analogue of :meth:`build`: encode ALL queries in one ``embed_many``, assemble each.

        Every query is assembled independently (no cross-query expansion), so each returned pack is
        bit-identical to ``build(query, ...)`` under a per-item embedder — the only change is that
        the dense leg encodes all the queries in a single batched forward pass, which is where an
        eval loop over a frozen query set (or any batch caller) spends its embedding time. With no
        vector leg wired in this degrades to a plain per-query loop (nothing to batch). See
        :meth:`VectorRetriever.encode_queries` for the batch-vs-single equivalence contract."""
        if self.vector is None:
            return [
                self._assemble(q, [q], budget_tokens=budget_tokens, limit=limit) for q in queries
            ]
        bundles = self.vector.encode_queries(queries)
        return [
            self._assemble(
                q, [q], budget_tokens=budget_tokens, limit=limit, query_bundles={q: bundle}
            )
            for q, bundle in zip(queries, bundles, strict=True)
        ]

    def build_expanded(
        self, query: str, variants: list[str], *, budget_tokens: int = 800, limit: int = 10
    ) -> ContextPack:
        """Like :meth:`build`, but also retrieve for ``variants`` (alternate phrasings).

        Recall runs over the original query plus each variant so a phrasing that matches the
        canon's wording surfaces what the user's phrasing missed. Crucially the rerank still
        scores against the *original* query, so any documents a variant dragged in that are not
        on-topic for what the user actually asked are demoted out of the budget -- expansion can
        only add recall, never steer the answer."""
        queries = [query, *(v for v in variants if v.strip() and v.strip() != query)]
        return self._assemble(query, queries, budget_tokens=budget_tokens, limit=limit)

    def _assemble(
        self,
        anchor: str,
        queries: list[str],
        *,
        budget_tokens: int,
        limit: int,
        query_bundles: dict[str, QueryBundle] | None = None,
    ) -> ContextPack:
        # Encode each distinct query once so the dense recall leg and the rerank similarity leg
        # reuse a single embedding per query instead of re-encoding it (the QueryBundle single-
        # encode pattern). ``query_bundles`` may be pre-supplied by a batch caller (build_many);
        # otherwise it is filled here. Skipped entirely when no vector leg is wired in.
        if self.vector is not None and query_bundles is None:
            query_bundles = {}
            for q in queries:
                if q not in query_bundles:
                    query_bundles[q] = self.vector.encode_query(q)
        recall: list[list[RetrievalHit]] = []
        for q in queries:
            recall.append(self.bm25.search(q, limit=limit))
            if self.vector is not None:
                assert query_bundles is not None
                recall.append(self.vector.search(query_bundles[q], limit=limit))
            if self.graph is not None:
                recall.append(self.graph.search(q, radius=2, limit=limit))
        if self.community is not None:
            # macro-overview reports compete against the anchor question; rerank decides whether
            # they belong (holistic question) or get trimmed (specific question)
            recall.append(self.community.search(anchor, limit=limit))
        result_lists = list(recall)
        # Relation completion: surface the relations of the entities recall already found, so a
        # "which factions, and how do they relate?" question retrieves the relations it needs
        # instead of falsely refusing because its phrasing did not match the relation text.
        entity_ids = {
            hit.ref.split(":", 1)[1]
            for hits in recall
            for hit in hits
            if hit.ref.startswith("entity:")
        }
        relation_hits = [
            _relation_hit(row) for row in self.bm25.store.relation_rows_for_entities(entity_ids)
        ]
        if relation_hits:
            result_lists.append(relation_hits)
        # Recall first (fuse the retrievers), then rerank for precision against the ANCHOR query,
        # then spend the token budget on the most on-topic hits -- the two-stage RAG ordering.
        fused = reciprocal_rank_fusion(result_lists)
        anchor_bundle = query_bundles.get(anchor) if query_bundles else None
        ranked = self._rerank(anchor, fused, anchor_bundle=anchor_bundle) if self.rerank else fused
        trimmed = trim_hits_to_budget(ranked, budget_tokens=budget_tokens)
        reasons = self._degradation_reasons(ranked)
        return ContextPack(
            query=anchor,
            budget_tokens=budget_tokens,
            hits=trimmed,
            degraded=bool(reasons),
            degradation_reasons=reasons,
        )

    def _degradation_reasons(self, ranked: list[RetrievalHit]) -> list[str]:
        """Return stable public reason codes, never provider/model exception messages."""
        reasons: list[str] = []
        if self.vector is not None:
            embedder = self.vector.embedder
            if getattr(embedder, "degraded", False):
                reasons.append(
                    str(getattr(embedder, "degradation_reason", None) or "embedder_degraded")
                )
        if self.neural_reranker is not None and getattr(
            self.neural_reranker, "degraded", False
        ):
            reasons.append(
                str(
                    getattr(self.neural_reranker, "degradation_reason", None)
                    or "neural_reranker_degraded"
                )
            )
        elif any(hit.source == "reranked_lexical_fallback" for hit in ranked):
            reasons.append("neural_reranker_degraded")
        return list(dict.fromkeys(reasons))

    def _rerank(
        self,
        anchor: str,
        fused: list[RetrievalHit],
        *,
        anchor_bundle: QueryBundle | None = None,
    ) -> list[RetrievalHit]:
        """The precision stage: neural cross-encoder when wired on, else the lexical re-scorer.

        Neural path (both size/env gates passed at construction): only the bounded top-K
        ``rerank_candidate_window`` is scored jointly by the cross-encoder (the canonical
        recall→rerank division of labour -- full-list joint scoring would be "rather slow");
        the tail keeps its fused order. The stage reorders and never drops -- budget trim stays
        the only stage that drops.

        Default path (``neural_reranker is None``): the field-weighted lexical re-scorer over the
        full fused list, with a semantic cosine leg when the vector retriever is semantic. The
        candidate window is NOT applied here -- lexical scoring is cheap and scoring the full list
        is the existing behaviour, so windowing it would perturb the default ordering."""
        if self.neural_reranker is not None:
            window = fused[: self.rerank_candidate_window]
            tail = fused[self.rerank_candidate_window :]
            return [*self.neural_reranker.rerank(anchor, window), *tail]
        semantic_scores = None
        if self.vector is not None and self.vector.is_semantic:
            # Reuse the anchor's already-computed embedding (the recall leg encoded it) so the
            # semantic rerank leg does not re-embed the same query text; fall back to the raw text
            # when no bundle was threaded in (e.g. a direct ``_rerank`` call in a unit test).
            query: str | QueryBundle = anchor_bundle if anchor_bundle is not None else anchor
            semantic_scores = self.vector.similarities(query, [hit.ref for hit in fused])
        return rerank_hits(anchor, fused, semantic_scores=semantic_scores)


def _relation_hit(row: sqlite3.Row) -> RetrievalHit:
    return RetrievalHit(
        ref=str(row["ref"]),
        object_type="relation",
        title=str(row["title"]),
        body=str(row["body"]),
        score=0.5,
        source="relation",
    )
