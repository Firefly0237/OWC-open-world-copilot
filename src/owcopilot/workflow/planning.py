"""Restricted, schema-checked creative planning; no generated code or implicit fallback graph."""

from __future__ import annotations

import json
from typing import Any

import networkx as nx

from ..llm.gateway import LLMGateway
from ..llm.structured import complete_structured
from .catalog import operation_parameter_schema, validate_executable_parameters
from .models import Interpretation, TaskRecord, WorkflowSpec
from .operators import OPERATION_KINDS, OPERATION_REQUIREMENTS

INTERPRET_SYSTEM = (
    "你是游戏叙事意图解释者。将作者的片段或灵感解释成创作目标，列出自主补全的假设与保护项。"
    "只输出符合给定schema的JSON，不把推测写成项目事实，不要求作者先写完整规格。"
)
WORKFLOW_SYSTEM = (
    "你是叙事制作流程规划者。只从给定操作目录构造可执行DAG，不输出代码。"
    "根据当前项目重新绑定，不复制旧故事事实。每种operation最多一个节点。"
    "本原型至少需要plan_events、draft_quest、draft_logic和package。"
    "必需数据依赖放depends_on；等待其他步骤但允许其失败放optional_dependencies。"
    "draft_dialogue可以与任务起草独立；bind_dialogue需要逻辑和对白。"
    "audit、package应以optional_dependencies等所有相关节点结束，汇总部分成功和未知。"
    "最多16个节点，不包含循环、无限改写或自动扩大预算。只输出符合schema的JSON。"
    "parameters只能使用操作目录中的可执行参数；人物目标、分歧与创作要求不是参数，"
    "应作为事件计划和步骤guidance传给执行角色，不得写入不会被执行的伪参数。"
    "若采用给定的已接受方法，其指导须进入对应步骤guidance并声明input_slots；"
    "method_source_ids只填写实际采用的候选方法ID，不适用时留空，不把召回当采用。"
)


def planning_context(task: TaskRecord) -> dict[str, Any]:
    bundle = task.base_bundle
    return {
        "brief": task.request.brief,
        "protected": task.request.protected,
        "feedback": task.feedback,
        "entities": [
            {key: value.get(key) for key in ("id", "name", "type", "description")}
            for value in list(bundle.get("entities", {}).values())[:40]
        ],
        "quests": [
            {key: value.get(key) for key in ("id", "title", "objective")}
            for value in list(bundle.get("quests", {}).values())[:15]
        ],
    }


def interpret_task(gateway: LLMGateway, task: TaskRecord) -> Interpretation:
    return complete_structured(
        gateway,
        task="studio_interpret",
        system=INTERPRET_SYSTEM,
        user=json.dumps(
            {"input": planning_context(task), "schema": Interpretation.model_json_schema()},
            ensure_ascii=False,
        ),
        model_cls=Interpretation,
        max_repair_retries=0,
    )


def validate_bindings(spec: WorkflowSpec) -> WorkflowSpec:
    spec = spec.model_copy(deep=True)
    graph = nx.DiGraph()
    graph.add_nodes_from(node.id for node in spec.nodes)
    for item in spec.nodes:
        graph.add_edges_from(
            (parent, item.id) for parent in item.depends_on + item.optional_dependencies
        )
    for node in spec.nodes:
        if node.operation not in {"audit", "package"}:
            continue
        available = [
            other.id
            for other in spec.nodes
            if other.id != node.id
            and (
                node.operation == "package"
                or other.operation
                not in {
                    "audit",
                    "compare",
                    "package",
                }
            )
        ]
        missing_order = set(available) - nx.ancestors(graph, node.id)
        if missing_order:
            node.optional_dependencies.extend(sorted(missing_order))
            graph.add_edges_from((parent, node.id) for parent in missing_order)
            note = f"{node.id}按汇总合同增加可选等待依赖：{sorted(missing_order)}"
            if note not in spec.compile_notes:
                spec.compile_notes.append(note)
    spec = WorkflowSpec.model_validate(spec.model_dump())
    by_id = {node.id: node for node in spec.nodes}
    for node in spec.nodes:
        supplied = {by_id[parent].operation for parent in node.depends_on}
        missing = OPERATION_REQUIREMENTS[node.operation] - supplied
        if missing:
            raise ValueError(f"{node.operation}缺少必需产物依赖：{sorted(missing)}")
    return spec


def plan_workflow(
    gateway: LLMGateway,
    task: TaskRecord,
    interpretation: dict[str, Any],
    recalled: list[dict[str, Any]],
) -> WorkflowSpec:
    spec = complete_structured(
        gateway,
        task="studio_workflow_plan",
        system=WORKFLOW_SYSTEM,
        user=json.dumps(
            {
                "input": planning_context(task),
                "interpretation": interpretation,
                "operator_catalog": {
                    name: {
                        "required": sorted(needs),
                        "output": OPERATION_KINDS[name],
                        "parameters_schema": operation_parameter_schema(name),
                    }
                    for name, needs in OPERATION_REQUIREMENTS.items()
                },
                "recalled_method_hints": [
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "spec": item["spec"],
                        "retrieval": item.get("retrieval", "explicit_lexical_baseline"),
                    }
                    for item in recalled
                ],
                "schema": WorkflowSpec.model_json_schema(),
            },
            ensure_ascii=False,
        ),
        model_cls=WorkflowSpec,
        max_repair_retries=0,
    )
    if not {"plan_events", "draft_quest", "draft_logic", "package"}.issubset(
        {node.operation for node in spec.nodes}
    ):
        raise ValueError("工作图缺少原型必需的事件、任务、逻辑或结果整理步骤")
    if set(spec.method_source_ids) - {item["id"] for item in recalled}:
        raise ValueError("规划声称采用了未提供的方法来源")
    return validate_executable_parameters(validate_bindings(spec))
