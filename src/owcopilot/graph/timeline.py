"""Timeline slicing helpers for content graph relations."""

from __future__ import annotations

from .index import ContentGraph, EdgeRef, relation_is_active


def active_edges_at(graph: ContentGraph, timeline_order: int) -> list[EdgeRef]:
    return graph.active_edges(timeline_order)


def is_active(valid_from: int | None, valid_until: int | None, timeline_order: int) -> bool:
    return relation_is_active(valid_from, valid_until, timeline_order)
