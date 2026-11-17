# v2 固定工作流的项目级装配。
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
from ..retrieval.neural_rerank import reranker_for_corpus
from ..retrieval.policy import RetrievalPolicy
from ..retrieval.vector import VectorRetriever
from ..storage import SQLiteStore
from ..storage.world_lock import world_write_lock

# The canonical default scope. A project opened without an explicit scope lives entirely under
# ("default", "v1") and behaves byte-for-byte as a single-world project: the scope dimension is
# threaded through storage, retrieval/audit reads are scope-filtered, and versions inherit
# copy-on-write.
DEFAULT_WORLD_ID = "default"
DEFAULT_VERSION = "v1"


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
    # The project's current (world_id, version) scope. Defaults to the canonical scope so existing
    # single-world projects are unchanged. The store is pointed at this scope, which stamps every
    # write and filters reads.
    world_id: str = DEFAULT_WORLD_ID
    version: str = DEFAULT_VERSION

    @classmethod
    def open(
        cls,
        content_root: str | Path,
        *,
        sqlite_path: str | Path = ":memory:",
        embedder: Embedder | None = None,
        world_id: str = DEFAULT_WORLD_ID,
        version: str = DEFAULT_VERSION,
        migrate: bool = False,
    ) -> ProjectContext:
        root = Path(content_root)
        content_store = ContentStore(root)
        with world_write_lock(root):
            # ``migrate=True`` transparently opens the world on its working line (creating it once,
            # on first open) instead of the baseline. Migration, the effective bundle snapshot,
            # and all derived SQLite indexes must describe one serialized world revision.
            if migrate:
                version = content_store.ensure_migrated()
            sqlite_store = SQLiteStore(sqlite_path, world_id=world_id, version=version)
            # Load the *effective* bundle for this (world, version) scope via the copy-on-write
            # overlay. The graph, audit and impact analysis then all run on the current version's
            # effective content, not the whole multi-version tree.
            bundle = content_store.load_scoped(world_id=world_id, version=version)
            graph = build_content_graph(bundle)
            sqlite_store.replace_content_index(bundle)
            sqlite_store.replace_graph_edges(graph)
            reference_store = ReferenceStore(root)
            # ``sync_index`` derives a whole-index SQLite snapshot from the file-backed reference
            # library. Keeping its snapshot + commit under the same lock prevents a late open from
            # pruning a reference that a concurrent writer has already published.
            reference_store.sync_index(sqlite_store)
        # One embedder per project: semantic (bge-m3) when [semantic] is installed, else the
        # deterministic hashing stub. Callers (e.g. the acceptance gate) may pin one explicitly
        # for reproducibility; otherwise it is resolved by env. Reused on reload.
        embedder = embedder or resolve_embedder()
        context_builder = ContextPackBuilder(
            bm25=BM25Retriever(sqlite_store),
            vector=VectorRetriever(sqlite_store, embedder=embedder, auto_tier=True),
            graph=GraphExpansionRetriever(graph),
            neural_reranker=reranker_for_corpus(len(graph.node_refs())),
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
            world_id=world_id,
            version=version,
        )

    def qa_context_builder(self) -> ContextPackBuilder:
        """A QA-only builder that also recalls the GraphRAG macro-overview reports *when the corpus
        is large enough to benefit* (:class:`RetrievalPolicy`). Kept separate from
        ``context_builder`` so draft/patch grounding still sees only specific canon rows — but it
        REUSES that builder's already-built bm25/vector/graph retrievers (the vector retriever
        reindexes the whole corpus in its constructor, so building a fresh one per question re-reads
        and re-stacks every vector for nothing). On a small corpus the community retriever is
        omitted: GraphRAG macro summaries are over-engineered at that scale, so they would add cost
        without meaningful recall."""
        base = self.context_builder
        policy = RetrievalPolicy.for_corpus(len(self.graph.node_refs()))
        community = (
            CommunityReportRetriever(self.sqlite_store, embedder=self.embedder)
            if policy.use_graphrag
            else None
        )
        return ContextPackBuilder(
            bm25=base.bm25,
            vector=base.vector,
            graph=base.graph,
            community=community,
            # Reuse the already-resolved reranker from context_builder (same corpus, single
            # resolve -- no second env read / model resolve for the same project).
            neural_reranker=base.neural_reranker,
        )

    def reload(self) -> None:
        # Re-resolve one effective world revision and atomically refresh all of its derived indexes.
        with world_write_lock(self.content_root):
            self.bundle = self.content_store.load_scoped(
                world_id=self.world_id, version=self.version
            )
            self.graph = build_content_graph(self.bundle)
            self.sqlite_store.replace_content_index(self.bundle)
            self.sqlite_store.replace_graph_edges(self.graph)
            self.reference_store.sync_index(self.sqlite_store)
        self.context_builder = ContextPackBuilder(
            bm25=BM25Retriever(self.sqlite_store),
            vector=VectorRetriever(self.sqlite_store, embedder=self.embedder, auto_tier=True),
            graph=GraphExpansionRetriever(self.graph),
            neural_reranker=reranker_for_corpus(len(self.graph.node_refs())),
        )
        self.reference_context_builder = ReferenceContextBuilder(
            self.sqlite_store, embedder=self.embedder
        )

    def resync_scope(self) -> None:
        """Re-point the project at the world's current working line, then reload.

        Update-ingest lands a merge as a NEW frozen merge point + working line (the copy-on-write
        ratchet in :meth:`ContentStore.advance_head`), so the working version the project opened on
        is now the previous one. This picks up the advanced working line from the head and rebuilds
        the scope so subsequent reads/writes hit the freshly merged content. A no-op on an
        un-migrated world (no head)."""
        with world_write_lock(self.content_root):
            head = self.content_store.read_head()
            if head is not None:
                self.version = str(head["working"])
                self.sqlite_store.set_scope(self.world_id, self.version)
            self.reload()

    def close(self) -> None:
        self.sqlite_store.close()
