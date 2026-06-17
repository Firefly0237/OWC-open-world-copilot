"""Relationship graph view: an ego subgraph around one focus, laid out for the SVG renderer.

The content graph already knows everything — every object is a node, relations and derived
references are typed edges. This view picks the focus's neighbourhood (``ego_distances``), lays it
on concentric rings (:func:`concentric_layout`), and labels each node from the bundle. Optionally it
runs the SAME deterministic :class:`ImpactAnalyzer` the impact page uses, so a planner can ask "if I
change this, what moves?" and see the ripple coloured in — a differentiator no wiki graph has.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import ContentBundle
from ..content.relation_kinds import is_symmetric_kind
from ..impact.analyzer import ImpactAnalyzer
from ..impact.models import Change, ChangeSet, ChangeType
from .index import ContentGraph, build_content_graph
from .layout import concentric_layout


class GraphNode(BaseModel):
    ref: str
    kind: str  # npc | faction | location | region | event | quest | poi | dialogue | term
    label: str
    sublabel: str = ""  # secondary line under the label (e.g. the dialogue node's speaker)
    x: float
    y: float
    focus: bool = False
    flag: str = ""  # impact level when an overlay is requested: must_change | suggest_check


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: str = ""
    label: str = ""  # used by the dialogue flow (choice text); the relationship graph uses kind
    symmetric: bool = False  # peer/undirected relation — the renderer draws it without an arrow


class GraphView(BaseModel):
    focus: str
    radius: int
    overview: bool = False
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


def build_graph_view(
    bundle: ContentBundle,
    *,
    focus_ref: str,
    radius: int = 1,
    kinds: set[str] | None = None,
    impact: bool = False,
) -> GraphView:
    graph = build_content_graph(bundle)
    if not graph.has_node(focus_ref):
        return GraphView(focus=focus_ref, radius=radius)

    distances = graph.ego_distances(focus_ref, radius=radius, kinds=kinds)
    visible = set(distances)
    positions = _apply_overrides(
        bundle, concentric_layout(distances, center=(300.0, 220.0), ring_gap=120.0)
    )
    flags = _impact_levels(bundle, focus_ref) if impact else {}

    nodes = _build_nodes(bundle, sorted(visible), positions, focus_ref=focus_ref, flags=flags)
    edges = _build_edges(graph, visible, bundle)
    return GraphView(focus=focus_ref, radius=radius, nodes=nodes, edges=edges)


def build_graph_overview(bundle: ContentBundle) -> GraphView:
    """The whole world at a glance: entities/pois/regions clustered by their home faction (a
    multi-hub, decentralised layout — no single focus). Solves "must pick a focus first"."""
    graph = build_content_graph(bundle)
    refs = (
        [f"entity:{eid}" for eid in bundle.entities]
        + [f"poi:{pid}" for pid in bundle.pois]
        + [f"region:{rid}" for rid in bundle.regions]
    )
    visible = set(refs)
    positions = _apply_overrides(bundle, _cluster_layout(bundle, refs))
    nodes = _build_nodes(bundle, sorted(visible), positions, focus_ref="", flags={})
    edges = _build_edges(graph, visible, bundle)
    return GraphView(focus="", radius=0, overview=True, nodes=nodes, edges=edges)


def _build_nodes(
    bundle: ContentBundle,
    refs: list[str],
    positions: dict[str, tuple[float, float]],
    *,
    focus_ref: str,
    flags: dict[str, str],
) -> list[GraphNode]:
    return [
        GraphNode(
            ref=ref,
            kind=_kind_of(bundle, ref),
            label=_label_of(bundle, ref),
            x=positions[ref][0],
            y=positions[ref][1],
            focus=ref == focus_ref,
            flag=flags.get(ref, ""),
        )
        for ref in refs
        if ref in positions
    ]


def _build_edges(graph: ContentGraph, visible: set[str], bundle: ContentBundle) -> list[GraphEdge]:
    symmetric = _symmetric_keys(bundle)
    seen: set[tuple[str, str, str]] = set()
    edges: list[GraphEdge] = []
    for edge in graph.edge_refs():
        if edge.source not in visible or edge.target not in visible or edge.source == edge.target:
            continue
        key = (edge.source, edge.target, edge.kind)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            GraphEdge(
                source=edge.source,
                target=edge.target,
                kind=edge.kind,
                symmetric=key in symmetric,
            )
        )
    edges.sort(key=lambda e: (e.source, e.target, e.kind))
    return edges


def _symmetric_keys(bundle: ContentBundle) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for rel in bundle.relations:
        if bool(rel.metadata.get("symmetric")) or is_symmetric_kind(rel.kind):
            src = _object_ref(bundle, rel.source)
            tgt = _object_ref(bundle, rel.target)
            if src and tgt:
                keys.add((src, tgt, rel.kind))
                keys.add((tgt, src, rel.kind))
    return keys


def _cluster_layout(bundle: ContentBundle, refs: list[str]) -> dict[str, tuple[float, float]]:
    """Centripetal petal clusters: unaffiliated nodes form an organic phyllotaxis core and each
    faction blooms outward as a petal of its members. Replaces the old single-blob "skull" layout.
    Deterministic & reproducible.
    """
    center = (560.0, 460.0)
    home = _home_faction(bundle)
    clusters: dict[str, list[str]] = {}
    for ref in refs:
        clusters.setdefault(home.get(ref, "__none__"), []).append(ref)

    faction_keys = sorted(k for k in clusters if k != "__none__")
    loose = sorted(clusters.get("__none__", []))
    positions: dict[str, tuple[float, float]] = {}

    # unaffiliated nodes form an organic phyllotaxis (sunflower) core — a deterministic golden-angle
    # spiral that reads as a galaxy, never a clean ring (which the eye turns into a "face").
    golden = math.pi * (3.0 - math.sqrt(5.0))
    core_scale = 31.0
    core_max_r = 0.0
    for loose_index, ref in enumerate(loose):
        r = core_scale * math.sqrt(loose_index + 1)
        a = loose_index * golden
        core_max_r = max(core_max_r, r)
        positions[ref] = (
            round(center[0] + r * math.cos(a), 1),
            round(center[1] + r * math.sin(a), 1),
        )

    # faction petals bloom outward around the core — each hub on a ring beyond the spiral, members
    # fanned outward, so factions read as satellite clusters orbiting the galaxy.
    fcount = max(len(faction_keys), 1)
    petal_r = core_max_r + 150.0
    fan = math.radians(190)
    for index, key in enumerate(faction_keys):
        angle = -math.pi / 2 + 2 * math.pi * index / fcount
        hub = (
            round(center[0] + petal_r * math.cos(angle), 1),
            round(center[1] + petal_r * math.sin(angle), 1),
        )
        positions[key] = hub  # the faction sits at its petal base
        members = sorted(m for m in clusters[key] if m != key)
        n = len(members)
        ring = 80.0 + 7.0 * max(0, n - 5)
        for member_index, member in enumerate(members):
            theta = angle if n <= 1 else angle - fan / 2 + fan * member_index / (n - 1)
            positions[member] = (
                round(hub[0] + ring * math.cos(theta), 1),
                round(hub[1] + ring * math.sin(theta), 1),
            )
    return positions


def _home_faction(bundle: ContentBundle) -> dict[str, str]:
    """Map each node ref to its home faction (member_of/vassal_of), or a poi's controller."""
    home: dict[str, str] = {}
    for rel in bundle.relations:
        if rel.kind in {"member_of", "vassal_of"} and rel.target in bundle.entities:
            home[f"entity:{rel.source}"] = f"entity:{rel.target}"
    for poi in bundle.pois.values():
        if poi.controlling_faction and poi.controlling_faction in bundle.entities:
            home[f"poi:{poi.id}"] = f"entity:{poi.controlling_faction}"
    return home


def _apply_overrides(
    bundle: ContentBundle, positions: dict[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    """Replace computed positions with any human-dragged ``metadata.graph_pos`` override."""
    for ref in list(positions):
        override = _graph_pos(bundle, ref)
        if override is not None:
            positions[ref] = override
    return positions


def _graph_pos(bundle: ContentBundle, ref: str) -> tuple[float, float] | None:
    object_type, object_id = _split(ref)
    collections: dict[str, Mapping[str, Any]] = {
        "entity": bundle.entities,
        "quest": bundle.quests,
        "poi": bundle.pois,
        "region": bundle.regions,
    }
    collection = collections.get(object_type)
    if collection is None or object_id not in collection:
        return None
    pos = collection[object_id].metadata.get("graph_pos")
    if isinstance(pos, list) and len(pos) == 2:
        try:
            return (float(pos[0]), float(pos[1]))
        except (TypeError, ValueError):
            return None
    return None


def _object_ref(bundle: ContentBundle, object_id: str) -> str:
    if object_id in bundle.entities:
        return f"entity:{object_id}"
    if object_id in bundle.pois:
        return f"poi:{object_id}"
    if object_id in bundle.regions:
        return f"region:{object_id}"
    if object_id in bundle.quests:
        return f"quest:{object_id}"
    return ""


def _impact_levels(bundle: ContentBundle, focus_ref: str) -> dict[str, str]:
    graph = build_content_graph(bundle)
    changes = ChangeSet(
        changes=[Change(change_type=ChangeType.ENTITY_FIELD_CHANGE, target_ref=focus_ref)]
    )
    result = ImpactAnalyzer(graph).analyze(changes, max_depth=2)
    return {item.target_ref: item.level.value for item in result.items}


def _split(ref: str) -> tuple[str, str]:
    object_type, _, object_id = ref.partition(":")
    return object_type, object_id


def _kind_of(bundle: ContentBundle, ref: str) -> str:
    object_type, object_id = _split(ref)
    if object_type == "entity":
        entity = bundle.entities.get(object_id)
        return entity.type.value if entity else "entity"
    return object_type


def _label_of(bundle: ContentBundle, ref: str) -> str:
    object_type, object_id = _split(ref)
    if object_type == "entity" and object_id in bundle.entities:
        return bundle.entities[object_id].name
    if object_type == "quest" and object_id in bundle.quests:
        return bundle.quests[object_id].title or object_id
    if object_type == "poi" and object_id in bundle.pois:
        return bundle.pois[object_id].name
    if object_type == "region" and object_id in bundle.regions:
        return bundle.regions[object_id].name
    if object_type == "dialogue" and object_id in bundle.dialogues:
        return object_id
    return object_id
