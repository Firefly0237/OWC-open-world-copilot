"""Real initial PlanIR generation; automatic planning never falls back to a seed graph."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from ..content.hash import content_hash
from ..llm.execution_policy import SINGLE_DISPATCH_POLICY
from ..llm.resilience import build_real_provider
from .evolution_v2 import PlanIR, compile_plan_ir
from .graph_v2 import GraphSpecV2, narrative_role_registry
from .harness import BudgetOwner, Harness, HarnessStore
from .models import RunRecord
from .requirements_v2 import MachineRequirements


def _capability_requirements(requirements: MachineRequirements | Mapping[str, Any] | None) -> dict:
    machine = MachineRequirements.model_validate(
        requirements.model_dump(mode="json")
        if isinstance(requirements, MachineRequirements)
        else dict(requirements or {})
    )
    playable = bool(
        machine.distinct_outcomes is True
        or machine.decision_option_count is not None
        or machine.decision_count is not None
        and machine.decision_count > 0
    )
    return {
        "schema_version": 1,
        "machine_requirements": machine.model_dump(mode="json"),
        "requirement_hash": content_hash(machine),
        "playable_coverage_required": playable,
        "required_capabilities": [
            "logic_contract",
            "validate_decisions",
            "dialogue_constrained",
            "same_decisions_and_choices_into_final_bind_and_audit",
            "audit_consumes_final_bound",
        ]
        if playable
        else [],
        "required_data_flow": [
            "plan_events.plan -> logic_contract.plan / validate_decisions.plan / "
            "dialogue_constrained.plan / coverage_audit.plan",
            "draft_quest.quest -> logic_contract.quest",
            "logic_contract.logic -> validate_decisions.logic / bind_dialogue.logic / "
            "coverage_audit.logic",
            "logic_contract.decisions -> validate_decisions.decisions / "
            "bind_dialogue.decisions / coverage_audit.decisions",
            "validate_decisions.contract -> dialogue_constrained.contract",
            "dialogue_constrained.dialogue -> bind_dialogue.dialogue",
            "dialogue_constrained.choices -> bind_dialogue.choices / coverage_audit.choices",
            "published bind_dialogue.bound -> published coverage_audit.bound",
        ]
        if playable
        else [],
        "unsupported_legacy_roles": ["draft_logic", "draft_dialogue"] if playable else [],
        "policy": "reject_missing_capability_without_graph_repair_or_model_retry",
    }


def validate_requirement_capabilities(
    graph: GraphSpecV2 | Mapping[str, Any],
    requirements: MachineRequirements | Mapping[str, Any] | None,
) -> dict:
    """Admit business evidence capability, separately from generic typed compilation.

    Checks real data ports leading to the published products. Role names elsewhere
    and control-only edges do not establish consumption. Never mutates a proposal.
    """
    spec = GraphSpecV2.model_validate(
        graph.model_dump(mode="json") if isinstance(graph, GraphSpecV2) else dict(graph)
    )
    admission = _capability_requirements(requirements)
    if not admission["playable_coverage_required"]:
        return {**admission, "status": "admitted", "graph_hash": content_hash(spec)}
    nodes = {node.id: node for node in spec.nodes}
    if any(node.operation in {"draft_logic", "draft_dialogue"} for node in spec.nodes):
        raise ValueError(
            "明确可玩需求缺少证据能力：legacy draft_logic／draft_dialogue不能满足正式决策覆盖"
        )

    def published(name: str, operation: str, port: str) -> str:
        ref = spec.outputs.get(name)
        if (
            ref is None
            or ref.port != port
            or ref.node_id not in nodes
            or nodes[ref.node_id].operation != operation
        ):
            raise ValueError("明确可玩需求必须发布实际" + operation + "." + port)
        return ref.node_id

    def wire(target: str, port: str, operation: str, source_port: str) -> str:
        edges = [
            edge
            for edge in spec.data_edges
            if (edge.target_node, edge.target_port) == (target, port)
        ]
        if len(edges) != 1:
            raise ValueError("明确可玩需求缺少唯一真实数据消费：" + target + "." + port)
        edge = edges[0]
        if (
            edge.source_node not in nodes
            or nodes[edge.source_node].operation != operation
            or edge.source_port != source_port
        ):
            raise ValueError("明确可玩需求的数据源能力不符：" + target + "." + port)
        return edge.source_node

    def same(target: str, port: str, operation: str, source_port: str, expected_node: str) -> None:
        if wire(target, port, operation, source_port) != expected_node:
            raise ValueError("最终绑定、合同与审计必须实际消费同一份" + source_port)

    binder = published("bound", "bind_dialogue", "bound")
    auditor = published("report", "coverage_audit", "report")
    logic = wire(binder, "logic", "logic_contract", "logic")
    dialogue = wire(binder, "dialogue", "dialogue_constrained", "dialogue")
    gate = wire(dialogue, "contract", "validate_decisions", "contract")
    planner = wire(logic, "plan", "plan_events", "plan")
    wire(logic, "quest", "draft_quest", "quest")
    same(gate, "logic", "logic_contract", "logic", logic)
    same(gate, "decisions", "logic_contract", "decisions", logic)
    same(gate, "plan", "plan_events", "plan", planner)
    same(dialogue, "plan", "plan_events", "plan", planner)
    same(binder, "decisions", "logic_contract", "decisions", logic)
    same(binder, "choices", "dialogue_constrained", "choices", dialogue)
    same(auditor, "bound", "bind_dialogue", "bound", binder)
    same(auditor, "logic", "logic_contract", "logic", logic)
    same(auditor, "decisions", "logic_contract", "decisions", logic)
    same(auditor, "choices", "dialogue_constrained", "choices", dialogue)
    same(auditor, "plan", "plan_events", "plan", planner)
    return {
        **admission,
        "status": "admitted",
        "graph_hash": content_hash(spec),
        "consuming_nodes": {
            "logic": logic,
            "gate": gate,
            "dialogue": dialogue,
            "final_binding": binder,
            "final_audit": auditor,
        },
    }


def plan_initial_graph(
    store,
    root,
    task,
    *,
    requirements=None,
    narrative_context=None,
    provider=None,
    emit=None,
    decision_id=None,
    skill_card_selections=None,
    apply_project_skills=True,
):
    from .v2_service import _json, reference_snapshot

    trace = RunRecord(task_id=task.id, revision=task.revision, status="running")
    with store.connection() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?)", (trace.id, task.id, trace.model_dump_json()))
    if decision_id:
        from .execution_records import ExecutionRecords

        ExecutionRecords(store).bind_run(task.project, decision_id, trace.id)
    events = HarnessStore(store)
    owned = provider is None
    try:
        from .skill_cards import SkillCards

        selected_cards = SkillCards(store).resolve(
            task.project, skill_card_selections, include_defaults=apply_project_skills
        )
        SkillCards.validate_task_inputs(selected_cards, task, requirements)
        provider = provider or build_real_provider(
            model=task.request.model or os.getenv("OWCOPILOT_CHEAP_MODEL") or "deepseek-v4-flash",
            timeout=120,
            max_output_tokens=6500,
            max_retries=0,
            execution_policy=SINGLE_DISPATCH_POLICY,
        )
        roles = narrative_role_registry()
        capability_contract = _capability_requirements(requirements)
        context = {
            "original_brief": task.request.brief,
            "protected": task.request.protected,
            "machine_requirements": requirements or {},
            "required_capability_contract": capability_contract,
            "narrative_project": narrative_context,
            "entities": task.base_bundle.get("entities", {}),
            "references": reference_snapshot(root, task),
            "registered_roles": {
                name: role.model_dump(mode="json") for name, role in roles.items()
            },
            "output_schema": PlanIR.model_json_schema(),
            "selected_skills": selected_cards,
        }
        response = Harness(events, trace.id, provider).complete(
            owner=BudgetOwner(
                project=task.project,
                task_id=task.id,
                product_key="slice/workflow-plan",
                problem_key="slice/workflow-plan/produce",
            ),
            node_id="initial_planner",
            role_id="initial_planner",
            system=(
                "根据实际创作需求提出可执行制作图，只使用注册角色及其真实端口。"
                "输出PlanIR JSON，不输出预算、任意代码或未注册工具。所有节点须服务真实产物。"
                "必须有plan_events、draft_quest、机制、对白、bind_dialogue、coverage_audit。"
                "outputs只含bound和report，report必须实际读取最终bound。"
                "selected_skills是本次明确选用的工作方法，应安排相应制作角色与依赖；"
                "方法不得覆盖当前明确要求、保护项或系统检查，也不能获得额外权限。"
                "若选择dialogue_constrained，必须读取validate_decisions产出的contract；"
                "logic_contract需要plan和quest。遵守来源用途与作者保护项，未知不假称通过。"
                "若明确distinct_outcomes=true、decision_option_count或正数decision_count，"
                "必须满足required_capability_contract的真实数据流：logic_contract→"
                "validate_decisions→dialogue_constrained，并将同一份decisions/choices送入"
                "最终bind与audit，audit读取最终bound；不得使用draft_logic/draft_dialogue。"
                "仅类型端口可编译不表示这些业务能力已满足，宿主会独立拒绝缺项。"
            ),
            user=json.dumps(context, ensure_ascii=False),
            context_manifest={
                "task_id": task.id,
                "task_revision": task.revision,
                "base_hash": task.base_hash,
                "input_hash": content_hash(context),
                "selection": "llm_plan_ir",
                "fallback": "forbidden",
            },
        )
        plan = PlanIR.model_validate(_json(response))
        SkillCards(store).assert_bindings(task.project, selected_cards)
        graph = compile_plan_ir(plan, roles)
        admission = validate_requirement_capabilities(graph, requirements)
        events.append(trace.id, "planning/requirement_capabilities", admission)
        provenance = {
            "run_id": trace.id,
            "plan_ir_hash": content_hash(plan),
            "task_id": task.id,
            "task_revision": task.revision,
            "skill_selection_hash": content_hash(
                [
                    {
                        key: binding[key]
                        for key in ("card_id", "version_id", "version_hash", "selection")
                    }
                    for binding in selected_cards
                ]
            ),
        }
        graph = graph.model_copy(
            update={
                "nodes": [
                    node.model_copy(
                        update={"parameters": {**node.parameters, "initial_planning": provenance}}
                    )
                    for node in graph.nodes
                ]
            }
        )
        events.append(
            trace.id,
            "planning/compiled",
            {**provenance, "graph": graph.model_dump(mode="json"), "rationale": plan.rationale},
        )
        store.update_run(trace.id, status="completed")
        if emit:
            emit("planning/compiled", provenance)
        return {
            "graph": graph.model_dump(mode="json"),
            "planning_run_id": trace.id,
            "plan_ir_hash": content_hash(plan),
            "selection": "llm_plan_ir",
        }
    except Exception as exc:
        store.update_run(trace.id, status="failed", error=f"{type(exc).__name__}: {exc}")
        events.append(trace.id, "planning/failed", {"error": str(exc), "fallback": "not_performed"})
        raise
    finally:
        if owned and provider is not None and getattr(provider, "close", None):
            provider.close()
