"""Graph-based impact analysis."""

from __future__ import annotations

from ..graph.index import ContentGraph
from .models import ChangeSet, ImpactItem, ImpactLevel, ImpactResult


class ImpactAnalyzer:
    def __init__(self, graph: ContentGraph) -> None:
        self.graph = graph

    def analyze(self, changes: ChangeSet, *, max_depth: int = 2) -> ImpactResult:
        items: dict[str, ImpactItem] = {}
        for change in changes.changes:
            if not self.graph.has_node(change.target_ref):
                continue
            # One BFS gives the EXACT minimal hop distance to every reachable object at once —
            # so a node is classified by its true distance (no re-derivation), and this works for
            # any max_depth (the old hand-rolled `_distance` capped at 2 and silently dropped
            # everything 3+ hops away).
            distances = self.graph.ego_distances(change.target_ref, radius=max_depth)
            for target_ref, distance in distances.items():
                if distance < 1:  # the changed object itself
                    continue
                level = ImpactLevel.MUST_CHANGE if distance == 1 else ImpactLevel.SUGGEST_CHECK
                item = ImpactItem(
                    target_ref=target_ref,
                    level=level,
                    distance=distance,
                    reason=_reason(change.target_ref, target_ref, distance),
                    source_change=change.target_ref,
                    evidence={
                        "change_type": change.change_type.value,
                        "source": change.target_ref,
                        "distance": distance,
                    },
                )
                _keep_stronger(items, item)
        return ImpactResult(items=sorted(items.values(), key=lambda item: item.target_ref))


def _keep_stronger(items: dict[str, ImpactItem], item: ImpactItem) -> None:
    """When several changes reach the same object, keep the strongest signal: the nearer hop
    (and thus the higher level) wins, so a must-change from one change is never masked by a
    suggest-check from another."""
    existing = items.get(item.target_ref)
    if existing is None or item.distance < existing.distance:
        items[item.target_ref] = item


def _reason(source_ref: str, target_ref: str, distance: int) -> str:
    if distance == 1:
        return f"{target_ref} directly references or relates to changed object {source_ref}"
    return f"{target_ref} is within {distance} graph hops of changed object {source_ref}"
