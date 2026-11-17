"""Bind abstract workflow guidance to the current frozen task, never to a prior story."""

from __future__ import annotations

import json
import re
from typing import Any

from .models import TaskRecord, WorkflowInputSlot, WorkflowNode

_PLACEHOLDER = re.compile(r"\{\{([a-zA-Z][a-zA-Z0-9_]*)\}\}")


def bind_guidance(node: WorkflowNode, task: TaskRecord, raw_slots: list[dict[str, Any]]) -> str:
    if not node.guidance:
        return ""
    slots = {slot.name: slot for slot in map(WorkflowInputSlot.model_validate, raw_slots)}
    values: dict[str, Any] = {
        "brief": task.request.brief,
        "protected": task.request.protected,
        "entities": [
            {key: row.get(key) for key in ("id", "name", "type", "description")}
            for row in task.base_bundle.get("entities", {}).values()
        ],
        "quests": [
            {key: row.get(key) for key in ("id", "title", "objective")}
            for row in task.base_bundle.get("quests", {}).values()
        ],
        "reference_ids": task.request.reference_ids,
    }

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in slots:
            raise ValueError(f"方法指导引用未声明的输入槽位：{name}")
        return json.dumps(values[slots[name].source], ensure_ascii=False)

    return _PLACEHOLDER.sub(substitute, node.guidance)


def task_with_guidance(
    node: WorkflowNode, task: TaskRecord, raw_slots: list[dict[str, Any]]
) -> TaskRecord:
    guidance = bind_guidance(node, task, raw_slots)
    if not guidance:
        return task
    return task.model_copy(
        update={
            "feedback": [
                *task.feedback,
                {
                    "text": "可复用步骤方法指导（已绑定当前任务，不是原故事事实）：" + guidance,
                    "scope": "method_guidance",
                    "target_operation": node.operation,
                    "priority": "author_and_project_constraints_first",
                },
            ]
        }
    )
