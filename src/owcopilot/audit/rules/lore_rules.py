"""World-lore rules that depend on timeline or entity state."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import networkx as nx

from ...content.models import QuestEventRefKind
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_ORDER_RE = re.compile(r"^order\s*=\s*(-?\d+)$")


class TimelineViolationRule:
    code = "TIMELINE_VIOLATION"
    severity = Severity.ERROR
    category = Category.LORE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        cyclic_quests = _cyclic_quest_ids(ctx)
        for quest in ctx.bundle.quests.values():
            if quest.timeline_order is None:
                continue
            for prereq_id in quest.prerequisites:
                if quest.id in cyclic_quests or prereq_id in cyclic_quests:
                    continue
                prereq = ctx.bundle.quests.get(prereq_id)
                if prereq and prereq.timeline_order is not None:
                    if prereq.timeline_order >= quest.timeline_order:
                        yield Issue(
                            rule_code=self.code,
                            severity=self.severity,
                            category=self.category,
                            target_ref=f"quest:{quest.id}",
                            message=(
                                f"Prerequisite quest '{prereq_id}' does not occur before "
                                f"quest '{quest.id}'"
                            ),
                            evidence=[
                                Evidence(
                                    kind="field_path",
                                    target_ref=f"quest:{quest.id}",
                                    path="prerequisites",
                                    data={
                                        "quest_order": quest.timeline_order,
                                        "prereq_order": prereq.timeline_order,
                                    },
                                )
                            ],
                        )


class EventResultReferencedTooEarlyRule:
    code = "EVENT_RESULT_REFERENCED_TOO_EARLY"
    severity = Severity.ERROR
    category = Category.LORE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        yielded: set[tuple[str, str]] = set()
        for event_ref in ctx.bundle.quest_event_refs.values():
            if event_ref.ref_kind is not QuestEventRefKind.REFERENCES_RESULT:
                continue
            quest = ctx.bundle.quests.get(event_ref.quest_id)
            if quest is None or quest.timeline_order is None:
                continue
            if self._too_early(ctx, quest.id, quest.timeline_order, event_ref.event_id):
                yielded.add((quest.id, event_ref.event_id))
                yield self._issue(
                    quest.id,
                    event_ref.event_id,
                    quest.timeline_order,
                    _event_order(ctx, event_ref.event_id),
                    path=f"quest_event_refs.{event_ref.id}",
                )
        for quest in ctx.bundle.quests.values():
            if quest.timeline_order is None:
                continue
            event_ids = _list(quest.metadata.get("references_event_results"))
            for event_id in event_ids:
                if (quest.id, event_id) in yielded:
                    continue
                if self._too_early(ctx, quest.id, quest.timeline_order, event_id):
                    yield self._issue(
                        quest.id,
                        event_id,
                        quest.timeline_order,
                        _event_order(ctx, event_id),
                        path="metadata.references_event_results",
                    )

    def _too_early(
        self, ctx: AuditContext, quest_id: str, quest_order: int, event_id: str
    ) -> bool:
        event_order = _event_order(ctx, event_id)
        return event_order is not None and quest_order < event_order

    def _issue(
        self,
        quest_id: str,
        event_id: str,
        quest_order: int,
        event_order: int | None,
        *,
        path: str,
    ) -> Issue:
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"quest:{quest_id}",
            message=(
                f"Quest '{quest_id}' references result of event '{event_id}' before "
                "that event occurs"
            ),
            evidence=[
                Evidence(
                    kind="field_path",
                    target_ref=f"quest:{quest_id}",
                    path=path,
                    data={
                        "quest_order": quest_order,
                        "event_order": event_order,
                    },
                )
            ],
        )


class CharacterStateContradictionRule:
    code = "CHARACTER_STATE_CONTRADICTION"
    severity = Severity.ERROR
    category = Category.LORE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for entity in ctx.bundle.entities.values():
            tags = {tag.lower() for tag in entity.tags}
            if entity.status.lower() in {"dead", "destroyed"} and "active" in tags:
                target_ref = f"entity:{entity.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=f"Entity '{entity.id}' is marked {entity.status} and active",
                    evidence=[
                        Evidence(
                            kind="field_path",
                            target_ref=target_ref,
                            path="status/tags",
                            data={"status": entity.status, "tags": entity.tags},
                        )
                    ],
                )


def _timeline_order(metadata: dict[str, Any] | None, tags: list[str]) -> int | None:
    if metadata:
        raw = metadata.get("timeline_order")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            return int(raw.strip())
    for tag in tags:
        match = _ORDER_RE.match(tag.strip())
        if match:
            return int(match.group(1))
    return None


def _event_order(ctx: AuditContext, event_id: str) -> int | None:
    event = ctx.bundle.entities.get(event_id)
    return _timeline_order(event.metadata if event else None, event.tags if event else [])


def _cyclic_quest_ids(ctx: AuditContext) -> set[str]:
    graph = nx.DiGraph()
    for quest in ctx.bundle.quests.values():
        for prereq in quest.prerequisites:
            graph.add_edge(quest.id, prereq)
    return {quest_id for cycle in nx.simple_cycles(graph) for quest_id in cycle}


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []
