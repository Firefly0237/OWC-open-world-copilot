"""Trial admission/receipts use real persistence; executor is explicitly controlled.

This is not narrative quality, a browser test or evidence of real author approval.
"""

import json

import pytest

from owcopilot.workflow import v2_service, v2_skills
from owcopilot.workflow.graph_delta import (
    baseline_to_constrained_delta,
    compile_graph_edit,
    project_legacy_graph_delta,
)
from owcopilot.workflow.graph_v2 import (
    PortSpec,
    create_narrative_graph,
    narrative_role_registry,
)
from tests.platform import test_workflow_v2_service as source_fixtures

studio = source_fixtures.studio


def test_trial_receipts_are_candidate_scoped_idempotent_and_revocation_is_real(studio, monkeypatch):
    root, store, task = studio
    skills = v2_skills.NarrativeEvolutionStore(store)
    baseline = create_narrative_graph()
    family = skills.create_family("demo", "Controlled trial admission")
    parent = skills.add_version(
        "demo",
        family["id"],
        graph=baseline.model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
    )
    source = v2_service.prepare_v2_run(
        store,
        root,
        task,
        parent["graph"],
        skill_version_id=parent["id"],
        collaboration_policy={"allow_candidate_trial": True},
    )
    store.update_run(
        source["run_id"],
        status="failed",
        error="Controlled source deliberately not executed; no narrative outcome is claimed.",
    )
    candidates = []
    for index in range(2):
        edit = project_legacy_graph_delta(
            baseline_to_constrained_delta(baseline).model_dump(mode="json")
        )["edit"]
        next(step for step in edit["operations"] if step["op"] == "insert_registered_role")[
            "guidance"
        ] = f"Controlled candidate {index}, not a proven improvement."
        compiled = compile_graph_edit(
            baseline,
            edit,
            narrative_role_registry(),
            {"task": PortSpec(type_name="task_context")},
        )
        candidates.append(
            skills.add_version(
                "demo",
                family["id"],
                graph=compiled.applied.graph.model_dump(mode="json"),
                parent_version_id=parent["id"],
                expected_family_hash=skills.get_family("demo", family["id"])["family_hash"],
            )
        )
    selected = [0]
    executed = []

    def method_fixture(*args, **kwargs):
        index = selected[0]
        return {
            "id": "controlled-episode",
            "result": {
                "status": "candidate",
                "candidate_version_id": candidates[index]["id"],
            },
            "attempt_state": {"latest": {"id": f"controlled-attempt-{index}"}},
        }

    def controlled_executor(store, root, project, run_id, *, emit=None):
        executed.append(run_id)
        store.update_run(run_id, status="completed")
        value = v2_service.v2_detail(store, project, run_id)
        # No narrative is synthesized: empty result keeps comparison withheld.
        value["result"] = {"status": "controlled_executor_only", "outputs": {}, "outcomes": {}}
        return value

    monkeypatch.setattr(v2_skills, "evolve_family", method_fixture)
    monkeypatch.setattr(v2_service, "execute_v2_run", controlled_executor)
    first = v2_service.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        source["run_id"],
        "Controlled fixture feedback",
    )
    duplicate = v2_service.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        source["run_id"],
        "Controlled fixture feedback",
    )
    assert first["trial"] == duplicate["trial"] and len(executed) == 1, first["trial"]
    selected[0] = 1
    second = v2_service.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        source["run_id"],
        "Controlled fixture feedback",
        attempt_reason="Independent later candidate after a known result",
    )
    assert len(executed) == 2
    assert second["trial"]["trial_key"] != first["trial"]["trial_key"]
    assert second["trial"]["comparison"]["status"] == "withheld"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM creative_v2_trial_claims_v2").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM creative_v2_trial_results_v2").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM creative_v2_trials").fetchone()[0] == 0
        raw = json.loads(
            db.execute(
                "SELECT payload FROM creative_v2_trial_results_v2 WHERE trial_key=?",
                (first["trial"]["trial_key"],),
            ).fetchone()[0]
        )
        assert raw == first["trial"]
    skills.authorize_trial(
        "demo",
        candidates[1]["id"],
        expected_family_hash=skills.get_family("demo", family["id"])["family_hash"],
        allowed=False,
        authority="explicit_author_action",
        note="Controlled author-action fixture: revoke permission",
    )
    candidate_run = v2_service.v2_detail(store, "demo", executed[-1])
    with pytest.raises(ValueError, match="撤销"):
        v2_skills.check_run_authorization(store, "demo", candidate_run)
    assert (
        skills.get_version("demo", candidates[1]["id"])["state"]["author_acceptance"] == "unknown"
    )
