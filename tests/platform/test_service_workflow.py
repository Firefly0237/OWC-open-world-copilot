"""Studio REST and guarded review contracts using real local storage, without model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.review import decide_review_action
from owcopilot.app.actions.workflow import open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app
from owcopilot.workflow.models import Artifact, NodeOutcome, WorkflowNode, WorkflowSpec

PREFIX = "/projects/demo/creative"


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "content"
    quest = Quest(
        id="q_medicine",
        title="取药",
        objective="取出唯一一剂药",
        stages=[QuestStage(id="cabinet", summary="用一枚金币取药")],
        logic=QuestLogic(
            variables=[LogicVar(id="gold", type=LogicVarType.INT, default=2)],
            stage_logic=[
                StageLogic(
                    stage_id="cabinet",
                    effects_on_complete=[Effect(var="gold", op="dec", value=1)],
                )
            ],
            branches=[
                Branch(id="take", from_stage="cabinet", condition="gold >= 0", outcome="取得药剂")
            ],
        ),
    )
    ContentStore(root).save(ContentBundle(quests={quest.id: quest}))
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root), "alias": str(root)})
    )
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    with TestClient(create_app()) as client:
        yield client, root, quest


def _task(client: TestClient) -> dict:
    response = client.post(f"{PREFIX}/tasks", json={"brief": "保留只有一剂药的取舍"})
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _content_artifact(root: Path, task_id: str, quest: Quest):
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    run, _ = store.create_run(task)
    spec = WorkflowSpec(
        name="任务创作",
        nodes=[
            WorkflowNode(id="events", operation="plan_events"),
            WorkflowNode(
                id="quest",
                operation="draft_quest",
                depends_on=["events"],
                parameters={"old_character_id": "must-not-be-copied"},
            ),
        ],
    )
    store.update_run(run.id, workflow=spec.model_dump(mode="json"), status="completed")
    artifact = Artifact(
        task_id=task.id,
        revision=task.revision,
        run_id=run.id,
        node_id="draft_quest",
        kind="quest",
        payload={"quest": quest.model_dump(mode="json")},
        input_hash="input-contract",
        content_hash=content_hash({"quest": quest}),
    )
    store.complete_node(
        run.id,
        NodeOutcome(
            node_id="quest",
            operation="draft_quest",
            status="succeeded",
            attempts=1,
            budget_id=f"{task.id}:1:draft_quest",
            artifact_id=artifact.id,
        ),
        artifact,
    )
    return store, run, artifact


def test_task_preview_idempotence_feedback_and_scope(studio) -> None:
    client, root, _ = studio
    task = _task(client)
    assert "base_bundle" not in task
    capabilities = client.get(f"{PREFIX}/capabilities").json()
    assert capabilities["can_run"] is False
    assert capabilities["preview_quests"][0]["has_logic"] is True
    assert client.post(f"{PREFIX}/tasks/{task['id']}/runs").status_code == 503
    preview = client.post(f"{PREFIX}/tasks/{task['id']}/preview", json={"quest_id": "q_medicine"})
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert body["state"]["gold"] == 2
    action = {"action_id": "take-once", "choice_id": "take", "expected_seq": 0}
    stepped = client.post(f"{PREFIX}/previews/{body['preview_id']}/steps", json=action)
    assert stepped.status_code == 200, stepped.text
    assert stepped.json()["state"]["gold"] == 1
    repeated = client.post(f"{PREFIX}/previews/{body['preview_id']}/steps", json=action)
    assert repeated.status_code == 200
    assert repeated.json()["state"]["gold"] == 1
    assert repeated.json()["action_seq"] == 1
    changed = client.post(
        f"{PREFIX}/tasks/{task['id']}/feedback",
        json={
            "text": "减少煽情",
            "expected_revision": 1,
            "scope": "project",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["task"]["revision"] == 2
    assert (
        client.post(f"{PREFIX}/previews/{body['preview_id']}/steps", json=action).status_code == 409
    )
    assert client.get(f"/projects/alias/creative/tasks/{task['id']}").status_code == 404
    inherited = _task(client)
    assert inherited["feedback"][0]["text"] == "减少煽情"
    preference = client.get(f"{PREFIX}/preferences").json()["preferences"][0]
    assert client.delete(f"{PREFIX}/preferences/{preference['id']}").status_code == 200
    assert _task(client)["feedback"] == []
    assert ContentStore(root).load().quests["q_medicine"].objective == "取出唯一一剂药"


def test_detail_edit_and_workflow_library_use_immutable_contracts(studio) -> None:
    client, root, quest = studio
    task = _task(client)
    store, run, artifact = _content_artifact(root, task["id"], quest)
    detail = client.get(f"{PREFIX}/tasks/{task['id']}").json()
    assert next(node for node in detail["nodes"] if node["node_id"] == "quest")["attempts"] == 1
    saved = client.post(f"{PREFIX}/workflows", json={"name": "取舍流程", "run_id": run.id})
    assert saved.status_code == 201, saved.text
    workflow = saved.json()["workflow"]
    assert workflow["spec"]["nodes"][1]["parameters"] == {}
    assert client.get(f"{PREFIX}/workflows", params={"query": "取舍"}).json()["workflows"]
    assert (
        client.patch(f"{PREFIX}/workflows/{workflow['id']}", json={"disabled": True}).status_code
        == 200
    )
    assert client.get(f"{PREFIX}/workflows", params={"query": "取舍"}).json()["workflows"] == []
    payload = artifact.payload.copy()
    payload["quest"] = {**payload["quest"], "objective": "保留选择代价，修改表达"}
    edited = client.post(
        f"{PREFIX}/artifacts/{artifact.id}/revisions",
        json={
            "expected_hash": artifact.content_hash,
            "payload": payload,
        },
    )
    assert edited.status_code == 201, edited.text
    new_artifact = edited.json()["artifact"]
    assert new_artifact["revision"] == 2
    assert new_artifact["input_hash"] == artifact.input_hash
    assert store.get_artifact(artifact.id).payload["quest"]["objective"] == quest.objective
    assert (
        client.post(
            f"{PREFIX}/artifacts/{artifact.id}/revisions",
            json={
                "expected_hash": artifact.content_hash,
                "payload": payload,
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{PREFIX}/tasks/{task['id']}/preview",
            json={
                "artifact_id": new_artifact["id"],
            },
        ).status_code
        == 201
    )


def test_restore_historical_artifact_creates_a_cas_guarded_revision(studio) -> None:
    client, root, quest = studio
    task = _task(client)
    store, _, original = _content_artifact(root, task["id"], quest)
    changed = original.model_copy(deep=True).payload
    changed["quest"]["objective"] = "先询问药剂去向"
    edited = client.post(
        f"{PREFIX}/artifacts/{original.id}/revisions",
        json={"expected_hash": original.content_hash, "payload": changed},
    )
    assert edited.status_code == 201, edited.text
    restored = client.post(
        f"{PREFIX}/artifacts/{original.id}:restore",
        json={"expected_revision": 2, "source_hash": original.content_hash},
    )
    assert restored.status_code == 201, restored.text
    result = restored.json()
    assert result["task"]["revision"] == 3
    assert result["artifact"]["id"] not in {original.id, edited.json()["artifact"]["id"]}
    assert result["artifact"]["payload"] == original.payload
    assert store.get_artifact(edited.json()["artifact"]["id"]).payload == changed
    assert store.get_artifact(original.id).payload == original.payload
    assert len(store.list_runs(task["id"])) == 1
    assert store.model_calls(original.run_id) == []
    assert (
        client.post(
            f"{PREFIX}/artifacts/{original.id}:restore",
            json={"expected_revision": 2, "source_hash": original.content_hash},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/projects/alias/creative/artifacts/{original.id}:restore",
            json={"expected_revision": 3, "source_hash": original.content_hash},
        ).status_code
        == 404
    )


def test_edit_saved_workflow_preserves_history_and_safe_parameters(studio) -> None:
    client, root, quest = studio
    task = _task(client)
    store, run, _ = _content_artifact(root, task["id"], quest)
    saved = client.post(f"{PREFIX}/workflows", json={"name": "取舍", "run_id": run.id}).json()[
        "workflow"
    ]
    query_record = client.get(f"{PREFIX}/workflows", params={"query": "取舍"}).json()["workflows"][
        0
    ]
    assert query_record["content_hash"] == saved["content_hash"]
    spec = saved["spec"]
    spec["nodes"][1]["parameters"] = {"budget_tokens": 700}
    edited = client.patch(
        f"{PREFIX}/workflows/{saved['id']}",
        json={"expected_hash": saved["content_hash"], "name": "紧凑取舍", "spec": spec},
    )
    assert edited.status_code == 200, edited.text
    current = edited.json()["workflow"]
    assert current["id"] != saved["id"]
    assert current["source_workflow_id"] == saved["id"]
    assert current["spec"]["nodes"][1]["parameters"] == {"budget_tokens": 700}
    assert next(row for row in store.workflows() if row["id"] == saved["id"])["disabled"] is True
    assert (
        client.patch(
            f"{PREFIX}/workflows/{saved['id']}",
            json={"expected_hash": saved["content_hash"], "name": "迟到修改"},
        ).status_code
        == 409
    )
    bad = current["spec"]
    bad["nodes"][1]["parameters"] = {"old_character_id": "not-a-reusable-parameter"}
    assert (
        client.patch(
            f"{PREFIX}/workflows/{current['id']}",
            json={"expected_hash": current["content_hash"], "spec": bad},
        ).status_code
        == 409
    )
    bad["nodes"][0]["depends_on"] = [bad["nodes"][1]["id"]]
    assert (
        client.patch(
            f"{PREFIX}/workflows/{current['id']}",
            json={"expected_hash": current["content_hash"], "spec": bad},
        ).status_code
        == 422
    )
    assert len(store.workflows()) == 2
    assert store.model_calls(run.id) == []


def test_manual_workflow_artifact_edit_invalidates_only_changed_dependencies(studio) -> None:
    client, root, quest = studio
    task = _task(client)
    store, run, _ = _content_artifact(root, task["id"], quest)
    spec = store.get_run(run.id).workflow
    assert spec is not None
    original = Artifact(
        task_id=task["id"],
        revision=1,
        run_id=run.id,
        node_id="_plan",
        kind="workflow_spec",
        payload=spec.model_dump(mode="json"),
        input_hash="original-plan-input",
        content_hash=content_hash(spec),
    )
    store.append_artifact(original)
    payload = original.model_copy(deep=True).payload
    payload["nodes"][1]["parameters"] = {"budget_tokens": 900}
    response = client.post(
        f"{PREFIX}/artifacts/{original.id}/revisions",
        json={"expected_hash": original.content_hash, "payload": payload},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["task"]["revision"] == 2
    assert result["artifact"]["input_hash"] == original.input_hash
    assert result["artifact"]["payload"]["nodes"][1]["parameters"] == {"budget_tokens": 900}
    assert set(result["affected_operations"]) == {"workflow_spec", "draft_quest"}
    assert store.get_artifact(original.id).payload == original.payload
    assert store.model_calls(run.id) == []


def test_selected_project_workflow_cannot_cross_a_shared_content_root(studio, monkeypatch) -> None:
    from owcopilot.workflow import runtime

    client, root, quest = studio
    first_task = _task(client)
    store, first_run, _ = _content_artifact(root, first_task["id"], quest)
    flow = client.post(
        f"{PREFIX}/workflows", json={"name": "只用于demo", "run_id": first_run.id}
    ).json()["workflow"]
    response = client.post(
        "/projects/alias/creative/tasks",
        json={"brief": "相同物理目录的另一个项目", "workflow_id": flow["id"]},
    )
    assert response.status_code == 201, response.text
    task = store.get_task(response.json()["task"]["id"])
    interpretation = {"goal": "检查流程作用域", "assumptions": [], "protected": []}
    run, _ = store.create_run(task)
    store.append_artifact(
        Artifact(
            task_id=task.id,
            revision=task.revision,
            run_id=run.id,
            node_id="_interpret",
            kind="interpretation",
            payload=interpretation,
            input_hash=content_hash({"request": task.request, "base": task.base_hash}),
            content_hash=content_hash(interpretation),
        )
    )

    def forbidden_model(**kwargs):
        raise AssertionError("本检查不得构造真实模型")

    monkeypatch.setattr(runtime, "build_real_provider", forbidden_model)
    result = runtime.execute_workflow(store, run.id, str(root))
    outcomes = store.outcomes(run.id)
    assert result["status"] == "completed_with_skips"
    assert outcomes["_interpret"].status == "succeeded"
    assert outcomes["_plan"].status == "skipped_nonretryable"
    assert "所选流程不可用" in outcomes["_plan"].error
    assert store.model_calls(run.id) == []
    assert store.get_run(run.id).workflow is None


def test_task_graph_retains_planner_notes_and_runtime_reuses_the_human_plan(
    studio, monkeypatch
) -> None:
    from copy import deepcopy

    from owcopilot.workflow import runtime

    client, root, quest = studio
    response = client.post(
        f"{PREFIX}/tasks", json={"brief": "保留故事方向", "participation": "direction"}
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]
    store, first_run, _ = _content_artifact(root, task_id, quest)
    task = store.get_task(task_id)
    spec = store.get_run(first_run.id).workflow
    assert spec is not None
    spec.nodes[0].parameters = {"source": "frozen_project", "task_shape": {"stages": 3}}
    spec.nodes[1].parameters = {"source": "event_plan", "budget_tokens": 1500}
    fingerprint = content_hash({"request": task.request, "base": task.base_hash})
    original = Artifact(
        task_id=task_id,
        revision=1,
        run_id=first_run.id,
        node_id="_plan",
        kind="workflow_spec",
        payload=spec.model_dump(mode="json"),
        input_hash=fingerprint,
        content_hash=content_hash(spec),
    )
    store.append_artifact(original)
    interpretation = {"goal": "保留方向", "assumptions": [], "protected": []}
    store.append_artifact(
        Artifact(
            task_id=task_id,
            revision=1,
            run_id=first_run.id,
            node_id="_interpret",
            kind="interpretation",
            payload=interpretation,
            input_hash=fingerprint,
            content_hash=content_hash(interpretation),
        )
    )
    payload = deepcopy(original.payload)
    payload["nodes"][1]["parameters"]["budget_tokens"] = 700
    edited = client.post(
        f"{PREFIX}/artifacts/{original.id}/revisions",
        json={"expected_hash": original.content_hash, "payload": payload},
    )
    assert edited.status_code == 201, edited.text
    current = edited.json()["artifact"]
    assert (
        current["payload"]["nodes"][0]["parameters"] == original.payload["nodes"][0]["parameters"]
    )
    assert current["payload"]["nodes"][1]["parameters"]["budget_tokens"] == 700
    for changed_notes in ({"source": "changed"}, {"brand_new_note": "not-from-original"}):
        invalid = deepcopy(current["payload"])
        invalid["nodes"][0]["parameters"].update(changed_notes)
        assert (
            client.post(
                f"{PREFIX}/artifacts/{current['id']}/revisions",
                json={"expected_hash": current["content_hash"], "payload": invalid},
            ).status_code
            == 409
        )

    def forbidden_model(**kwargs):
        raise AssertionError("此检查不得调用模型重规划")

    monkeypatch.setattr(runtime, "build_real_provider", forbidden_model)
    run, _ = store.create_run(store.get_task(task_id))
    result = runtime.execute_workflow(store, run.id, str(root))
    assert result["status"] == "waiting_user"
    assert store.get_run(run.id).workflow.model_dump(mode="json") == current["payload"]
    assert store.outcomes(run.id)["_plan"].artifact_id == current["id"]
    assert store.model_calls(run.id) == []


def test_invalid_task_paths_and_invalid_candidate_fail_closed(studio) -> None:
    client, root, quest = studio
    assert (
        client.post(f"{PREFIX}/tasks", json={"brief": "x", "version": "../v1"}).status_code == 409
    )
    assert (
        client.post(f"{PREFIX}/tasks", json={"brief": "x", "world_id": "../x"}).status_code == 409
    )
    task = _task(client)
    _, _, artifact = _content_artifact(root, task["id"], quest)
    invalid = client.post(
        f"{PREFIX}/artifacts/{artifact.id}/revisions",
        json={
            "expected_hash": artifact.content_hash,
            "payload": {"quest": {"title": "missing ID"}},
        },
    )
    assert invalid.status_code == 422, invalid.text


def test_submit_once_then_accept_through_existing_review(studio) -> None:
    client, root, quest = studio
    task = _task(client)
    updated = quest.model_copy(update={"objective": "取出唯一药剂，保留选择代价"})
    _content_artifact(root, task["id"], updated)
    before = content_hash(ContentStore(root).load())
    submitted = client.post(f"{PREFIX}/tasks/{task['id']}:submit-review")
    assert submitted.status_code == 200, submitted.text
    review_id = submitted.json()["review_item_id"]
    assert (
        client.post(f"{PREFIX}/tasks/{task['id']}:submit-review").json()["review_item_id"]
        == review_id
    )
    assert content_hash(ContentStore(root).load()) == before
    result = decide_review_action(root, item_id=review_id, decision="accepted", operator="author")
    assert result["decision"] == "accepted"
    assert ContentStore(root).load().quests[quest.id].objective == updated.objective


@pytest.mark.parametrize("change", ["task_revision", "canon"])
def test_review_revalidates_task_revision_and_canon_before_write(studio, change: str) -> None:
    client, root, quest = studio
    task = _task(client)
    _content_artifact(root, task["id"], quest.model_copy(update={"objective": "候选表达"}))
    submitted = client.post(f"{PREFIX}/tasks/{task['id']}:submit-review")
    assert submitted.status_code == 200, submitted.text
    if change == "task_revision":
        response = client.post(
            f"{PREFIX}/tasks/{task['id']}/feedback",
            json={
                "text": "继续修改",
                "scope": "content",
                "expected_revision": 1,
            },
        )
        assert response.status_code == 200
    else:
        bundle = ContentStore(root).load()
        bundle.quests[quest.id].objective = "作者在另处修改了正典"
        ContentStore(root).save(bundle)
    before = content_hash(ContentStore(root).load())
    with pytest.raises(ValueError, match="版本已改变|基线已改变"):
        decide_review_action(
            root, item_id=submitted.json()["review_item_id"], decision="accepted", operator="author"
        )
    assert content_hash(ContentStore(root).load()) == before


def test_working_version_review_keeps_baseline_unchanged(studio) -> None:
    client, root, quest = studio
    content = ContentStore(root)
    version = content.ensure_migrated()
    task = _task(client)
    assert task["base_version"] == version
    updated = quest.model_copy(update={"objective": "工作版本中的新表达"})
    _content_artifact(root, task["id"], updated)
    submitted = client.post(f"{PREFIX}/tasks/{task['id']}:submit-review")
    assert submitted.status_code == 200, submitted.text
    decide_review_action(
        root, item_id=submitted.json()["review_item_id"], decision="accepted", operator="author"
    )
    assert content.load_scoped(version=version).quests[quest.id].objective == updated.objective
    assert content.load().quests[quest.id].objective == quest.objective
