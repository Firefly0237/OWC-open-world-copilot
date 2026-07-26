# 世界设定规则：依赖时间线或实体状态的一致性（先后矛盾/状态冲突等）。
"""World-lore rules that depend on timeline or entity state."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import networkx as nx

from ...content.models import QuestEventRefKind
from ...graph.ordering import timeline_order_of
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


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
                    quest_order = quest.timeline_order
                    prereq_order = prereq.timeline_order
                    if prereq_order >= quest_order:
                        yield Issue(
                            rule_code=self.code,
                            severity=self.severity,
                            category=self.category,
                            target_ref=f"quest:{quest.id}",
                            message=(
                                f"任务《{quest.id}》把《{prereq_id}》设成了前置任务,要求玩家先完成"
                                f"《{prereq_id}》再做《{quest.id}》;但世界时间线上,《{prereq_id}》"
                                f"排在第 {prereq_order} 位,《{quest.id}》排在第 {quest_order} 位—— "
                                "前置任务并没有比当前任务更早发生,时间线和任务的先后要求对不上。"
                            ),
                            evidence=[
                                Evidence(
                                    kind="field_path",
                                    target_ref=f"quest:{quest.id}",
                                    path="prerequisites",
                                    data={
                                        "quest_order": quest_order,
                                        "prereq_order": prereq_order,
                                    },
                                )
                            ],
                            suggested_action=(
                                "两边的时间线数字都合法,改哪一边需要策划判断剧情真正的先后关系:"
                                f"①把任务《{quest.id}》的时间线顺序号从 {quest_order} 改成大于 "
                                f"{prereq_order} 的数,承认《{quest.id}》确实发生在《{prereq_id}》之"
                                f"后;②或者把《{prereq_id}》的时间线顺序号从 {prereq_order} 改成小于"
                                f" {quest_order} 的数,让它真正发生在《{quest.id}》之前。"
                            ),
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

    def _too_early(self, ctx: AuditContext, quest_id: str, quest_order: int, event_id: str) -> bool:
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
        # 走到这里时 event_order 必然不是 None——两处调用点都先经过 ``_too_early`` 用同一对
        # (ctx, event_id) 确认过；重新算一遍类型上仍是 Optional，这里断言把它坐实。
        assert event_order is not None
        via_formal_ref = path.startswith("quest_event_refs.")
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"quest:{quest_id}",
            message=(
                f"任务《{quest_id}》引用了事件《{event_id}》的结果,但世界时间线上,事件"
                f"《{event_id}》排在第 {event_order} 位,任务《{quest_id}》排在更早的第 "
                f"{quest_order} 位——玩家在事件真正发生之前,就已经能在这个任务里看到它的"
                "结果,时间线说不通。"
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
            suggested_action=self._suggested_action(
                quest_id, event_id, quest_order, event_order, via_formal_ref=via_formal_ref
            ),
        )

    @staticmethod
    def _suggested_action(
        quest_id: str, event_id: str, quest_order: int, event_order: int, *, via_formal_ref: bool
    ) -> str:
        third_option = (
            "③如果这个任务其实不需要依赖事件的结果,把这条引用的类型从『依赖事件结果』改成"
            "『只是提到这个事件』,就不用再满足先后顺序。"
            if via_formal_ref
            else f"③如果这个任务其实不需要依赖事件的结果,把《{event_id}》从这个任务的『引用"
            "事件结果』列表里删掉。"
        )
        return (
            "三种改法选一种,具体选哪个需要策划判断:"
            f"①把任务《{quest_id}》的时间线顺序号从 {quest_order} 改成大于 {event_order} 的数;"
            f"②把事件《{event_id}》的时间线顺序号从 {event_order} 改成小于 {quest_order} 的数;"
            f"{third_option}"
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
                # tags 里可能是任意大小写写法（"Active"/"ACTIVE"），消息与建议里要照实引用原文，
                # 不能把策划自己的写法换成小写化之后的版本。
                active_tag = next(tag for tag in entity.tags if tag.lower() == "active")
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"角色《{entity.id}》的状态填的是『{entity.status}』,但标签列表里还留着"
                        f"『{active_tag}』这个标签——角色都已经死亡/被摧毁了,却还被标记成活跃,"
                        "前后矛盾。"
                    ),
                    evidence=[
                        Evidence(
                            kind="field_path",
                            target_ref=target_ref,
                            path="status/tags",
                            data={"status": entity.status, "tags": entity.tags},
                        )
                    ],
                    suggested_action=(
                        f"把角色《{entity.id}》标签列表里的『{active_tag}』删掉,和状态"
                        f"『{entity.status}』保持一致;如果这个角色其实应该还活跃,需要策划判断"
                        f"状态字段该改成什么(不是简单删标签能解决的),程序猜不出正确的状态值。"
                    ),
                )


def _event_order(ctx: AuditContext, event_id: str) -> int | None:
    event = ctx.bundle.entities.get(event_id)
    return timeline_order_of(event.metadata if event else None, event.tags if event else [])


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
