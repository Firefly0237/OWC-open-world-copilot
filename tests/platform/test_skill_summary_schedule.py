"""Observe optional background learning without repeating successful reuse."""

import time

import pytest
from fastapi.testclient import TestClient

from owcopilot.service.api import create_app
from owcopilot.workflow import skill_learning, v2_service, v2_skills
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.skill_cards import SkillCards
from owcopilot.workflow.v2_evidence import export_v2_evidence
from tests.platform.test_skill_cards_v2 import (
    PREFIX,
    ControlledSkillSummary,
    ControlledThreeChoiceProvider,
    _finished_http_job,
    _selection,
)
from tests.platform.test_skill_cards_v2 import studio as studio


def card_case(store):
    return SkillCards(store).add_candidate(
        "demo",
        {
            "name": "Current option count",
            "purpose": "Use the current choice contract",
            "steps": [
                {
                    "id": "options",
                    "title": "Keep the current options",
                    "operation": "draft_quest",
                    "instruction": "Keep {{count}} distinct options in this scene.",
                    "slots": [
                        {
                            "name": "count",
                            "kind": "requirement",
                            "binding_key": "decision_option_count",
                            "source_value": "2",
                        }
                    ],
                }
            ],
            "applies_to": ["An explicit choice scene"],
            "does_not_apply_to": [],
        },
        {"kind": "import", "collection_condition": "synthetic_protocol"},
    )[0]


def test_opted_in_first_run_schedules_one_separate_candidate_job(studio, monkeypatch):
    root, store, task = studio
    production = ControlledThreeChoiceProvider()
    summary = ControlledSkillSummary()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    monkeypatch.setattr(skill_learning, "build_real_provider", lambda **_: summary)
    with TestClient(create_app()) as client:
        result = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/tasks/{task.id}/runs",
                json={
                    "selection": "template",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 2},
                    "apply_project_skills": False,
                    "summarize_skill": True,
                },
            ),
        )
        assert result["run"]["status"] == "completed"
        job_id = result["skill_summary_job_id"]
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] in {"done", "failed"}:
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        candidate = job["result"]
        assert candidate["status"] == "candidate"
        assert candidate["source_run_id"] == result["run_id"]
        card = SkillCards(store).detail("demo", candidate["card_id"])
        assert card["active_version_id"] is None
        assert len(summary.calls) == 1
        # Reading completed jobs and cards observes the same result, without replay.
        client.get(f"/jobs/{job_id}")
        client.get(PREFIX + "/skill-cards")
        assert len(summary.calls) == 1
    trajectory = export_v2_evidence(store, root, "demo", result["run_id"])[
        "maintenance_trajectories"
    ]
    assert trajectory["training_eligible"] is False
    transition = trajectory["transitions"][0]
    assert transition["decision_id"] == result["skill_maintenance_decision_id"]
    assert transition["actual_action"] == "summarize"
    assert transition["costs"]["dispatch_count"] == 1
    assert transition["costs"]["total_tokens"] == 26
    assert transition["reward"] is None
    assert transition["author_feedback"]["verified_human_evaluation"] is False


def test_successful_skill_reuse_does_not_pay_for_repeat_summary(studio, monkeypatch):
    root, store, task = studio
    card = card_case(store)
    production = ControlledThreeChoiceProvider()
    summaries = []
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)

    def summary_factory(**kwargs):
        summaries.append(kwargs)
        raise AssertionError("Normal reuse must not launch another model reflection")

    monkeypatch.setattr(skill_learning, "build_real_provider", summary_factory)
    with TestClient(create_app()) as client:
        result = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/tasks/{task.id}/runs",
                json={
                    "selection": "template",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 2},
                    "skill_card_selections": [_selection(card)],
                    "apply_project_skills": False,
                    "summarize_skill": True,
                },
            ),
        )
    assert result["run"]["status"] == "completed"
    assert result["skill_summary_status"] == "retained_existing"
    assert "skill_summary_job_id" not in result
    assert not summaries
    assert len(SkillCards(store).detail("demo", card["id"])["versions"]) == 1
    transition = export_v2_evidence(store, root, "demo", result["run_id"])[
        "maintenance_trajectories"
    ]["transitions"][0]
    assert transition["actual_action"] == "retain"
    assert transition["costs"]["dispatch_count"] == 0
    assert transition["costs"]["total_tokens"] == 0
    assert transition["costs"]["excluded_source_run_id"] == result["run_id"]


def test_failed_background_summary_keeps_story_and_unknown_action_attribution(studio, monkeypatch):
    root, store, task = studio
    production = ControlledThreeChoiceProvider()

    class InvalidSummary(ControlledSkillSummary):
        def complete(self, **kwargs):
            result = super().complete(**kwargs)
            return "{invalid-json", *result[1:]

    summary = InvalidSummary()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    monkeypatch.setattr(skill_learning, "build_real_provider", lambda **_: summary)
    with TestClient(create_app()) as client:
        result = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/tasks/{task.id}/runs",
                json={
                    "selection": "template",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 2},
                    "summarize_skill": True,
                },
            ),
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{result['skill_summary_job_id']}").json()
            if job["status"] in {"done", "failed"}:
                break
            time.sleep(0.05)
        assert job["status"] == "failed"
        observed = client.get(PREFIX + f"/runs/{result['run_id']}").json()
        assert observed["run"]["status"] == "completed"
    assert len(summary.calls) == 1
    evidence = export_v2_evidence(store, root, "demo", result["run_id"])
    transition = evidence["maintenance_trajectories"]["transitions"][0]
    assert transition["outcome"]["status"] == "unknown"
    assert transition["costs"]["dispatch_count"] is None
    assert len(evidence["procedural_skills"]["summary_attempts"]) == 1
    assert evidence["procedural_skills"]["summary_traces"][0]["run"]["status"] == "failed"


def test_missing_skill_parameters_are_rejected_before_model_planning(studio):
    root, store, task = studio
    card = card_case(store)
    provider = ControlledThreeChoiceProvider()
    with pytest.raises(ValueError, match="缺少本次所需"):
        v2_skills.plan_initial_graph(
            store,
            root,
            task,
            provider=provider,
            skill_card_selections=[_selection(card)],
            apply_project_skills=False,
        )
    assert provider.calls == []


def test_maintenance_admission_error_is_visible_without_failing_the_story(studio, monkeypatch):
    from owcopilot.workflow import skill_policy

    _, _, task = studio
    production = ControlledThreeChoiceProvider()
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)

    def reject(*args, **kwargs):
        raise ValueError("维护策略准入记录待核对")

    monkeypatch.setattr(skill_policy, "record_maintenance_decision", reject)
    with TestClient(create_app()) as client:
        result = _finished_http_job(
            client,
            client.post(
                PREFIX + f"/tasks/{task.id}/runs",
                json={
                    "selection": "template",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 2},
                    "summarize_skill": True,
                },
            ),
        )
        assert result["run"]["status"] == "completed"
        assert result["skill_summary_error"] == "维护策略准入记录待核对"
        assert "skill_summary_job_id" not in result
        observed = client.get(PREFIX + f"/runs/{result['run_id']}").json()
        assert observed["run"]["status"] == "completed"
        assert observed["skill_summary_error"] == result["skill_summary_error"]


def test_cancel_at_author_gate_uses_the_real_resume_http_path(studio):
    root, store, task = studio
    state = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph("constrained").model_dump(mode="json"),
        machine_requirements={"decision_option_count": 2},
        collaboration_policy={"mode": "direction"},
    )
    provider = ControlledThreeChoiceProvider()
    waiting = v2_service.execute_v2_run(store, root, "demo", state["run_id"], provider=provider)
    assert waiting["run"]["status"] == "waiting_user"
    with TestClient(create_app()) as client:
        cancelled = client.post(
            PREFIX + f"/runs/{state['run_id']}/resume",
            json={
                "action": "cancel",
                "expected_gate_hash": waiting["collaboration"]["pending_gate"]["gate_hash"],
            },
        )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["run"]["status"] == "cancelled"
    assert len(provider.calls) == 1


def test_cancel_source_via_http_also_stops_its_prepared_repair(studio):
    from owcopilot.workflow import repair_v2
    from tests.platform.test_workflow_targeted_repair import broken_source

    root, store, source, _ = broken_source(studio)
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    prepared = repair_v2.prepare_repair_run(
        store,
        root,
        "demo",
        source["run_id"],
        expected_diagnosis_hash=diagnosis["diagnosis_hash"],
        command_id="http-cancel-repair",
    )
    with TestClient(create_app()) as client:
        cancelled = client.post(PREFIX + f"/runs/{source['run_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert prepared["prepared"]["run_id"] in cancelled.json()["cancelled_repair_run_ids"]
    child = v2_service.v2_detail(store, "demo", prepared["prepared"]["run_id"])
    with pytest.raises(ValueError, match="取消"):
        repair_v2.validate_repair_contract(store, "demo", child)
