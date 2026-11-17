# 内容图上的社区检测：GraphRAG 索引的底座。
"""Community detection over the substantive content graph.

The default backend uses ``networkx.community.greedy_modularity_communities``. An optional
Leiden backend uses a fixed seed and requires ``python-igraph`` and ``leidenalg``.
Community members and output groups are sorted after partitioning to assign readable IDs;
this does not guarantee identical partitions across dependency versions or tie-breaking changes.

Hierarchical detection partitions the whole graph, then recursively partitions oversized
communities in their induced subgraphs. It stops when a group meets ``max_cluster_size``
or cannot be split. Parents and leaves retain their hierarchy; only leaves carry their
intra-community relations. Textual reports are generated separately from this structural pass.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

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

# Env knob: the hierarchical subdivision threshold. A level-0 community with more members than this
# is isolated and re-partitioned one level deeper. Default 1000 matches graspologic's
# ``hierarchical_leiden`` signature default (``max_cluster_size=1000``). NOTE: Microsoft GraphRAG's
# own pipeline config defaults far finer (``ClusterGraphDefaults.max_cluster_size = 10`` — it pays
# an LLM summarisation call per community, so it wants tiny clusters); we summarise only final
# clusters and gate the hierarchy on corpus size, so the graspologic default is the honest match.
# Boundary difference vs graspologic (deliberate): graspologic isolates clusters with size
# ``>= max_cluster_size``; we keep ``len(members) <= max_cluster_size`` as leaves, i.e. subdivide
# only strictly-greater. The off-by-one matters only for a cluster of exactly the threshold size,
# where we take the cheaper "don't split" side.
_MAX_CLUSTER_SIZE_ENV = "OWCOPILOT_COMMUNITY_MAX_CLUSTER_SIZE"
DEFAULT_MAX_CLUSTER_SIZE = 1000


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

    # Hierarchical GraphRAG (opt-in, large corpora): the level this community was resolved at
    # (0 = the coarsest whole-graph partition) and the id of the parent community it was subdivided
    # from (None at level 0). Flat single-level detection leaves both at their defaults, so existing
    # construction sites and ``fingerprint_basis()`` are byte-for-byte unchanged.
    hier_level: int = 0
    parent_id: str | None = None

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
    * ``leiden``: optional fixed-seed partition; requires
      ``pip install python-igraph leidenalg``. Dependency changes may affect results.
    """
    graph = build_content_graph(bundle)
    projection = _projection(graph)
    if projection.number_of_nodes() == 0:
        return []

    groups = _sorted_partition(projection, _partition_projection)

    member_to_community: dict[str, int] = {}
    for index, members in enumerate(groups):
        for ref in members:
            member_to_community[ref] = index

    communities = [
        Community(id=f"c{index}", member_refs=members) for index, members in enumerate(groups)
    ]
    _attach_intra_relations(graph, communities, member_to_community)
    return communities


def resolve_max_cluster_size() -> int:
    """The hierarchical subdivision threshold from ``OWCOPILOT_COMMUNITY_MAX_CLUSTER_SIZE`` (default
    1000 — graspologic ``hierarchical_leiden``'s signature default, NOT Microsoft GraphRAG's
    pipeline-config default of 10; see the env-knob comment for why). A malformed or non-positive
    value falls back to the default."""
    raw = os.getenv(_MAX_CLUSTER_SIZE_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_CLUSTER_SIZE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CLUSTER_SIZE
    return value if value > 0 else DEFAULT_MAX_CLUSTER_SIZE


def detect_hierarchical_communities(
    bundle: ContentBundle,
    *,
    max_cluster_size: int,
    partition: Callable[[nx.Graph], list[list[str]]] | None = None,
) -> list[Community]:
    """Recursively partition oversized communities using one selected primitive.

    Level 0 is the base partition of the whole projection (ids ``c0``, ``c1``, … exactly as
    :func:`detect_communities` orders them). Any community whose member count exceeds
    ``max_cluster_size`` is isolated into its induced sub-graph and re-partitioned with the SAME
    base primitive; its groups become children one level deeper (``parent_id`` links them, ids
    extend as ``{parent}.{k}``). This recurses until no community is oversized or one can no longer
    be subdivided (its isolated re-partition yields a single group — graspologic's
    ``is_final_cluster``). A coarser (lower-level) community therefore contains its finer
    descendants, and its ``member_refs`` is exactly the union of them.

    ``partition`` is the base community-detection primitive re-run at every level (dependency
    injection): the product hierarchical path injects Leiden (GraphRAG's canonical choice); ``None``
    falls back to the ``OWCOPILOT_COMMUNITY``-selected backend. The returned list holds every
    community at every level (parents and leaves); only leaves carry intra-community relations —
    a parent's report is synthesised from its children's reports, not from raw ties.
    """
    graph = build_content_graph(bundle)
    projection = _projection(graph)
    if projection.number_of_nodes() == 0:
        return []
    run_partition = partition if partition is not None else _partition_projection

    communities: list[Community] = []
    leaf_ids: set[str] = set()

    def _emit(members: list[str], community_id: str, level: int, parent_id: str | None) -> None:
        communities.append(
            Community(id=community_id, member_refs=members, hier_level=level, parent_id=parent_id)
        )
        if len(members) <= max_cluster_size:
            leaf_ids.add(community_id)
            return
        child_groups = _sorted_partition(projection.subgraph(members).copy(), run_partition)
        if len(child_groups) <= 1:
            # Oversized but indivisible: re-partitioning the isolated sub-graph can't split it, so
            # it stays a leaf (graspologic's ``is_final_cluster``). Recursion also terminates here
            # because every deeper level operates on a strictly smaller node set.
            leaf_ids.add(community_id)
            return
        for index, child_members in enumerate(child_groups):
            _emit(child_members, f"{community_id}.{index}", level + 1, community_id)

    for index, members in enumerate(_sorted_partition(projection, run_partition)):
        _emit(members, f"c{index}", 0, None)

    # Attach intra-community relations to the LEAF communities only: every projection node lands in
    # exactly one leaf, so mapping each ref to its leaf's index buckets the ties correctly and
    # leaves every parent's bucket empty (a parent's report synthesises its children, not raw ties).
    leaf_index_by_ref: dict[str, int] = {}
    for idx, community in enumerate(communities):
        if community.id in leaf_ids:
            for ref in community.member_refs:
                leaf_index_by_ref[ref] = idx
    _attach_intra_relations(graph, communities, leaf_index_by_ref)
    return communities


def _partition_projection(projection: nx.Graph) -> list[list[str]]:
    """Run the ``OWCOPILOT_COMMUNITY``-selected base backend on a projection, returning raw
    (unsorted) member-ref groups. This is the single base primitive both the flat and the
    hierarchical detector run — the latter re-runs it on each oversized sub-network (mirroring
    graspologic's ``hierarchical_leiden``, which re-runs the Leiden primitive per sub-network)."""
    mode = os.getenv(_COMMUNITY_MODE_ENV, "cnm").strip().lower()
    if mode == "leiden":
        return _leiden_partition(projection)
    raw = nx.community.greedy_modularity_communities(projection, weight="weight")
    return [list(group) for group in raw]


def _sorted_partition(
    projection: nx.Graph, partition: Callable[[nx.Graph], list[list[str]]]
) -> list[list[str]]:
    """Partition ``projection`` and apply the stable ordering (members sorted; groups by desc size
    then lowest member ref) that makes the ``c{index}`` ids stable for the same graph structure."""
    return sorted((sorted(group) for group in partition(projection)), key=lambda g: (-len(g), g[0]))


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
