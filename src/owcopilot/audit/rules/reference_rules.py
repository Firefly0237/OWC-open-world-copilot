# 引用完整性规则（悬空引用/指向不存在对象等）。
"""Reference integrity rules."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

# 字段路径 -> 策划能看懂的说法。消息与建议动作共用同一份映射，避免同一个字段在两处被翻成两种说法。
_FIELD_LABELS = {
    "giver_npc": "任务发布人",
    "location": "任务地点",
    "controlling_faction": "据点的控制阵营",
    "speaker_id": "说话人",
    "event_id": "关联事件",
}
_STAGE_LOCATION = re.compile(r"stages\.(\d+)\.location")
_STAGE_ENTITIES = re.compile(r"stages\.(\d+)\.required_entities")

_TARGET_LABELS = {
    "quest": "任务",
    "poi": "据点",
    "dialogue": "对话",
    "quest_event_ref": "任务事件关联",
}

# 修复器（patches/fixers.py::_fix_unknown_entity_ref）会直接删掉整个字段的那几个字段；
# 建议动作要与它实际会做的事保持一致，就必须知道这条边界。
_REMOVABLE_FIELDS = {"giver_npc", "location", "speaker_id", "controlling_faction"}


def _field_label(field_path: str) -> str:
    """把内部字段路径翻成策划能看懂的说法；翻不出来的原样返回（目前枚举已覆盖全部调用点）。"""
    if field_path in _FIELD_LABELS:
        return _FIELD_LABELS[field_path]
    stage_location = _STAGE_LOCATION.fullmatch(field_path)
    if stage_location:
        return f"第 {int(stage_location.group(1)) + 1} 阶段的地点"
    stage_entities = _STAGE_ENTITIES.fullmatch(field_path)
    if stage_entities:
        return f"第 {int(stage_entities.group(1)) + 1} 阶段需要的对象"
    return field_path


def _target_label(target_ref: str) -> str:
    kind, _, object_id = target_ref.partition(":")
    return f"{_TARGET_LABELS.get(kind, kind)}《{object_id}》"


def _suggest_unknown_entity_fix(target_ref: str, field_path: str, entity_id: str) -> str:
    """与修复器 ``_fix_unknown_entity_ref`` 实际会做的事保持一致：
    可整字段删除的字段 -> 建议删除该字段；阶段必要对象列表 -> 建议只去掉那一个 id；
    阶段地点 -> 建议清空该字段；其余（当前只有任务事件关联的关联事件）修复器不处理，
    给出同样具体但不假装有自动修复的建议。"""
    label = _field_label(field_path)
    target = _target_label(target_ref)
    if field_path in _REMOVABLE_FIELDS:
        return f"删掉{target}的{label}这一项（不再指向 '{entity_id}'），或者把它改成正确的对象 id。"
    if _STAGE_ENTITIES.fullmatch(field_path):
        return f"从{target}的{label}列表里去掉 '{entity_id}' 这一项，列表里的其它对象保留。"
    if _STAGE_LOCATION.fullmatch(field_path):
        return f"清空{target}的{label}（不再指向 '{entity_id}'），或者把它改成正确的地点 id。"
    return (
        f"把{target}的{label}改成一个真实存在的对象 id（当前的 '{entity_id}' 不存在），"
        "或者删掉这条引用。"
    )


class MissingEntityReferenceRule:
    code = "UNKNOWN_ENTITY_REF"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, field_path, entity_id in _entity_references(ctx):
            if entity_id and not ctx.has_object(entity_id):
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"{_target_label(target_ref)}的{_field_label(field_path)}写了一个不存在的"
                        f"对象 '{entity_id}' —— 内容库里根本没有这个对象，游戏运行到这里会出问题。"
                    ),
                    suggested_action=_suggest_unknown_entity_fix(target_ref, field_path, entity_id),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path=field_path)],
                )


class DeprecatedEntityReferenceRule:
    code = "DEPRECATED_ENTITY_REF"
    severity = Severity.WARNING
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, field_path, entity_id in _entity_references(ctx):
            entity = ctx.bundle.entities.get(entity_id)
            if entity is not None and entity.status == "deprecated":
                target = _target_label(target_ref)
                label = _field_label(field_path)
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"{target}的{label}指向的对象 '{entity_id}' 已经被标记为停用 —— 这个对象"
                        "可能已经下线或改名，继续引用它容易出岔子。"
                    ),
                    # 是否有替代对象、该换成谁，内容里没有记录，只能如实说明需要人来判断，
                    # 不编一个假的替代 id。
                    suggested_action=(
                        f"需要策划确认 '{entity_id}' 为什么停用、应该换成哪个对象："
                        f"确认后把{target}的{label}改成新对象的 id；如果这条引用已经不需要了，"
                        "也可以直接删除。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path=field_path)],
                )


class MissingPrerequisiteRule:
    code = "PREREQ_MISSING"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            for prereq_id in quest.prerequisites:
                if prereq_id not in ctx.bundle.quests:
                    target_ref = f"quest:{quest.id}"
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"任务《{quest.id}》的前置任务列表里，写了一个不存在的任务 "
                            f"'{prereq_id}' —— 这个前置条件永远无法满足，玩家也就永远解锁不了"
                            "这个任务。"
                        ),
                        suggested_action=(
                            f"从任务《{quest.id}》的前置任务列表里删掉 '{prereq_id}' 这一项；"
                            "如果这个前置任务本来就该存在，改成正确的任务 id。"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=f"prerequisites.{prereq_id}",
                            )
                        ],
                    )


class MissingDialogueReferenceRule:
    code = "MISSING_DIALOGUE_REF"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            for dialogue_id in quest.dialogue_refs:
                if dialogue_id not in ctx.bundle.dialogues:
                    target_ref = f"quest:{quest.id}"
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"任务《{quest.id}》关联的对话列表里，写了一个不存在的对话 "
                            f"'{dialogue_id}' —— 玩家推进到这里会加载不到对应的对话内容。"
                        ),
                        suggested_action=(
                            f"从任务《{quest.id}》关联的对话列表里删掉 '{dialogue_id}' 这一项；"
                            "如果对话只是换了 id，改成正确的对话 id。"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=f"dialogue_refs.{dialogue_id}",
                            )
                        ],
                    )


def _entity_references(ctx: AuditContext) -> Iterable[tuple[str, str, str]]:
    for quest in ctx.bundle.quests.values():
        target_ref = f"quest:{quest.id}"
        if quest.giver_npc:
            yield target_ref, "giver_npc", quest.giver_npc
        if quest.location:
            yield target_ref, "location", quest.location
        for index, stage in enumerate(quest.stages):
            if stage.location:
                yield target_ref, f"stages.{index}.location", stage.location
            for entity_id in stage.required_entities:
                yield target_ref, f"stages.{index}.required_entities", entity_id

    for poi in ctx.bundle.pois.values():
        if poi.controlling_faction:
            yield f"poi:{poi.id}", "controlling_faction", poi.controlling_faction

    for dialogue in ctx.bundle.dialogues.values():
        if dialogue.speaker_id:
            yield f"dialogue:{dialogue.id}", "speaker_id", dialogue.speaker_id

    for event_ref in ctx.bundle.quest_event_refs.values():
        if event_ref.event_id:
            yield f"quest_event_ref:{event_ref.id}", "event_id", event_ref.event_id
