"""Context pack assembly."""

from __future__ import annotations

import sqlite3

from .bm25 import BM25Retriever
from .budget import trim_hits_to_budget
from .community_reports import CommunityReportRetriever
from .fusion import reciprocal_rank_fusion
from .graph_expand import GraphExpansionRetriever
from .models import ContextPack, RetrievalHit
from .rerank import rerank_hits
from .vector import VectorRetriever


class ContextPackBuilder:
    def __init__(
        self,
        *,
        bm25: BM25Retriever,
        vector: VectorRetriever | None = None,
        graph: GraphExpansionRetriever | None = None,
        community: CommunityReportRetriever | None = None,
        rerank: bool = True,
    ) -> None:
        self.bm25 = bm25
        self.vector = vector
        self.graph = graph
        # GraphRAG macro-overview reports, surfaced only for QA so draft grounding stays on
        # specific canon rows. A no-op until an overview index has been built.
        self.community = community
        self.rerank = rerank

    def build(self, query: str, *, budget_tokens: int = 800, limit: int = 10) -> ContextPack:
        return self._assemble(query, [query], budget_tokens=budget_tokens, limit=limit)

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
        self, anchor: str, queries: list[str], *, budget_tokens: int, limit: int
    ) -> ContextPack:
        recall: list[list[RetrievalHit]] = []
        for q in queries:
            recall.append(self.bm25.search(q, limit=limit))
            if self.vector is not None:
                recall.append(self.vector.search(q, limit=limit))
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
        if self.rerank:
            semantic_scores = None
            if self.vector is not None and self.vector.is_semantic:
                semantic_scores = self.vector.similarities(anchor, [hit.ref for hit in fused])
            ranked = rerank_hits(anchor, fused, semantic_scores=semantic_scores)
        else:
            ranked = fused
        trimmed = trim_hits_to_budget(ranked, budget_tokens=budget_tokens)
        return ContextPack(query=anchor, budget_tokens=budget_tokens, hits=trimmed)


def _relation_hit(row: sqlite3.Row) -> RetrievalHit:
    return RetrievalHit(
        ref=str(row["ref"]),
        object_type="relation",
        title=str(row["title"]),
        body=str(row["body"]),
        score=0.5,
        source="relation",
    )
