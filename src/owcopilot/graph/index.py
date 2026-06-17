"""Content graph index for v2.

Nodes use stable refs (`entity:npc_aldric`, `quest:quest_missing_caravan`) so audit evidence,
impact reports and retrieval citations can all point at the same object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx

from ..content.models import ContentBundle, Relation


@dataclass(frozen=True)
class EdgeRef:
    source: str
    target: str
    kind: str
    edge_type: str
    valid_from: int | None = None
    valid_until: int | None = None


class ContentGraph:
    def __init__(self, bundle: ContentBundle) -> None:
        self.bundle = bundle
        self.g = nx.MultiDiGraph()
        self._index_nodes()
        self._index_relations()
        self._index_derived_references()
        # The graph is immutable once built, so the per-`kinds` filtered view (and its undirected
        # form) is the same every call — memoize both instead of deep-copying the whole graph twice
        # on every neighbors()/ego_distances() (impact analysis and graph retrieval call these in a
        # tight loop over many seeds). Keyed by frozenset(kinds) | None.
        self._filtered_cache: dict[frozenset[str] | None, Any] = {}
        self._undirected_cache: dict[frozenset[str] | None, Any] = {}

    def has_node(self, node_ref: str) -> bool:
        return node_ref in self.g

    def node_refs(self) -> list[str]:
        return sorted(str(node) for node in self.g.nodes)

    def edge_refs(self, *, kind: str | None = None, edge_type: str | None = None) -> list[EdgeRef]:
        edges: list[EdgeRef] = []
        for source, target, data in self.g.edges(data=True):
            if edge_type is None and data.get("edge_type") == "relation_ref":
                continue
            if kind is not None and data.get("kind") != kind:
                continue
            if edge_type is not None and data.get("edge_type") != edge_type:
                continue
            edges.append(
                EdgeRef(
                    source=str(source),
                    target=str(target),
                    kind=str(data.get("kind")),
                    edge_type=str(data.get("edge_type")),
                    valid_from=data.get("valid_from"),
                    valid_until=data.get("valid_until"),
                )
            )
        return edges

    def neighbors(
        self,
        node_ref: str,
        *,
        radius: int = 1,
        kinds: set[str] | None = None,
    ) -> list[str]:
        return sorted(self.ego_distances(node_ref, radius=radius, kinds=kinds))

    def ego_distances(
        self,
        node_ref: str,
        *,
        radius: int = 1,
        kinds: set[str] | None = None,
    ) -> dict[str, int]:
        """BFS hop-distance from ``node_ref`` to every node within ``radius`` (focus itself = 0).

        ``neighbors`` is just ``sorted(this.keys())``; layout needs the distances too (which ring a
        node sits on), so the shortest-path call lives here once."""
        if node_ref not in self.g:
            return {}
        graph = self._undirected_filtered_graph(kinds)
        if node_ref not in graph:
            return {}
        lengths = nx.single_source_shortest_path_length(graph, node_ref, cutoff=radius)
        return {str(ref): int(dist) for ref, dist in lengths.items()}

    def active_edges(self, timeline_order: int) -> list[EdgeRef]:
        return [
            edge
            for edge in self.edge_refs()
            if relation_is_active(edge.valid_from, edge.valid_until, timeline_order)
        ]

    def _index_nodes(self) -> None:
        for entity in self.bundle.entities.values():
            self.g.add_node(ref("entity", entity.id), object_type="entity", object_id=entity.id)
        for quest in self.bundle.quests.values():
            self.g.add_node(ref("quest", quest.id), object_type="quest", object_id=quest.id)
        for region in self.bundle.regions.values():
            self.g.add_node(ref("region", region.id), object_type="region", object_id=region.id)
        for poi in self.bundle.pois.values():
            self.g.add_node(ref("poi", poi.id), object_type="poi", object_id=poi.id)
        for dialogue in self.bundle.dialogues.values():
            self.g.add_node(
                ref("dialogue", dialogue.id), object_type="dialogue", object_id=dialogue.id
            )
        for text in self.bundle.localized_texts.values():
            self.g.add_node(
                ref("localized_text", text.id),
                object_type="localized_text",
                object_id=text.id,
            )
        for event_ref in self.bundle.quest_event_refs.values():
            self.g.add_node(
                ref("quest_event_ref", event_ref.id),
                object_type="quest_event_ref",
                object_id=event_ref.id,
            )
        for term in self.bundle.terms.values():
            self.g.add_node(ref("term", term.id), object_type="term", object_id=term.id)
        for style in self.bundle.style_guides.values():
            self.g.add_node(
                ref("style_guide", style.id),
                object_type="style_guide",
                object_id=style.id,
            )

    def _index_relations(self) -> None:
        for index, relation in enumerate(self.bundle.relations):
            relation_ref = _relation_ref(relation, index)
            self.g.add_node(relation_ref, object_type="relation", object_id=relation_ref)
            self._add_relation_edge(relation, relation_ref=relation_ref)

    def _index_derived_references(self) -> None:
        for quest in self.bundle.quests.values():
            quest_ref = ref("quest", quest.id)
            self._add_ref(quest_ref, object_ref(self.bundle, quest.giver_npc), "giver_npc")
            self._add_ref(quest_ref, object_ref(self.bundle, quest.location), "quest_location")
            for prereq in quest.prerequisites:
                self._add_ref(quest_ref, ref("quest", prereq), "requires")
            for dialogue_id in quest.dialogue_refs:
                self._add_ref(quest_ref, ref("dialogue", dialogue_id), "dialogue_ref")
            for key in quest.localization_keys:
                localization_ref = ref("localization", key)
                self.g.add_node(localization_ref, object_type="localization", object_id=key)
                self._add_ref(quest_ref, localization_ref, "localization_key")
            for stage in quest.stages:
                self._add_ref(quest_ref, object_ref(self.bundle, stage.location), "stage_location")
                for entity_id in stage.required_entities:
                    self._add_ref(quest_ref, object_ref(self.bundle, entity_id), "stage_entity")

        for event_ref in self.bundle.quest_event_refs.values():
            event_ref_node = ref("quest_event_ref", event_ref.id)
            self._add_ref(event_ref_node, ref("quest", event_ref.quest_id), "quest_event_ref")
            self._add_ref(event_ref_node, object_ref(self.bundle, event_ref.event_id), "event_ref")

        for poi in self.bundle.pois.values():
            poi_ref = ref("poi", poi.id)
            self._add_ref(poi_ref, ref("region", poi.region_id), "poi_region")
            self._add_ref(
                poi_ref,
                object_ref(self.bundle, poi.controlling_faction),
                "controlled_by",
            )

        for dialogue in self.bundle.dialogues.values():
            dialogue_ref = ref("dialogue", dialogue.id)
            self._add_ref(dialogue_ref, object_ref(self.bundle, dialogue.speaker_id), "speaker")
            self._add_ref(dialogue_ref, ref("quest", dialogue.quest_id), "quest_dialogue")

        for text in self.bundle.localized_texts.values():
            text_ref = ref("localized_text", text.id)
            self._add_ref(text_ref, ref("localization", text.text_key), "localized_text_key")

    def _add_relation_edge(self, relation: Relation, *, relation_ref: str) -> None:
        source = object_ref(self.bundle, relation.source)
        target = object_ref(self.bundle, relation.target)
        if source is None or target is None:
            return
        self._add_edge(
            relation_ref,
            source,
            kind="relation_source",
            edge_type="relation_ref",
            valid_from=relation.valid_from,
            valid_until=relation.valid_until,
        )
        self._add_edge(
            relation_ref,
            target,
            kind="relation_target",
            edge_type="relation_ref",
            valid_from=relation.valid_from,
            valid_until=relation.valid_until,
        )
        self._add_edge(
            source,
            target,
            kind=relation.kind,
            edge_type="relation",
            valid_from=relation.valid_from,
            valid_until=relation.valid_until,
        )

    def _add_ref(self, source: str | None, target: str | None, kind: str) -> None:
        if source is None or target is None:
            return
        self._add_edge(source, target, kind=kind, edge_type="reference")

    def _add_edge(
        self,
        source: str,
        target: str,
        *,
        kind: str,
        edge_type: str,
        valid_from: int | None = None,
        valid_until: int | None = None,
    ) -> None:
        self.g.add_node(source)
        self.g.add_node(target)
        self.g.add_edge(
            source,
            target,
            kind=kind,
            edge_type=edge_type,
            valid_from=valid_from,
            valid_until=valid_until,
        )

    def _filtered_graph(self, kinds: set[str] | None) -> Any:
        cache_key = frozenset(kinds) if kinds is not None else None
        if cache_key not in self._filtered_cache:
            graph = nx.MultiDiGraph()
            graph.add_nodes_from(self.g.nodes(data=True))
            for source, target, key, data in self.g.edges(keys=True, data=True):
                if kinds is None and data.get("edge_type") == "relation_ref":
                    continue
                if kinds is None or data.get("kind") in kinds:
                    graph.add_edge(source, target, key=key, **data)
            self._filtered_cache[cache_key] = graph
        return self._filtered_cache[cache_key]

    def _undirected_filtered_graph(self, kinds: set[str] | None) -> Any:
        cache_key = frozenset(kinds) if kinds is not None else None
        if cache_key not in self._undirected_cache:
            self._undirected_cache[cache_key] = self._filtered_graph(kinds).to_undirected()
        return self._undirected_cache[cache_key]


def build_content_graph(bundle: ContentBundle) -> ContentGraph:
    return ContentGraph(bundle)


def ref(object_type: str, object_id: str | None) -> str | None:
    if not object_id:
        return None
    if ":" in object_id:
        return object_id
    return f"{object_type}:{object_id}"


def entity_ref(entity_id: str | None) -> str | None:
    return ref("entity", entity_id)


def object_ref(bundle: ContentBundle, object_id: str | None) -> str | None:
    if not object_id:
        return None
    if ":" in object_id:
        return object_id
    if object_id in bundle.entities:
        return ref("entity", object_id)
    if object_id in bundle.pois:
        return ref("poi", object_id)
    if object_id in bundle.regions:
        return ref("region", object_id)
    if object_id in bundle.quests:
        return ref("quest", object_id)
    if object_id in bundle.dialogues:
        return ref("dialogue", object_id)
    if object_id in bundle.localized_texts:
        return ref("localized_text", object_id)
    return ref("entity", object_id)


def relation_is_active(
    valid_from: int | None, valid_until: int | None, timeline_order: int
) -> bool:
    starts_before = valid_from is None or valid_from <= timeline_order
    ends_after = valid_until is None or timeline_order <= valid_until
    return starts_before and ends_after


def _relation_ref(relation: Relation, index: int) -> str:
    return f"relation:{relation.source}:{relation.kind}:{relation.target}:{index}"
