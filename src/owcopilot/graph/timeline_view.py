"""Chronology view: lay quests and events on one ordered axis and surface timeline violations.

The data is already here — ``Quest.timeline_order``, events as ``Entity(type=event)`` with an order
in metadata, and ``Quest.prerequisites`` — and so is the judgement: the deterministic audit already
flags ``TIMELINE_VIOLATION`` (a prerequisite that does not occur earlier) and
``EVENT_RESULT_REFERENCED_TOO_EARLY``. This view does NOT re-derive correctness; it lays the ordered
items out and *consumes* the audit's findings, hanging the friendly reason on the offending item.

Orders are dense-ranked so sparse values (3, 50, 999) don't stretch the axis. Items with no order
fall into an ``unsequenced`` bucket — which doubles as a completeness signal (what still needs
placing on the timeline), in the same spirit as the readiness panel.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..audit.context import AuditContext
from ..audit.default_rules import build_default_rule_registry
from ..audit.runner import AuditRunner
from ..content.models import ContentBundle, EntityType
from .ordering import timeline_order_of

# rule_code -> plain-Chinese reason shown on the timeline (the audit decides *that* it is wrong;
# we only relabel its code into something a planner reads at a glance).
_RULE_REASON = {
    "TIMELINE_VIOLATION": "前置任务没有排在它之前",
    "EVENT_RESULT_REFERENCED_TOO_EARLY": "引用了尚未发生的事件结果",
}


class TimelineItem(BaseModel):
    ref: str
    kind: str  # "quest" | "event"
    label: str
    order: int
    rank: int  # dense rank, 0-based — the x slot on the axis
    flags: list[str] = Field(default_factory=list)


class TimelineDependency(BaseModel):
    source: str  # prerequisite quest ref (must come first)
    target: str  # dependent quest ref
    violation: bool = False


class TimelineEntry(BaseModel):
    ref: str
    kind: str
    label: str


class TimelineView(BaseModel):
    items: list[TimelineItem] = Field(default_factory=list)
    dependencies: list[TimelineDependency] = Field(default_factory=list)
    unsequenced: list[TimelineEntry] = Field(default_factory=list)
    rank_count: int = 0


def build_timeline_view(bundle: ContentBundle) -> TimelineView:
    issues = _timeline_issues(bundle)
    ordered: list[tuple[str, str, str, int]] = []
    unsequenced: list[TimelineEntry] = []

    for quest in bundle.quests.values():
        ref = f"quest:{quest.id}"
        label = quest.title or quest.id
        if quest.timeline_order is None:
            unsequenced.append(TimelineEntry(ref=ref, kind="quest", label=label))
        else:
            ordered.append((ref, "quest", label, quest.timeline_order))

    for entity in bundle.entities.values():
        if entity.type is not EntityType.EVENT:
            continue
        ref = f"entity:{entity.id}"
        label = entity.name or entity.id
        order = timeline_order_of(entity.metadata, entity.tags)
        if order is None:
            unsequenced.append(TimelineEntry(ref=ref, kind="event", label=label))
        else:
            ordered.append((ref, "event", label, order))

    distinct = sorted({order for _, _, _, order in ordered})
    rank_of = {value: index for index, value in enumerate(distinct)}
    items = [
        TimelineItem(
            ref=ref,
            kind=kind,
            label=label,
            order=order,
            rank=rank_of[order],
            flags=issues.get(ref, []),
        )
        for ref, kind, label, order in sorted(ordered, key=lambda row: (row[3], row[0]))
    ]

    order_by_quest = {quest.id: quest.timeline_order for quest in bundle.quests.values()}
    dependencies: list[TimelineDependency] = []
    for quest in bundle.quests.values():
        target_ref = f"quest:{quest.id}"
        target_order = quest.timeline_order
        for prereq in quest.prerequisites:
            if prereq not in bundle.quests:
                continue
            prereq_order = order_by_quest.get(prereq)
            # red only when the audit flagged this quest AND this prereq is the one out of order —
            # the audit owns the verdict, the ordering picks which edge to highlight.
            violation = (
                bool(issues.get(target_ref))
                and target_order is not None
                and prereq_order is not None
                and prereq_order >= target_order
            )
            dependencies.append(
                TimelineDependency(source=f"quest:{prereq}", target=target_ref, violation=violation)
            )

    dependencies.sort(key=lambda dep: (dep.target, dep.source))
    unsequenced.sort(key=lambda entry: (entry.kind, entry.ref))
    return TimelineView(
        items=items,
        dependencies=dependencies,
        unsequenced=unsequenced,
        rank_count=len(distinct),
    )


def _timeline_issues(bundle: ContentBundle) -> dict[str, list[str]]:
    """Run the deterministic audit and keep only the two timeline rules, grouped by target ref."""
    runner = AuditRunner(build_default_rule_registry())
    result = runner.run(AuditContext.from_bundle(bundle))
    grouped: dict[str, list[str]] = {}
    for issue in result.issues:
        reason = _RULE_REASON.get(issue.rule_code)
        if reason is None:
            continue
        reasons = grouped.setdefault(issue.target_ref, [])
        if reason not in reasons:
            reasons.append(reason)
    return grouped
