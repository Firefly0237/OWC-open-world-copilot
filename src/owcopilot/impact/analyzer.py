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
            for distance in range(1, max_depth + 1):
                level = ImpactLevel.MUST_CHANGE if distance == 1 else ImpactLevel.SUGGEST_CHECK
                for target_ref in self.graph.neighbors(change.target_ref, radius=distance):
                    if target_ref == change.target_ref:
                        continue
                    if _distance(self.graph, change.target_ref, target_ref) != distance:
                        continue
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


def _distance(graph: ContentGraph, source: str, target: str) -> int | None:
    for distance in range(0, 3):
        if target in graph.neighbors(source, radius=distance):
            return distance
    return None


def _keep_stronger(items: dict[str, ImpactItem], item: ImpactItem) -> None:
    existing = items.get(item.target_ref)
    if existing is None:
        items[item.target_ref] = item
        return
    if existing.level is ImpactLevel.SUGGEST_CHECK and item.level is ImpactLevel.MUST_CHANGE:
        items[item.target_ref] = item


def _reason(source_ref: str, target_ref: str, distance: int) -> str:
    if distance == 1:
        return f"{target_ref} directly references or relates to changed object {source_ref}"
    return f"{target_ref} is within {distance} graph hops of changed object {source_ref}"
