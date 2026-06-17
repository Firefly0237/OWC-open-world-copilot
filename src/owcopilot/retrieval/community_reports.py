"""Retriever that surfaces GraphRAG community reports as ordinary hits.

The macro-overview reports built by :mod:`owcopilot.qa.community_index` join the QA recall pool as
candidate hits (ref ``community:<id>``). The existing two-stage reranker then does the routing for
free: a holistic question ("the main powers and how they relate?") lifts the community/global
reports to the top, while a specific question ("who is Mara?") demotes them out of the token budget
in favour of the entity rows — so no brittle "is this a global question?" classifier is needed.

The global synthesis report is always kept in the candidate set so a genuinely macro question can
always reach the whole-world picture even when its wording shares few terms with any one cluster.
"""

from __future__ import annotations

from typing import Any

from ..storage import SQLiteStore
from .models import RetrievalHit
from .text_match import lexical_score

_GLOBAL_REPORT_ID = "_global"


class CommunityReportRetriever:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def search(self, query: str, *, limit: int = 8) -> list[RetrievalHit]:
        reports = self.store.list_community_reports()
        if not reports:
            return []  # no index built yet → graceful no-op, QA falls back to row retrieval
        scored = sorted(
            reports,
            key=lambda r: (-lexical_score(query, (r["id"], r["title"], r["summary"])), r["id"]),
        )
        kept = scored[:limit]
        # keep the whole-world synthesis reachable for abstract macro questions even if it didn't
        # make the lexical cut
        if not any(r["id"] == _GLOBAL_REPORT_ID for r in kept):
            globals_ = [r for r in reports if r["id"] == _GLOBAL_REPORT_ID]
            kept = kept[: max(0, limit - 1)] + globals_
        return [_hit(report) for report in kept]


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
