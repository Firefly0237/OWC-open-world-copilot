"""Production creative contracts and prompt binding, using offline transport responses only."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from owcopilot.audit.registry import RuleRegistry
from owcopilot.audit.rules.logic_rules import QuestLogicRule
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.execution_policy import SINGLE_DISPATCH_POLICY
from owcopilot.llm.gateway import LLMGateway
from owcopilot.retrieval.models import ContextPack
from owcopilot.workflow.models import EventPlan, TaskInput, TaskRecord, WorkflowNode
from owcopilot.workflow.operators import execute_operator
from owcopilot.workflow.planning import plan_workflow


class ControlledProvider:
    """Replace transport alone; keep production parsing, operators and writer service."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.users = []

    def complete(self, *, system, user, model):
        self.users.append(user)
        return json.dumps(self.responses.pop(0), ensure_ascii=False), 1, 1


class EmptyContext:
    def build(self, query, *, budget_tokens):
        return ContextPack(query=query, budget_tokens=budget_tokens)


def gateway(provider):
    return LLMGateway(providers={"cheap": provider}, execution_policy=SINGLE_DISPATCH_POLICY)


def task_and_project(npc_id="current_npc"):
    bundle = ContentBundle(entities={npc_id: Entity(id=npc_id, name=npc_id, type=EntityType.NPC)})
    task = TaskRecord(
        project="contract-check",
        request=TaskInput(
            brief=f"围绕{npc_id}的承诺设计一次抉择", protected=[f"保留{npc_id}的承诺"]
        ),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    project = SimpleNamespace(
        bundle=bundle,
        context_builder=EmptyContext(),
        reference_context_builder=None,
        audit_runner=AuditRunner(RuleRegistry([QuestLogicRule()])),
    )
    return task, project


def workflow(parameters):
    return {
        "name": "创作合同",
        "nodes": [
            {"id": "events", "operation": "plan_events", "parameters": parameters},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
            {"id": "logic", "operation": "draft_logic", "depends_on": ["quest"]},
            {"id": "package", "operation": "package"},
        ],
    }


def event_plan(npc_id="current_npc"):
    return {
        "contract_version": 1,
        "premise": "一项承诺引发相互冲突的目标",
        "conflict": "履行承诺与保护村民发生冲突",
        "beats": ["NPC提出请求", "玩家决定如何回应"],
        "participant_ids": [npc_id],
        "character_goals": [{"character_id": npc_id, "goal": "履行承诺", "stakes": "失去信任"}],
        "events": [
            {"id": "request", "summary": "提出请求", "participant_ids": [npc_id]},
            {
                "id": "response",
                "summary": "回应请求",
                "participant_ids": [npc_id],
                "causes": ["request"],
            },
        ],
        "decisions": [
            {
                "id": "promise",
                "event_id": "response",
                "options": ["接受", "拒绝"],
                "consequences": ["承担风险", "失去信任"],
            }
        ],
    }


def test_new_planner_rejects_unconsumed_creative_pseudoparameter():
    task, _ = task_and_project()
    provider = ControlledProvider(workflow({"choice_count": 2}))
    with pytest.raises(ValidationError, match="choice_count"):
        plan_workflow(gateway(provider), task, {}, [])
    assert len(provider.users) == 1


def test_new_planner_accepts_executable_integer_and_exposes_strict_schema():
    task, _ = task_and_project()
    provider = ControlledProvider(workflow({"budget_tokens": 1200}))
    spec = plan_workflow(gateway(provider), task, {}, [])
    assert spec.nodes[0].parameters == {"budget_tokens": 1200}
    schema = json.loads(provider.users[0])["operator_catalog"]["plan_events"]["parameters_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["budget_tokens"]["type"] == "integer"
    assert "choice_count" not in schema["properties"]
    assert len(provider.users) == 1


def test_shared_event_contract_rejects_cycles_and_dangling_events():
    causal_cycle = event_plan()
    causal_cycle["events"][0]["causes"] = ["response"]
    dangling_cause = event_plan()
    dangling_cause["events"][1]["causes"] = ["absent"]
    dangling_decision = event_plan()
    dangling_decision["decisions"][0]["event_id"] = "absent"
    for payload, message in [
        (causal_cycle, "因果关系包含循环"),
        (dangling_cause, "不存在的前因"),
        (dangling_decision, "不存在的事件"),
    ]:
        with pytest.raises(ValidationError, match=message):
            EventPlan.model_validate(payload)


def test_new_event_operator_rejects_legacy_contract_and_unavailable_npc():
    task, project = task_and_project()
    # Old saved plans remain readable; a fresh generating operation must supply v1.
    legacy = {"premise": "旧格式", "beats": ["旧事件"]}
    assert EventPlan.model_validate(legacy).contract_version == 0
    missing_conflict = event_plan()
    del missing_conflict["conflict"]
    invalid_npc = event_plan()
    invalid_npc["events"][1]["participant_ids"] = ["absent_npc"]
    for payload, message in [
        (legacy, "v1共享创作合同"),
        (missing_conflict, "明确冲突"),
        (invalid_npc, "不存在的NPC"),
    ]:
        provider = ControlledProvider(payload)
        with pytest.raises(ValueError, match=message):
            execute_operator(
                WorkflowNode(id="events", operation="plan_events"),
                task=task,
                project=project,
                gateway=gateway(provider),
                inputs={},
            )
        assert len(provider.users) == 1


def test_actual_event_and_writer_prompts_bind_current_slots_and_share_creative_contract():
    slots = [
        {"name": "cast", "source": "entities"},
        {"name": "request", "source": "brief"},
        {"name": "protected", "source": "protected"},
    ]
    guidance = "当前角色={{cast}}；当前灵感={{request}}；保护项={{protected}}；保持选择后果可见。"
    template = WorkflowNode(id="events", operation="plan_events", guidance=guidance)
    for current_id, previous_id in [("alpha_npc", "beta_npc"), ("beta_npc", "alpha_npc")]:
        task, project = task_and_project(current_id)
        original_task = task.model_dump(mode="json")
        provider = ControlledProvider(
            event_plan(current_id),
            {"id": "candidate", "title": "承诺与后果", "giver_npc": current_id},
        )
        transport = gateway(provider)
        _, events = execute_operator(
            template,
            task=task,
            project=project,
            gateway=transport,
            inputs={"_method_slots": deepcopy(slots)},
        )
        _, quest = execute_operator(
            WorkflowNode(
                id="quest", operation="draft_quest", depends_on=["events"], guidance=guidance
            ),
            task=task,
            project=project,
            gateway=transport,
            inputs={"plan_events": events, "_method_slots": deepcopy(slots)},
        )
        assert len(provider.users) == 2
        for prompt in provider.users:
            assert current_id in prompt and previous_id not in prompt
            assert "保持选择后果可见" in prompt
            assert "{{cast}}" not in prompt
        writer_input = json.loads(provider.users[1])
        shared = writer_input["候选事件计划（不是已确认正典）"]
        assert shared["character_goals"][0]["character_id"] == current_id
        assert shared["events"][1]["causes"] == ["request"]
        assert shared["decisions"][0]["consequences"] == ["承担风险", "失去信任"]
        assert shared["protected_facts"] == task.request.protected
        assert writer_input["当前反馈"][-1]["scope"] == "method_guidance"
        assert quest["quest"]["giver_npc"] == current_id
        assert events["execution_contract"]["method_guidance_bound"] is True
        assert task.model_dump(mode="json") == original_task
    assert template.guidance == guidance
