"""A directed graph over World Bible entities/relations.

Used for neighbourhood retrieval (feed only the relevant lore sub-graph to the model —
a core token-saving move) and structural checks like prerequisite-cycle detection.
"""

from __future__ import annotations

import networkx as nx

from .models import WorldBible


class LoreGraph:
    def __init__(self, wb: WorldBible):
        self.wb = wb
        self.g = nx.DiGraph()
        for e in wb.entities.values():
            self.g.add_node(e.id, name=e.name, type=e.type.value)
        for r in wb.relations:
            self.g.add_edge(r.source, r.target, kind=r.kind)

    def neighbors(self, entity_id: str, radius: int = 1) -> list[str]:
        """Entity ids within `radius` hops — the 'relevant lore' to retrieve for a prompt."""
        if entity_id not in self.g:
            return []
        return list(nx.ego_graph(self.g, entity_id, radius=radius).nodes)

    def has_cycle(self, kind: str | None = None) -> bool:
        """True if there is a directed cycle (optionally restricted to a relation `kind`,
        e.g. 'requires' for quest-prerequisite loops)."""
        if kind is None:
            h = self.g
        else:
            edges = [(u, v) for u, v, k in self.g.edges(data="kind") if k == kind]
            h = self.g.edge_subgraph(edges).copy()
        try:
            nx.find_cycle(h, orientation="original")
            return True
        except nx.NetworkXNoCycle:
            return False
