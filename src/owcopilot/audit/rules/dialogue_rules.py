# 对话树结构完整性规则（悬空跳转/不可达节点/环等）。
"""Dialogue tree structural integrity rules."""

from __future__ import annotations

from collections.abc import Iterable

from ...content.models import EntityType
from ...logic import parse_expr, refs_in
from ...logic.expr import LogicSyntaxError
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


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
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=f"dialogue_tree:{tree.id}",
                        message=(
                            f"Dialogue tree '{tree.id}' link at {path} points to "
                            f"missing node {target!r}"
                        ),
                        evidence=[Evidence(kind="dialogue_tree", path=path)],
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
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=f"dialogue_tree:{tree.id}",
                        message=(
                            f"Dialogue tree '{tree.id}' references unknown speaker '{speaker}'"
                        ),
                        evidence=[Evidence(kind="dialogue_tree", path=f"speaker:{speaker}")],
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
                            yield self._issue(
                                tree.id,
                                "DIALOGUE_CONDITION_SYNTAX_ERROR",
                                path,
                                f"condition does not parse: {exc}",
                            )
                            tree_expr = None
                        if tree_expr is not None:
                            for ref in sorted(refs_in(tree_expr)):
                                if known(ref):
                                    continue
                                yield self._issue(
                                    tree.id,
                                    self.code,
                                    path,
                                    f"condition references '{ref}', not declared in quest "
                                    f"'{quest.id}' logic (nor a known quest/reputation ref)",
                                )
                    for effect in choice.effects:
                        if known(effect.var):
                            continue
                        yield self._issue(
                            tree.id,
                            self.code,
                            path,
                            f"effect mutates '{effect.var}', not declared in quest "
                            f"'{quest.id}' logic (nor a known quest/reputation ref)",
                        )

    def _issue(self, tree_id: str, code: str, path: str, message: str) -> Issue:
        return Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref=f"dialogue_tree:{tree_id}",
            message=f"[{code}] Dialogue tree '{tree_id}' {message}",
            evidence=[Evidence(kind="dialogue_tree", path=path)],
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
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=f"dialogue_tree:{tree.id}",
                    message=(
                        f"Dialogue tree '{tree.id}' node '{node_id}' is unreachable from root"
                    ),
                    evidence=[Evidence(kind="dialogue_tree", path=f"nodes/{node_id}")],
                )
