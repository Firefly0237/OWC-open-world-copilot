"""Semantic mechanics designs compiled into the existing narrative runtime.

The model chooses game consequences and prose. This compiler owns the redundant
control identifiers, selector expressions, complete effect indices and jumps.
It is an OWC compiler, not an implementation of the full ink or Yarn languages.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from ..content.models import (
    Branch,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    LogicVar,
    LogicVarType,
    Origin,
    Quest,
    QuestLogic,
    QuestStage,
    ReviewStatus,
    StageLogic,
)
from ..logic.expr import parse_expr, refs_in
from ..logic.semantics import Value, WorldState, evaluate, type_errors
from ..logic.stepper import PreviewSession, advance_preview, preview_view, start_preview
from .decision_contract import (
    ChoiceMap,
    DecisionChoiceRef,
    DecisionEffectMapV2,
    DecisionOptionEffect,
    validate_choice_coverage,
    validate_decisions,
)
from .models import EventPlan
from .narrative_localization_v2 import (
    DIALOGUE_COMPILER_VERSION,
    localize_tree,
    with_quest_localization,
)
from .requirements_v2 import BusinessRoleClaims

COMPILER_VERSION = "mechanic-ir-v2.1"
_CONTROL_PREFIX = "_owc_"
_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,79}$"
_SEALED_KEY = "mechanic_compilation_v2"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QuestStageDraftV2(_Model):
    id: str = Field(pattern=_ID_PATTERN)
    summary: str = Field(min_length=1, max_length=8000)
    location: str | None = None
    required_entities: list[str] = Field(default_factory=list, max_length=60)


class QuestDraftV2(_Model):
    """Author-facing content only; no raw mechanics or arbitrary metadata channel."""

    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=1000)
    objective: str = Field(min_length=1, max_length=12000)
    giver_npc: str | None = None
    location: str | None = None
    stages: list[QuestStageDraftV2] = Field(min_length=1, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=40)
    business_role_claims: BusinessRoleClaims | None = None

    @model_validator(mode="after")
    def unique_stages(self) -> QuestDraftV2:
        if len({stage.id for stage in self.stages}) != len(self.stages):
            raise ValueError("Quest草稿阶段ID重复")
        return self


def materialize_quest_draft(
    draft: QuestDraftV2 | Mapping[str, Any],
    *,
    quest_id: str,
    provenance: Mapping[str, Any] | None = None,
) -> Quest:
    """Move actual claims without copying expected requirements or inventing approval."""
    value = QuestDraftV2.model_validate(draft)
    if not quest_id.strip():
        raise ValueError("宿主任务ID不能为空")
    metadata: dict[str, Any] = {
        "quest_draft_v2": {
            "schema_version": 2,
            "source_hash": content_hash(value),
            "source": value.model_dump(mode="json"),
        }
    }
    if value.business_role_claims is not None:
        metadata["requirements_v2"] = {
            "business_role_claims": value.business_role_claims.model_dump(mode="json")
        }
    allowed_provenance = {"source_ref"}
    if provenance is not None and set(provenance) - allowed_provenance:
        raise ValueError("Quest草稿provenance只接受来源字段，不能覆盖内容或机制")
    return with_quest_localization(
        Quest(
            id=quest_id,
            title=value.title,
            objective=value.objective,
            giver_npc=value.giver_npc,
            location=value.location,
            stages=[QuestStage(**stage.model_dump(mode="json")) for stage in value.stages],
            tags=value.tags,
            metadata=metadata,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
            **dict(provenance or {}),
        )
    )


class MechanicVariableV2(_Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_:.]{0,119}$")
    name: str = Field(default="", max_length=1000)
    type: Literal["bool", "int", "enum"]
    default: bool | int | str
    enum_values: list[str] = Field(default_factory=list, max_length=80)

    @model_validator(mode="after")
    def valid_default(self) -> MechanicVariableV2:
        if self.id.startswith(_CONTROL_PREFIX) or self.id.startswith("quest:"):
            raise ValueError("游戏变量不能使用宿主控制命名空间或外部任务状态")
        _check_typed_value(self, self.default)
        if self.type != "enum" and self.enum_values:
            raise ValueError("只有enum变量可声明enum_values")
        if self.type == "enum" and (
            not self.enum_values or len(set(self.enum_values)) != len(self.enum_values)
        ):
            raise ValueError("enum_values必须非空且不重复")
        return self


class MechanicEffectV2(_Model):
    var: str = Field(min_length=1, max_length=120)
    op: Literal["set", "inc", "dec"] = "set"
    value: bool | int | str


class MechanicTransitionV2(_Model):
    purpose: str = Field(min_length=1, max_length=4000)
    to_stage: str | None = None
    outcome: str = Field(default="", max_length=8000)
    extra_guard: str = Field(default="", max_length=1500)
    effects: list[MechanicEffectV2] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def target_or_outcome(self) -> MechanicTransitionV2:
        if bool(self.to_stage) == bool(self.outcome.strip()):
            raise ValueError("过渡必须明确指定下一阶段或可见终局，二者恰选其一")
        return self


class MechanicStageV2(_Model):
    stage_id: str = Field(pattern=_ID_PATTERN)
    purpose: str = Field(min_length=1, max_length=4000)
    precondition: str = Field(default="", max_length=1500)
    decision_id: str | None = None
    options: list[MechanicTransitionV2] = Field(default_factory=list, max_length=6)
    automatic: MechanicTransitionV2 | None = None

    @model_validator(mode="after")
    def one_flow_kind(self) -> MechanicStageV2:
        if self.decision_id:
            if not 2 <= len(self.options) <= 6 or self.automatic is not None:
                raise ValueError("决策阶段需要2至6个选项且不能兼有自动过渡")
        elif self.options or self.automatic is None:
            raise ValueError("非决策阶段需要唯一显式自动过渡，不允许隐式顺序回落")
        return self


class MechanicDesignIRV2(_Model):
    schema_version: Literal[2] = 2
    purpose: str = Field(min_length=1, max_length=8000)
    variables: list[MechanicVariableV2] = Field(default_factory=list, max_length=100)
    quest_precondition: str = Field(default="", max_length=1500)
    stages: list[MechanicStageV2] = Field(min_length=1, max_length=64)


class MechanicCompilationV2(_Model):
    schema_version: Literal[2] = 2
    compiler_version: Literal["mechanic-ir-v2.1"] = "mechanic-ir-v2.1"
    raw_ir: dict[str, Any]
    input_plan: dict[str, Any]
    input_quest: dict[str, Any]
    quest: Quest
    decisions: DecisionEffectMapV2 | None
    manifest: dict[str, Any]
    validated_contract: dict[str, Any] | None


class _CompilationSealV2(_Model):
    schema_version: Literal[2] = 2
    compiler_version: Literal["mechanic-ir-v2.1"] = "mechanic-ir-v2.1"
    raw_ir: dict[str, Any]
    input_quest: dict[str, Any]
    manifest: dict[str, Any]
    require_distinct: bool


class DialogueOptionProseV2(_Model):
    text: str = Field(min_length=1, max_length=4000)
    response_text: str = Field(default="", max_length=12000)


class DialogueStageProseV2(_Model):
    stage_id: str = Field(pattern=_ID_PATTERN)
    speaker_id: str | None = None
    text: str = Field(min_length=1, max_length=16000)
    options: list[DialogueOptionProseV2] = Field(default_factory=list, max_length=6)


class DialogueDesignIRV2(_Model):
    schema_version: Literal[2] = 2
    participants: list[str] = Field(default_factory=list, max_length=60)
    stages: list[DialogueStageProseV2] = Field(min_length=1, max_length=64)


class DialogueOptionProseV3(DialogueOptionProseV2):
    ending_text: str = Field(default="", max_length=12000)


class DialogueStageProseV3(DialogueStageProseV2):
    options: list[DialogueOptionProseV3] = Field(default_factory=list, max_length=6)
    ending_text: str = Field(default="", max_length=12000)


class DialogueDesignIRV3(_Model):
    """Player text is distinct from the designer's mechanical outcome notes.

    V2 remains unchanged so its persisted source and hashes can still be verified.
    """

    schema_version: Literal[3] = 3
    participants: list[str] = Field(default_factory=list, max_length=60)
    stages: list[DialogueStageProseV3] = Field(min_length=1, max_length=64)


class MechanicDialogueCompilationV2(_Model):
    raw_ir: dict[str, Any]
    tree: DialogueTree
    choices: ChoiceMap
    manifest: dict[str, Any]
    playability: dict[str, Any]


def _check_typed_value(variable: MechanicVariableV2, value: Value) -> None:
    if variable.type == "bool" and type(value) is not bool:
        raise ValueError("bool游戏变量需要严格布尔值：" + variable.id)
    if variable.type == "int" and type(value) is not int:
        raise ValueError("int游戏变量需要严格整数：" + variable.id)
    if variable.type == "enum" and (type(value) is not str or value not in variable.enum_values):
        raise ValueError("enum游戏变量值不在声明范围：" + variable.id)


def _check_expression(text: str, variables: dict[str, MechanicVariableV2]) -> set[str]:
    if not text.strip():
        return set()
    expression = parse_expr(text)
    refs = refs_in(expression)
    if refs - variables.keys():
        raise ValueError("guard引用未声明的游戏变量或宿主selector")
    if type_errors(expression, {key: value.type for key, value in variables.items()}):
        raise ValueError("guard类型不合法")
    state = {key: value.default for key, value in variables.items()}
    if type(evaluate(expression, state)) is not bool:
        raise ValueError("guard必须是布尔表达式")
    return refs


def _true(text: str, state: Mapping[str, Value]) -> bool:
    return not text.strip() or evaluate(parse_expr(text), state) is True


def _stable_id(kind: str, quest_id: str, *parts: Any) -> str:
    return _CONTROL_PREFIX + kind + "_" + content_hash([quest_id, *parts])[:24]


def _transitions(stage: MechanicStageV2) -> list[MechanicTransitionV2]:
    return stage.options if stage.decision_id else [stage.automatic] if stage.automatic else []


def compile_mechanic_design(
    ir: MechanicDesignIRV2 | Mapping[str, Any],
    plan: EventPlan | Mapping[str, Any],
    quest: Quest | Mapping[str, Any],
    *,
    require_distinct: bool = False,
) -> MechanicCompilationV2:
    """Compile complete mechanics; unsupported temporal semantics fail explicitly."""
    source = MechanicDesignIRV2.model_validate(ir)
    plan = cast(EventPlan, EventPlan.model_validate(plan))
    quest = Quest.model_validate(quest)
    if quest.logic is not None:
        raise ValueError("MechanicDesignIR只消费无logic的Quest正文草稿，不能混入陈旧机制")
    if _SEALED_KEY in quest.metadata or "mechanic_ir_v2" in quest.metadata:
        raise ValueError("Quest正文草稿不能携带旧的宿主编译seal或机制metadata")
    stages = {stage.stage_id: stage for stage in source.stages}
    quest_stages = {stage.id for stage in quest.stages}
    if len(stages) != len(source.stages) or set(stages) != quest_stages or not quest.stages:
        raise ValueError("机制阶段必须不重不漏地覆盖真实Quest.stages")
    variables = {variable.id: variable for variable in source.variables}
    if len(variables) != len(source.variables):
        raise ValueError("游戏变量ID重复")
    defaults = {key: value.default for key, value in variables.items()}
    _check_expression(source.quest_precondition, variables)
    if not _true(source.quest_precondition, defaults):
        raise ValueError("本次声明初态不满足Quest前置，不能声明默认可玩")
    planned = {decision.id: decision for decision in plan.decisions}
    assigned = [stage.decision_id for stage in source.stages if stage.decision_id]
    if len(set(assigned)) != len(assigned) or set(assigned) != set(planned):
        raise ValueError("计划决策必须恰好绑定一个阶段，不能遗漏或重用")
    graph = nx.DiGraph()
    graph.add_nodes_from(stages)
    written: set[str] = set()
    for stage in source.stages:
        _check_expression(stage.precondition, variables)
        if stage.decision_id and len(stage.options) != len(planned[stage.decision_id].options):
            raise ValueError("机制选项数必须与计划顺序一一对应")
        for transition in _transitions(stage):
            if transition.to_stage:
                if transition.to_stage not in stages:
                    raise ValueError("机制过渡引用不存在的真实阶段")
                graph.add_edge(stage.stage_id, transition.to_stage)
            _check_expression(transition.extra_guard, variables)
            for effect in transition.effects:
                variable = variables.get(effect.var)
                if variable is None:
                    raise ValueError("真实后果引用未声明游戏变量，不能写宿主selector")
                if effect.op == "set":
                    _check_typed_value(variable, effect.value)
                elif variable.type != "int" or type(effect.value) is not int:
                    raise ValueError("inc/dec需要严格整数变量与整数增量")
                written.add(effect.var)
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("当前运行时不支持循环或sticky选择，不能伪编译为可玩流程")
    entry = quest.stages[0].id
    if {entry} | nx.descendants(graph, entry) != quest_stages:
        raise ValueError("存在从真实首阶段不可达的机制阶段")
    # All dialogue selectors are chosen before Quest settlement in this runtime.
    for stage in source.stages:
        for transition in _transitions(stage):
            refs = _check_expression(transition.extra_guard, variables)
            if refs & written:
                raise ValueError("当前预选对白不支持依赖任务内后续结算状态的动态option guard")
            if not _true(transition.extra_guard, defaults):
                raise ValueError("选项guard在声明初态不可用，不能声称全部计划选项有可执行见证")
    logic_variables = [
        LogicVar(**variable.model_dump(mode="json")) for variable in source.variables
    ]
    branches: list[Branch] = []
    bindings: list[dict[str, Any]] = []
    mappings: list[DecisionOptionEffect] = []
    selector_ids: list[str] = []
    for stage in source.stages:
        selector = None
        option_ids = []
        if stage.decision_id:
            selector = _stable_id("sel", quest.id, stage.decision_id)
            selector_ids.append(selector)
            option_ids = [
                _stable_id("opt", quest.id, stage.decision_id, index)
                for index in range(len(stage.options))
            ]
            logic_variables.append(
                LogicVar(
                    id=selector,
                    type=LogicVarType.ENUM,
                    default="unset",
                    enum_values=["unset", *option_ids],
                )
            )
        for index, transition in enumerate(_transitions(stage)):
            branch_id = _stable_id("br", quest.id, stage.stage_id, stage.decision_id, index)
            condition = transition.extra_guard
            if selector:
                selected = f"{selector} == '{option_ids[index]}'"
                condition = f"({selected}) and ({condition})" if condition.strip() else selected
            effects = [Effect(**effect.model_dump(mode="json")) for effect in transition.effects]
            branches.append(
                Branch(
                    id=branch_id,
                    from_stage=stage.stage_id,
                    condition=condition,
                    to_stage=transition.to_stage or "",
                    outcome=transition.outcome,
                    effects=effects,
                )
            )
            if stage.decision_id:
                mappings.append(
                    DecisionOptionEffect(
                        decision_id=stage.decision_id,
                        option_index=index,
                        branch_id=branch_id,
                        effect_indices=list(range(len(effects))),
                    )
                )
            bindings.append(
                {
                    "stage_id": stage.stage_id,
                    "decision_id": stage.decision_id,
                    "option_index": index if stage.decision_id else None,
                    "option_id": option_ids[index] if selector else None,
                    "selector_variable": selector,
                    "branch_id": branch_id,
                    "to_stage": transition.to_stage,
                    "outcome": transition.outcome,
                    "purpose": transition.purpose,
                }
            )
    logic = QuestLogic(
        variables=logic_variables,
        precondition=source.quest_precondition,
        stage_logic=[
            StageLogic(stage_id=stage.stage_id, precondition=stage.precondition)
            for stage in source.stages
        ],
        branches=branches,
    )
    raw = source.model_dump(mode="json")
    source_binding = {
        "compiler_version": COMPILER_VERSION,
        "ir_hash": content_hash(raw),
        "plan_hash": content_hash(plan),
        "input_quest_hash": content_hash(quest),
        "control_bindings": bindings,
    }
    metadata = dict(quest.metadata)
    metadata["mechanic_ir_v2"] = {**source_binding, "source_ir": raw}
    compiled = quest.model_copy(update={"logic": logic, "metadata": metadata}, deep=True)
    if "localization_v2" in quest.metadata:
        compiled = with_quest_localization(compiled)
    mapping = (
        DecisionEffectMapV2(quest_id=quest.id, selector_variables=selector_ids, options=mappings)
        if mappings
        else None
    )
    contract = (
        validate_decisions(plan, compiled, mapping, require_distinct=require_distinct)
        if mapping is not None
        else None
    )
    manifest = {
        **source_binding,
        "quest_hash": content_hash(compiled),
        "mapping_hash": content_hash(mapping) if mapping else None,
        "entry_stage_id": entry,
        "execution_order": "dialogue_selector_preselection_then_quest_settlement",
        "status": "compiled",
        "semantic_status": "unknown",
        "author_satisfaction": "unknown",
        "require_distinct": require_distinct,
    }
    manifest["manifest_hash"] = content_hash(manifest)
    return MechanicCompilationV2(
        raw_ir=raw,
        input_plan=plan.model_dump(mode="json"),
        input_quest=quest.model_dump(mode="json"),
        quest=compiled,
        decisions=mapping,
        manifest=manifest,
        validated_contract=contract,
    )


def verify_mechanic_compilation(
    compilation: MechanicCompilationV2 | Mapping[str, Any],
    plan: EventPlan | Mapping[str, Any],
) -> MechanicCompilationV2:
    value = MechanicCompilationV2.model_validate(compilation)
    if value.input_plan != EventPlan.model_validate(plan).model_dump(mode="json"):
        raise ValueError("编译产物与实际输入计划不一致")
    expected = compile_mechanic_design(
        value.raw_ir,
        plan,
        value.input_quest,
        require_distinct=value.manifest.get("require_distinct") is True,
    )
    if value.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("编译IR、正式产物或manifest已改变，不能沿用旧验证")
    return value


def seal_compilation_quest(
    compilation: MechanicCompilationV2 | Mapping[str, Any],
) -> Quest:
    """Carry reconstructible compiler lineage through the existing typed Quest port.

    The manifest hashes the unsealed actual Quest. The outer typed-graph artifact
    hashes the sealed Quest, avoiding a self-referential hash or hidden cache.
    This is a consistency seal, not a signature authenticating an external author.
    """
    value = MechanicCompilationV2.model_validate(compilation)
    value = verify_mechanic_compilation(value, value.input_plan)
    seal = _CompilationSealV2(
        raw_ir=value.raw_ir,
        input_quest=value.input_quest,
        manifest=value.manifest,
        require_distinct=value.manifest["require_distinct"],
    )
    metadata = dict(value.quest.metadata)
    metadata[_SEALED_KEY] = seal.model_dump(mode="json")
    return value.quest.model_copy(update={"metadata": metadata}, deep=True)


def read_compilation_quest(
    quest: Quest | Mapping[str, Any],
    plan: EventPlan | Mapping[str, Any],
) -> MechanicCompilationV2:
    """Recompile sealed lineage and compare the entire actual unsealed content."""
    actual = Quest.model_validate(quest)
    payload = actual.metadata.get(_SEALED_KEY)
    if payload is None:
        raise ValueError("Quest缺少宿主mechanic_compilation_v2来源seal")
    seal = _CompilationSealV2.model_validate(payload)
    compiled = compile_mechanic_design(
        seal.raw_ir, plan, seal.input_quest, require_distinct=seal.require_distinct
    )
    if seal.manifest != compiled.manifest:
        raise ValueError("Quest编译seal的manifest或计划/IR来源hash不一致")
    metadata = dict(actual.metadata)
    metadata.pop(_SEALED_KEY)
    stripped = actual.model_copy(update={"metadata": metadata}, deep=True)
    if content_hash(stripped) != content_hash(compiled.quest) or stripped.model_dump(
        mode="json"
    ) != compiled.quest.model_dump(mode="json"):
        raise ValueError("Quest实际正文/机制与sealed编译来源不一致")
    return compiled


def compile_mechanic_dialogue(
    ir: DialogueDesignIRV2 | DialogueDesignIRV3 | Mapping[str, Any],
    compilation: MechanicCompilationV2 | Mapping[str, Any],
    plan: EventPlan | Mapping[str, Any],
    *,
    tree_id: str,
    title: str = "",
) -> MechanicDialogueCompilationV2:
    """Compile prose against the same graph, never ask the model to rewrite selectors."""
    player_profile = isinstance(ir, DialogueDesignIRV3) or (
        isinstance(ir, Mapping) and ir.get("schema_version") == 3
    )
    prose = (DialogueDesignIRV3 if player_profile else DialogueDesignIRV2).model_validate(ir)
    dialogue_version = "dialogue-ir-v3.0" if player_profile else DIALOGUE_COMPILER_VERSION
    compiled = verify_mechanic_compilation(compilation, plan)
    source = MechanicDesignIRV2.model_validate(compiled.raw_ir)
    by_stage = {stage.stage_id: stage for stage in prose.stages}
    if len(by_stage) != len(prose.stages) or set(by_stage) != {s.stage_id for s in source.stages}:
        raise ValueError("对白正文必须不重不漏地覆盖真实机制阶段")
    if len(set(prose.participants)) != len(prose.participants) or any(
        stage.speaker_id and stage.speaker_id not in prose.participants for stage in prose.stages
    ):
        raise ValueError("对白speaker必须来自明确不重复的participants")
    node_ids = {
        stage.stage_id: _stable_id("node", tree_id, stage.stage_id) for stage in source.stages
    }
    nodes: dict[str, DialogueNode] = {}
    refs: list[DecisionChoiceRef] = []
    player_bindings: list[dict[str, Any]] = []
    player_endings: dict[str, str] = {}
    automatic_ids: list[str] = []
    bindings = {
        (item["stage_id"], item["option_index"]): item
        for item in compiled.manifest["control_bindings"]
    }
    for stage in source.stages:
        text = by_stage[stage.stage_id]
        if len(text.options) != len(stage.options):
            raise ValueError("对白选项正文数量必须与既定计划顺序相同")
        node_id = node_ids[stage.stage_id]
        choices = []
        next_node = None
        if stage.decision_id:
            for index, transition in enumerate(stage.options):
                binding = bindings[(stage.stage_id, index)]
                target = node_ids[transition.to_stage] if transition.to_stage else None
                response = text.options[index].response_text
                if player_profile:
                    option = text.options[index]
                    ending = option.ending_text
                    if transition.to_stage and ending:
                        raise ValueError("中间选项不能声明终局正文")
                    if not transition.to_stage and not ending.strip():
                        raise ValueError("终局选项需要独立的玩家结局正文")
                    if ending:
                        player_endings[binding["branch_id"]] = ending
                    player_bindings.append(
                        {
                            "node_id": node_id,
                            "choice_index": index,
                            "stage_id": stage.stage_id,
                            "branch_id": binding["branch_id"],
                            "selector_variable": binding["selector_variable"],
                            "option_value": binding["option_id"],
                        }
                    )
                if response:
                    response_id = _stable_id("response", tree_id, stage.stage_id, index)
                    nodes[response_id] = DialogueNode(
                        id=response_id, text=response, next_node=target
                    )
                    target = response_id
                choices.append(
                    DialogueChoice(
                        text=text.options[index].text,
                        next_node=target,
                        condition=transition.extra_guard,
                        effects=[
                            Effect(var=binding["selector_variable"], value=binding["option_id"])
                        ],
                    )
                )
                refs.append(
                    DecisionChoiceRef(
                        decision_id=stage.decision_id,
                        option_index=index,
                        node_id=node_id,
                        choice_index=index,
                    )
                )
        else:
            assert stage.automatic is not None
            next_node = node_ids[stage.automatic.to_stage] if stage.automatic.to_stage else None
            if player_profile:
                binding = bindings[(stage.stage_id, None)]
                automatic_ids.append(binding["branch_id"])
                ending = text.ending_text
                if stage.automatic.to_stage and ending:
                    raise ValueError("中间阶段不能声明终局正文")
                if not stage.automatic.to_stage and not ending.strip():
                    raise ValueError("终局自动阶段需要独立的玩家结局正文")
                if ending:
                    player_endings[binding["branch_id"]] = ending
        nodes[node_id] = DialogueNode(
            id=node_id,
            text=text.text,
            speaker_id=text.speaker_id,
            choices=choices,
            next_node=next_node,
        )
    tree = DialogueTree(
        id=tree_id,
        title=title,
        quest_id=compiled.quest.id,
        participants=prose.participants,
        root_node=node_ids[compiled.quest.stages[0].id],
        nodes=nodes,
        metadata={
            "mechanic_ir_v2": {
                "compiler_version": COMPILER_VERSION,
                "dialogue_compiler_version": dialogue_version,
                "mechanic_manifest_hash": compiled.manifest["manifest_hash"],
                "dialogue_ir_hash": content_hash(prose),
                "source_ir": prose.model_dump(mode="json"),
            }
        },
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )
    if player_profile:
        tree.metadata["player_presentation_v1"] = {
            "schema_version": 1,
            "quest_id": compiled.quest.id,
            "bindings": player_bindings,
            "automatic_branch_ids": automatic_ids,
            "endings": player_endings,
        }
    tree = localize_tree(tree)
    choice_map = ChoiceMap(quest_id=compiled.quest.id, tree_id=tree.id, choices=refs)
    report = validate_compiled_playability(
        plan,
        compiled.quest,
        compiled.decisions,
        tree,
        choice_map,
        require_distinct=compiled.manifest["require_distinct"],
    )
    manifest = {
        "compiler_version": COMPILER_VERSION,
        "dialogue_compiler_version": dialogue_version,
        "mechanic_manifest_hash": compiled.manifest["manifest_hash"],
        "dialogue_ir_hash": content_hash(prose),
        "tree_hash": content_hash(tree),
        "choices_hash": content_hash(choice_map),
        "playability_hash": content_hash(report),
    }
    manifest["manifest_hash"] = content_hash(manifest)
    return MechanicDialogueCompilationV2(
        raw_ir=prose.model_dump(mode="json"),
        tree=tree,
        choices=choice_map,
        manifest=manifest,
        playability=report,
    )


def validate_compiled_playability(
    plan: EventPlan | Mapping[str, Any],
    quest: Quest | Mapping[str, Any],
    mapping: DecisionEffectMapV2 | Mapping[str, Any] | None,
    tree: DialogueTree | Mapping[str, Any],
    choices: ChoiceMap | Mapping[str, Any],
    *,
    max_paths: int = 2048,
    require_distinct: bool = False,
) -> dict[str, Any]:
    """Exercise actual pure runtime paths; never substitute model claims for effects."""
    plan = cast(EventPlan, EventPlan.model_validate(plan))
    quest = Quest.model_validate(quest)
    tree = DialogueTree.model_validate(tree)
    choices = ChoiceMap.model_validate(choices)
    if quest.logic is None or type(max_paths) is not int or not 1 <= max_paths <= 10000:
        raise ValueError("实际路径检查需要正式logic和有效检查边界")
    mapped = DecisionEffectMapV2.model_validate(mapping) if mapping is not None else None
    if mapped is not None:
        validate_choice_coverage(
            plan, quest, tree, mapped, choices, require_distinct=require_distinct
        )
    elif plan.decisions or choices.choices or any(node.choices for node in tree.nodes.values()):
        raise ValueError("可选剧情缺少正式决策映射")
    selectors = set(mapped.selector_variables) if mapped else set()
    all_game_vars = {variable.id for variable in quest.logic.variables} - selectors
    branches = {branch.id: branch for branch in quest.logic.branches}
    if any(stage.effects_on_complete for stage in quest.logic.stage_logic):
        raise ValueError("编译合同不允许共同stage effects重复结算选项后果")
    initial = start_preview(quest=quest, dialogue_tree=tree)
    frontier: list[tuple[PreviewSession, list[tuple[str, int]]]] = [(initial, [])]
    completed: list[dict[str, Any]] = []
    covered = set()
    expected = {(ref.decision_id, ref.option_index) for ref in choices.choices}
    locations = {
        (ref.node_id, ref.choice_index): (ref.decision_id, ref.option_index)
        for ref in choices.choices
    }
    visits = 0
    distinct_checked: set[str] = set()
    while frontier:
        session, decisions_taken = frontier.pop()
        visits += 1
        if visits > max_paths * max(4, (len(tree.nodes) + len(quest.stages) + 2)):
            raise ValueError("实际路径状态空间超出检查边界，不能截断后声明通过")
        view = preview_view(session)
        if view["status"] == "completed":
            if len(completed) >= max_paths:
                raise ValueError("实际可玩路径超过检查边界，不能截断后声明通过")
            actual = WorldState(
                {variable.id: variable.default for variable in quest.logic.variables}
            )
            seen_branches = set()
            for step in session.history:
                if step.phase != "quest":
                    continue
                branch = branches.get(step.choice_id or "")
                if branch is None or branch.id in seen_branches:
                    raise ValueError("Quest实际路径存在未映射或重复结算分支")
                seen_branches.add(branch.id)
                for effect in branch.effects:
                    actual.apply(effect.var, effect.op, effect.value)
            final = actual.as_mapping()
            if any(final[key] != session.values[key] for key in all_game_vars):
                raise ValueError("实际游戏状态与分支恰好结算一次不一致")
            covered.update(decisions_taken)
            completed.append(
                {
                    "decisions": [list(key) for key in decisions_taken],
                    "branch_ids": [
                        step.choice_id for step in session.history if step.phase == "quest"
                    ],
                    "final_game_state": {key: session.values[key] for key in sorted(all_game_vars)},
                    "outcome": session.outcome,
                    "history_hash": content_hash(session.history),
                }
            )
            continue
        if view["status"] not in {"ready", "waiting_choice"}:
            raise ValueError("实际stepper路径无法完成：" + view["status"] + ":" + view["message"])
        if require_distinct and mapped is not None and session.phase == "quest":
            by_decision: dict[str, list[Branch]] = {}
            for item in mapped.options:
                branch = branches[item.branch_id]
                if branch.from_stage == session.cursor:
                    by_decision.setdefault(item.decision_id, []).append(branch)
            for decision_id, candidates in by_decision.items():
                check_key = content_hash([decision_id, session.values])
                if check_key in distinct_checked:
                    continue
                distinct_checked.add(check_key)
                states = []
                for branch in candidates:
                    projected = WorldState(session.values)
                    for effect in branch.effects:
                        projected.apply(effect.var, effect.op, effect.value)
                    values = projected.as_mapping()
                    states.append(content_hash({key: values[key] for key in all_game_vars}))
                if len(set(states)) != len(states):
                    raise ValueError("实际可达决策前态下的真实游戏后果相同：" + decision_id)
        options = view["choices"] or [{"id": None}]
        for option in options:
            action = option["id"]
            next_session = advance_preview(
                session,
                action_id=f"compile:{session.action_seq}:{action}",
                expected_seq=session.action_seq,
                choice_id=action,
            )
            taken = list(decisions_taken)
            if session.phase == "dialogue":
                if any(next_session.values[key] != session.values[key] for key in all_game_vars):
                    raise ValueError("对白提前应用了真实游戏后果")
                if action is not None:
                    node_id, index = action.rsplit(":", 1)
                    key = locations.get((node_id, int(index)))
                    if key is None or key in taken:
                        raise ValueError("实际对白选择缺少映射或重复选择同一决策")
                    taken.append(key)
            frontier.append((next_session, taken))
    if not completed or covered != expected:
        raise ValueError("并非每个真实计划选项都有可完成的运行时路径")
    payload = {
        "status": "checked",
        "mechanical_status": "validated",
        "semantic_status": "unknown",
        "author_satisfaction": "unknown",
        "scope": "all_compiled_paths_at_declared_defaults",
        "require_distinct": require_distinct,
        "quest_hash": content_hash(quest),
        "tree_hash": content_hash(tree),
        "mapping_hash": content_hash(mapped) if mapped else None,
        "choices_hash": content_hash(choices),
        "completed_path_count": len(completed),
        "covered_option_count": len(covered),
        "paths": completed,
        "limitations": [
            "只覆盖声明初态与当前有限前向图，不保证任意外部状态或Ink/Yarn全语言语义",
            "对白预选控制路线，Quest分阶段结算；未实现每阶段中途对白",
            "机械可执行和一次结算不证明自然语言意图、保护事实、业务语义或作者满意",
        ],
    }
    return {**payload, "report_hash": content_hash(payload)}


def mechanic_prompt_contract(
    plan: EventPlan | Mapping[str, Any], quest: Quest | Mapping[str, Any]
) -> dict[str, Any]:
    plan = cast(EventPlan, EventPlan.model_validate(plan))
    quest = Quest.model_validate(quest)
    return {
        "schema": MechanicDesignIRV2.model_json_schema(),
        "plan": plan.model_dump(mode="json"),
        "quest": quest.model_dump(mode="json"),
        "compiler_version": COMPILER_VERSION,
        "model_responsibility": [
            "声明真正游戏变量及严格初值，设计每个计划选项的真实effects、目的和下一阶段/终局",
            "每个游戏变量填写面向策划的name，如信任、线索已公开；内部id保持稳定",
            "按plan.decisions[].options顺序给选项；一个决策对应一个已有Quest阶段",
            "覆盖所有已有阶段，无决策阶段给唯一automatic；可多阶段、多决策与前向汇流",
            "extra_guard只允许读取任务内不改变的声明游戏变量，并须在声明初态可用",
        ],
        "host_responsibility": [
            "生成稳定option/selector/branch IDs、selector条件、完整effect_indices和正式映射",
            "拒绝未声明变量、循环、同阶段多个决策、动态提前gate或无法完成的实际路径",
        ],
        "forbidden_model_fields": [
            "selector_variables",
            "branch_id",
            "condition",
            "effect_indices",
        ],
        "semantic_status": "unknown",
    }


def dialogue_prompt_contract(
    compilation: MechanicCompilationV2 | Mapping[str, Any],
    plan: EventPlan | Mapping[str, Any],
) -> dict[str, Any]:
    value = verify_mechanic_compilation(compilation, plan)
    return {
        "schema": DialogueDesignIRV3.model_json_schema(),
        "plan": EventPlan.model_validate(plan).model_dump(mode="json"),
        "quest": value.quest.model_dump(mode="json"),
        "mechanic_ir": value.raw_ir,
        "control_bindings": value.manifest["control_bindings"],
        "instruction": (
            "只写每个实际阶段及其既定选项的正文，按已有顺序；"
            "text是当前玩家所读的场景或对白；options.text是简短行动，response_text是人物回应；"
            "终局选项或无决策终局阶段填写ending_text，写该路线具体发生的事，中间阶段不填。"
            "不要复述其他路线、数值结算、验证说明、工具调用或作者审批；"
            "人物只表达其能知道的事，来源和事实疑点在叙事中自然表现。"
            "策划目的与机制说明保留在plan/quest/mechanic_ir，不抄进玩家正文。"
            "宿主编译ID、跳转、条件、effects和ChoiceMap。"
        ),
        "forbidden_model_fields": ["effects", "condition", "next_node", "choices", "metadata"],
        "execution_order": value.manifest["execution_order"],
    }
