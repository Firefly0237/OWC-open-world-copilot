"""Community detection over the content graph — the GraphRAG indexing substrate.

Line-level retrieval cannot answer macro/holistic questions ("what are the main powers and how do
they relate?") once a world is large: the relations of every recalled entity overflow the token
budget, so the truly global picture never fits. GraphRAG's answer is to partition the graph into
communities, summarise each (and then summarise across them), and answer the macro question over the
summaries instead of the raw rows.

**Algorithm: greedy modularity maximisation (Clauset–Newman–Moore / CNM)**

``networkx.community.greedy_modularity_communities`` is used.  CNM is a good default for
worlds in the ~50–300 node range typical of this project: it runs in milliseconds, needs no
external dependencies, and produces reasonable faction/region clusters.

**Stability note (important -- do NOT mark this as "DETERMINISTIC")**

CNM's internal priority queue can break ties differently across Python versions or when the
graph has equally-weighted edges.  The *output order* is stabilised by the explicit
``sorted(...)`` calls in ``detect_communities()``: members within each community are sorted
lexicographically, and communities are ordered by (desc size, first member ref).  This
means the community IDs (``c0``, ``c1``, …) and ``member_refs`` lists are **stable for the
same graph structure**, but are not guaranteed bit-for-bit reproducible if tie-breaking in
the CNM heap changes across dependency versions.

For a fully deterministic, academically rigorous alternative, ``leidenalg`` with
``seed=42`` can be used (Leiden algorithm, Traag et al. 2019 -- fixes Louvain's
connectivity-violation problem).  It is available as an opt-in by setting
``OWCOPILOT_COMMUNITY=leiden`` (requires ``pip install python-igraph leidenalg``).
At the 50–300 node scale of current worlds, CNM and Leiden produce equivalent clusters
in practice; Leiden is the better choice for larger or denser graphs.

Only the per-community *prose* is written by an LLM later; the structural partition is
machine-decided and testable, keeping the "stable / auditable" north star intact.
"""

from __future__ import annotations

import logging
import os

import networkx as nx
from pydantic import BaseModel, Field

from ..content.models import ContentBundle
from .index import ContentGraph, build_content_graph

logger = logging.getLogger(__name__)

# The objects a macro "power structure" question is actually about (factions/NPCs are all
# ``entity:`` refs). Dialogue/terms/localisation are leaf detail, not structure, so they stay out of
# the partition — and out of every community report.
_SUBSTANTIVE = {"entity", "poi", "region", "quest"}

# Env knob: OWCOPILOT_COMMUNITY in {cnm (default), leiden}
# leiden requires: pip install python-igraph leidenalg
_COMMUNITY_MODE_ENV = "OWCOPILOT_COMMUNITY"


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
    """Partition the substantive content graph into communities.

    Communities are ordered by size (desc) then by their lowest member ref, and members within each
    are sorted — so ids (``c0``, ``c1`` …) are stable for the same graph structure.

    Algorithm is controlled by ``OWCOPILOT_COMMUNITY`` env var:
    * ``cnm`` (default): greedy modularity maximisation (networkx built-in, no extra deps).
      Tie-breaking is stabilised by the post-sort but is not guaranteed bit-for-bit
      reproducible across Python/networkx versions.
    * ``leiden``: Leiden algorithm (Traag et al. 2019) with fixed seed -- truly
      reproducible; requires ``pip install python-igraph leidenalg``.
    """
    graph = build_content_graph(bundle)
    projection = _projection(graph)
    if projection.number_of_nodes() == 0:
        return []

    mode = os.getenv(_COMMUNITY_MODE_ENV, "cnm").strip().lower()
    if mode == "leiden":
        raw_groups = _leiden_partition(projection)
    else:
        raw = nx.community.greedy_modularity_communities(projection, weight="weight")
        raw_groups = [list(group) for group in raw]
    groups = sorted((sorted(group) for group in raw_groups), key=lambda g: (-len(g), g[0]))

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
    references tie two objects together (a denser tie pulls them into the same community).

    **Relay-node bridging.** Some substantive objects are only ever wired to each other *through* a
    non-substantive intermediary. The clearest case: an event ``entity:evt_*`` reaches its quest
    only via a ``quest_event_ref:*`` relay (``index.py`` builds ref->quest and ref->event edges but
    never a direct event->quest edge). If we kept only edges whose *both* endpoints are substantive,
    the relay would sever that path and every event would drop to degree 0 -- a permanent singleton,
    excluded from every community report even though events (wars, pacts, sieges) are core
    macro-narrative objects.

    The fix collapses each non-substantive intermediary into direct substantive-to-substantive ties:
    for every non-substantive node, we bridge each pair of its substantive neighbours with a
    weighted edge. This is the standard GraphRAG projection move (drop leaf/relay detail, preserve
    the structure it carried) and pulls events into the community of the quests that reference them.

    **Complexity bound (relay bridging is O(Σ kᵢ²)).** Bridging a relay with ``kᵢ`` substantive
    neighbours emits ``C(kᵢ, 2)`` edges, so the total bridge cost is ``Σ_i C(kᵢ, 2)`` over relays —
    quadratic in the *degree of the densest relay*, not in the node count. In current worlds this is
    negligible: relay degrees are tiny (dialogue/qer ≈ 2, localisation = 1), so the bridge does a
    few dozen ``_bump`` calls. The cliff only appears if a single relay fans out to many substantive
    neighbours — e.g. a ``localization:<key>`` shared by N quests (a legitimate localisation
    pattern: ``Quest.localization_keys`` is not deduplicated/unique), so that one relay produces
    ``C(N, 2)`` edges (≈125k for N=500). Not a bug at the 50–300 node scale this project targets,
    but a real scaling consideration: if worlds grow large or develop shared-key hubs, cap a relay's
    neighbour count (skip bridging above a threshold and log a warning) or switch the projection to
    a sparser scheme.
    """
    sub = nx.Graph()
    sub.add_nodes_from(ref for ref in graph.node_refs() if _is_substantive(ref))

    def _bump(source: str, target: str) -> None:
        if source == target or source not in sub or target not in sub:
            return
        weight = sub.get_edge_data(source, target, default={}).get("weight", 0) + 1
        sub.add_edge(source, target, weight=weight)

    # 1. direct substantive<->substantive edges (relation + reference; relation_ref excluded)
    # 2. substantive neighbours of each non-substantive node, collected for relay bridging below.
    relay_neighbours: dict[str, set[str]] = {}
    for edge in graph.edge_refs():
        source, target = edge.source, edge.target
        if source == target:
            continue
        src_sub, tgt_sub = _is_substantive(source), _is_substantive(target)
        if src_sub and tgt_sub:
            _bump(source, target)
            continue
        # one endpoint is a non-substantive relay/leaf: record the substantive endpoint(s) under it
        if not src_sub and tgt_sub:
            relay_neighbours.setdefault(source, set()).add(target)
        elif src_sub and not tgt_sub:
            relay_neighbours.setdefault(target, set()).add(source)

    # bridge: collapse each relay into direct ties between every pair of its substantive neighbours
    for substantive_neighbours in relay_neighbours.values():
        members = sorted(substantive_neighbours)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                _bump(members[i], members[j])
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


def _leiden_partition(g: nx.Graph, seed: int = 42) -> list[list[str]]:
    """Leiden community detection via python-igraph + leidenalg (opt-in).

    Fully reproducible: fixed ``seed`` → same partition every run on any hardware.
    Requires: ``pip install python-igraph leidenalg`` (not in default or semantic extras).

    Raises ImportError with a helpful message if the packages are missing.
    """
    try:
        import igraph as ig  # noqa: PLC0415
        import leidenalg  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "Leiden community detection requires 'python-igraph' and 'leidenalg'. "
            "Install with: pip install python-igraph leidenalg"
        ) from exc

    # Convert networkx Graph → igraph Graph preserving node labels
    ig_graph = ig.Graph.from_networkx(g)
    # edge weights — check if any edge has a "weight" attribute.
    # NOTE: "weight" in <generator> is always False (generators have no __contains__);
    # nx.get_edge_attributes returns {} when no edge carries the attribute.
    if nx.get_edge_attributes(g, "weight"):
        ig_graph.es["weight"] = [g[u][v].get("weight", 1) for u, v in g.edges()]
        partition = leidenalg.find_partition(
            ig_graph,
            leidenalg.ModularityVertexPartition,
            weights="weight",
            seed=seed,
        )
    else:
        partition = leidenalg.find_partition(
            ig_graph,
            leidenalg.ModularityVertexPartition,
            seed=seed,
        )

    # Map igraph vertex ids back to networkx node labels
    node_names: list[str] = ig_graph.vs["_nx_name"]
    return [[node_names[v] for v in community] for community in partition]
