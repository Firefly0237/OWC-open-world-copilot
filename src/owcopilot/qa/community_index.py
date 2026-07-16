# GraphRAG 社区报告索引：每簇摘要 + 全局综合层，支撑『整个世界』级别的问答。
"""GraphRAG community-report index: per-cluster summaries + a whole-world synthesis layer.

This is the LLM "indexing" pass that sits on top of the deterministic partition in
:mod:`owcopilot.graph.community`. For each community it writes a short report (title + summary);
then one global report synthesises across them and the ties *between* clusters. The reports are what
a macro question retrieves over instead of the raw rows it can no longer fit.

Guardrails kept inside the GraphRAG choice (the north star is "stable / auditable"):
* **Provenance** — every report carries the exact canon ids it summarises (``member_refs``), taken
  from the deterministic partition, not from the model. A macro answer can always trace back.
* **Determinism where it counts** — the partition and the cache key are machine-decided; only the
  prose is the model's. An unparsable reply degrades to a deterministic member roster, never a crash
  and never a fabrication.
* **Caching** — a report is keyed by its community fingerprint (member ids + their text). A re-index
  only re-summarises the clusters that actually changed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..graph.community import (
    Community,
    CommunityRelation,
    _leiden_partition,
    cross_community_relations,
    detect_communities,
    detect_hierarchical_communities,
    resolve_max_cluster_size,
)
from ..graph.index import build_content_graph
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..retrieval.policy import RetrievalPolicy
from ..storage import SQLiteStore

logger = logging.getLogger(__name__)

# Sentinel marking a community-report call so the deterministic offline provider can recognise it.
COMMUNITY_REPORT_MARKER = "[[COMMUNITY_REPORT]]"
GLOBAL_REPORT_ID = "_global"

_COMMUNITY_SYSTEM = (
    "You are a world-bible analyst. Given one cluster of related world objects and the ties inside "
    "it, write a SHORT community report. Return ONE JSON object with keys: title, summary.\n"
    "- title: a 2-6 word name for what this cluster IS (a faction bloc, a region + its players).\n"
    "- summary: 2-4 sentences naming the key members and the power or tension that binds them — "
    "grounded ONLY in the members and ties listed, inventing nothing not present.\n"
    + COMMUNITY_REPORT_MARKER
)
_GLOBAL_SYSTEM = (
    "You synthesise a WHOLE-WORLD structural overview from per-cluster reports and the ties "
    "BETWEEN clusters. Return ONE JSON object with keys: title, summary.\n"
    "- title: a 2-6 word name for the world's overall structure.\n"
    "- summary: 3-5 sentences on the overall power structure — the main blocs and how they relate "
    "or oppose — grounded only in the reports and cross-cluster ties listed.\n"
    + COMMUNITY_REPORT_MARKER
)


class CommunityReport(BaseModel):
    id: str
    level: str  # "community" | "global" (unchanged: retriever + global-first ordering key on this)
    title: str
    summary: str
    member_refs: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    # Hierarchical GraphRAG (opt-in): the Leiden level this report summarises (0 = coarsest) and the
    # parent community it rolls up into (None at level 0). Flat single-level indexing leaves both at
    # their defaults, so an existing overview index is byte-for-byte unchanged.
    hier_level: int = 0
    parent_id: str | None = None


class CommunityIndexResult(BaseModel):
    reports: list[CommunityReport]
    community_count: int
    regenerated: int  # how many reports were freshly generated (the rest were cache hits)


class CommunityIndexService:
    def __init__(self, *, gateway: LLMGateway, store: SQLiteStore, bundle: ContentBundle) -> None:
        self.gateway = gateway
        self.store = store
        self.bundle = bundle

    def build(
        self, *, progress: Callable[[str, dict[str, Any]], None] | None = None
    ) -> CommunityIndexResult:
        communities = self._detect()
        children_by_parent: dict[str, list[Community]] = {}
        for community in communities:
            if community.parent_id is not None:
                children_by_parent.setdefault(community.parent_id, []).append(community)

        report_by_id: dict[str, CommunityReport] = {}
        regenerated = 0
        total = len(communities)
        # Bottom-up (GraphRAG §2.3): process the deepest communities first, so a parent is
        # synthesised from its already-built child reports (sub-community substitution). A stable
        # sort keeps detection order within a level, so the flat case (every community at level 0)
        # is processed c0, c1, … exactly as the previous single-level loop did.
        order = sorted(range(total), key=lambda i: -communities[i].hier_level)
        for position, i in enumerate(order):
            community = communities[i]
            children = children_by_parent.get(community.id)
            if children:
                report, did_regenerate = self._build_parent(
                    community, children, [report_by_id[c.id] for c in children], progress
                )
            else:
                report, did_regenerate = self._build_leaf(community, position, total, progress)
            report_by_id[community.id] = report
            regenerated += int(did_regenerate)

        # The root global report synthesises across the coarsest (level-0) reports and the ties
        # between them — GraphRAG's whole-graph view. In the flat case level 0 is every community,
        # so this is byte-identical to the previous single global-synthesis pass.
        level0 = [c for c in communities if c.hier_level == 0]
        global_report = self._generate_global(level0, [report_by_id[c.id] for c in level0])
        cached_global = self.store.get_community_report(GLOBAL_REPORT_ID, global_report.fingerprint)
        if cached_global is None:
            if progress is not None:
                progress("global", {"id": GLOBAL_REPORT_ID})
            self.store.save_community_report(global_report.model_dump())
            regenerated += 1
        else:
            global_report = CommunityReport.model_validate(cached_global)

        all_reports = [report_by_id[c.id] for c in communities] + [global_report]
        self.store.prune_community_reports([r.id for r in all_reports])
        return CommunityIndexResult(
            reports=all_reports, community_count=total, regenerated=regenerated
        )

    def _detect(self) -> list[Community]:
        """Choose flat vs hierarchical community detection by corpus size (:class:`RetrievalPolicy`,
        same node-count signal the pipeline uses). Below ``HIERARCHY_MIN_SIZE`` this is exactly the
        previous flat single-level partition. Above it, attempt graspologic-style hierarchical
        Leiden detection; if the Leiden backend is not installed, soft-degrade to the flat one with
        a warning rather than a raw ImportError (the explicit ``OWCOPILOT_COMMUNITY=leiden``
        contract, which does raise, is a different path and is untouched)."""
        node_count = len(build_content_graph(self.bundle).node_refs())
        if not RetrievalPolicy.for_corpus(node_count).use_hierarchical_community:
            return detect_communities(self.bundle)
        try:
            return detect_hierarchical_communities(
                self.bundle,
                max_cluster_size=resolve_max_cluster_size(),
                partition=_leiden_partition,
            )
        except ImportError:
            logger.warning(
                "Hierarchical GraphRAG requested (corpus_size=%d >= HIERARCHY_MIN_SIZE) but the "
                "Leiden backend is unavailable (pip install python-igraph leidenalg); falling back "
                "to a flat single-level community partition.",
                node_count,
            )
            return detect_communities(self.bundle)

    # --- generation ----------------------------------------------------------------------------

    def _build_leaf(
        self,
        community: Community,
        index: int,
        total: int,
        progress: Callable[[str, dict[str, Any]], None] | None,
    ) -> tuple[CommunityReport, bool]:
        """A leaf community's report, cached by its member+content fingerprint. Returns the report
        and whether it was freshly generated (a cache hit returns ``False``)."""
        fingerprint = self._fingerprint(community)
        cached = self.store.get_community_report(community.id, fingerprint)
        if cached is not None:
            return CommunityReport.model_validate(cached), False
        if progress is not None:
            progress("community", {"id": community.id, "index": index + 1, "of": total})
        report = self._generate_community(community, fingerprint)
        self.store.save_community_report(report.model_dump())
        return report, True

    def _build_parent(
        self,
        parent: Community,
        children: list[Community],
        child_reports: list[CommunityReport],
        progress: Callable[[str, dict[str, Any]], None] | None,
    ) -> tuple[CommunityReport, bool]:
        """A parent community's report: synthesised from its children's reports and the ties between
        them (GraphRAG sub-community substitution). Its fingerprint folds in the child fingerprints,
        so a leaf change invalidates the whole ancestor chain while sibling subtrees stay cached."""
        crosses, fingerprint = self._cross_and_fingerprint(children, child_reports)
        cached = self.store.get_community_report(parent.id, fingerprint)
        if cached is not None:
            return CommunityReport.model_validate(cached), False
        if progress is not None:
            progress("community", {"id": parent.id, "hier_level": parent.hier_level})
        title, summary = self._synthesize_prose(child_reports, crosses)
        report = CommunityReport(
            id=parent.id,
            level="community",
            title=title,
            summary=summary,
            member_refs=parent.member_refs,
            fingerprint=fingerprint,
            hier_level=parent.hier_level,
            parent_id=parent.parent_id,
        )
        self.store.save_community_report(report.model_dump())
        return report, True

    def _generate_community(self, community: Community, fingerprint: str) -> CommunityReport:
        user = _community_user_prompt(self.bundle, community)
        title, summary = self._complete_report(_COMMUNITY_SYSTEM, user) or _fallback_community(
            self.bundle, community
        )
        return CommunityReport(
            id=community.id,
            level="community",
            title=title,
            summary=summary,
            member_refs=community.member_refs,
            fingerprint=fingerprint,
            hier_level=community.hier_level,
            parent_id=community.parent_id,
        )

    def _generate_global(
        self, communities: list[Community], reports: list[CommunityReport]
    ) -> CommunityReport:
        crosses, fingerprint = self._cross_and_fingerprint(communities, reports)
        title, summary = self._synthesize_prose(reports, crosses)
        members = [ref for report in reports for ref in report.member_refs]
        return CommunityReport(
            id=GLOBAL_REPORT_ID,
            level="global",
            title=title,
            summary=summary,
            member_refs=members,
            fingerprint=fingerprint,
        )

    def _cross_and_fingerprint(
        self, communities: list[Community], reports: list[CommunityReport]
    ) -> tuple[list[CommunityRelation], str]:
        """The cross-community ties and the synthesis fingerprint shared by every parent report and
        the root global report: keyed on the sub-reports' fingerprints + the ties between them."""
        crosses = cross_community_relations(self.bundle, communities)
        fingerprint = content_hash(
            {
                "communities": [r.fingerprint for r in reports],
                "cross": [f"{c.source}|{c.kind}|{c.target}" for c in crosses],
            }
        )
        return crosses, fingerprint

    def _synthesize_prose(
        self, reports: list[CommunityReport], crosses: list[CommunityRelation]
    ) -> tuple[str, str]:
        """(title, summary) synthesised across a set of sub-reports and the ties between them —
        shared by parent and global reports. Uses the same ``_GLOBAL_SYSTEM`` prompt + "Cluster
        reports:" structure the offline provider already recognises, so the $0 path is unchanged."""
        user = _global_user_prompt(self.bundle, reports, crosses)
        return self._complete_report(_GLOBAL_SYSTEM, user) or _fallback_global(reports)

    def _complete_report(self, system: str, user: str) -> tuple[str, str] | None:
        """Return (title, summary) or ``None`` when the reply can't be parsed (caller degrades to a
        deterministic roster — never a crash, never a fabricated summary)."""
        try:
            raw = self.gateway.complete(task="community_report", system=system, user=user)
            payload = extract_json_object(raw)
            report = _ReportShape.model_validate(payload)
        except Exception:  # noqa: BLE001 - a bad report degrades to a roster, never fails the index
            return None
        title, summary = report.title.strip(), report.summary.strip()
        if not summary:
            return None
        return title or summary[:40], summary

    def _fingerprint(self, community: Community) -> str:
        return content_hash(
            {
                "basis": community.fingerprint_basis(),
                "content": [_member_text(self.bundle, ref) for ref in community.member_refs],
            }
        )


class _ReportShape(BaseModel):
    title: str = ""
    summary: str = ""


# --- prompt assembly + deterministic fallbacks -------------------------------------------------


def _community_user_prompt(bundle: ContentBundle, community: Community) -> str:
    lines = ["Members:"]
    for ref in community.member_refs:
        name, kind, text = _resolve(bundle, ref)
        detail = f": {text}" if text else ""
        lines.append(f"- [{ref}] {name} ({kind}){detail}")
    if community.relations:
        lines.append("Ties:")
        for rel in community.relations:
            src = _resolve(bundle, rel.source)[0]
            tgt = _resolve(bundle, rel.target)[0]
            lines.append(f"- {src} {rel.kind} {tgt}")
    return "\n".join(lines)


def _global_user_prompt(
    bundle: ContentBundle, reports: list[CommunityReport], crosses: list[Any]
) -> str:
    lines = ["Cluster reports:"]
    for report in reports:
        lines.append(f"- {report.title}: {report.summary}")
    if crosses:
        lines.append("Cross-cluster ties:")
        for rel in crosses:
            src = _resolve(bundle, rel.source)[0]
            tgt = _resolve(bundle, rel.target)[0]
            lines.append(f"- {src} {rel.kind} {tgt}")
    return "\n".join(lines)


def _fallback_community(bundle: ContentBundle, community: Community) -> tuple[str, str]:
    names = [_resolve(bundle, ref)[0] for ref in community.member_refs]
    title = f"{names[0]} 等 {len(names)} 个相关对象" if names else community.id
    summary = "该聚类包含：" + "、".join(names[:12]) + ("…" if len(names) > 12 else "") + "。"
    return title, summary


def _fallback_global(reports: list[CommunityReport]) -> tuple[str, str]:
    titles = [r.title for r in reports if r.level == "community"]
    summary = (
        "世界由以下聚类构成：" + "；".join(titles[:12]) + ("…" if len(titles) > 12 else "") + "。"
    )
    return "世界结构概览", summary


def _resolve(bundle: ContentBundle, ref: str) -> tuple[str, str, str]:
    """(display name, kind, short text) for a content ref, for prompts and fallbacks."""
    object_type, _, object_id = ref.partition(":")
    if object_type == "entity" and object_id in bundle.entities:
        e = bundle.entities[object_id]
        return e.name, e.type.value, e.description
    if object_type == "poi" and object_id in bundle.pois:
        p = bundle.pois[object_id]
        return p.name, "poi", p.purpose
    if object_type == "region" and object_id in bundle.regions:
        r = bundle.regions[object_id]
        return r.name, "region", "、".join(r.themes)
    if object_type == "quest" and object_id in bundle.quests:
        q = bundle.quests[object_id]
        return q.title or object_id, "quest", q.objective
    return object_id, object_type, ""


def _member_text(bundle: ContentBundle, ref: str) -> str:
    name, _, text = _resolve(bundle, ref)
    return f"{name}:{text}"
