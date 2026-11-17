"""GraphRAG macro-overview: deterministic community detection + cached LLM reports + holistic QA.

Pins the S4 guardrails the design promised inside the GraphRAG choice: a deterministic, reproducible
partition; provenance on every report; fingerprint caching that only re-summarises changed clusters;
honest degradation on a bad model reply; and the rerank-driven routing (holistic → reports, specific
→ rows) with no brittle classifier.
"""

from __future__ import annotations

from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    QuestEventReference,
    QuestEventRefKind,
    Relation,
)
from owcopilot.graph.community import cross_community_relations, detect_communities
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.qa.community_index import GLOBAL_REPORT_ID, CommunityIndexService
from owcopilot.qa.offline import OfflineCommunityReportProvider
from owcopilot.retrieval.community_reports import CommunityReportRetriever
from owcopilot.retrieval.text_match import lexical_score
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


def test_event_entity_joins_its_quest_community_through_the_relay_node() -> None:
    """Regression: an event entity reaches its quest only via a ``quest_event_ref`` relay node.

    The relay is non-substantive, so a naive substantive-only projection severs the
    ``event ↔ ref ↔ quest`` path and the event drops to degree 0 — a permanent singleton excluded
    from every community report. The projection now bridges through the relay, so the event lands in
    the same community as the quest that references it.
    """
    bundle = ContentBundle(
        entities={
            "fac_iron": Entity(
                id="fac_iron", name="铁盟", type=EntityType.FACTION, description="势力"
            ),
            "npc_a": Entity(id="npc_a", name="阿尔", type=EntityType.NPC, description="角色"),
            "evt_siege": Entity(
                id="evt_siege", name="铁壁围城", type=EntityType.EVENT, description="一场战争"
            ),
        },
        quests={
            "quest_war": Quest(id="quest_war", title="围城余烬", giver_npc="npc_a"),
        },
        quest_event_refs={
            "qer_1": QuestEventReference(
                id="qer_1",
                quest_id="quest_war",
                event_id="evt_siege",
                ref_kind=QuestEventRefKind.MENTIONS_EVENT,
            ),
        },
        relations=[Relation(source="npc_a", target="fac_iron", kind="member_of")],
    )
    communities = detect_communities(bundle)
    # the event is no longer a singleton...
    home = {ref: c for c in communities for ref in c.member_refs}
    assert "entity:evt_siege" in home, "event entity must be assigned to a community, not dropped"
    event_community = home["entity:evt_siege"]
    assert len(event_community.member_refs) > 1, "event must not be a degree-0 singleton community"
    # ...and it sits with the quest that references it (the relay bridge worked)
    assert "quest:quest_war" in event_community.member_refs


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


def test_flat_build_is_byte_identical_after_adding_hierarchy_fields() -> None:
    """Backward compat: below ``HIERARCHY_MIN_SIZE`` the build keeps the flat single-level
    partition, so the new ``hier_level``/``parent_id`` fields never perturb the existing output —
    every report is level 0 with no parent, exactly as before T13."""
    store = SQLiteStore(":memory:")
    result = CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=_world()
    ).build()
    assert result.community_count == 3
    shape = {(r.id, r.level, r.hier_level, r.parent_id) for r in result.reports}
    assert shape == {
        ("c0", "community", 0, None),
        ("c1", "community", 0, None),
        ("c2", "community", 0, None),
        (GLOBAL_REPORT_ID, "global", 0, None),
    }
    store.close()


def test_flat_detection_leaves_hierarchy_fields_at_defaults() -> None:
    """The new optional fields don't disturb the flat partition: every community is level 0 with no
    parent, and the ids/members are unchanged from the pre-T13 detection."""
    communities = detect_communities(_world())
    assert all(c.hier_level == 0 and c.parent_id is None for c in communities)
    assert [c.id for c in communities] == ["c0", "c1", "c2"]


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


def test_holistic_question_grounds_on_reports_specific_on_rows(tmp_path, monkeypatch) -> None:
    from owcopilot.app.actions import run_ask_action, run_build_overview_action
    from owcopilot.content.store import ContentStore

    # Feature test on a tiny world: open the RetrievalPolicy size-gate so GraphRAG community
    # reports are used (they would be omitted at this small scale by default).
    monkeypatch.setattr("owcopilot.retrieval.policy.GRAPHRAG_MIN_SIZE", 0)
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


# --- T12: DRIFT-style dense recall over community-report embeddings -----------------------------


class _AxisEmbedder:
    """A real-model stand-in (model_id ``st:*``) that maps the "overall power" topic to one axis
    and everything else to another, so a macro query lands a dense hit on a report whose wording it
    shares no term with — the lexical leg would score that report 0. Hand-crafted fixed vectors are
    the sanctioned way to demonstrate semantic bridging deterministically offline (see
    ``tests/platform/test_cache.py:_StubSemanticEmbedder``)."""

    model_id = "st:stub"

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [
            [1.0, 0.0] if ("opposes" in t or "overall power" in t) else [0.0, 1.0] for t in texts
        ]


def _seed_reports(store: SQLiteStore) -> None:
    """Four community reports + a global synthesis, saved via the production writer.

    ``c_target`` shares no query term with "overall power structure" (lexical_score 0), while
    ``c_power`` matches on "power" — so the lexical leg surfaces c_power and misses c_target."""
    reports = [
        {
            "id": "c_power",
            "level": "community",
            "title": "Power Grid",
            "summary": "the power lines hum",
            "member_refs": ["entity:x"],
            "fingerprint": "f1",
        },
        {
            "id": "c_target",
            "level": "community",
            "title": "Iron Pact",
            "summary": "the iron guild opposes the salt league",
            "member_refs": ["entity:iron", "entity:salt"],
            "fingerprint": "f2",
        },
        {
            "id": "c_a1",
            "level": "community",
            "title": "Alpha",
            "summary": "nothing relevant here",
            "member_refs": [],
            "fingerprint": "f3",
        },
        {
            "id": "c_a2",
            "level": "community",
            "title": "Beta",
            "summary": "unrelated content",
            "member_refs": [],
            "fingerprint": "f4",
        },
        {
            "id": GLOBAL_REPORT_ID,
            "level": "global",
            "title": "World Overview",
            "summary": "the whole world map",
            "member_refs": ["entity:x", "entity:iron"],
            "fingerprint": "fg",
        },
    ]
    for report in reports:
        store.save_community_report(report)


_MACRO_QUERY = "overall power structure"


def test_no_embedder_path_stays_the_lexical_only_ranking() -> None:
    # Backward compat: with no embedder the retriever is byte-for-byte the pre-T12 lexical ranking.
    store = SQLiteStore(":memory:")
    _seed_reports(store)
    # c_target is a genuine lexical miss — established via the production scorer, not by assertion.
    assert (
        lexical_score(
            _MACRO_QUERY, ("c_target", "Iron Pact", "the iron guild opposes the salt league")
        )
        == 0.0
    )
    lexical_only = CommunityReportRetriever(store)  # no embedder
    refs = [h.ref for h in lexical_only.search(_MACRO_QUERY, limit=3)]
    # c_power (matches "power") ranks first; the rest score 0 and order by id, so the top-3 is
    # exactly this — and c_target is NOT in it.
    assert refs == ["community:c_power", "community:_global", "community:c_a1"]
    assert "community:c_target" not in refs
    store.close()


def test_dense_leg_surfaces_a_report_the_lexical_leg_misses() -> None:
    # The T12 win: a macro query semantically about c_target (but sharing no term with it) reaches
    # c_target through the DRIFT-style dense leg, fused with lexical via RRF.
    store = SQLiteStore(":memory:")
    _seed_reports(store)
    dense = CommunityReportRetriever(store, embedder=_AxisEmbedder())
    refs = [h.ref for h in dense.search(_MACRO_QUERY, limit=3)]
    assert "community:c_target" in refs  # dense recall lifted the lexically-invisible report in
    assert "community:c_power" in refs  # the lexical hit is still fused in, not replaced
    assert f"community:{GLOBAL_REPORT_ID}" in refs  # global synthesis stays reachable
    # the returned hit still carries full report provenance (member_refs), same as the lexical path
    target = next(h for h in dense.search(_MACRO_QUERY, limit=3) if h.ref == "community:c_target")
    assert target.metadata["member_refs"] == "entity:iron,entity:salt"
    assert target.object_type == "community_report"
    # the dense leg persisted its embeddings into the third vector corpus (whitelist round-trip)
    assert store.get_vectors("st:stub", table="community_vectors") != {}
    store.close()


def test_dense_retriever_is_still_a_noop_until_an_index_is_built() -> None:
    # The no-op guard holds with an embedder too: no reports → no hits, no embedding work.
    store = SQLiteStore(":memory:")
    assert CommunityReportRetriever(store, embedder=_AxisEmbedder()).search("anything") == []
    store.close()
