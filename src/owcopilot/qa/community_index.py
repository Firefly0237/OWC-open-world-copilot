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

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..graph.community import Community, cross_community_relations, detect_communities
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..storage import SQLiteStore

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
    level: str  # "community" | "global"
    title: str
    summary: str
    member_refs: list[str] = Field(default_factory=list)
    fingerprint: str = ""


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
        communities = detect_communities(self.bundle)
        reports: list[CommunityReport] = []
        regenerated = 0
        for index, community in enumerate(communities):
            fingerprint = self._fingerprint(community)
            cached = self.store.get_community_report(community.id, fingerprint)
            if cached is not None:
                reports.append(CommunityReport.model_validate(cached))
                continue
            if progress is not None:
                progress(
                    "community", {"id": community.id, "index": index + 1, "of": len(communities)}
                )
            report = self._generate_community(community, fingerprint)
            self.store.save_community_report(report.model_dump())
            reports.append(report)
            regenerated += 1

        global_report = self._generate_global(communities, reports)
        cached_global = self.store.get_community_report(GLOBAL_REPORT_ID, global_report.fingerprint)
        if cached_global is None:
            if progress is not None:
                progress("global", {"id": GLOBAL_REPORT_ID})
            self.store.save_community_report(global_report.model_dump())
            regenerated += 1
        else:
            global_report = CommunityReport.model_validate(cached_global)

        all_reports = reports + [global_report]
        self.store.prune_community_reports([r.id for r in all_reports])
        return CommunityIndexResult(
            reports=all_reports, community_count=len(communities), regenerated=regenerated
        )

    # --- generation ----------------------------------------------------------------------------

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
        )

    def _generate_global(
        self, communities: list[Community], reports: list[CommunityReport]
    ) -> CommunityReport:
        crosses = cross_community_relations(self.bundle, communities)
        user = _global_user_prompt(self.bundle, reports, crosses)
        fingerprint = content_hash(
            {
                "communities": [r.fingerprint for r in reports],
                "cross": [f"{c.source}|{c.kind}|{c.target}" for c in crosses],
            }
        )
        title, summary = self._complete_report(_GLOBAL_SYSTEM, user) or _fallback_global(reports)
        members = [ref for report in reports for ref in report.member_refs]
        return CommunityReport(
            id=GLOBAL_REPORT_ID,
            level="global",
            title=title,
            summary=summary,
            member_refs=members,
            fingerprint=fingerprint,
        )

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
