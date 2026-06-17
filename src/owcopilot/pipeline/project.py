"""Project-level assembly for the v2 fixed workflow pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..audit.default_rules import build_default_rule_registry
from ..audit.runner import AuditRunner
from ..content.models import ContentBundle
from ..content.store import ContentStore
from ..graph.index import ContentGraph, build_content_graph
from ..inspiration import ReferenceContextBuilder, ReferenceStore
from ..llm.cache import Embedder
from ..retrieval.bm25 import BM25Retriever
from ..retrieval.community_reports import CommunityReportRetriever
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.embedding import resolve_embedder
from ..retrieval.graph_expand import GraphExpansionRetriever
from ..retrieval.vector import VectorRetriever
from ..storage import SQLiteStore


@dataclass
class ProjectContext:
    content_root: Path
    content_store: ContentStore
    sqlite_store: SQLiteStore
    bundle: ContentBundle
    graph: ContentGraph
    audit_runner: AuditRunner
    context_builder: ContextPackBuilder
    reference_store: ReferenceStore
    reference_context_builder: ReferenceContextBuilder
    embedder: Embedder

    @classmethod
    def open(
        cls,
        content_root: str | Path,
        *,
        sqlite_path: str | Path = ":memory:",
        embedder: Embedder | None = None,
    ) -> ProjectContext:
        root = Path(content_root)
        content_store = ContentStore(root)
        sqlite_store = SQLiteStore(sqlite_path)
        bundle = content_store.load()
        graph = build_content_graph(bundle)
        sqlite_store.replace_content_index(bundle)
        sqlite_store.replace_graph_edges(graph)
        reference_store = ReferenceStore(root)
        reference_store.sync_index(sqlite_store)
        # One embedder per project: semantic (bge-m3) when [semantic] is installed, else the
        # deterministic hashing stub. Callers (e.g. the acceptance gate) may pin one explicitly
        # for reproducibility; otherwise it is resolved by env. Reused on reload.
        embedder = embedder or resolve_embedder()
        context_builder = ContextPackBuilder(
            bm25=BM25Retriever(sqlite_store),
            vector=VectorRetriever(sqlite_store, embedder=embedder),
            graph=GraphExpansionRetriever(graph),
        )
        return cls(
            content_root=root,
            content_store=content_store,
            sqlite_store=sqlite_store,
            bundle=bundle,
            graph=graph,
            audit_runner=AuditRunner(build_default_rule_registry()),
            context_builder=context_builder,
            reference_store=reference_store,
            reference_context_builder=ReferenceContextBuilder(sqlite_store, embedder=embedder),
            embedder=embedder,
        )

    def qa_context_builder(self) -> ContextPackBuilder:
        """A QA-only builder that also recalls the GraphRAG macro-overview reports. Kept separate
        from ``context_builder`` so draft/patch grounding still sees only specific canon rows — but
        it REUSES that builder's already-built bm25/vector/graph retrievers (the vector retriever
        reindexes the whole corpus in its constructor, so building a fresh one per question re-reads
        and re-stacks every vector for nothing). Only the cheap community retriever is added."""
        base = self.context_builder
        return ContextPackBuilder(
            bm25=base.bm25,
            vector=base.vector,
            graph=base.graph,
            community=CommunityReportRetriever(self.sqlite_store),
        )

    def reload(self) -> None:
        self.bundle = self.content_store.load()
        self.graph = build_content_graph(self.bundle)
        self.sqlite_store.replace_content_index(self.bundle)
        self.sqlite_store.replace_graph_edges(self.graph)
        self.reference_store.sync_index(self.sqlite_store)
        self.context_builder = ContextPackBuilder(
            bm25=BM25Retriever(self.sqlite_store),
            vector=VectorRetriever(self.sqlite_store, embedder=self.embedder),
            graph=GraphExpansionRetriever(self.graph),
        )
        self.reference_context_builder = ReferenceContextBuilder(
            self.sqlite_store, embedder=self.embedder
        )

    def close(self) -> None:
        self.sqlite_store.close()
