"""Quest references use flat DialogueRef IDs; a dialogue tree is a distinct namespace.

The production operator and QuestDraftService run unchanged. ControlledProvider only
supplies transport responses; these checks are not live-model or narrative-quality evidence.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.audit.registry import RuleRegistry
from owcopilot.audit.rules.reference_rules import MissingDialogueReferenceRule
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.hash import content_hash
from owcopilot.content.models import (
    ContentBundle,
    DialogueNode,
    DialogueRef,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
)
from owcopilot.content.store import ContentStore
from owcopilot.llm.execution_policy import SINGLE_DISPATCH_POLICY
from owcopilot.llm.gateway import LLMGateway
from owcopilot.retrieval.models import ContextPack
from owcopilot.workflow import runtime
from owcopilot.workflow.models import EventPlan, TaskInput, TaskRecord, WorkflowNode, WorkflowSpec
from owcopilot.workflow.operators import execute_operator


class ControlledProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.users = []

    def complete(self, *, system, user, model):
        self.users.append(user)
        return json.dumps(self.responses.pop(0), ensure_ascii=False), 1, 1


class EmptyContext:
    def build(self, query, *, budget_tokens):
        return ContextPack(query=query, budget_tokens=budget_tokens)


def _objects(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _objects(nested)


@pytest.mark.parametrize(
    ("reference", "flat_ids", "tree_ids", "accepted"),
    [
        pytest.param("flat_line", ["flat_line"], ["tree_story"], True, id="existing-flat"),
        pytest.param("shared_id", ["shared_id"], ["shared_id"], True, id="flat-and-tree-collision"),
        pytest.param("tree_story", [], ["tree_story"], False, id="tree-is-not-flat"),
        pytest.param("missing_id", ["flat_line"], ["tree_story"], False, id="missing-reference"),
        pytest.param(None, [], ["tree_story"], True, id="empty-flat-allows-empty-reference"),
    ],
)
def test_actual_quest_writer_respects_flat_reference_namespace(
    reference,
    flat_ids,
    tree_ids,
    accepted,
):
    bundle = ContentBundle(
        entities={"current_npc": Entity(id="current_npc", name="当前角色", type=EntityType.NPC)},
        dialogues={
            dialogue_id: DialogueRef(id=dialogue_id, text_key=f"line.{dialogue_id}")
            for dialogue_id in flat_ids
        },
        dialogue_trees={
            tree_id: DialogueTree(
                id=tree_id,
                title="已有对白树",
                participants=["current_npc"],
                root_node="entry",
                nodes={
                    "entry": DialogueNode(id="entry", speaker_id="current_npc", text="已有台词")
                },
            )
            for tree_id in tree_ids
        },
        # An unrelated legacy error must neither be repaired nor reject a valid new quest.
        quests={
            "legacy_quest": Quest(
                id="legacy_quest", title="旧任务", dialogue_refs=["legacy_missing"]
            )
        },
    )
    task = TaskRecord(
        project="reference-contract",
        request=TaskInput(brief="围绕现有对白编写一条候选任务"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    plan = EventPlan(
        contract_version=1,
        premise="承诺引发抉择",
        conflict="履行承诺与避免风险发生冲突",
        beats=["玩家听到请求", "玩家作出回应"],
        participant_ids=["current_npc"],
        character_goals=[
            {"character_id": "current_npc", "goal": "履行承诺", "stakes": "可能失去信任"}
        ],
        events=[{"id": "request", "summary": "提出请求", "participant_ids": ["current_npc"]}],
    )
    project = SimpleNamespace(
        bundle=bundle,
        context_builder=EmptyContext(),
        reference_context_builder=None,
        audit_runner=AuditRunner(RuleRegistry([MissingDialogueReferenceRule()])),
    )
    provider = ControlledProvider(
        {
            "id": "new_candidate",
            "title": "一次承诺",
            "giver_npc": "current_npc",
            "objective": "作出回应",
            "dialogue_refs": [reference] if reference else [],
        }
    )
    gateway = LLMGateway(providers={"cheap": provider}, execution_policy=SINGLE_DISPATCH_POLICY)
    before_bundle = deepcopy(bundle.model_dump(mode="json"))
    before_task = deepcopy(task.model_dump(mode="json"))

    def generate():
        return execute_operator(
            WorkflowNode(id="quest", operation="draft_quest", depends_on=["events"]),
            task=task,
            project=project,
            gateway=gateway,
            inputs={"plan_events": {"plan": plan.model_dump(mode="json")}},
        )

    if accepted:
        kind, output = generate()
        assert kind == "quest"
        assert output["quest"]["dialogue_refs"] == ([reference] if reference else [])
    else:
        with pytest.raises(ValueError) as error:
            generate()
        assert reference in str(error.value)
    assert len(provider.users) == 1
    writer_input = json.loads(provider.users[0])
    contract = next(
        obj
        for obj in _objects(writer_input)
        if "allowed_dialogue_refs" in obj and "tree_only_ids" in obj
    )
    assert contract["allowed_dialogue_refs"] == sorted(flat_ids)
    assert contract["tree_only_ids"] == sorted(set(tree_ids) - set(flat_ids))
    assert "dialogue_refs" in provider.users[0] and "tree.quest_id" in provider.users[0]
    assert bundle.model_dump(mode="json") == before_bundle
    assert task.model_dump(mode="json") == before_task


def test_changed_quest_reference_contract_executes_new_writer_then_same_contract_reuses(
    tmp_path,
    monkeypatch,
):
    """Exercise production cache decisions; only the transport response is controlled."""
    root = tmp_path / "project"
    bundle = ContentBundle(
        entities={"current_npc": Entity(id="current_npc", name="当前角色", type=EntityType.NPC)},
        dialogues={"flat_line": DialogueRef(id="flat_line", text_key="line.flat_line")},
    )
    ContentStore(root).save(bundle)
    task_id = create_task_action(
        root,
        "reference-contract",
        TaskInput(brief="根据现有对白生成一次承诺任务"),
    )["task"]["id"]
    store = open_workflow_store(root)
    plan = EventPlan(
        contract_version=1,
        premise="承诺引发抉择",
        conflict="履行承诺与避免风险发生冲突",
        beats=["提出请求"],
        participant_ids=["current_npc"],
        character_goals=[{"character_id": "current_npc", "goal": "履行承诺", "stakes": "失去信任"}],
        events=[{"id": "request", "summary": "提出请求", "participant_ids": ["current_npc"]}],
    )
    quest_response = {
        "id": "new_candidate",
        "title": "一次承诺",
        "giver_npc": "current_npc",
        "objective": "作出回应",
        "dialogue_refs": ["flat_line"],
    }
    provider = ControlledProvider(plan.model_dump(mode="json"), quest_response, quest_response)
    monkeypatch.setattr(runtime, "build_real_provider", lambda **kwargs: provider)
    spec = WorkflowSpec(
        name="对白引用合同",
        nodes=[
            {"id": "events", "operation": "plan_events"},
            {"id": "quest", "operation": "draft_quest", "depends_on": ["events"]},
        ],
    )
    task = store.get_task(task_id)
    quest_artifacts = []
    call_counts = []
    for revision, contract_version in ((1, 1), (2, 2), (3, 2)):
        monkeypatch.setitem(runtime.OPERATION_CONTRACT_VERSIONS, "draft_quest", contract_version)
        if revision > 1:
            task = task.model_copy(update={"revision": revision})
            store.save_task(task, expected_revision=revision - 1)
        run, _ = store.create_run(task)
        store.update_run(run.id, workflow=spec.model_dump(mode="json"))
        result = runtime.execute_workflow(store, run.id, str(root))
        assert result["status"] == "completed", result
        outcome = store.outcomes(run.id)["quest"]
        quest_artifacts.append(outcome.artifact_id)
        call_counts.append(len(store.model_calls(run.id)))
        if revision == 3:
            assert outcome.attempts == 0
    assert call_counts == [2, 1, 0]
    assert len(provider.users) == 3
    assert quest_artifacts[0] != quest_artifacts[1]
    assert quest_artifacts[1] == quest_artifacts[2]
    assert ContentStore(root).load().model_dump(mode="json") == bundle.model_dump(mode="json")
