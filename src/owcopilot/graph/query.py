"""Query helpers for the v2 content graph."""

from __future__ import annotations

from .index import ContentGraph, EdgeRef


def neighbors(
    graph: ContentGraph,
    node_ref: str,
    *,
    radius: int = 1,
    kinds: set[str] | None = None,
) -> list[str]:
    return graph.neighbors(node_ref, radius=radius, kinds=kinds)


def edges_by_kind(graph: ContentGraph, kind: str) -> list[EdgeRef]:
    return graph.edge_refs(kind=kind)


def edges_by_type(graph: ContentGraph, edge_type: str) -> list[EdgeRef]:
    return graph.edge_refs(edge_type=edge_type)
