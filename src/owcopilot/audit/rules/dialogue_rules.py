# 对话树结构完整性规则（悬空跳转/不可达节点/环等）
# + 人物自述前后矛盾（同一 bool 变量被后续无门禁选项翻转）
"""Dialogue tree structural integrity rules.

Also home to ``DialogueSelfReportInconsistencyRule`` — a purely structural check for a specific
self-consistency gap: a choice sets a boolean self-report variable, and a later reachable choice
flips it back with no condition gating on that variable. See the class docstring for the exact
(deliberately narrow) criteria.

消息与建议动作全部中文化、面向策划：不出现蛇形字段名/类名等代码标识符，只保留对象 id、节点 id 与
内容里本来就有的 DSL 字面量（如 ``rep:faction_id``、``quest:x.done``）。建议动作一律由本文件内的
确定性字符串拼接生成，不调用任何模型——参见 ``docs/研究/可执行报告改造_计划.md`` 的纪律。"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ...content.models import DialogueTree, EntityType
from ...logic import parse_expr, refs_in
from ...logic.expr import LogicSyntaxError
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity
from ..source_location import node_ref_from_path

_CHOICE_PATH = re.compile(r"nodes/([^/]+)/choices/(\d+)")
_NEXT_NODE_PATH = re.compile(r"nodes/([^/]+)/next_node")
_NODE_PATH = re.compile(r"nodes/([^/]+)")


def _describe_path(path: str) -> str:
    """把一条证据路径（如 ``nodes/n030/choices/0``）翻成策划看得懂的定位短语。

    找不到已知形状时原样返回路径本身，绝不瞎编一个听着像话但其实不对的说法。"""
    if path == "root_node":
        return "开场节点(根节点)"
    choice_match = _CHOICE_PATH.fullmatch(path)
    if choice_match:
        node_id, index = choice_match.group(1), int(choice_match.group(2))
        return f"第 {node_id} 个节点的第 {index + 1} 个选项"
    next_match = _NEXT_NODE_PATH.fullmatch(path)
    if next_match:
        return f"第 {next_match.group(1)} 个节点结束后的自动跳转"
    # 光指到节点本身（``nodes/n030``）：不翻的话，"nodes/n030" 这串原样进消息，就是策划看不懂的
    # 代码路径——正是本轮要消灭的那种黑话。
    node_match = _NODE_PATH.fullmatch(path)
    if node_match:
        return f"第 {node_match.group(1)} 个节点"
    return path


class DialogueTreeBrokenLinkRule:
    """Every choice/next pointer and the root must land on an existing node."""

    code = "DIALOGUE_TREE_BROKEN_LINK"
    severity = Severity.ERROR
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for tree in ctx.bundle.dialogue_trees.values():
            targets: list[tuple[str, str | None]] = [("root_node", tree.root_node or None)]
            for node in tree.nodes.values():
                targets.append((f"nodes/{node.id}/next_node", node.next_node))
                for index, choice in enumerate(node.choices):
                    targets.append((f"nodes/{node.id}/choices/{index}", choice.next_node))
            for path, target in targets:
                if target is None and path != "root_node":
                    continue  # an absent next_node ends the branch by design
                if target is None or target not in tree.nodes:
                    yield self._issue(tree.id, path, target)

    def _issue(self, tree_id: str, path: str, missing: str | None) -> Issue:
        location = _describe_path(path)
        if missing is None:
            message = (
                f"对话树《{tree_id}》没有指定开场节点(根节点为空)——"
                "玩家一进入这棵对话树就没有内容可看。"
            )
            action = "打开这份对话树文件，把根节点设置成节点列表里已经存在的某个节点 id。"
        else:
            message = (
                f"对话树《{tree_id}》里，{location}指向了一个不存在的节点 '{missing}' "
                "—— 玩家走到这里会卡住，后面的内容全部看不到。"
            )
            action = (
                f"要么在这份对话树里新增一个 id 为 '{missing}' 的节点补上这段内容，"
                f"要么把{location}的跳转目标改成一个已经存在的节点 id。"
            )
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=message,
            evidence=[Evidence(kind="dialogue_tree", path=path)],
            node_ref=node_ref_from_path(path),
            suggested_action=action,
        )


class DialogueTreeUnknownSpeakerRule:
    """Node speakers and listed participants must exist in the entity graph."""

    code = "DIALOGUE_TREE_UNKNOWN_SPEAKER"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for tree in ctx.bundle.dialogue_trees.values():
            speakers = {node.speaker_id for node in tree.nodes.values() if node.speaker_id}
            speakers.update(tree.participants)
            for speaker in sorted(speakers):
                if speaker not in ctx.bundle.entities:
                    yield self._issue(tree.id, speaker)

    def _issue(self, tree_id: str, speaker: str) -> Issue:
        path = f"speaker:{speaker}"
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=(
                f"对话树《{tree_id}》里用到了说话人 '{speaker}'，但世界观里没有叫这个 id 的角色"
                "或阵营 —— 这个说话人可能还没建好，也可能是 id 写错了。"
            ),
            evidence=[Evidence(kind="dialogue_tree", path=path)],
            node_ref=node_ref_from_path(path),
            suggested_action=(
                f"在实体列表里新建一个 id 为 '{speaker}' 的角色或阵营，或者把对话树里这个说话人"
                "的 id 改成一个已经存在的角色/阵营 id。"
            ),
        )


class DialogueChoiceConditionRule:
    """A choice must read AND write state that actually exists. For a tree tied to a quest
    (`quest_id`): the variables its `condition` reads and its `effects` mutate must be declared in
    that quest's logic; cross-quest state (`quest:<id>.done`) and faction reputation
    (`rep:<faction>`) are recognized by convention. Otherwise a branch silently never (or always)
    fires — or an effect mutates a variable nothing else reads — exactly the consistency gaps the
    audit exists to surface. Standalone trees (no quest_id / no logic) have no scope to check."""

    code = "DIALOGUE_CONDITION_UNDEFINED_VAR"
    severity = Severity.WARNING
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        factions = {e.id for e in ctx.bundle.entities.values() if e.type == EntityType.FACTION}
        for tree in ctx.bundle.dialogue_trees.values():
            quest = ctx.bundle.quests.get(tree.quest_id or "")
            if quest is None or quest.logic is None:
                continue  # no quest-logic scope to validate the condition's variables against
            declared = {var.id for var in quest.logic.variables}

            def known(ref: str) -> bool:
                if ref in declared:  # noqa: B023 — declared rebinds per tree by design
                    return True
                if ref.startswith("quest:") and ref.endswith(".done"):
                    return True
                return ref.startswith("rep:") and ref[len("rep:") :] in factions  # noqa: B023

            for node in tree.nodes.values():
                for index, choice in enumerate(node.choices):
                    path = f"nodes/{node.id}/choices/{index}"
                    if choice.condition.strip():
                        try:
                            tree_expr = parse_expr(choice.condition)
                        except LogicSyntaxError as exc:
                            yield self._syntax_issue(tree.id, path, exc)
                            tree_expr = None
                        if tree_expr is not None:
                            for ref in sorted(refs_in(tree_expr)):
                                if known(ref):
                                    continue
                                yield self._undeclared_issue(
                                    tree.id, quest.id, path, ref, "condition"
                                )
                    for effect in choice.effects:
                        if known(effect.var):
                            continue
                        yield self._undeclared_issue(tree.id, quest.id, path, effect.var, "effect")

    def _syntax_issue(self, tree_id: str, path: str, exc: LogicSyntaxError) -> Issue:
        location = _describe_path(path)
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=(
                f"[DIALOGUE_CONDITION_SYNTAX_ERROR] 对话树《{tree_id}》里，{location}的显示条件"
                f"写错了，解析失败（{exc}）—— 这个条件在游戏里永远不会生效。"
            ),
            evidence=[Evidence(kind="dialogue_tree", path=path)],
            node_ref=node_ref_from_path(path),
            suggested_action=(
                f"打开这份对话树文件，检查{location}的显示条件文本，修正其中的运算符或括号；"
                "具体应该改成什么样，需要结合这句台词原本想表达的意思人工判断。"
            ),
        )

    def _undeclared_issue(
        self, tree_id: str, quest_id: str, path: str, ref: str, kind: str
    ) -> Issue:
        location = _describe_path(path)
        if kind == "condition":
            message = (
                f"对话树《{tree_id}》里，{location}的显示条件用到了变量 '{ref}'，但这个变量没有"
                f"在任务《{quest_id}》的逻辑里声明 —— 这个条件可能永远不会按预期生效。"
            )
        else:
            message = (
                f"对话树《{tree_id}》里，{location}选中后会修改变量 '{ref}'，但这个变量没有在"
                f"任务《{quest_id}》的逻辑里声明 —— 这个效果改的东西没有任何地方会去读它。"
            )
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=message,
            evidence=[Evidence(kind="dialogue_tree", path=path)],
            node_ref=node_ref_from_path(path),
            suggested_action=(
                f"在任务《{quest_id}》的任务逻辑变量表里新增一个 id 为 '{ref}' 的变量，"
                f"或者把{location}里的 '{ref}' 改成一个已经声明过的变量 id。"
            ),
        )


class DialogueTreeMissingTextKeyRule:
    """A node/choice line with real text but no ``text_key`` never reaches localization export.

    A1 remediation: XLIFF, the Unity Localization CSV and the UE StringTable CSV
    (``exporters/xliff.py``, ``exporters/unity_localization.py``, ``exporters/unreal_datatable.py``)
    all key their translation units on ``text_key``; a line with text but no key is silently
    untranslatable. WARNING, not ERROR — a key-less line still plays fine in-engine as an inline
    string, it just cannot be handed to a translator."""

    code = "DIALOGUE_TREE_MISSING_TEXT_KEY"
    severity = Severity.WARNING
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for tree in ctx.bundle.dialogue_trees.values():
            for node in tree.nodes.values():
                if node.text.strip() and not node.text_key:
                    yield self._issue(tree.id, f"nodes/{node.id}", node.id)
                for index, choice in enumerate(node.choices):
                    if choice.text.strip() and not choice.text_key:
                        yield self._issue(
                            tree.id, f"nodes/{node.id}/choices/{index}", node.id, index=index
                        )

    def _issue(self, tree_id: str, path: str, node_id: str, *, index: int | None = None) -> Issue:
        location = _describe_path(path)
        suggested_key = (
            f"dlg.{tree_id}.{node_id}" if index is None else f"dlg.{tree_id}.{node_id}.c{index}"
        )
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=(
                f"对话树《{tree_id}》里，{location}写了台词文本，但没有配置本地化键 —— 这句台词"
                "导不出翻译表，只能一直留在游戏里的原文，没法交给翻译。"
            ),
            evidence=[Evidence(kind="dialogue_tree", path=path)],
            node_ref=node_ref_from_path(path),
            suggested_action=(
                f"给{location}加一个本地化键，按现有命名习惯可以用 '{suggested_key}'。"
            ),
        )


class DialogueTreeUnreachableNodeRule:
    """Nodes that cannot be reached from the root are dead content."""

    code = "DIALOGUE_TREE_UNREACHABLE_NODE"
    severity = Severity.WARNING
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for tree in ctx.bundle.dialogue_trees.values():
            if tree.root_node not in tree.nodes:
                continue  # broken-link rule already covers a missing root
            reachable: set[str] = set()
            stack = [tree.root_node]
            while stack:
                node_id = stack.pop()
                if node_id in reachable or node_id not in tree.nodes:
                    continue
                reachable.add(node_id)
                node = tree.nodes[node_id]
                if node.next_node:
                    stack.append(node.next_node)
                stack.extend(choice.next_node for choice in node.choices if choice.next_node)
            for node_id in sorted(set(tree.nodes) - reachable):
                yield self._issue(tree.id, node_id)

    def _issue(self, tree_id: str, node_id: str) -> Issue:
        path = f"nodes/{node_id}"
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=(
                f"对话树《{tree_id}》里，第 {node_id} 个节点从开场节点(根节点)出发怎么走都到不了"
                "—— 这段内容玩家永远看不到。"
            ),
            evidence=[Evidence(kind="dialogue_tree", path=path)],
            node_ref=node_ref_from_path(path),
            suggested_action=(
                f"检查这份对话树里其他节点的跳转链，看看是不是漏了一条能走到第 {node_id} 个节点"
                f"的路径；如果这段内容确实不再需要了，可以把第 {node_id} 个节点从对话树里删掉。"
            ),
        )


class DialogueSelfReportInconsistencyRule:
    """A choice sets a boolean self-report variable; a later, reachable choice flips it back with
    no condition gating on that variable — a player-visible self-contradiction (e.g. a character
    says "I have read books" in one line, then "I have never read books" in a later line, with
    nothing stopping the second line from firing on the very path where the first already did).

    Deliberately narrow, purely structural, zero-LLM criteria — every one of these must hold for a
    (choice A, choice B) pair to be reported:
      1. A has an effect ``op == "set"`` on some **bool** variable ``v``.
      2. B sits on a node reachable from A (via ``next_node``/choice links, starting at
         ``A.next_node``); reachability is a plain graph search, cycle-safe via a visited set.
      3. B also has an effect ``op == "set"`` on the SAME ``v``, to the OPPOSITE bool value.
      4. B's ``condition`` does not reference ``v`` at all — i.e. nothing gates B on the very
         value A just set, so the path "pick A, then pick B" is legally reachable.

    Criterion 3 is intentionally strict: B must itself ``set`` the same variable. A choice whose
    text merely *reads* as contradictory but whose effects touch a different variable (or no
    variable) will NOT be flagged — that would need a semantic/LLM signal this rule does not use.
    Recall is deliberately sacrificed for zero false positives: if B's condition fails to parse, we
    cannot prove it does not gate on ``v``, so we do not report (a syntax error there is already
    surfaced by ``DialogueChoiceConditionRule``)."""

    code = "DIALOGUE_SELF_REPORT_INCONSISTENCY"
    severity = Severity.WARNING
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for tree in ctx.bundle.dialogue_trees.values():
            yield from self._check_tree(tree)

    def _check_tree(self, tree: DialogueTree) -> Iterable[Issue]:
        for node_a in tree.nodes.values():
            for index_a, choice_a in enumerate(node_a.choices):
                for effect_a in choice_a.effects:
                    if effect_a.op != "set" or not isinstance(effect_a.value, bool):
                        continue
                    yield from self._flag_flips(
                        tree, node_a.id, index_a, effect_a.var, effect_a.value, choice_a.next_node
                    )

    def _flag_flips(
        self,
        tree: DialogueTree,
        node_a_id: str,
        index_a: int,
        var: str,
        value_a: bool,
        start: str | None,
    ) -> Iterable[Issue]:
        for node_b_id in self._reachable_from(tree, start):
            node_b = tree.nodes[node_b_id]
            for index_b, choice_b in enumerate(node_b.choices):
                if node_b_id == node_a_id and index_b == index_a:
                    continue  # never compare a choice against itself
                for effect_b in choice_b.effects:
                    if (
                        effect_b.op == "set"
                        and effect_b.var == var
                        and isinstance(effect_b.value, bool)
                        and effect_b.value != value_a
                        and not self._condition_covers(choice_b.condition, var)
                    ):
                        yield self._issue(
                            tree.id, node_a_id, index_a, node_b_id, index_b, var, value_a,
                            effect_b.value,
                        )

    @staticmethod
    def _reachable_from(tree: DialogueTree, start: str | None) -> set[str]:
        """Every node reachable starting AT ``start`` (inclusive), following ``next_node`` and
        choice links. Visited-set based so a cyclic dialogue tree cannot hang the search."""
        if start is None or start not in tree.nodes:
            return set()
        visited: set[str] = set()
        stack = [start]
        while stack:
            node_id = stack.pop()
            if node_id in visited or node_id not in tree.nodes:
                continue
            visited.add(node_id)
            node = tree.nodes[node_id]
            if node.next_node:
                stack.append(node.next_node)
            stack.extend(choice.next_node for choice in node.choices if choice.next_node)
        return visited

    @staticmethod
    def _condition_covers(condition: str, var: str) -> bool:
        """Whether ``condition`` references ``var`` at all (any gate on it, any comparison, any
        polarity — we do not need to know WHICH value it requires, only that the author already
        considered this variable here). An unparsable condition is treated as covering ``var``:
        we cannot prove otherwise, and this rule must never produce a false positive."""
        if not condition.strip():
            return False
        try:
            expr = parse_expr(condition)
        except LogicSyntaxError:
            return True
        return var in refs_in(expr)

    def _issue(
        self,
        tree_id: str,
        node_a_id: str,
        index_a: int,
        node_b_id: str,
        index_b: int,
        var: str,
        value_a: bool,
        value_b: bool,
    ) -> Issue:
        path_a = f"nodes/{node_a_id}/choices/{index_a}"
        path_b = f"nodes/{node_b_id}/choices/{index_b}"
        a_zh = "真" if value_a else "假"
        b_zh = "真" if value_b else "假"
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=(
                f"对话树《{tree_id}》里，第 {node_a_id} 个节点的第 {index_a + 1} 个选项会把变量 "
                f"'{var}' 设为{a_zh}；玩家选完这个选项之后还能走到第 {node_b_id} 个节点的第 "
                f"{index_b + 1} 个选项，那个选项没有任何条件挡着，会把同一个变量 '{var}' 又改回"
                f"{b_zh} —— 这是一句台词自己打自己脸的自相矛盾（比如先说『读过书』，后面又能选"
                "『从没读过书』）。"
            ),
            evidence=[
                Evidence(kind="dialogue_tree", path=path_a),
                Evidence(kind="dialogue_tree", path=path_b),
            ],
            node_ref=node_ref_from_path(path_a),
            suggested_action=(
                f"给第 {node_b_id} 个节点的第 {index_b + 1} 个选项加一条条件，比如 "
                f'"{var} == {str(value_b).lower()}"，这样玩家选过第 {node_a_id} 个节点的第 '
                f"{index_a + 1} 个选项之后，这条自相矛盾的台词就不会再出现。"
            ),
        )


class DialogueUngatedSiblingChoiceRule:
    """One choice on a node is gated on a boolean variable; a sibling choice on the SAME node is
    not gated on it at all — so on the path where that variable already holds, the player can pick
    the ungated sibling and contradict what was established.

    The signal is the author's own gate: putting ``said_read_books == true`` on one line proves the
    author knew this variable decides what the character may say *here*; a sibling line left ungated
    is then a gap, not a deliberate choice. That inference is why this stays structural and
    zero-LLM — it never guesses whether two lines *mean* opposite things, it only reports that the
    author gated one sibling and not another on the same node.

    Reported when all hold:
      1. Choice A on node N has a condition referencing bool variable ``v``.
      2. Sibling choice B on the SAME node N has no condition referencing ``v``.
      3. ``v`` is set somewhere in the tree (some choice's effects ``set`` it) — otherwise the gate
         is about outside state this tree never establishes, and there is nothing to contradict.

    Complements :class:`DialogueSelfReportInconsistencyRule`, which needs B to itself flip ``v``.
    Here B need not touch ``v`` at all — the common real shape, where the contradicting line simply
    says something inconsistent without writing any variable back.
    """

    code = "DIALOGUE_UNGATED_SIBLING_CHOICE"
    severity = Severity.WARNING
    category = Category.LOGIC

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for tree in ctx.bundle.dialogue_trees.values():
            established = self._bool_vars_set_in_tree(tree)
            if not established:
                continue
            for node in tree.nodes.values():
                yield from self._check_node(tree.id, node, established)

    @staticmethod
    def _bool_vars_set_in_tree(tree: DialogueTree) -> set[str]:
        """Bool variables this tree itself establishes via a ``set`` effect."""
        return {
            effect.var
            for node in tree.nodes.values()
            for choice in node.choices
            for effect in choice.effects
            if effect.op == "set" and isinstance(effect.value, bool)
        }

    def _check_node(self, tree_id: str, node: object, established: set[str]) -> Iterable[Issue]:
        choices = list(node.choices)  # type: ignore[attr-defined]
        if len(choices) < 2:
            return
        gated: dict[str, list[int]] = {}
        for index, choice in enumerate(choices):
            for var in self._gated_vars(choice.condition) & established:
                gated.setdefault(var, []).append(index)

        for var, gated_indices in sorted(gated.items()):
            gated_condition = choices[gated_indices[0]].condition
            for index, choice in enumerate(choices):
                if index in gated_indices or var in self._gated_vars(choice.condition):
                    continue
                yield self._issue(
                    tree_id,
                    node.id,  # type: ignore[attr-defined]
                    gated_indices[0],
                    index,
                    var,
                    gated_condition,
                )

    @staticmethod
    def _gated_vars(condition: str) -> set[str]:
        """Variables the condition references; unparsable conditions gate on nothing we can prove
        (their syntax error is already reported by ``DialogueChoiceConditionRule``), but we treat
        them as gating on everything so this rule cannot fire on them."""
        if not condition.strip():
            return set()
        try:
            return set(refs_in(parse_expr(condition)))
        except LogicSyntaxError:
            return set()

    def _issue(
        self,
        tree_id: str,
        node_id: str,
        gated_index: int,
        ungated_index: int,
        var: str,
        gated_condition: str,
    ) -> Issue:
        gated_path = f"nodes/{node_id}/choices/{gated_index}"
        ungated_path = f"nodes/{node_id}/choices/{ungated_index}"
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=(
                f"对话树《{tree_id}》里，第 {node_id} 个节点的第 {gated_index + 1} 个选项用条件"
                f"挡住了变量 '{var}'，但同一个节点的第 {ungated_index + 1} 个选项完全没检查这个"
                f"变量 —— 玩家在 '{var}' 已经成立的情况下，还是能选到这条没检查的台词，跟前面"
                "已经确立的设定自相矛盾。"
            ),
            evidence=[
                Evidence(kind="dialogue_tree", path=gated_path),
                Evidence(kind="dialogue_tree", path=ungated_path),
            ],
            node_ref=node_ref_from_path(gated_path),
            suggested_action=(
                f"参考第 {gated_index + 1} 个选项已有的条件『{gated_condition}』，给第 "
                f"{ungated_index + 1} 个选项也加上跟 '{var}' 相关、互斥的条件，把两条台词的"
                "出现时机分开。"
            ),
        )
