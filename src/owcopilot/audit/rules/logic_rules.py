# 任务逻辑层规则（WS-A）：把逻辑层发现的确定性问题以审计 finding 暴露。
"""Quest logic-layer rules (WS-A): surface the deterministic logic problems found by
``logic.audit_quest_logic`` (undefined vars / type errors / unreachable stages / deadlocks) plus
the bundle-aware dangling-reference check (stage/quest ids that do not exist).

消息与建议动作全部中文化、面向策划：本文件不改 ``logic/audit.py`` 与 ``logic/semantics.py``（不是
本文件的地盘），而是在这里把它们回传的英文 ``LogicIssue.message``/``.ref`` 解析成确定的中文说法——
标签格式（``"stage <id> precondition: ..."`` 等）与类型错误的四种句式都是那两个模块目前唯一会产出
的稳定形状，解析不到的形状一律原样保留英文细节，绝不瞎猜。全程不调用模型。"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ...content.models import EntityType, Quest
from ...logic import audit_quest_logic, parse_expr, refs_in
from ...logic.audit import LogicIssue
from ...logic.expr import LogicSyntaxError
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

# ``logic/audit.py`` 的 ``_expressions()`` 给每条表达式贴的标签前缀——解析出"这是任务前置/哪个阶段
# 的前置/哪个分支的条件"，从而在中文消息里点明位置。
_STAGE_LABEL = re.compile(r"^stage (?P<stage>\S+) precondition: (?P<detail>.*)$", re.S)
_BRANCH_LABEL = re.compile(r"^branch (?P<branch>\S+) condition: (?P<detail>.*)$", re.S)
_QUEST_LABEL_PREFIX = "quest precondition: "

# ``logic/semantics.type_errors()`` 目前只产出这四种句式；碰到其它形状就诚实地原样保留英文细节。
_CANNOT_COMPARE = re.compile(r"^cannot compare (\S+) to (\S+)$")
_NEEDS_INT_OPERANDS = re.compile(r"^'(.+)' needs integer operands, got (\S+) and (\S+)$")
_NEEDS_BOOL_OPERANDS = re.compile(r"^'(.+)' needs booleans, got (\S+)$")
_NEEDS_BOOL_NOT = re.compile(r"^'not' needs a boolean, got (\S+)$")

_TYPE_NAME_CN = {
    "bool": "真假值(bool)",
    "int": "数字(int)",
    "enum": "文本(enum)",
    "None": "未知类型",
}


def _cn_type(name: str) -> str:
    return _TYPE_NAME_CN.get(name, name)


def _expression_location(quest_id: str, message: str) -> tuple[str, str]:
    """把 ``LogicIssue.message`` 拆成 (中文定位短语, 剩余细节)。"""
    stage_match = _STAGE_LABEL.match(message)
    if stage_match:
        stage = stage_match.group("stage")
        return f"任务《{quest_id}》阶段 '{stage}' 的前置条件", stage_match.group("detail")
    branch_match = _BRANCH_LABEL.match(message)
    if branch_match:
        branch = branch_match.group("branch")
        return f"任务《{quest_id}》分支 '{branch}' 的触发条件", branch_match.group("detail")
    if message.startswith(_QUEST_LABEL_PREFIX):
        return f"任务《{quest_id}》的整体前置条件", message[len(_QUEST_LABEL_PREFIX) :]
    return f"任务《{quest_id}》的逻辑表达式", message


def _translate_type_mismatch(detail: str) -> str:
    match = _CANNOT_COMPARE.match(detail)
    if match:
        left, right = _cn_type(match.group(1)), _cn_type(match.group(2))
        return f"两边类型对不上，没法比较：一边是{left}，一边是{right}。"
    match = _NEEDS_INT_OPERANDS.match(detail)
    if match:
        op, left, right = match.group(1), _cn_type(match.group(2)), _cn_type(match.group(3))
        return f"比较符 '{op}' 需要两边都是数字，但现在一边是{left}，一边是{right}。"
    match = _NEEDS_BOOL_OPERANDS.match(detail)
    if match:
        op, side = match.group(1), _cn_type(match.group(2))
        return f"逻辑运算符 '{op}' 需要两边都是真假值，但这边是{side}。"
    match = _NEEDS_BOOL_NOT.match(detail)
    if match:
        return f"取反(not)后面需要跟一个真假值，但这边是{_cn_type(match.group(1))}。"
    return detail  # 未知句式：原样保留，不假装翻译成功


def _translate_logic_issue(quest: Quest, issue: LogicIssue) -> tuple[str, str]:
    """把一条 bundle 无关的 ``LogicIssue``（未定义变量/类型错误/不可达阶段/死局）翻成
    (中文消息, 建议动作)。"""
    location, detail = _expression_location(quest.id, issue.message)

    if issue.code == "LOGIC_UNDEFINED_VAR":
        var = detail.removeprefix("undefined variable: ").strip()
        message = (
            f"{location}用到了变量 '{var}'，但这个变量没有在任务《{quest.id}》的变量表里声明。"
        )
        action = (
            f"在任务《{quest.id}》的任务逻辑变量表里新增一个 id 为 '{var}' 的变量，"
            f"或者把{location}里的 '{var}' 改成一个已经声明过的变量 id。"
        )
        return message, action

    if issue.code == "LOGIC_TYPE_MISMATCH":
        message = f"{location}有类型错误：{_translate_type_mismatch(detail)}"
        action = (
            f"打开这份任务文件，检查{location}左右两边引用的变量类型是不是写反了；"
            "具体应该改成什么类型需要人工判断。"
        )
        return message, action

    if issue.code == "LOGIC_SYNTAX_ERROR":
        message = f"{location}写错了，语法解析失败（{detail}）—— 这个表达式在游戏里永远不会生效。"
        action = f"打开这份任务文件，检查{location}的书写；具体怎么改需要人工判断。"
        return message, action

    if issue.code == "LOGIC_UNREACHABLE_STAGE":
        stage_id = issue.ref
        message = (
            f"任务《{quest.id}》的阶段 '{stage_id}' 从任务开始的地方怎么走都到不了 —— "
            "玩家永远推进不到这个阶段。"
        )
        action = (
            f"检查任务《{quest.id}》的分支设置，看是不是漏了一条能走到阶段 '{stage_id}' 的路径；"
            f"如果这个阶段确实不需要了，可以把它从阶段列表里删掉。"
        )
        return message, action

    if issue.code == "LOGIC_DEADLOCK":
        message = (
            f"任务《{quest.id}》从第一个阶段出发，没有任何一条路径能走到结局 —— "
            "这个任务永远无法通关。"
        )
        stage_ids = [stage.id for stage in quest.stages]
        if len(stage_ids) >= 2:
            action = (
                f"检查任务《{quest.id}》的分支设置，需要人工补一条能从阶段 '{stage_ids[0]}' "
                f"走到某个结局阶段（比如 '{stage_ids[-1]}'）的路径。"
            )
        else:
            action = f"需要人工检查任务《{quest.id}》的阶段与分支设置，补一条能走到结局的路径。"
        return message, action

    # 未来若 logic/audit.py 新增了这里没覆盖的代码：至少给出定位，不假装有具体建议。
    return f"{location}：{detail}", "需要人工判断具体改法。"


class QuestLogicRule:
    code = "QUEST_LOGIC"
    severity = Severity.ERROR
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            if quest.logic is None:
                continue
            target = f"quest:{quest.id}"
            for logic_issue in audit_quest_logic(quest):
                message, action = _translate_logic_issue(quest, logic_issue)
                yield self._issue(target, logic_issue.code, message, action=action)
            yield from self._dangling(ctx, quest, target)

    def _dangling(self, ctx: AuditContext, quest: Quest, target: str) -> Iterable[Issue]:
        logic = quest.logic
        assert logic is not None
        stage_ids = {stage.id for stage in quest.stages}
        for stage in logic.stage_logic:
            if stage.stage_id not in stage_ids:
                yield self._issue(
                    target,
                    "LOGIC_DANGLING_STATE_REF",
                    f"任务《{quest.id}》的阶段逻辑引用了一个不存在的阶段 '{stage.stage_id}' —— "
                    "这段阶段逻辑绑定的阶段本身都没有，永远不会生效。",
                    action=(
                        f"在任务《{quest.id}》的阶段列表里新增一个 id 为 '{stage.stage_id}' 的"
                        f"阶段，或者把这段阶段逻辑改成绑定到一个已经存在的阶段 id。"
                    ),
                )
        for branch in logic.branches:
            for field_name, ref_id in (("起点", branch.from_stage), ("终点", branch.to_stage)):
                if ref_id and ref_id not in stage_ids:
                    yield self._issue(
                        target,
                        "LOGIC_DANGLING_STATE_REF",
                        f"任务《{quest.id}》的分支 '{branch.id}' 的{field_name}阶段写的是 "
                        f"'{ref_id}'，但这个阶段不存在。",
                        action=(
                            f"在任务《{quest.id}》的阶段列表里新增一个 id 为 '{ref_id}' 的阶段，"
                            f"或者把分支 '{branch.id}' 的{field_name}阶段改成已经存在的阶段 id。"
                        ),
                    )
        for quest_id in logic.unlocks:
            if quest_id not in ctx.bundle.quests:
                yield self._issue(
                    target,
                    "LOGIC_DANGLING_STATE_REF",
                    f"任务《{quest.id}》完成后要解锁的任务 '{quest_id}' 在世界观里不存在。",
                    action=(
                        f"新建一个 id 为 '{quest_id}' 的任务，或者把《{quest.id}》要解锁的任务 id "
                        "改成一个已经存在的任务。"
                    ),
                )
        yield from self._dangling_quest_refs(ctx, quest, target)
        yield from self._dangling_reputation_refs(ctx, quest, target)

    def _labeled_sources(self, quest: Quest) -> list[tuple[str, str]]:
        """(中文定位短语, 表达式源码) —— 覆盖任务整体前置、各阶段前置、各分支触发条件。"""
        logic = quest.logic
        assert logic is not None
        out = [(f"任务《{quest.id}》的整体前置条件", logic.precondition)]
        out += [
            (f"任务《{quest.id}》阶段 '{stage.stage_id}' 的前置条件", stage.precondition)
            for stage in logic.stage_logic
        ]
        out += [
            (f"任务《{quest.id}》分支 '{branch.id}' 的触发条件", branch.condition)
            for branch in logic.branches
        ]
        return out

    def _dangling_quest_refs(
        self, ctx: AuditContext, quest: Quest, target: str
    ) -> Iterable[Issue]:
        for location, source in self._labeled_sources(quest):
            if not source.strip():
                continue
            try:
                tree = parse_expr(source)
            except LogicSyntaxError:
                continue  # syntax errors are already reported by audit_quest_logic
            for ref in refs_in(tree):
                if not (ref.startswith("quest:") and ref.endswith(".done")):
                    continue
                ref_quest_id = ref[len("quest:") : -len(".done")]
                if ref_quest_id in ctx.bundle.quests:
                    continue
                yield self._issue(
                    target,
                    "LOGIC_DANGLING_STATE_REF",
                    f"{location}里引用了任务 '{ref_quest_id}' 的完成状态({ref})，但这个任务在"
                    "世界观里不存在。",
                    action=(
                        f"新建一个 id 为 '{ref_quest_id}' 的任务，或者把{location}里的 '{ref}' "
                        "改成一个已经存在的任务 id。"
                    ),
                )

    def _dangling_reputation_refs(
        self, ctx: AuditContext, quest: Quest, target: str
    ) -> Iterable[Issue]:
        """A `rep:<faction_id>` reference (read in a condition, or written by a stage/branch effect)
        must name a real faction entity — otherwise a choice's reputation consequence points at a
        faction that does not exist, the kind of silent inconsistency the audit is here to catch."""
        logic = quest.logic
        assert logic is not None
        factions = {e.id for e in ctx.bundle.entities.values() if e.type == EntityType.FACTION}
        seen: set[str] = set()

        def check(ref_name: str, location: str) -> Iterable[Issue]:
            if ref_name.startswith("rep:") and ref_name not in seen:
                seen.add(ref_name)
                faction = ref_name[len("rep:") :]
                if faction not in factions:
                    yield self._issue(
                        target,
                        "LOGIC_DANGLING_STATE_REF",
                        f"{location}引用了阵营声望 '{ref_name}'，但世界观里没有叫 '{faction}' "
                        "的阵营。",
                        action=(
                            f"在实体列表里新建一个 id 为 '{faction}' 的阵营，或者把{location}里"
                            f"的 '{ref_name}' 改成一个已经存在的阵营 id。"
                        ),
                    )

        for location, source in self._labeled_sources(quest):
            if not source.strip():
                continue
            try:
                tree = parse_expr(source)
            except LogicSyntaxError:
                continue
            for ref in refs_in(tree):
                yield from check(ref, location)

        effects = [
            (effect, f"任务《{quest.id}》阶段 '{stage.stage_id}' 完成时的效果")
            for stage in logic.stage_logic
            for effect in stage.effects_on_complete
        ]
        effects += [
            (effect, f"任务《{quest.id}》分支 '{branch.id}' 的效果")
            for branch in logic.branches
            for effect in branch.effects
        ]
        for effect, location in effects:
            yield from check(effect.var, location)

    def _issue(self, target: str, code: str, message: str, *, action: str = "") -> Issue:
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=target,
            message=f"[{code}] {message}",
            evidence=[Evidence(kind="logic", target_ref=target, path=code)],
            suggested_action=action,
        )
