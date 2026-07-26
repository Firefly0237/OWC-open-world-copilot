# 把 GraphRAG 社区报告当普通命中项召回。
"""Retriever that surfaces GraphRAG community reports as ordinary hits.

The macro-overview reports built by :mod:`owcopilot.qa.community_index` join the QA recall pool as
candidate hits (ref ``community:<id>``). The existing two-stage reranker then does the routing for
free: a holistic question ("the main powers and how they relate?") lifts the community/global
reports to the top, while a specific question ("who is Mara?") demotes them out of the token budget
in favour of the entity rows — so no brittle "is this a global question?" classifier is needed.

The global synthesis report is always kept in the candidate set so a genuinely macro question can
always reach the whole-world picture even when its wording shares few terms with any one cluster.

Lexical matching alone misses a macro question whose wording shares no *term* with a report (the
contiguous-substring recall in :mod:`owcopilot.retrieval.text_match` scores it 0). So — when an
embedder is supplied — this also runs a dense leg in the shape of DRIFT's Primer phase (Microsoft
Research): embed each report's text and the query with the SAME encoder, cosine-rank the reports,
take the top-K, and fuse that with the lexical leg via reciprocal rank fusion. No instruction
prefix / asymmetric encoding — the macro query and the report text go through the same ``encode`` as
everywhere else. With no embedder the retriever stays on the lexical-only path, byte-for-byte as
before.
"""

from __future__ import annotations

from typing import Any

from ..llm.cache import Embedder
from ..storage import SQLiteStore
from .fusion import reciprocal_rank_fusion
from .models import RetrievalHit
from .text_match import lexical_score
from .vector import VectorRetriever, load_community_report_rows

_GLOBAL_REPORT_ID = "_global"


class CommunityReportRetriever:
    def __init__(self, store: SQLiteStore, *, embedder: Embedder | None = None) -> None:
        self.store = store
        # Dense leg, only when an embedder is supplied. Reuses the shared VectorRetriever over a
        # third corpus (``community_vectors``); ``auto_tier`` resolves the tiny report corpus to the
        # lossless fp32 exact scan (deterministic, offline-capable with the hashing embedder). When
        # None, ``search`` stays byte-identical to the pre-T12 lexical-only retriever.
        self._vector = (
            VectorRetriever(
                store,
                embedder=embedder,
                rows_loader=load_community_report_rows,
                vectors_table="community_vectors",
                auto_tier=True,
            )
            if embedder is not None
            else None
        )

    def search(self, query: str, *, limit: int = 8) -> list[RetrievalHit]:
        reports = self.store.list_community_reports()
        if not reports:
            return []  # no index built yet → graceful no-op, QA falls back to row retrieval
        ordered = sorted(
            reports,
            key=lambda r: (-lexical_score(query, (r["id"], r["title"], r["summary"])), r["id"]),
        )
        if self._vector is None:
            # Lexical-only path — unchanged from before T12.
            kept = self._keep_global_reachable(ordered[:limit], reports, limit=limit)
            return [_hit(report) for report in kept]

        # DRIFT-style Primer: dense recall over report embeddings, fused with the lexical leg so a
        # macro question that shares no term with a report can still reach it semantically.
        lexical_hits = [_hit(report) for report in ordered[:limit]]
        dense_hits = self._vector.search(query, limit=limit)
        fused = reciprocal_rank_fusion([lexical_hits, dense_hits])
        reports_by_ref = {f"community:{report['id']}": report for report in reports}
        kept = [reports_by_ref[hit.ref] for hit in fused[:limit] if hit.ref in reports_by_ref]
        kept = self._keep_global_reachable(kept, reports, limit=limit)
        # Re-materialise the display hit from the report itself: whichever leg surfaced a ref, the
        # returned hit carries the same title/summary/member-ref provenance as the lexical path.
        return [_hit(report) for report in kept]

    def _keep_global_reachable(
        self, kept: list[dict[str, Any]], reports: list[dict[str, Any]], *, limit: int
    ) -> list[dict[str, Any]]:
        """Keep the whole-world synthesis reachable for abstract macro questions even if it didn't
        make the cut — same guarantee as before, shared by both the lexical and the fused path."""
        if any(r["id"] == _GLOBAL_REPORT_ID for r in kept):
            return kept
        globals_ = [r for r in reports if r["id"] == _GLOBAL_REPORT_ID]
        return kept[: max(0, limit - 1)] + globals_


def _hit(report: dict[str, Any]) -> RetrievalHit:
    return RetrievalHit(
        ref=f"community:{report['id']}",
        object_type="community_report",
        title=str(report["title"]),
        body=str(report["summary"]),
        score=0.5,
        source="community",
        # provenance: the canon ids this overview is built from (so a macro answer can trace back)
        metadata={"member_refs": ",".join(str(r) for r in report.get("member_refs", []))},
    )
