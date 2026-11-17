"""Mechanical decision-to-branch and dialogue-to-selector contracts.

These checks bind generated plans to the existing QuestLogic/DialogueTree consumers.
They do not prove literary meaning, global path reachability, or author satisfaction.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from ..content.hash import content_hash
from ..content.models import Branch, DialogueTree, Effect, LogicVar, Quest
from ..logic.expr import parse_expr, refs_in
from ..logic.semantics import WorldState, evaluate, type_errors
from .models import EventPlan


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DecisionOptionEffect(_Model):
    decision_id: str = Field(min_length=1)
    option_index: int = Field(ge=0, strict=True)
    branch_id: str = Field(min_length=1)
    effect_indices: list[StrictInt] = Field(default_factory=list, max_length=64)


class DecisionEffectMap(_Model):
    schema_version: Literal[1] = 1
    quest_id: str = Field(min_length=1)
    options: list[DecisionOptionEffect] = Field(default_factory=list, max_length=60)


class DecisionEffectMapV2(_Model):
    """Explicit control selectors; all other declared variables remain game state."""

    schema_version: Literal[2] = 2
    quest_id: str = Field(min_length=1)
    options: list[DecisionOptionEffect] = Field(default_factory=list, max_length=60)
    selector_variables: list[str] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def distinct_selectors(self) -> DecisionEffectMapV2:
        if len(self.selector_variables) != len(set(self.selector_variables)) or any(
            not value.strip() for value in self.selector_variables
        ):
            raise ValueError("selector_variables必须是非空且不重复的显式控制变量ID")
        return self


def parse_decision_mapping(value: Any) -> DecisionEffectMap | DecisionEffectMapV2:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        raise ValueError("决策映射必须是结构化对象")
    version = value.get("schema_version", 1)
    if version == 2:
        return DecisionEffectMapV2.model_validate(value)
    if version == 1:
        return DecisionEffectMap.model_validate(value)
    raise ValueError("未支持的决策映射schema版本")


class DecisionChoiceRef(_Model):
    decision_id: str = Field(min_length=1)
    option_index: int = Field(ge=0, strict=True)
    node_id: str = Field(min_length=1)
    choice_index: int = Field(ge=0, strict=True)


class ChoiceMap(_Model):
    schema_version: Literal[1] = 1
    quest_id: str = Field(min_length=1)
    tree_id: str = Field(min_length=1)
    choices: list[DecisionChoiceRef] = Field(default_factory=list, max_length=60)


class _ValidatedContract(_Model):
    schema_version: Literal[1, 2] = 1
    status: Literal["validated"] = "validated"
    semantic_status: Literal["withheld"] = "withheld"
    require_distinct: bool
    distinct_scope: Literal["declared_default_state"] = "declared_default_state"
    plan: dict[str, Any]
    quest: dict[str, Any]
    mapping: dict[str, Any]
    plan_hash: str
    quest_hash: str
    mapping_hash: str
    contract_hash: str
    limitations: list[str]


def _variables(quest: Quest) -> dict[str, LogicVar]:
    if quest.logic is None or not quest.stages:
        raise ValueError("决策合同需要有阶段和正式logic的任务")
    variables = {variable.id: variable for variable in quest.logic.variables}
    if len(variables) != len(quest.logic.variables):
        raise ValueError("逻辑变量ID重复")
    for variable in variables.values():
        _typed_value(variable, variable.default)
    return variables


def _typed_value(variable: LogicVar, value: Any) -> None:
    kind = variable.type.value
    if kind == "bool" and type(value) is not bool:
        raise ValueError(f"布尔变量值类型不合法：{variable.id}")
    if kind == "int" and type(value) is not int:
        raise ValueError(f"整数变量值类型不合法：{variable.id}")
    if kind == "enum" and (not isinstance(value, str) or value not in variable.enum_values):
        raise ValueError(f"枚举变量值不在声明范围：{variable.id}")


def _effect(effect: Effect, variables: dict[str, LogicVar]) -> None:
    variable = variables.get(effect.var)
    if variable is None:
        raise ValueError("状态效果引用未声明的本任务变量：" + effect.var)
    if effect.op == "set":
        _typed_value(variable, effect.value)
    elif variable.type.value != "int" or type(effect.value) is not int:
        raise ValueError("inc/dec仅支持整数变量及整数增量")


def _expression(text: str, variables: dict[str, LogicVar], state: dict[str, Any]) -> bool:
    if not text.strip():
        return True
    expression = parse_expr(text)
    symbols = {key: value.type.value for key, value in variables.items()}
    if refs_in(expression) - symbols.keys():
        raise ValueError("条件引用未声明的本任务变量")
    if type_errors(expression, symbols):
        raise ValueError("条件表达式与声明类型不一致")
    result = evaluate(expression, state)
    if type(result) is not bool:
        raise ValueError("条件必须是布尔表达式")
    return result


def _effects_state(effects: list[Effect], defaults: dict[str, Any]) -> dict[str, Any]:
    state = WorldState(defaults)
    for effect in effects:
        state.apply(effect.var, effect.op, effect.value)
    return state.as_mapping()


def _plan_options(plan: EventPlan) -> set[tuple[str, int]]:
    if len({decision.id for decision in plan.decisions}) != len(plan.decisions):
        raise ValueError("计划决策ID重复")
    return {
        (decision.id, index)
        for decision in plan.decisions
        for index in range(len(decision.options))
    }


def _bindings(
    plan: EventPlan, quest: Quest, mapping: DecisionEffectMap | DecisionEffectMapV2
) -> tuple[dict[str, LogicVar], dict[tuple[str, int], Branch]]:
    variables = _variables(quest)
    if mapping.quest_id != quest.id:
        raise ValueError("决策映射与任务ID不匹配")
    assert quest.logic is not None
    stages = {stage.id for stage in quest.stages}
    branches = {branch.id: branch for branch in quest.logic.branches}
    if len(stages) != len(quest.stages) or len(branches) != len(quest.logic.branches):
        raise ValueError("任务阶段或分支ID重复")
    defaults = {key: variable.default for key, variable in variables.items()}
    selectors = (
        set(mapping.selector_variables) if isinstance(mapping, DecisionEffectMapV2) else set()
    )
    if selectors - variables.keys() or any(
        variables[name].type.value not in {"bool", "enum"} for name in selectors
    ):
        raise ValueError(
            "显式selector必须引用本任务已声明的bool／enum变量，不能将金钱等int状态改标控制变量"
        )
    for existing_branch in quest.logic.branches:
        if existing_branch.from_stage not in stages or (
            existing_branch.to_stage and existing_branch.to_stage not in stages
        ):
            raise ValueError("分支引用不存在的阶段")
        _expression(existing_branch.condition, variables, defaults)
        for effect in existing_branch.effects:
            _effect(effect, variables)
    bound = {}
    for item in mapping.options:
        key = (item.decision_id, item.option_index)
        if key in bound or key not in _plan_options(plan):
            raise ValueError("映射包含重复或不存在的决策选项")
        branch = branches.get(item.branch_id)
        if branch is None:
            raise ValueError("决策映射引用不存在的真实Branch")
        if selectors:
            condition_variables = (
                refs_in(parse_expr(branch.condition)) if branch.condition.strip() else set()
            )
            if not condition_variables & selectors:
                raise ValueError("每个决策分支条件必须实际读取显式selector")
            if any(effect.var in selectors and effect.op != "set" for effect in branch.effects):
                raise ValueError("分支对控制selector只允许set，不能隐含重复加减")
        if any(type(index) is not int or index < 0 for index in item.effect_indices):
            raise ValueError("效果索引必须是非负整数")
        if len(set(item.effect_indices)) != len(item.effect_indices) or set(
            item.effect_indices
        ) != set(range(len(branch.effects))):
            raise ValueError("效果索引必须完整且不重复地引用真实Branch.effects")
        bound[key] = branch
    if set(bound) != _plan_options(plan):
        raise ValueError("计划决策选项未被完整覆盖")
    return variables, bound


def validate_decisions(
    plan: EventPlan | dict[str, Any],
    quest: Quest | dict[str, Any],
    mapping: DecisionEffectMap | DecisionEffectMapV2 | dict[str, Any],
    *,
    require_distinct: bool = False,
) -> dict[str, Any]:
    """Return a content-bound contract after checking real branch references and effects."""
    plan = EventPlan.model_validate(plan)
    quest = Quest.model_validate(quest)
    mapping = parse_decision_mapping(mapping)
    variables, bound = _bindings(plan, quest, mapping)
    defaults = {key: variable.default for key, variable in variables.items()}
    if require_distinct:
        selectors = (
            set(mapping.selector_variables) if isinstance(mapping, DecisionEffectMapV2) else set()
        )
        for decision in plan.decisions:
            states = [
                content_hash(
                    {
                        key: value
                        for key, value in _effects_state(
                            bound[(decision.id, index)].effects, defaults
                        ).items()
                        if key not in selectors
                    }
                )
                for index in range(len(decision.options))
            ]
            if len(set(states)) != len(states):
                raise ValueError("明确要求不同状态后果，但选项的实际Branch.effects在声明初态相同")
    normalized: dict[str, Any] = {
        "plan": plan.model_dump(mode="json"),
        "quest": quest.model_dump(mode="json"),
        "mapping": mapping.model_dump(mode="json"),
    }
    payload: dict[str, Any] = {
        **normalized,
        "schema_version": mapping.schema_version,
        "status": "validated",
        "semantic_status": "withheld",
        "require_distinct": require_distinct,
        "distinct_scope": "declared_default_state",
        "plan_hash": content_hash(normalized["plan"]),
        "quest_hash": content_hash(normalized["quest"]),
        "mapping_hash": content_hash(normalized["mapping"]),
        "limitations": [
            "只核对计划选项到实际分支/效果的机械关联，不证明自然语言后果或文学意图",
            "状态差异仅在声明变量默认初态检查，不证明全路径可达或任意初态性质",
            "模型生成的映射和断言不是作者已确认要求",
        ],
    }
    return _ValidatedContract(**payload, contract_hash=content_hash(payload)).model_dump(
        mode="json"
    )


def validate_contract_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _ValidatedContract.model_validate(value)
    actual = validate_decisions(
        snapshot.plan, snapshot.quest, snapshot.mapping, require_distinct=snapshot.require_distinct
    )
    if snapshot.model_dump(mode="json") != actual:
        raise ValueError("已验证决策合同的来源或hash已改变")
    return actual


def validate_choice_coverage(
    plan: EventPlan | dict[str, Any],
    quest: Quest | dict[str, Any],
    tree: DialogueTree | dict[str, Any],
    mapping: DecisionEffectMap | DecisionEffectMapV2 | dict[str, Any],
    choices: ChoiceMap | dict[str, Any],
    *,
    require_distinct: bool = False,
) -> dict[str, Any]:
    """Check actual selector effects enable the mapped branch without applying outcomes twice."""
    plan = EventPlan.model_validate(plan)
    quest = Quest.model_validate(quest)
    tree = DialogueTree.model_validate(tree)
    mapping = parse_decision_mapping(mapping)
    choices = ChoiceMap.model_validate(choices)
    contract = validate_decisions(plan, quest, mapping, require_distinct=require_distinct)
    variables, bound = _bindings(plan, quest, mapping)
    if tree.quest_id != quest.id or choices.quest_id != quest.id or choices.tree_id != tree.id:
        raise ValueError("对白/choice映射与任务/对白ID不匹配")
    if tree.root_node not in tree.nodes:
        raise ValueError("对白根节点不存在")
    graph: dict[str, set[str]] = {key: set() for key in tree.nodes}
    for node_id, tree_node in tree.nodes.items():
        if tree_node.id != node_id:
            raise ValueError("对白节点键与ID不一致")
        targets = [tree_node.next_node, *(choice.next_node for choice in tree_node.choices)]
        for target in targets:
            if target is not None:
                if target not in tree.nodes:
                    raise ValueError("对白引用不存在的后继")
                graph[node_id].add(target)
    reachable = {tree.root_node}
    pending = [tree.root_node]
    while pending:
        for target in graph[pending.pop()]:
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    expected = _plan_options(plan)
    seen = set()
    locations = set()
    defaults = {key: variable.default for key, variable in variables.items()}
    for item in choices.choices:
        key = (item.decision_id, item.option_index)
        location = (item.node_id, item.choice_index)
        if key not in expected or key in seen or location in locations:
            raise ValueError("choice映射包含未知或重复选项/对白位置")
        node = tree.nodes.get(item.node_id)
        if node is None or item.node_id not in reachable or item.choice_index >= len(node.choices):
            raise ValueError("choice映射引用不存在或结构不可达的对白选项")
        choice = node.choices[item.choice_index]
        _expression(choice.condition, variables, defaults)
        for effect in choice.effects:
            _effect(effect, variables)
        branch = bound[key]
        if not branch.condition.strip():
            raise ValueError("决策分支必须有显式selector条件")
        condition_refs = refs_in(parse_expr(branch.condition))
        selector_refs = (
            set(mapping.selector_variables) & condition_refs
            if isinstance(mapping, DecisionEffectMapV2)
            else condition_refs
        )
        if not {effect.var for effect in choice.effects}.issubset(selector_refs):
            raise ValueError("对白选择只可设置所映射分支条件的selector变量")
        if isinstance(mapping, DecisionEffectMapV2) and any(
            effect.op != "set" for effect in choice.effects
        ):
            raise ValueError("对白对显式selector只允许set，不允许inc／dec")
        consequence_vars = {
            effect.var
            for mapped_key, candidate in bound.items()
            if mapped_key[0] == item.decision_id
            for effect in candidate.effects
            if not isinstance(mapping, DecisionEffectMapV2)
            or effect.var not in mapping.selector_variables
        }
        if {effect.var for effect in choice.effects} & consequence_vars:
            raise ValueError("对白selector不能提前应用分支后果，避免重复执行")
        selected_state = _effects_state(choice.effects, defaults)
        if isinstance(mapping, DecisionEffectMapV2):
            for effect in branch.effects:
                if effect.var in mapping.selector_variables and (
                    effect.op != "set" or selected_state[effect.var] != effect.value
                ):
                    raise ValueError(
                        "分支重设selector必须与实际对白选择后的值相同，不能改变所选路线"
                    )
        if not _expression(branch.condition, variables, selected_state):
            raise ValueError("实际对白selector效果不能满足所映射分支条件")
        other_branches = {
            candidate.id: candidate
            for other, candidate in bound.items()
            if other[0] == item.decision_id and candidate.id != branch.id
        }
        if any(
            _expression(candidate.condition, variables, selected_state)
            for candidate in other_branches.values()
        ):
            raise ValueError("实际选择同时启用了其他选项分支，映射不明确")
        seen.add(key)
        locations.add(location)
    if seen != expected:
        raise ValueError("choice映射未覆盖全部计划选项")
    actual_locations = {
        (node_id, index)
        for node_id, node in tree.nodes.items()
        for index in range(len(node.choices))
    }
    if locations != actual_locations:
        raise ValueError("存在未映射的对白选择，不能声明完整覆盖")
    return {
        "status": "checked",
        "mechanical_status": "validated",
        "semantic_status": "withheld",
        "contract_hash": contract["contract_hash"],
        "quest_hash": content_hash(quest),
        "tree_hash": content_hash(tree),
        "choices_hash": content_hash(choices),
        "decision_option_count": len(expected),
        "covered_choice_count": len(locations),
        "selector_scope": "declared_defaults_plus_choice_effects",
        "limitations": contract["limitations"]
        + ["分支条件的selector检查不证明该分支从全部游戏路径可达"],
    }
