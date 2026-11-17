"""The studio logic operator checks structured model output against the frozen world."""

import json
from types import SimpleNamespace

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    QuestLogic,
    QuestStage,
)
from owcopilot.llm.execution_policy import SINGLE_DISPATCH_POLICY
from owcopilot.llm.gateway import LLMGateway
from owcopilot.workflow.models import TaskInput, TaskRecord, WorkflowNode
from owcopilot.workflow.operators import execute_operator


class LogicProvider:
    """Controlled transport only; production parsing, drafting and auditing remain real."""

    def __init__(self, logic):
        self.logic = logic
        self.users = []

    def complete(self, *, system, user, model):
        self.users.append(user)
        return json.dumps(self.logic), 1, 1


def run_logic(bundle, logic):
    quest = Quest(
        id="candidate", title="New task", stages=[QuestStage(id="start", summary="Begin")]
    )
    task = TaskRecord(
        project="isolated",
        request=TaskInput(brief="Create a playable choice without new world entities"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    provider = LogicProvider(logic)
    gateway = LLMGateway(providers={"cheap": provider}, execution_policy=SINGLE_DISPATCH_POLICY)
    arguments = {
        "task": task,
        "project": SimpleNamespace(bundle=bundle),
        "gateway": gateway,
        "inputs": {"draft_quest": {"quest": quest.model_dump(mode="json")}},
    }
    return provider, lambda: execute_operator(
        WorkflowNode(id="logic", operation="draft_logic", depends_on=["quest"]), **arguments
    )


@pytest.mark.parametrize(
    "logic",
    [
        {
            "branches": [
                {
                    "id": "ending",
                    "from_stage": "start",
                    "outcome": "End",
                    "effects": [{"var": "rep:absent", "op": "inc", "value": 1}],
                }
            ]
        },
        {"precondition": "quest:absent.done"},
        {"stage_logic": [{"stage_id": "absent"}]},
    ],
)
def test_grounding_error_is_returned_after_one_dispatch_without_mutating_world(logic):
    bundle = ContentBundle()
    before = content_hash(bundle)
    provider, execute = run_logic(bundle, logic)
    with pytest.raises(ValueError, match="LOGIC_DANGLING_STATE_REF"):
        execute()
    assert len(provider.users) == 1
    assert content_hash(bundle) == before


def test_existing_faction_and_task_refs_are_allowed_and_grounding_reaches_model():
    bundle = ContentBundle(
        entities={"guild": Entity(id="guild", name="Guild", type=EntityType.FACTION)},
        quests={"prior": Quest(id="prior", title="Earlier")},
    )
    provider, execute = run_logic(
        bundle,
        {
            "precondition": "quest:prior.done",
            "branches": [
                {
                    "id": "ending",
                    "from_stage": "start",
                    "outcome": "End",
                    "effects": [{"var": "rep:guild", "op": "inc", "value": 1}],
                },
            ],
        },
    )
    kind, payload = execute()
    assert kind == "quest_with_logic"
    assert payload["quest"]["logic"]["branches"][0]["effects"][0]["var"] == "rep:guild"
    assert len(provider.users) == 1
    assert '"allowed_reputation_refs": ["rep:guild"]' in provider.users[0]
    assert '"quest:prior.done"' in provider.users[0]


def test_unrelated_legacy_errors_do_not_block_clean_candidate():
    legacy = Quest(
        id="legacy", title="Earlier", logic=QuestLogic(precondition="quest:missing.done")
    )
    provider, execute = run_logic(ContentBundle(quests={legacy.id: legacy}), {"variables": []})
    kind, payload = execute()
    assert kind == "quest_with_logic"
    assert payload["logic_issues"] == []
    assert len(provider.users) == 1
