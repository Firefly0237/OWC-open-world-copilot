"""Graph and relationship consistency rules."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import networkx as nx

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class MissingRelationEndpointRule:
    code = "MISSING_RELATION_ENDPOINT"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for relation in ctx.bundle.relations:
            for side, entity_id in (("source", relation.source), ("target", relation.target)):
                if not _object_exists(ctx, entity_id):
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=f"relation:{relation.source}:{relation.kind}:{relation.target}",
                        message=f"Relation {side} '{entity_id}' does not exist",
                        evidence=[
                            Evidence(
                                kind="relation",
                                relation=(relation.source, relation.kind, relation.target),
                                path=side,
                            )
                        ],
                    )


class DuplicateRelationRule:
    code = "DUPLICATE_RELATION"
    severity = Severity.WARNING
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        groups: dict[tuple[str, str, str], list[str | None]] = {}
        for relation in ctx.bundle.relations:
            key = (relation.source, relation.kind, relation.target)
            groups.setdefault(key, []).append(
                relation.source_ref.path if relation.source_ref else None
            )
        for (source, kind, target), source_paths in groups.items():
            counts = Counter(source_paths)
            repeated_in_source = max(counts.values(), default=0)
            count = len(source_paths)
            if repeated_in_source <= 1:
                continue
            if count > 1:
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=f"relation:{source}:{kind}:{target}",
                    message=f"Relation '{source} {kind} {target}' is duplicated {count} times",
                    evidence=[Evidence(kind="relation", relation=(source, kind, target))],
                )


class RelationshipConflictRule:
    code = "RELATION_CONFLICT"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        allied = {
            _pair(r.source, r.target)
            for r in ctx.bundle.relations
            if r.kind == "allied_with"
        }
        enemies = {_pair(r.source, r.target) for r in ctx.bundle.relations if r.kind == "enemy_of"}
        for source, target in sorted(allied & enemies):
            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=f"relation:{source}:conflict:{target}",
                message=f"Entities '{source}' and '{target}' are both allied and enemies",
                evidence=[
                    Evidence(kind="relation", relation=(source, "allied_with", target)),
                    Evidence(kind="relation", relation=(source, "enemy_of", target)),
                ],
            )


class PrerequisiteCycleRule:
    code = "PREREQ_CYCLE"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        graph = nx.DiGraph()
        for quest in ctx.bundle.quests.values():
            for prereq in quest.prerequisites:
                graph.add_edge(quest.id, prereq)
        try:
            cycle = nx.find_cycle(graph, orientation="original")
        except nx.NetworkXNoCycle:
            return
        refs = [str(edge[0]) for edge in cycle]
        yield Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref="quest_prerequisites",
            message="Quest prerequisites form a cycle",
            evidence=[Evidence(kind="graph_cycle", data={"cycle": refs})],
        )


class FactionConflictRule:
    code = "FACTION_CONFLICT"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        member_of = _first_targets(ctx, "member_of")
        controlled_by = _first_targets(ctx, "controlled_by")
        enemies = {_pair(r.source, r.target) for r in ctx.bundle.relations if r.kind == "enemy_of"}
        for quest in ctx.bundle.quests.values():
            if _is_main_quest(quest):
                continue
            if not quest.giver_npc or not quest.location:
                continue
            npc_faction = member_of.get(quest.giver_npc)
            location_faction = controlled_by.get(quest.location)
            if npc_faction and location_faction and _pair(npc_faction, location_faction) in enemies:
                target_ref = f"quest:{quest.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"Quest '{quest.id}' sends '{quest.giver_npc}' into enemy-controlled "
                        f"location '{quest.location}'"
                    ),
                    evidence=[
                        Evidence(
                            kind="relation",
                            relation=(quest.giver_npc, "member_of", npc_faction),
                        ),
                        Evidence(
                            kind="relation",
                            relation=(quest.location, "controlled_by", location_faction),
                        ),
                        Evidence(
                            kind="relation",
                            relation=(npc_faction, "enemy_of", location_faction),
                        ),
                    ],
                )


def _pair(left: str, right: str) -> tuple[str, str]:
    first, second = sorted((left, right))
    return first, second


def _first_targets(ctx: AuditContext, kind: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for relation in ctx.bundle.relations:
        if relation.kind == kind and relation.source not in targets:
            targets[relation.source] = relation.target
    return targets


def _is_main_quest(quest: object) -> bool:
    tags = {str(tag).strip().lower() for tag in getattr(quest, "tags", [])}
    metadata = getattr(quest, "metadata", {}) or {}
    metadata_values = {
        str(metadata.get(key, "")).strip().lower()
        for key in ("type", "quest_type", "category")
    }
    return bool({"main", "mainline", "主线"} & (tags | metadata_values))


def _object_exists(ctx: AuditContext, object_id: str) -> bool:
    return (
        object_id in ctx.bundle.entities
        or object_id in ctx.bundle.pois
        or object_id in ctx.bundle.regions
        or object_id in ctx.bundle.quests
    )
