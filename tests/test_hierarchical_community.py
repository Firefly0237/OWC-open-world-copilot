"""Hierarchical (multi-level) GraphRAG community detection + bottom-up report synthesis (T13).

Pins the graspologic-style ``hierarchical_leiden`` re-implementation (Edge et al. 2024): level 0 is
the whole-graph partition, any community over ``max_cluster_size`` is isolated and re-partitioned
one level deeper, and coarse communities contain their finer descendants. The level ≥ 1 layers only
appear once a level-0 community exceeds the threshold, so these tests drive the recursion with a
small ``max_cluster_size`` on a ring-of-cliques world — the classic modularity resolution-limit
topology (Fortunato & Barthélemy 2007), where greedy modularity merges adjacent cliques at the
global level but splits them back apart once a merged community is isolated.

The base partition primitive is a dependency-injection seam: the product hierarchical path injects
Leiden (GraphRAG's canonical choice). These tests inject the real CNM primitive
(``_partition_projection``) that already ships as a core dependency — a substitution of one
production clustering primitive for another to exercise the same production recursion/build code
without the optional Leiden dependency, NOT a mock and not a reimplementation of the clustering.
"""

from __future__ import annotations

import logging
import sys

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.graph import community as community_module
from owcopilot.graph.community import detect_communities, detect_hierarchical_communities
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.qa.community_index import GLOBAL_REPORT_ID, CommunityIndexService
from owcopilot.qa.offline import OfflineCommunityReportProvider
from owcopilot.storage import SQLiteStore

_CNM = community_module._partition_projection


def _ring_of_cliques(k: int, s: int) -> ContentBundle:
    """``k`` cliques of ``s`` entities, wired into a ring by one cross edge per adjacent pair. With
    enough cliques the modularity resolution limit makes greedy modularity merge adjacent cliques
    into one level-0 community that splits back into the individual cliques once isolated."""
    ents: dict[str, Entity] = {}
    rels: list[Relation] = []
    cliques: list[list[str]] = []
    for c in range(k):
        nodes = [f"n{c}_{i}" for i in range(s)]
        cliques.append(nodes)
        for nid in nodes:
            ents[nid] = Entity(id=nid, name=nid, type=EntityType.NPC, description=f"{nid}角色")
        for i in range(s):
            for j in range(i + 1, s):
                rels.append(Relation(source=nodes[i], target=nodes[j], kind="allied_with"))
    for c in range(k):
        rels.append(Relation(source=cliques[c][0], target=cliques[(c + 1) % k][1], kind="borders"))
    return ContentBundle(entities=ents, relations=rels)


def _gateway(provider: object) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"community_report": "cheap"}),
    )


def test_detect_hierarchical_builds_a_multi_level_tree(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "cnm")
    bundle = _ring_of_cliques(10, 3)
    communities = detect_hierarchical_communities(bundle, max_cluster_size=4, partition=_CNM)
    by_id = {c.id: c for c in communities}

    # A real hierarchy formed (not just a flat partition relabelled).
    assert max(c.hier_level for c in communities) >= 1

    # Level 0 is byte-identical to the flat partition (same backend, same ordering).
    flat = detect_communities(bundle)
    level0 = [c for c in communities if c.hier_level == 0]
    assert [c.id for c in level0] == [c.id for c in flat]
    assert [c.member_refs for c in level0] == [c.member_refs for c in flat]

    parents = {c.parent_id for c in communities if c.parent_id is not None}
    for community in communities:
        if community.parent_id is not None:
            parent = by_id[community.parent_id]
            # child links to a real parent, one level shallower, id encodes the parent chain
            assert parent.hier_level == community.hier_level - 1
            assert community.id.startswith(f"{community.parent_id}.")
        children = [c for c in communities if c.parent_id == community.id]
        if children:
            # a parent's members are exactly the union of its descendants' members (provenance)
            union = sorted({ref for c in children for ref in c.member_refs})
            assert community.member_refs == union
            # a parent carries no ties of its own — its report synthesises its children's reports
            assert community.relations == []

    # Leaves partition the whole node set and are where intra-community ties live.
    leaves = [c for c in communities if c.id not in parents]
    leaf_members = [ref for c in leaves for ref in c.member_refs]
    assert sorted(leaf_members) == sorted({ref for c in flat for ref in c.member_refs})
    assert len(leaf_members) == len(set(leaf_members))  # disjoint
    assert any(c.relations for c in leaves)  # a clique-leaf keeps its internal ties


def test_hierarchical_index_build_synthesises_parents_and_cascades_cache(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "cnm")
    monkeypatch.setenv("OWCOPILOT_COMMUNITY_MAX_CLUSTER_SIZE", "4")
    monkeypatch.setattr("owcopilot.retrieval.policy.HIERARCHY_MIN_SIZE", 0)
    # Drive the production recursion + bottom-up build with the real CNM primitive in place of the
    # (uninstalled) Leiden backend the product path injects — DI of one production clustering
    # primitive for another (approved seam), not a mock.
    monkeypatch.setattr("owcopilot.qa.community_index._leiden_partition", _CNM)

    bundle = _ring_of_cliques(10, 3)
    store = SQLiteStore(":memory:")
    service = CommunityIndexService(
        gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=bundle
    )
    first = service.build()
    by_id = {r.id: r for r in first.reports}

    parents = [r for r in first.reports if r.level == "community" and r.hier_level == 0]
    leaves = [r for r in first.reports if r.level == "community" and r.hier_level == 1]
    assert parents and leaves  # both layers were reported on
    assert all(r.parent_id is None for r in parents)
    # every leaf rolls up to a real level-0 parent report (sub-community substitution wired through)
    assert all(by_id[r.parent_id].hier_level == 0 for r in leaves)
    assert any(r.id == GLOBAL_REPORT_ID for r in first.reports)
    # even parent/global reports get a non-empty deterministic $0 summary (offline provider)
    assert all(r.summary for r in first.reports)
    assert first.regenerated == len(first.reports)  # first pass: everything fresh

    # Re-index the unchanged world → every report is a fingerprint cache hit.
    assert service.build().regenerated == 0

    # Change one leaf's member text → that leaf + its ancestor chain (its parent, then the global
    # synthesis) regenerate, while every sibling subtree stays cached.
    changed_ref = leaves[0].member_refs[0].split(":", 1)[1]
    bundle.entities[changed_ref].description = "改写后的角色设定，与此前完全不同。"
    third = service.build()
    assert 0 < third.regenerated < len(first.reports)
    store.close()


def test_hierarchical_build_soft_degrades_to_flat_without_leiden(monkeypatch, caplog) -> None:
    """When the policy wants the hierarchy but the Leiden backend is unavailable, the build must
    fall back to the flat single-level partition with a warning — never a raw ImportError."""
    monkeypatch.setenv("OWCOPILOT_COMMUNITY_MAX_CLUSTER_SIZE", "4")
    monkeypatch.setattr("owcopilot.retrieval.policy.HIERARCHY_MIN_SIZE", 0)
    # Force the real Leiden import to fail regardless of what is installed on this machine.
    monkeypatch.setitem(sys.modules, "leidenalg", None)
    monkeypatch.setitem(sys.modules, "igraph", None)

    bundle = _ring_of_cliques(10, 3)
    store = SQLiteStore(":memory:")
    with caplog.at_level(logging.WARNING):
        result = CommunityIndexService(
            gateway=_gateway(OfflineCommunityReportProvider()), store=store, bundle=bundle
        ).build()
    # Flat fallback: every report is a level-0 community with no parent, and nothing crashed.
    assert all(r.hier_level == 0 and r.parent_id is None for r in result.reports)
    assert any("falling back" in rec.getMessage() for rec in caplog.records)
    store.close()
