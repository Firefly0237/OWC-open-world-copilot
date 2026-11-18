"""Actual production integration with isolated controlled transports and no human claims."""

import json

from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import create_task_action
from owcopilot.service.api import create_app
from owcopilot.workflow import memory_lessons, memory_routing, v2_planning, v2_service
from owcopilot.workflow.execution_records import ExecutionRecords
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.models import TaskCreateInput
from owcopilot.workflow.routed_memory import MemoryClusters
from owcopilot.workflow.skill_cards import SkillCards
from owcopilot.workflow.v2_evidence import export_v2_evidence
from tests.platform.test_creator_production_v2 import ControlledProductionProvider
from tests.platform.test_memory_lessons import ControlledLessonEncoder, ControlledLessonProvider
from tests.platform.test_memory_routing import ControlledRouter
from tests.platform.test_skill_cards_v2 import (
    PREFIX,
    ControlledThreeChoiceProvider,
    _finished_http_job,
    _selection,
)
from tests.platform.test_skill_cards_v2 import studio as studio
from tests.platform.test_skill_summary_schedule import card_case


def router_for_stream():
    def choose(context):
        if context["clusters"]:
            return {
                "action": "reuse",
                "cluster_id": context["clusters"][0]["id"],
                "backend": None,
                "description": None,
                "reason": "Controlled reuse of the existing experience",
            }
        return {
            "action": "create",
            "cluster_id": None,
            "backend": "trajectory",
            "description": "Short choice scenes",
            "reason": "Controlled first experience group",
        }

    return ControlledRouter(choose)


def test_http_routing_reads_updates_and_supplies_real_experience_to_the_next_task(
    studio, monkeypatch
):
    root, store, task = studio
    card = card_case(store)
    router = router_for_stream()
    production = ControlledThreeChoiceProvider()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(memory_routing, "build_real_provider", lambda **_: router)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    payload = {
        "selection": "template",
        "variant": "constrained",
        "memory_mode": "routed",
        "machine_requirements": {"decision_option_count": 2},
        "skill_card_selections": [_selection(card)],
        "apply_project_skills": False,
        "summarize_skill": False,
    }
    with TestClient(create_app()) as client:
        first = _finished_http_job(
            client, client.post(PREFIX + f"/tasks/{task.id}/runs", json=payload)
        )
        assert first["run"]["status"] == "completed"
        assert first["memory_route"]["read_snapshot"]["payload"]["messages"] == []
        assert first["memory_update"]["status"] == "recorded"
        second_task = store.get_task(
            create_task_action(
                root,
                "demo",
                TaskCreateInput(
                    brief="Controlled different scene, preserving two distinct choices",
                    origin_kind="development",
                ),
            )["task"]["id"]
        )
        second = _finished_http_job(
            client, client.post(PREFIX + f"/tasks/{second_task.id}/runs", json=payload)
        )
    assert second["run"]["status"] == "completed"
    route = second["memory_route"]
    assert route["cluster_id"] == first["memory_route"]["cluster_id"]
    assert route["read_snapshot"]["payload"]["messages"]
    assert second["memory_update"]["state_version"] == 2
    assert len(router.calls) == 2
    requests = HarnessStore(store).requests(second["run_id"])
    assert len(requests) == 4
    for request in requests:
        user = json.loads(request["request"]["user"])
        assert user["past_experience"] == route["read_snapshot"]["payload"]
        assert request["context_manifest"]["memory_route_hash"] == route["receipt_hash"]
    quest = next(row for row in requests if row["request"]["role_id"] == "draft_quest")
    assert "Keep 2 distinct options" in json.loads(quest["request"]["user"])["guidance"]
    assert SkillCards(store).detail("demo", card["id"])["active_version_id"] is None
    evidence = export_v2_evidence(store, root, "demo", second["run_id"])
    assert evidence["recording"]["current_task_measurements"]["dispatch_count"] == 5
    assert len(MemoryClusters(store).catalog("demo")) == 1


def test_author_gate_resume_uses_the_same_route_without_model_rerouting(studio, monkeypatch):
    _, store, task = studio
    router = router_for_stream()
    production = ControlledThreeChoiceProvider()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(memory_routing, "build_real_provider", lambda **_: router)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    with TestClient(create_app()) as client:
        first = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/tasks/{task.id}/runs",
                json={
                    "selection": "template",
                    "memory_mode": "routed",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 2},
                    "collaboration_policy": {"mode": "direction"},
                },
            ),
        )
        assert first["run"]["status"] == "waiting_user"
        assert "memory_update" not in first
        resumed = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/runs/{first['run_id']}/resume",
                json={
                    "action": "continue",
                    "expected_gate_hash": first["collaboration"]["pending_gate"]["gate_hash"],
                    "actor_kind": "development_operator",
                },
            ),
        )
    assert resumed["run"]["status"] == "completed"
    assert resumed["memory_route"] == first["memory_route"]
    assert resumed["memory_update"]["state_version"] == 1
    assert len(router.calls) == 1
    assert len(production.calls) == 4


def test_planned_production_binds_the_route_to_the_actual_planner_and_final_run(
    studio, monkeypatch
):
    _, store, task = studio
    router = router_for_stream()
    production = ControlledProductionProvider()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(memory_routing, "build_real_provider", lambda **_: router)
    monkeypatch.setattr(v2_planning, "build_real_provider", lambda **_: production)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    with TestClient(create_app()) as client:
        result = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/tasks/{task.id}/runs",
                json={
                    "selection": "planned",
                    "memory_mode": "routed",
                    "machine_requirements": {"decision_option_count": 2},
                },
            ),
        )
    assert result["run"]["status"] == "completed"
    route = result["memory_route"]
    planner_request = next(
        row["user"] for row in production.calls if "registered_roles" in row["user"]
    )
    assert planner_request["past_experience"] == route["read_snapshot"]["payload"]
    decision = next(
        row
        for row in ExecutionRecords(store).for_task("demo", task.id)
        if row["id"] == route["decision_id"]
    )
    planner_id = next(
        node["parameters"]["initial_planning"]["run_id"]
        for node in result["graph"]["nodes"]
        if "initial_planning" in node.get("parameters", {})
    )
    assert set(decision["run_ids"]) == {route["router_run_id"], planner_id, result["run_id"]}
    assert len(router.calls) == 1
    assert len(production.calls) == 5


def test_http_strategy_forms_from_the_actual_run_and_reaches_a_new_task_without_adopting_skills(
    studio, monkeypatch
):
    root, store, task = studio

    def choose(context):
        if context["clusters"]:
            return {
                "action": "reuse",
                "cluster_id": context["clusters"][0]["id"],
                "backend": None,
                "description": None,
                "reason": "Controlled reuse",
            }
        return {
            "action": "create",
            "cluster_id": None,
            "backend": "strategy",
            "description": "Independent branching scene methods",
            "reason": "Controlled cold start",
        }

    router = ControlledRouter(choose)
    production = ControlledThreeChoiceProvider()
    formation = ControlledLessonProvider()
    encoder = ControlledLessonEncoder()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(memory_routing, "build_real_provider", lambda **_: router)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    monkeypatch.setattr(memory_lessons, "build_real_provider", lambda **_: formation)
    monkeypatch.setattr(memory_lessons, "get_lesson_encoder", lambda: encoder)
    monkeypatch.setattr(memory_lessons, "lesson_encoder_identity", lambda: encoder.identity)
    payload = {
        "selection": "template",
        "memory_mode": "routed",
        "variant": "constrained",
        "machine_requirements": {"decision_option_count": 2},
        "summarize_skill": False,
    }
    with TestClient(create_app()) as client:
        first = _finished_http_job(
            client, client.post(PREFIX + f"/tasks/{task.id}/runs", json=payload)
        )
        assert first["run"]["status"] == "completed"
        assert first["memory_route"]["read_snapshot"]["payload"]["lessons"] == []
        assert first["memory_update"]["learning_status"] == "completed"
        assert first["memory_update"]["lesson_count"] == 1
        next_task = store.get_task(
            create_task_action(
                root,
                "demo",
                TaskCreateInput(
                    brief="Another independent two-option scene", origin_kind="development"
                ),
            )["task"]["id"]
        )
        second = _finished_http_job(
            client, client.post(PREFIX + f"/tasks/{next_task.id}/runs", json=payload)
        )
    assert second["run"]["status"] == "completed"
    route = second["memory_route"]
    assert route["selection"] == "reuse"
    assert route["read_snapshot"]["payload"]["lessons"][0]["source_run_id"] == first["run_id"]
    for request in HarnessStore(store).requests(second["run_id"]):
        assert (
            json.loads(request["request"]["user"])["past_experience"]
            == route["read_snapshot"]["payload"]
        )
    assert len(formation.calls) == 4
    assert len(production.calls) == 8
    assert len(router.calls) == 2
    assert SkillCards(store).list("demo") == []
    evidence = export_v2_evidence(store, root, "demo", second["run_id"])
    assert evidence["recording"]["current_task_measurements"]["dispatch_count"] == 7
    assert evidence["routed_memory"]["semantic_compliance"] == "not_established"
