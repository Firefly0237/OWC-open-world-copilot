"""Dialogue-tree flow view: turn a branching ``DialogueTree`` into a laid-out node graph.

We already generate dialogue trees (nodes wired by choices/next links) but only ever showed counts.
This lays them out top-to-bottom by depth (:func:`layered_layout`) and emits the same node/edge
shape the relationship graph uses, so the one SVG renderer draws both. Choice text rides on the
edges; each node shows a snippet + its speaker.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from ..content.models import DialogueTree
from .graph_view import GraphEdge, GraphNode
from .layout import layered_layout


class DialogueFlow(BaseModel):
    tree_id: str
    title: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


def build_dialogue_flow(tree: DialogueTree, *, speaker_names: Mapping[str, str]) -> DialogueFlow:
    node_ids = list(tree.nodes)
    adjacency: dict[str, list[str]] = {}
    edges: list[GraphEdge] = []
    for node in tree.nodes.values():
        targets: list[str] = []
        for choice in node.choices:
            if choice.next_node and choice.next_node in tree.nodes:
                targets.append(choice.next_node)
                label = _clip(choice.condition or choice.text, 18)
                edges.append(GraphEdge(source=node.id, target=choice.next_node, label=label))
        if node.next_node and node.next_node in tree.nodes:
            targets.append(node.next_node)
            edges.append(GraphEdge(source=node.id, target=node.next_node))
        adjacency[node.id] = targets

    root = tree.root_node if tree.root_node in tree.nodes else (node_ids[0] if node_ids else "")
    positions = layered_layout(node_ids, adjacency, root=root, x_gap=170.0, y_gap=110.0)
    positions = _apply_node_overrides(positions, tree.metadata.get("node_pos"))

    nodes = [
        GraphNode(
            ref=node.id,
            kind=_kind(node.id, root, adjacency),
            label=_clip(node.text, 8) or node.id,
            sublabel=speaker_names.get(node.speaker_id or "", node.speaker_id or ""),
            x=positions[node.id][0],
            y=positions[node.id][1],
            focus=node.id == root,
        )
        for node in tree.nodes.values()
    ]
    nodes.sort(key=lambda n: n.ref)
    edges.sort(key=lambda e: (e.source, e.target, e.label))
    return DialogueFlow(tree_id=tree.id, title=tree.title, nodes=nodes, edges=edges)


def _apply_node_overrides(
    positions: dict[str, tuple[float, float]], overrides: object
) -> dict[str, tuple[float, float]]:
    """Replace laid-out positions with human-dragged ones from ``tree.metadata['node_pos']``."""
    if not isinstance(overrides, dict):
        return positions
    for node_id, pos in overrides.items():
        if node_id in positions and isinstance(pos, list) and len(pos) == 2:
            try:
                positions[node_id] = (float(pos[0]), float(pos[1]))
            except (TypeError, ValueError):
                continue
    return positions


def _kind(node_id: str, root: str, adjacency: Mapping[str, list[str]]) -> str:
    if node_id == root:
        return "root"
    return "end" if not adjacency.get(node_id) else "line"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"
