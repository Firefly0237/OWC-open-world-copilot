"""Deterministic community detection over the content graph — the GraphRAG indexing substrate.

Line-level retrieval cannot answer macro/holistic questions ("what are the main powers and how do
they relate?") once a world is large: the relations of every recalled entity overflow the token
budget, so the truly global picture never fits. GraphRAG's answer is to partition the graph into
communities, summarise each (and then summarise across them), and answer the macro question over the
summaries instead of the raw rows.

The partition here is DETERMINISTIC — greedy modularity maximisation (Clauset–Newman–Moore), no
RNG — so the index is reproducible run to run and therefore cache-keyable by fingerprint. Only
the per-community *prose* is written by an LLM later; the structure is machine-decided and testable,
keeping the "stable / auditable" north star intact even inside the GraphRAG choice.
"""

from __future__ import annotations

import networkx as nx
from pydantic import BaseModel, Field

from ..content.models import ContentBundle
from .index import ContentGraph, build_content_graph

# The objects a macro "power structure" question is actually about (factions/NPCs are all
# ``entity:`` refs). Dialogue/terms/localisation are leaf detail, not structure, so they stay out of
# the partition — and out of every community report.
_SUBSTANTIVE = {"entity", "poi", "region", "quest"}


class CommunityRelation(BaseModel):
    source: str
    kind: str
    target: str


class Community(BaseModel):
    """One detected cluster: its member object refs and the relations wholly inside it.

    ``member_refs`` and ``relations`` are the provenance a report must cite — a community summary is
    only trustworthy if it traces back to these exact canon ids.
    """

    id: str
    member_refs: list[str] = Field(default_factory=list)
    relations: list[CommunityRelation] = Field(default_factory=list)

    def fingerprint_basis(self) -> list[str]:
        """The id-level basis for a cache key: members + intra-community edges (content hashed
        separately by the caller). Two detections of the same structure produce the same basis."""
        rels = sorted(f"{r.source}|{r.kind}|{r.target}" for r in self.relations)
        return sorted(self.member_refs) + rels


def detect_communities(bundle: ContentBundle) -> list[Community]:
    """Partition the substantive content graph into communities, deterministically.

    Communities are ordered by size (desc) then by their lowest member ref, and members within each
    are sorted — so ids (``c0``, ``c1`` …) are stable for the same world.
    """
    graph = build_content_graph(bundle)
    projection = _projection(graph)
    if projection.number_of_nodes() == 0:
        return []

    raw = nx.community.greedy_modularity_communities(projection, weight="weight")
    groups = sorted((sorted(group) for group in raw), key=lambda g: (-len(g), g[0]))

    member_to_community: dict[str, int] = {}
    for index, members in enumerate(groups):
        for ref in members:
            member_to_community[ref] = index

    communities = [
        Community(id=f"c{index}", member_refs=members) for index, members in enumerate(groups)
    ]
    _attach_intra_relations(graph, communities, member_to_community)
    return communities


def cross_community_relations(
    bundle: ContentBundle, communities: list[Community]
) -> list[CommunityRelation]:
    """Relations whose endpoints fall in *different* communities — the inter-cluster tensions the
    global synthesis layer summarises (a community report only sees what is inside it)."""
    graph = build_content_graph(bundle)
    home = {ref: c.id for c in communities for ref in c.member_refs}
    seen: set[tuple[str, str, str]] = set()
    out: list[CommunityRelation] = []
    for edge in graph.edge_refs(edge_type="relation"):
        src_home, tgt_home = home.get(edge.source), home.get(edge.target)
        if src_home is None or tgt_home is None or src_home == tgt_home:
            continue
        key = (edge.source, edge.kind, edge.target)
        if key in seen:
            continue
        seen.add(key)
        out.append(CommunityRelation(source=edge.source, kind=edge.kind, target=edge.target))
    out.sort(key=lambda r: (r.source, r.kind, r.target))
    return out


def _projection(graph: ContentGraph) -> nx.Graph:
    """Undirected, weighted projection over substantive nodes; edge weight = how many relations /
    references tie two objects together (a denser tie pulls them into the same community)."""
    sub = nx.Graph()
    sub.add_nodes_from(ref for ref in graph.node_refs() if _is_substantive(ref))
    for edge in graph.edge_refs():  # relation + reference edges (relation_ref bookkeeping excluded)
        source, target = edge.source, edge.target
        if source == target or source not in sub or target not in sub:
            continue
        weight = sub.get_edge_data(source, target, default={}).get("weight", 0) + 1
        sub.add_edge(source, target, weight=weight)
    return sub


def _attach_intra_relations(
    graph: ContentGraph, communities: list[Community], home: dict[str, int]
) -> None:
    buckets: list[set[tuple[str, str, str]]] = [set() for _ in communities]
    for edge in graph.edge_refs(edge_type="relation"):
        src_home, tgt_home = home.get(edge.source), home.get(edge.target)
        if src_home is not None and src_home == tgt_home:
            buckets[src_home].add((edge.source, edge.kind, edge.target))
    for community, bucket in zip(communities, buckets, strict=True):
        community.relations = [
            CommunityRelation(source=s, kind=k, target=t) for s, k, t in sorted(bucket)
        ]


def _is_substantive(ref: str) -> bool:
    return ref.split(":", 1)[0] in _SUBSTANTIVE
