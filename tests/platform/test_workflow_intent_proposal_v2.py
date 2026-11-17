"""Single-call AI proposal adapter with real sealed intent/shadow review and no author decision."""

import json

import pytest

from owcopilot.app.actions.intent_workflow import (
    IntentConfirmInput,
    IntentContractInput,
    confirm_intent_contract_action,
    create_intent_contract_action,
)
from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.intent_proposal_v2 import propose_intent_patch
from owcopilot.workflow.models import Artifact, TaskInput


def test_ai_patch_is_single_claimed_review_and_contract_versions_share_budget(tmp_path):
    root = tmp_path / "world"
    quest = Quest(
        id="q",
        title="Locked title",
        objective="Before",
        stages=[QuestStage(id="s", summary="Keep this stage")],
    )
    ContentStore(root).save(ContentBundle(quests={"q": quest}))
    task_id = create_task_action(root, "p", TaskInput(brief="Controlled local patch input"))[
        "task"
    ]["id"]
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    run, _ = store.create_run(task)
    store.update_run(run.id, status="completed")
    payload = {"quest": quest.model_dump(mode="json")}
    baseline = Artifact(
        task_id=task.id,
        revision=task.revision,
        run_id=run.id,
        node_id="draft_quest",
        kind="quest",
        payload=payload,
        input_hash=content_hash("controlled input"),
        content_hash=content_hash(payload),
    )
    store.append_artifact(baseline)
    fields = {
        "expected_revision": task.revision,
        "baseline_artifact_id": baseline.id,
        "baseline_artifact_hash": baseline.content_hash,
        "allowed_paths": ["/quests/q/objective"],
        "hard_locks": [{"path": "/quests/q/title"}],
        "modification_goal": "Replace only the objective; preserve the title and stage",
    }
    draft = create_intent_contract_action(
        root, "p", task.id, IntentContractInput.model_validate(fields)
    )
    confirmed = confirm_intent_contract_action(
        root,
        "p",
        draft["contract"]["id"],
        IntentConfirmInput(expected_revision=1, expected_contract_hash=draft["contract_hash"]),
    )

    class ControlledPatchTransport:
        model = "controlled-patch-transport"
        max_retries = 0

        def __init__(self):
            self.calls = []

        def complete(self, *, system, user, model):
            request = json.loads(user)
            self.calls.append(request)
            assert request["allowed_fields"] == [{"path": "/quests/q/objective", "value": "Before"}]
            assert request["locked_fields"] == [
                {"path": "/quests/q/title", "value": "Locked title"}
            ]
            assert request["output_schema"]["properties"]["ops"]
            return (
                json.dumps(
                    {
                        "ops": [{"op": "replace", "path": "/quests/q/objective", "value": "After"}],
                        "rationale": "Controlled transport candidate",
                    }
                ),
                10,
                8,
                0,
                self.model,
            )

    provider = ControlledPatchTransport()
    canon_hash = content_hash(ContentStore(root).load())
    with pytest.raises(ValueError):
        propose_intent_patch(
            store,
            root,
            "p",
            confirmed["contract"]["id"],
            expected_revision=1,
            expected_contract_hash="0" * 64,
            provider=provider,
        )
    assert not provider.calls
    kwargs = {
        "expected_revision": 1,
        "expected_contract_hash": confirmed["contract_hash"],
        "provider": provider,
    }
    reviewed = propose_intent_patch(store, root, "p", confirmed["contract"]["id"], **kwargs)
    repeated = propose_intent_patch(store, root, "p", confirmed["contract"]["id"], **kwargs)
    assert reviewed == repeated and len(provider.calls) == 1
    assert reviewed["report"]["status"] == "ready_for_author"
    assert reviewed["report"]["goal_status"] == "withheld"
    assert reviewed["author_acceptance"] == "unknown" and reviewed["applied"] is False
    assert store.get_task(task.id).revision == 1
    assert store.get_artifact(baseline.id).payload["quest"]["objective"] == "Before"
    assert content_hash(ContentStore(root).load()) == canon_hash
    assert store.get_run(reviewed["proposal_trace_run_id"]).status == "completed"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM creative_intent_decisions").fetchone()[0] == 0
    newer = create_intent_contract_action(
        root,
        "p",
        task.id,
        IntentContractInput.model_validate(
            {
                **fields,
                "supersedes_contract_id": confirmed["contract"]["id"],
                "expected_parent_hash": confirmed["contract_hash"],
                "change_reason": "Controlled permission revision",
            }
        ),
    )
    newer = confirm_intent_contract_action(
        root,
        "p",
        newer["contract"]["id"],
        IntentConfirmInput(expected_revision=1, expected_contract_hash=newer["contract_hash"]),
    )
    second = propose_intent_patch(
        store,
        root,
        "p",
        newer["contract"]["id"],
        expected_revision=1,
        expected_contract_hash=newer["contract_hash"],
        provider=provider,
    )
    first_dispatch = HarnessStore(store).requests(reviewed["proposal_trace_run_id"])[0]
    second_dispatch = HarnessStore(store).requests(second["proposal_trace_run_id"])[0]
    assert first_dispatch["budget_id"] == second_dispatch["budget_id"]
    assert [first_dispatch["attempt"], second_dispatch["attempt"]] == [1, 2]
    assert len(provider.calls) == 2
