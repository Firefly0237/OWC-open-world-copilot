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
from ..retrieval.bm25 import BM25Retriever
from ..retrieval.context_pack import ContextPackBuilder
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

    @classmethod
    def open(
        cls,
        content_root: str | Path,
        *,
        sqlite_path: str | Path = ":memory:",
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
        context_builder = ContextPackBuilder(
            bm25=BM25Retriever(sqlite_store),
            vector=VectorRetriever(sqlite_store),
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
            reference_context_builder=ReferenceContextBuilder(sqlite_store),
        )

    def reload(self) -> None:
        self.bundle = self.content_store.load()
        self.graph = build_content_graph(self.bundle)
        self.sqlite_store.replace_content_index(self.bundle)
        self.sqlite_store.replace_graph_edges(self.graph)
        self.reference_store.sync_index(self.sqlite_store)
        self.context_builder = ContextPackBuilder(
            bm25=BM25Retriever(self.sqlite_store),
            vector=VectorRetriever(self.sqlite_store),
            graph=GraphExpansionRetriever(self.graph),
        )
        self.reference_context_builder = ReferenceContextBuilder(self.sqlite_store)

    def close(self) -> None:
        self.sqlite_store.close()
