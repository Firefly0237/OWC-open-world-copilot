"""Deterministic graph layouts.

The frontend has no graph library — it renders bare SVG from coordinates we compute here. Computing
layout server-side (rather than running a random force simulation in the browser) means the same
world always produces the same picture: reproducible, diffable, and assertable in a golden test.

``concentric_layout`` (rings by hop-distance from a focus) backs the relationship graph;
``layered_layout`` (rows by depth from a root) backs the dialogue-tree flow view. Both are pure:
they take plain mappings, never a live graph object, so they test without any fixture.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

Point = tuple[float, float]


def concentric_layout(
    distances: Mapping[str, int],
    *,
    center: Point = (300.0, 200.0),
    ring_gap: float = 110.0,
) -> dict[str, Point]:
    """Place nodes on concentric rings keyed by hop-distance (distance 0 = focus, at the center).

    Within a ring, nodes are sorted by ref and spread evenly around the circle starting from the
    top — a stable assignment, so coordinates are reproducible."""
    cx, cy = center
    by_ring: dict[int, list[str]] = {}
    for node, dist in distances.items():
        by_ring.setdefault(dist, []).append(node)

    pos: dict[str, Point] = {}
    for dist, nodes in by_ring.items():
        ordered = sorted(nodes)
        if dist == 0:
            for node in ordered:
                pos[node] = (cx, cy)
            continue
        radius = ring_gap * dist
        count = len(ordered)
        for index, node in enumerate(ordered):
            angle = -math.pi / 2 + 2 * math.pi * index / count
            x = round(cx + radius * math.cos(angle), 1)
            y = round(cy + radius * math.sin(angle), 1)
            pos[node] = (x, y)
    return pos


def layered_layout(
    nodes: Iterable[str],
    adjacency: Mapping[str, list[str]],
    *,
    root: str,
    x_gap: float = 150.0,
    y_gap: float = 90.0,
    origin: Point = (40.0, 40.0),
) -> dict[str, Point]:
    """Lay nodes out top-to-bottom in rows by BFS depth from ``root``; siblings spread along x.

    Nodes unreachable from the root (orphan dialogue nodes) drop into one trailing row so they are
    still visible rather than silently lost. Sorted within each row for reproducibility."""
    all_nodes = list(dict.fromkeys(nodes))
    depth: dict[str, int] = {root: 0}
    queue = [root]
    while queue:
        node = queue.pop(0)
        for nxt in adjacency.get(node, []):
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                queue.append(nxt)

    orphan_row = (max(depth.values()) + 1) if depth else 0
    for node in all_nodes:
        depth.setdefault(node, orphan_row)

    by_row: dict[int, list[str]] = {}
    for node, row in depth.items():
        by_row.setdefault(row, []).append(node)

    ox, oy = origin
    pos: dict[str, Point] = {}
    for row, row_nodes in by_row.items():
        for index, node in enumerate(sorted(row_nodes)):
            pos[node] = (round(ox + index * x_gap, 1), round(oy + row * y_gap, 1))
    return pos
