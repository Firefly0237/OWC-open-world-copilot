"""GraphRAG macro-overview: deterministic community detection + cached LLM reports + holistic QA.

Pins the S4 guardrails the design promised inside the GraphRAG choice: a deterministic, reproducible
partition; provenance on every report; fingerprint caching that only re-summarises changed clusters;
honest degradation on a bad model reply; and the rerank-driven routing (holistic → reports, specific
→ rows) with no brittle classifier.
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.graph.community import cross_community_relations, detect_communities
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.qa.community_index import GLOBAL_REPORT_ID, CommunityIndexService
from owcopilot.qa.offline import OfflineCommunityReportProvider
from owcopilot.retrieval.community_reports import CommunityReportRetriever
from owcopilot.storage import SQLiteStore


def _world() -> ContentBundle:
    ents = {}

    def npc(i: str, n: str) -> None:
        ents[i] = Entity(id=i, name=n, type=EntityType.NPC, description=f"{n}是一名角色")

    def fac(i: str, n: str) -> None:
        ents[i] = Entity(id=i, name=n, type=EntityType.FACTION, description=f"{n}是一个势力")

    fac("fac_iron", "铁盟")
    fac("fac_salt", "盐会")
    fac("fac_mist", "雾党")
    npc("npc_a", "阿尔")
    npc("npc_b", "贝拉")
    npc("npc_c", "卡尔")
    npc("npc_d", "黛西")
    npc("npc_e", "俄岚")
    rels = [
        Relation(source="npc_a", target="fac_iron", kind="member_of"),
        Relation(source="npc_b", target="fac_iron", kind="member_of"),
        Relation(source="npc_c", target="fac_salt", kind="member_of"),
        Relation(source="npc_d", target="fac_salt", kind="member_of"),
        Relation(source="npc_e", target="fac_mist", kind="member_of"),
        Relation(source="fac_iron", target="fac_salt", kind="enemy_of"),
        Relation(source="fac_salt", target="fac_mist", kind="rival_of"),
    ]
    return ContentBundle(entities=ents, relations=rels)


def _gateway(provider: object) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},  # type: ignore[dict-item]
        router=StaticRouter(mapping={"community_report": "cheap"}),
    )


def test_detection_clusters_factions_with_members_and_is_reproducible() -> None:
    bundle = _world()
    communities = detect_communities(bundle)
    assert len(communities) == 3  # one per faction-and-its-cast
    # every faction sits with its own members
    by_members = {frozenset(c.member_refs) for c in communities}
    assert frozenset({"entity:fac_iron", "entity:npc_a", "entity:npc_b"}) in by_members
    # the cross-faction tensions go to the global layer, not inside a community
    crosses = {(c.source, c.kind, c.target) for c in cross_community_relations(bundle, communities)}
    assert ("entity:fac_iron", "enemy_of", "entity:fac_salt") in crosses
    # deterministic: same world → same partition + same ids
    again = detect_communities(bundle)
    assert [c.id for c in communities] == [c.id for c in again]
    assert [c.member_refs for c in communities] == [c.member_refs for c in again]


def test_build_produces_reports_with_provenance_and_a_global_layer() -> None:
    store = SQLiteStore(":memory:")
    result = CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=_world()
    ).build()
    assert result.community_count == 3
    levels = {r.level for r in result.reports}
    assert levels == {"community", "global"}
    glob = next(r for r in result.reports if r.id == GLOBAL_REPORT_ID)
    assert glob.member_refs  # provenance: the global report traces back to canon ids
    for report in result.reports:
        if report.level == "community":
            assert report.member_refs  # every report carries the ids it summarises
    store.close()


def test_fingerprint_cache_only_regenerates_changed_clusters() -> None:
    store = SQLiteStore(":memory:")
    bundle = _world()
    first = CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=bundle
    ).build()
    assert first.regenerated == 4  # 3 communities + global, all fresh

    # re-index the unchanged world → every report is a fingerprint cache hit
    again = CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=bundle
    ).build()
    assert again.regenerated == 0

    # change one member's text → that community's fingerprint changes, so it (and the global
    # synthesis above it) regenerate, while the untouched clusters stay cached
    bundle.entities["npc_a"].description = "阿尔如今改换门庭，倒戈盐会。"
    third = CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=bundle
    ).build()
    assert 0 < third.regenerated < 4
    store.close()


def test_unparsable_report_degrades_to_a_roster_never_crashes() -> None:
    class GarbageProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            return "not json at all <<<", 1, 1

    store = SQLiteStore(":memory:")
    result = CommunityIndexService(
        gateway=_gateway(GarbageProvider()), store=store, bundle=_world()
    ).build()
    # no crash; each report still has a non-empty summary built deterministically from its members
    assert all(r.summary for r in result.reports)
    assert all(r.member_refs for r in result.reports if r.level == "community")
    store.close()


def test_holistic_question_grounds_on_reports_specific_on_rows(tmp_path) -> None:
    from owcopilot.app.actions import run_ask_action, run_build_overview_action
    from owcopilot.content.store import ContentStore

    root = tmp_path / "content"
    ContentStore(root).save(_world())
    db = str(tmp_path / "rt.sqlite")
    run_build_overview_action(root, sqlite_path=db, llm_mode="offline")

    holistic = run_ask_action(
        root, query="列出这个世界的主要势力以及它们之间的关系", sqlite_path=db, llm_mode="offline"
    )["answer"]
    assert not holistic["refused"]
    # the macro question reaches the community/global overview reports
    assert any(c["ref"].startswith("community:") for c in holistic["citations"])

    specific = run_ask_action(root, query="阿尔是谁", sqlite_path=db, llm_mode="offline")["answer"]
    # a specific question still grounds on the entity row (reports don't crowd it out)
    assert any(c["ref"] == "entity:npc_a" for c in specific["citations"])


def test_retriever_is_a_noop_until_an_index_is_built() -> None:
    store = SQLiteStore(":memory:")
    retriever = CommunityReportRetriever(store)
    assert retriever.search("anything") == []
    CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=_world()
    ).build()
    hits = retriever.search("主要势力 关系")
    assert hits  # reports now surface as candidate hits
    assert all(h.ref.startswith("community:") for h in hits)
    # the whole-world synthesis is always reachable for an abstract macro question
    assert any(h.ref == f"community:{GLOBAL_REPORT_ID}" for h in hits)
    store.close()
