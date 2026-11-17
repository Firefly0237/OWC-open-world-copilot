"""Validated player presentation bindings over the existing execution contract.

This module does not select a route or apply effects. It verifies compiler-owned
choice/selector/branch bindings without importing the workflow compiler (which
itself exercises the stepper). Legacy, uncompiled quests have no such contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..content.models import DialogueTree, Quest
from .expr import BoolOp, Compare, Ref, StrLit, parse_expr, refs_in
from .semantics import type_errors


@dataclass(frozen=True)
class PlayerChoiceBinding:
    node_id: str
    choice_index: int
    stage_id: str
    branch_id: str
    selector_variable: str
    option_value: str


@dataclass(frozen=True)
class PlayerPresentation:
    bindings: tuple[PlayerChoiceBinding, ...]
    automatic_branch_ids: frozenset[str]
    endings: dict[str, str]


def _selector_guard(source: str, selector: str, value: str) -> bool:
    """A positive AND conjunct must require the selected enum, not merely mention it."""

    def required(node: Any) -> bool:
        if isinstance(node, BoolOp) and node.op == "and":
            return required(node.left) or required(node.right)
        return (
            isinstance(node, Compare)
            and node.op == "=="
            and (
                isinstance(node.left, Ref)
                and node.left.name == selector
                and isinstance(node.right, StrLit)
                and node.right.value == value
                or isinstance(node.right, Ref)
                and node.right.name == selector
                and isinstance(node.left, StrLit)
                and node.left.value == value
            )
        )

    return required(parse_expr(source))


def _matches_choice(tree: DialogueTree, selector: str, value: str) -> list[tuple[str, int]]:
    return [
        (node.id, index)
        for node in tree.nodes.values()
        for index, choice in enumerate(node.choices)
        if len(choice.effects) == 1
        and choice.effects[0].var == selector
        and choice.effects[0].op == "set"
        and choice.effects[0].value == value
    ]


def player_presentation_for(quest: Quest, tree: DialogueTree | None) -> PlayerPresentation | None:
    """Validate an explicit presentation or narrowly recover an old compiler binding.

    ``None`` identifies an uncompiled legacy quest, whose explicit stage stepping
    remains unchanged. A marked compiler artifact with a missing/ambiguous binding
    raises instead of falling back to design summaries or guessing a route.
    """
    explicit = tree.metadata.get("player_presentation_v1") if tree else None
    legacy = quest.metadata.get("mechanic_ir_v2")
    tree_compiler = tree.metadata.get("mechanic_ir_v2") if tree else None
    marked = explicit is not None or legacy is not None or tree_compiler is not None
    if not marked:
        return None
    if not tree or not quest.logic or tree.quest_id != quest.id:
        raise ValueError("编译场景缺少匹配的对白和任务逻辑。")

    if explicit is not None:
        if (
            not isinstance(explicit, dict)
            or type(explicit.get("schema_version")) is not int
            or explicit["schema_version"] != 1
        ):
            raise ValueError("试玩呈现绑定格式无效。")
        if explicit.get("quest_id") != quest.id:
            raise ValueError("试玩呈现绑定的场景不匹配。")
        raw_bindings = explicit.get("bindings")
        raw_automatic = explicit.get("automatic_branch_ids", [])
        endings = explicit.get("endings", {})
    else:
        if (
            not isinstance(legacy, dict)
            or not isinstance(tree_compiler, dict)
            or not legacy.get("compiler_version")
            or legacy.get("compiler_version") != tree_compiler.get("compiler_version")
            or not tree_compiler.get("mechanic_manifest_hash")
        ):
            raise ValueError("旧编译场景缺少完整的分支绑定。")
        controls = legacy.get("control_bindings")
        if not isinstance(controls, list) or not controls:
            raise ValueError("旧编译场景缺少分支绑定。")
        raw_bindings, raw_automatic, endings = [], [], {}
        for row in controls:
            if not isinstance(row, dict):
                raise ValueError("旧编译场景的分支绑定无效。")
            if row.get("decision_id") is None:
                if row.get("selector_variable") is not None or row.get("option_id") is not None:
                    raise ValueError("自动阶段不能包含待选择的分支。")
                raw_automatic.append(row.get("branch_id"))
                continue
            selector, value = row.get("selector_variable"), row.get("option_id")
            if not isinstance(selector, str) or not isinstance(value, str):
                raise ValueError("旧编译场景的选择状态绑定无效。")
            matches = _matches_choice(tree, selector, value)
            if len(matches) != 1 or matches[0][1] != row.get("option_index"):
                raise ValueError("旧编译场景的对白选项无法唯一绑定。")
            raw_bindings.append(
                {
                    "node_id": matches[0][0],
                    "choice_index": matches[0][1],
                    "stage_id": row.get("stage_id"),
                    "branch_id": row.get("branch_id"),
                    "selector_variable": selector,
                    "option_value": value,
                }
            )

    if not isinstance(raw_bindings, list) or not isinstance(raw_automatic, list):
        raise ValueError("试玩呈现缺少分支绑定列表。")
    if not isinstance(endings, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in endings.items()
    ):
        raise ValueError("结局正文绑定无效。")
    branches = {branch.id: branch for branch in quest.logic.branches}
    if len(branches) != len(quest.logic.branches):
        raise ValueError("场景存在重复分支编号。")
    bindings = []
    choice_refs: set[tuple[str, int]] = set()
    branch_refs: set[str] = set()
    selectors: set[str] = set()
    variables = {variable.id: variable for variable in quest.logic.variables}
    # The compiler permits only declared variables. The legacy simulator seeds
    # referenced names to False, so membership in the seeded state is not proof
    # that a condition is known or well-typed.
    expressions = [
        quest.logic.precondition,
        *(stage.precondition for stage in quest.logic.stage_logic),
        *(branch.condition for branch in quest.logic.branches),
    ]
    symbols = {key: variable.type.value for key, variable in variables.items()}
    for source in expressions:
        if source.strip():
            expression = parse_expr(source)
            if refs_in(expression) - variables.keys() or type_errors(expression, symbols):
                raise ValueError("场景条件引用了未声明或类型不匹配的状态。")
    for row in raw_bindings:
        names = ("node_id", "stage_id", "branch_id", "selector_variable", "option_value")
        if (
            not isinstance(row, dict)
            or any(not isinstance(row.get(name), str) or not row[name] for name in names)
            or type(row.get("choice_index")) is not int
        ):
            raise ValueError("试玩呈现的选项绑定无效。")
        binding = PlayerChoiceBinding(**{name: row[name] for name in (*names, "choice_index")})
        ref = (binding.node_id, binding.choice_index)
        branch = branches.get(binding.branch_id)
        variable = variables.get(binding.selector_variable)
        if (
            ref in choice_refs
            or binding.branch_id in branch_refs
            or branch is None
            or branch.from_stage != binding.stage_id
            or variable is None
            or binding.option_value not in variable.enum_values
            or _matches_choice(tree, binding.selector_variable, binding.option_value) != [ref]
            or not _selector_guard(
                branch.condition, binding.selector_variable, binding.option_value
            )
        ):
            raise ValueError("试玩呈现与实际选项、条件或分支不一致。")
        bindings.append(binding)
        choice_refs.add(ref)
        branch_refs.add(binding.branch_id)
        selectors.add(binding.selector_variable)
    if any(not isinstance(item, str) for item in raw_automatic):
        raise ValueError("自动阶段的分支绑定无效。")
    automatic = frozenset(raw_automatic)
    actual_choices = {
        (node.id, index) for node in tree.nodes.values() for index, _ in enumerate(node.choices)
    }
    if (
        len(automatic) != len(raw_automatic)
        or automatic & branch_refs
        or automatic | branch_refs != set(branches)
        or choice_refs != actual_choices
        or not set(endings) <= set(branches)
        or any(effect.var in selectors for branch in branches.values() for effect in branch.effects)
        or any(
            effect.var in selectors
            for stage in quest.logic.stage_logic
            for effect in stage.effects_on_complete
        )
    ):
        raise ValueError("试玩呈现没有完整对应实际场景分支。")
    for branch_id in automatic:
        branch = branches[branch_id]
        if (
            sum(row.from_stage == branch.from_stage for row in branches.values()) != 1
            or branch.condition.strip()
            and refs_in(parse_expr(branch.condition)) & selectors
        ):
            raise ValueError("自动阶段不能绕过尚需选择的分支。")
    return PlayerPresentation(tuple(bindings), automatic, dict(endings))
