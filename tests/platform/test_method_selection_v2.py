"""Real executor integration with explicitly controlled development transports."""

from owcopilot.workflow import v2_service, v2_skills
from owcopilot.workflow.execution_records import ExecutionRecords
from owcopilot.workflow.method_assets import derive_assets
from tests.platform import test_creator_production_v2 as fixtures
from tests.platform.test_task_method_permissions import candidate_case, grant

studio = fixtures.studio


def test_selected_fragment_is_actually_executed_under_current_trial_scope(studio):
    root, store = studio
    task, skills, version = candidate_case(root, store)
    permit = grant(store, task, version)
    fragment = next(a for a in derive_assets(version) if a.kind == "skill")
    prepared = v2_skills.prepare_skill_run(
        store,
        root,
        task,
        version["id"],
        asset=fragment.model_dump(),
        machine_requirements=fixtures.MACHINE,
        trial_scope_id=permit["id"],
        expected_trial_scope_hash=permit["permit_hash"],
    )
    provider = fixtures.ControlledProductionProvider()
    state = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert state["run"]["status"] == "completed", state["run"]
    assert len(provider.calls) == 4
    assert state["asset_binding"]["asset"]["kind"] == "skill"
    assert state["result"]["outcomes"]["decision_gate"]["status"] == "succeeded"
    assert skills.get_version("demo", version["id"])["state"]["author_acceptance"] == "unknown"


def test_fresh_selection_records_candidates_and_links_planning_before_production(studio):
    root, store = studio
    task = fixtures.task_for(root, store)
    provider = fixtures.ControlledProductionProvider()
    prepared = v2_skills.prepare_auto_run(
        store, root, task, machine_requirements=fixtures.MACHINE, provider=provider
    )
    receipts = ExecutionRecords(store).for_task("demo", task.id)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["selected_action_id"] == "fresh"
    assert receipt["candidates"][0]["estimated_tokens"] is None
    assert len(receipt["run_ids"]) == 2
    assert prepared["method_decision"]["cost_status"] == "unknown"
    result = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert result["run"]["status"] == "completed", result["run"]
    assert len(provider.calls) == 5
