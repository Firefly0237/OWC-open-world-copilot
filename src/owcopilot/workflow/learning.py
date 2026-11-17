"""Execution-grounded method induction from observable task records.

Only observable records are supplied. No hidden reasoning is requested, no provider is
constructed here, and the caller must own the single-dispatch gateway and durable budget.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..llm.gateway import LLMGateway
from ..llm.structured import complete_structured
from .models import WorkflowSpec
from .store import WorkflowStore

SLOT_PATTERN = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
LEARNING_SYSTEM = (
    "你是执行后方法归纳者，不是原故事续写者。根据真实可观察任务过程、节点输入输出、"
    "失败与修复、作者反馈，提炼可以指导另一任务的通用制作方法。"
    "将实际执行中的有效步骤抽象为可复用流程，不照抄初始计划。"
    "只输出给定schema的JSON，操作限给定白名单与数据依赖；不得输出可执行代码。"
    "每一步guidance必须是后续执行会消费的制作指导，而非状态说明。"
    "为原任务、保护项、当前项目实体等声明input_slots，guidance用{{槽位名}}引用。"
    "source仅可为brief/protected/entities/quests/reference_ids。"
    "用槽位抽象原人物、任务、变量、地点ID及项目路径，不把原案例答案或剧情事实写入方法。"
    "适用条件写在applicability，不宣称作者满意或效果提升。"
    "剔除失败路径中无用步骤，保留导致成功的通用修复原则；不得加入重试循环或扩大预算。"
    "parameters只允许plan_events/draft_quest的budget_tokens(1..8000)，"
    "draft_dialogue的max_nodes(1..24)/max_chars(1..600)，无需要时为空。"
    "至少包含plan_events、draft_quest、draft_logic、package，并正确交接其产物。"
)


def _redact(value: Any) -> Any:
    serialized = json.dumps(value, ensure_ascii=False)
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OWCOPILOT_API_KEY"):
        secret = os.getenv(name)
        if secret:
            serialized = serialized.replace(secret, "[redacted]")
    return json.loads(serialized)


def _events(store: WorkflowStore, run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    after = 0
    while batch := store.events(run_id, after):
        rows.extend(batch)
        next_after = int(batch[-1]["seq"])
        if next_after <= after:
            raise ValueError("事件游标未推进，拒绝构造不完整的学习trace")
        after = next_after
    return rows


def build_learning_trace(store: WorkflowStore, run_id: str) -> dict[str, Any]:
    """Read the actual source run and its task lineage; never fabricate an experience."""
    source_run = store.get_run(run_id)
    task = store.get_task(source_run.task_id)
    runs = [
        run
        for run in reversed(store.list_runs(task.id))
        if run.created_at <= source_run.created_at and run.revision <= source_run.revision
    ]
    run_ids = {run.id for run in runs}
    artifacts = [
        item
        for item in store.artifacts(task.id)
        if item.run_id in run_ids and item.revision <= source_run.revision
    ]
    outcomes = store.outcomes(source_run.id)
    selected = [
        store.get_artifact(row.artifact_id).model_dump(mode="json")
        for row in outcomes.values()
        if row.artifact_id
    ]
    if any(item["task_id"] != task.id for item in selected):
        raise ValueError("学习trace引用了其他任务的产物")
    trace = {
        "trace_version": 1,
        "source": {
            "task_id": task.id,
            "run_id": run_id,
            "project": task.project,
            "revision": source_run.revision,
            "current_revision": task.revision,
            "base_hash": task.base_hash,
        },
        "task_request": task.request.model_dump(mode="json"),
        "feedback": [row for row in task.feedback if row.get("revision", 0) <= source_run.revision],
        "frozen_project": task.base_bundle,
        "source_run": source_run.model_dump(mode="json"),
        "source_outcomes": [row.model_dump(mode="json") for row in outcomes.values()],
        "source_artifacts": selected,
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "history": [
            {
                "run": run.model_dump(mode="json"),
                "outcomes": [
                    row.model_dump(mode="json") for row in store.outcomes(run.id).values()
                ],
                "events": _events(store, run.id),
                "model_calls": store.model_calls(run.id),
            }
            for run in runs
        ],
        "author_acceptance": "unknown",
        "record_scope": "observable application records; no hidden reasoning or external chat",
    }
    return _redact(trace)


def learning_eligibility(trace: dict[str, Any]) -> dict[str, Any]:
    """Technical eligibility is separate from the author's judgement or memory acceptance."""
    reasons = []
    source, run = trace["source"], trace["source_run"]
    if source["revision"] != source["current_revision"]:
        reasons.append("源运行不是当前任务版本")
    if run.get("status") != "completed":
        reasons.append("源运行尚未完整完成")
    outcomes = trace["source_outcomes"]
    if not outcomes or any(row.get("status") != "succeeded" for row in outcomes):
        reasons.append("存在未成功的实际节点")
    expected = {node["id"] for node in (run.get("workflow") or {}).get("nodes", [])}
    successful = {row["node_id"] for row in outcomes if row.get("status") == "succeeded"}
    if not expected or not expected.issubset(successful):
        reasons.append("源工作图未完整留下成功节点记录")
    audits = [
        item["payload"] for item in trace["source_artifacts"] if item["kind"] == "audit_report"
    ]
    if not audits:
        reasons.append("没有可核对的实际审计产物")
    elif any(audit.get("status") != "checked" for audit in audits):
        reasons.append("源审计产物未实际完成检查")
    errors = [
        issue
        for audit in audits
        for issue in audit.get("issues", [])
        if issue.get("severity") == "error"
    ]
    if errors:
        reasons.append("实际审计仍有确定性错误")
    return {
        "allowed_to_induce": not reasons,
        "technical_status": "validated" if not reasons else "unverified",
        "author_acceptance": "unknown",
        "acceptance_status": "pending",
        "reasons": reasons,
        "error_count": len(errors),
        "warning_count": sum(
            issue.get("severity") == "warning"
            for audit in audits
            for issue in audit.get("issues", [])
        ),
    }


def _instance_literals(trace: dict[str, Any]) -> set[str]:
    literals: set[str] = set()
    literals.update(str(value) for value in trace["task_request"].get("reference_ids", []))
    bundle = trace["frozen_project"]
    for section in ("entities", "quests", "dialogue_trees"):
        for key, value in bundle.get(section, {}).items():
            literals.add(str(key))
            for field in ("name", "title"):
                if value.get(field):
                    literals.add(str(value[field]))
    for artifact in trace["artifacts"]:
        payload = artifact["payload"]
        for key in ("quest", "tree"):
            obj = payload.get(key)
            if not isinstance(obj, dict):
                continue
            for field in ("id", "title", "name"):
                if obj.get(field):
                    literals.add(str(obj[field]))
            for stage in obj.get("stages", []):
                if stage.get("id"):
                    literals.add(str(stage["id"]))
            for variable in (obj.get("logic") or {}).get("variables", []):
                if variable.get("id"):
                    literals.add(str(variable["id"]))
    # Repository/project paths and UUIDs are examples, not transferable procedure.
    for text in _strings(trace):
        literals.update(re.findall(r"\b[a-f0-9]{32}\b", text))
        literals.update(re.findall(r"(?:[A-Za-z]:[\\/]|(?:demo|content)/)[^\s\"<>]+", text))
    return {value for value in literals if len(value) >= 2}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def validate_induced_workflow(spec: WorkflowSpec, trace: dict[str, Any]) -> WorkflowSpec:
    from .planning import validate_bindings  # local import avoids planner/operator cycles

    spec = validate_bindings(spec)
    if not {"plan_events", "draft_quest", "draft_logic", "package"}.issubset(
        {node.operation for node in spec.nodes}
    ):
        raise ValueError("归纳方法缺少原型必需制作步骤")
    if not spec.applicability or not spec.input_slots:
        raise ValueError("归纳方法必须声明适用条件和输入槽位")
    slots = {slot.name for slot in spec.input_slots}
    if len(slots) != len(spec.input_slots):
        raise ValueError("归纳方法输入槽位名称重复")
    if not {"brief", "protected", "entities"}.issubset({slot.source for slot in spec.input_slots}):
        raise ValueError("归纳方法必须绑定新任务、保护项和当前项目实体")
    fields: list[Any] = [
        spec.name,
        spec.description,
        spec.applicability,
        spec.compile_notes,
        [slot.description for slot in spec.input_slots],
        list(slots),
    ]
    limits = {
        "plan_events": {"budget_tokens": 8000},
        "draft_quest": {"budget_tokens": 8000},
        "draft_dialogue": {"max_nodes": 24, "max_chars": 600},
    }
    referenced: set[str] = set()
    for node in spec.nodes:
        if not node.guidance.strip():
            raise ValueError("每个归纳步骤都需要可消费的guidance")
        remainder = SLOT_PATTERN.sub("", node.guidance)
        if "{{" in remainder or "}}" in remainder:
            raise ValueError("归纳步骤包含无效的输入槽位占位符")
        node.guidance = SLOT_PATTERN.sub(lambda match: "{{" + match.group(1) + "}}", node.guidance)
        referenced.update(SLOT_PATTERN.findall(node.guidance))
        for key, value in node.parameters.items():
            maximum = limits.get(node.operation, {}).get(key)
            if (
                maximum is None
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise ValueError("归纳方法包含不通用或无效的步骤参数")
        fields.extend([node.id, node.guidance, node.parameters])
    if not referenced or not referenced.issubset(slots):
        raise ValueError("归纳步骤引用了未知槽位，或没有使用当前任务输入槽位")
    for literal in _instance_literals(trace):
        pattern = (
            rf"(?<![A-Za-z0-9_]){re.escape(literal)}(?![A-Za-z0-9_])"
            if literal.isascii()
            else re.escape(literal)
        )
        if any(re.search(pattern, text, flags=re.IGNORECASE) for text in _strings(fields)):
            raise ValueError(f"归纳方法仍包含旧实例信息：{literal}")
    return spec


def induce_workflow(gateway: LLMGateway, trace: dict[str, Any]) -> WorkflowSpec:
    eligibility = learning_eligibility(trace)
    if not eligibility["allowed_to_induce"]:
        raise ValueError("源执行不满足方法归纳条件：" + "；".join(eligibility["reasons"]))
    spec = complete_structured(
        gateway,
        task="studio_learn_method",
        system=LEARNING_SYSTEM,
        user=json.dumps(
            {
                "observable_trace": trace,
                "eligibility": eligibility,
                "schema": WorkflowSpec.model_json_schema(),
            },
            ensure_ascii=False,
        ),
        model_cls=WorkflowSpec,
        max_repair_retries=0,
    )
    return validate_induced_workflow(spec, trace)
