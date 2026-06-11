"""Context pack assembly."""

from __future__ import annotations

from .bm25 import BM25Retriever
from .budget import trim_hits_to_budget
from .fusion import reciprocal_rank_fusion
from .graph_expand import GraphExpansionRetriever
from .models import ContextPack
from .vector import VectorRetriever


class ContextPackBuilder:
    def __init__(
        self,
        *,
        bm25: BM25Retriever,
        vector: VectorRetriever | None = None,
        graph: GraphExpansionRetriever | None = None,
    ) -> None:
        self.bm25 = bm25
        self.vector = vector
        self.graph = graph

    def build(self, query: str, *, budget_tokens: int = 800, limit: int = 10) -> ContextPack:
        result_lists = [self.bm25.search(query, limit=limit)]
        if self.vector is not None:
            result_lists.append(self.vector.search(query, limit=limit))
        if self.graph is not None:
            result_lists.append(self.graph.search(query, radius=2, limit=limit))
        fused = reciprocal_rank_fusion(result_lists)
        trimmed = trim_hits_to_budget(fused, budget_tokens=budget_tokens)
        return ContextPack(query=query, budget_tokens=budget_tokens, hits=trimmed)
