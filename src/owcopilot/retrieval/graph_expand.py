"""Entity-anchored graph expansion retriever."""

from __future__ import annotations

from ..graph.index import ContentGraph
from .models import RetrievalHit
from .text_match import mentions


class GraphExpansionRetriever:
    def __init__(self, graph: ContentGraph) -> None:
        self.graph = graph

    def search(self, query: str, *, radius: int = 1, limit: int = 10) -> list[RetrievalHit]:
        seeds = self._seed_refs(query)
        hits: dict[str, RetrievalHit] = {}
        for seed in seeds:
            for distance in range(0, radius + 1):
                for ref in self.graph.neighbors(seed, radius=distance):
                    if ref in hits:
                        continue
                    hit = self._hit_for(ref, score=1.0 / (distance + 1))
                    if hit is not None:
                        hits[ref] = hit
        return sorted(hits.values(), key=lambda hit: (-hit.score, hit.ref))[:limit]

    def _seed_refs(self, query: str) -> list[str]:
        seeds: list[str] = []
        for entity in self.graph.bundle.entities.values():
            if mentions(query, [entity.id, entity.name, *entity.aliases, *entity.tags]):
                seeds.append(f"entity:{entity.id}")
        for region in self.graph.bundle.regions.values():
            if mentions(query, [region.id, region.name, *region.themes]):
                seeds.append(f"region:{region.id}")
        for poi in self.graph.bundle.pois.values():
            if mentions(query, [poi.id, poi.name, *poi.tags]):
                seeds.append(f"poi:{poi.id}")
        for quest in self.graph.bundle.quests.values():
            if mentions(query, [quest.id, quest.title, *quest.tags]):
                seeds.append(f"quest:{quest.id}")
        for term in self.graph.bundle.terms.values():
            if mentions(query, [term.id, term.canonical, *term.aliases, *term.forbidden]):
                seeds.append(f"term:{term.id}")
        return sorted(set(seeds))

    def _hit_for(self, ref: str, *, score: float) -> RetrievalHit | None:
        object_type, object_id = ref.split(":", 1)
        bundle = self.graph.bundle
        if object_type == "entity" and object_id in bundle.entities:
            entity = bundle.entities[object_id]
            return RetrievalHit(
                ref=ref,
                object_type=object_type,
                title=entity.name,
                body=" ".join(
                    [
                        entity.description,
                        entity.status,
                        " ".join(entity.aliases),
                        " ".join(entity.tags),
                        self._relation_text(ref),
                    ]
                ),
                score=score,
                source="graph",
            )
        if object_type == "quest" and object_id in bundle.quests:
            quest = bundle.quests[object_id]
            return RetrievalHit(
                ref=ref,
                object_type=object_type,
                title=quest.title,
                body=" ".join(
                    [
                        quest.objective,
                        f"giver_npc={quest.giver_npc or ''}",
                        f"location={quest.location or ''}",
                        self._relation_text(ref),
                    ]
                ),
                score=score,
                source="graph",
            )
        if object_type == "region" and object_id in bundle.regions:
            region = bundle.regions[object_id]
            return RetrievalHit(
                ref=ref,
                object_type=object_type,
                title=region.name,
                body=" ".join(
                    [region.id, " ".join(region.themes), " ".join(region.banned_content)]
                ),
                score=score,
                source="graph",
            )
        if object_type == "poi" and object_id in bundle.pois:
            poi = bundle.pois[object_id]
            return RetrievalHit(
                ref=ref,
                object_type=object_type,
                title=poi.name,
                body=" ".join(
                    [
                        poi.purpose,
                        f"region_id={poi.region_id or ''}",
                        f"controlling_faction={poi.controlling_faction or ''}",
                        self._relation_text(ref),
                    ]
                ),
                score=score,
                source="graph",
            )
        if object_type == "term" and object_id in bundle.terms:
            term = bundle.terms[object_id]
            return RetrievalHit(
                ref=ref,
                object_type=object_type,
                title=term.canonical,
                body=" ".join([term.description, " ".join(term.aliases), " ".join(term.forbidden)]),
                score=score,
                source="graph",
            )
        if object_type == "relation":
            return RetrievalHit(
                ref=ref,
                object_type=object_type,
                title=object_id,
                body=object_id.replace(":", " "),
                score=score,
                source="graph",
            )
        return None

    def _relation_text(self, ref: str) -> str:
        facts = set()
        pair_kinds: dict[tuple[str, str], set[str]] = {}
        for edge in self.graph.edge_refs(edge_type="relation"):
            first, second = sorted((edge.source, edge.target))
            pair = (first, second)
            pair_kinds.setdefault(pair, set()).add(edge.kind)
            if edge.source == ref or edge.target == ref:
                facts.add(f"relation {edge.source} {edge.kind} {edge.target}")
        for pair, kinds in pair_kinds.items():
            if ref in pair and {"allied_with", "enemy_of"} <= kinds:
                facts.add(
                    f"relation_conflict {pair[0]} {pair[1]} "
                    "both allied_with and enemy_of"
                )
        return " ".join(sorted(facts))
