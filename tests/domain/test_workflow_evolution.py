"""Skill evolution checks use real graph compilation, execution and SQLite persistence.

All author decisions here are explicitly controlled development inputs, not evidence
that a real author accepted the prototype's generated story or working method.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Lock

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.budget_policy import BudgetLedger, BudgetPolicy
from owcopilot.workflow.evolution import (
    EvolutionStore,
    ExecutionEvidence,
    GuidanceEntry,
    MachineRequirement,
    SkillConditions,
    SkillDelta,
    SourceRef,
    assess_conditions,
    run_evolution_episode,
)
from owcopilot.workflow.graph_v2 import (
    GraphSpecV2,
    NodeSpecV2,
    OutputRef,
    PortSpec,
    RoleContract,
    compile_graph,
    execute_graph,
    rewrite_graph,
)
from owcopilot.workflow.store import WorkflowStore


@pytest.fixture
def skill_store(tmp_path):
    roles = {
        "write": RoleContract(
            operation="write",
            inputs={"input": PortSpec(type_name="json")},
            outputs={"output": PortSpec(type_name="json")},
        )
    }
    context_ports = {"brief": PortSpec(type_name="json")}
    graph = GraphSpecV2(
        name="Local production method",
        nodes=[
            NodeSpecV2(
                id="writer",
                operation="write",
                product_key="story",
                problem_key="story-production",
                context_bindings={"input": "brief"},
                guidance="preserve the ending",
            )
        ],
        outputs={"story": OutputRef(node_id="writer", port="output")},
    )
    def validator(value):
        return compile_graph(value, roles, context_ports)
    store = EvolutionStore(WorkflowStore(tmp_path / "workflow.sqlite3"), validator)
    family = store.create_family("project", "Branching choice method", "Stateful game writing")
    version = store.add_version(
        "project",
        family["id"],
        graph=graph.model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
        entries=[GuidanceEntry(id="preserve_ending", text="Preserve the author's ending")],
    )
    return store, family, version, validator


def current_token(store, family):
    return store.get_family("project", family["id"])["family_hash"]


def run_and_record(store, version, validator, *, fail=False):
    compiled = validator(version["graph"])

    def handler(node, inputs):
        if fail:
            raise ValueError("Explicit development execution failure")
        return {"output": {"brief": inputs["input"], "guidance": node.guidance}}

    result = execute_graph(compiled, {"brief": "A guard protects his child"}, {"write": handler})
    consumed = [
        ref["content_hash"]
        for outcome in result.outcomes.values()
        for ref in outcome.input_refs.values()
    ]
    state = store.record_evaluation(
        "project",
        version["id"],
        ExecutionEvidence(
            run_id="controlled-development-" + version["id"],
            graph_hash=result.graph_hash,
            input_hash=content_hash({"brief": "A guard protects his child"}),
            status=result.status,
            consumed_artifact_hashes=consumed,
            failed_nodes=[
                key for key, value in result.outcomes.items() if value.status == "failed"
            ],
            checks={"declared_output_present": "story" in result.outputs},
            source_kind="controlled_development",
        ),
    )
    return result, state


def test_versions_keep_stable_default_until_independent_author_choice_and_rollback(skill_store):
    store, family, first, validator = skill_store
    result, state = run_and_record(store, first, validator)
    assert result.status == "completed" and state["technical_status"] == "passed"
    assert state["author_acceptance"] == "unknown"
    assert state["improvement_status"] == "unassessed"
    store.decide_version(
        "project",
        first["id"],
        expected_family_hash=current_token(store, family),
        decision="accepted",
        make_default=True,
        note="Controlled development author input",
    )
    delta = SkillDelta(
        rationale="The guard's intention must be visible in the plan",
        target_problem="lost-character-intent",
        graph_changes={"guidance": "state the character goal before the choice"},
        upsert_entries=[GuidanceEntry(id="character_goal", text="State the character goal first")],
    )

    def rewrite(value, changes):
        graph = GraphSpecV2.model_validate(value)
        node = graph.nodes[0].model_copy(update={"guidance": changes["guidance"]})
        return rewrite_graph(graph, nodes_upsert=[node]).model_dump(mode="json")

    second = store.apply_delta(
        "project",
        first["id"],
        delta,
        expected_family_hash=current_token(store, family),
        graph_rewriter=rewrite,
    )
    assert second["parent_version_id"] == first["id"] and second["version"] == 2
    assert store.get_family("project", family["id"])["default_version_id"] == first["id"]
    second_result, second_state = run_and_record(store, second, validator)
    assert second_result.outputs["story"]["guidance"] != result.outputs["story"]["guidance"]
    assert second_state["author_acceptance"] == "unknown"
    store.decide_version(
        "project",
        second["id"],
        expected_family_hash=current_token(store, family),
        decision="accepted",
        make_default=True,
        note="Controlled development choice of version 2",
    )
    store.set_default(
        "project",
        family["id"],
        first["id"],
        expected_family_hash=current_token(store, family),
        action="rollback",
    )
    assert store.get_family("project", family["id"])["default_version_id"] == first["id"]
    assert store.get_version("project", first["id"])["graph"] == first["graph"]
    assert len(store.versions("project", family["id"])) == 2


def test_authority_cas_parent_project_and_hash_tampering_are_rejected(skill_store):
    store, family, version, _ = skill_store
    with pytest.raises(KeyError):
        store.get_version("other-project", version["id"])
    with pytest.raises(ValueError, match="已改变"):
        store.decide_version(
            "project",
            version["id"],
            expected_family_hash=family["family_hash"],
            decision="accepted",
        )
    other = store.create_family("project", "Other method")
    with pytest.raises(ValueError, match="同一技能家族"):
        store.add_version(
            "project",
            other["id"],
            graph=version["graph"],
            parent_version_id=version["id"],
            expected_family_hash=other["family_hash"],
        )
    with store.workflow.connection() as db:
        db.execute(
            "UPDATE evolution_versions SET payload=replace(payload,?,?) WHERE id=?",
            ("preserve the ending", "secretly change the ending", version["id"]),
        )
    with pytest.raises(ValueError, match="hash"):
        store.get_version("project", version["id"])


def test_conditions_are_three_valued_and_trial_permission_is_not_author_acceptance(skill_store):
    store, family, version, _ = skill_store
    conditions = SkillConditions(
        required_resources=["npc:guard"],
        required_capabilities=["write"],
        requirements=[MachineRequirement(key="different_consequences", value=True, confirmed=True)],
    )
    unknown = assess_conditions(
        conditions,
        resources={"npc:guard"},
        capabilities={"write"},
        requirements={"different_consequences": True},
    )
    assert unknown["status"] == "unknown"
    assert (
        assess_conditions(
            conditions,
            resources={"npc:guard"},
            capabilities={"write"},
            requirements={"different_consequences": False},
            confirmed_requirement_keys={"different_consequences"},
        )["status"]
        == "conflict"
    )
    assert (
        assess_conditions(
            conditions,
            resources={"npc:guard"},
            capabilities={"write"},
            requirements={"different_consequences": 1},
            confirmed_requirement_keys={"different_consequences"},
        )["status"]
        == "conflict"
    )  # Python's True == 1 must not establish typed compatibility.
    assert (
        assess_conditions(
            SkillConditions(free_text=["The ending must feel tragic"]),
            resources=set(),
            capabilities=set(),
        )["status"]
        == "unknown"
    )
    state = store.authorize_trial(
        "project",
        version["id"],
        expected_family_hash=current_token(store, family),
        allowed=True,
        authority="preauthorized_policy",
        note="Controlled developer trial policy",
    )
    assert state["author_acceptance"] == "unknown"
    assert store.match("project", "branching", resources=set(), capabilities={"write"}) == []
    matches = store.match(
        "project",
        "branching",
        resources=set(),
        capabilities={"write"},
        include_trials=True,
    )
    assert matches[0]["selection"] == "authorized_trial"
    assert matches[0]["eligible_to_execute"] is True
    assert store.get_family("project", family["id"])["default_version_id"] is None


def test_negative_experience_and_episode_identity_do_not_convert_environment_failure_to_skill(
    skill_store,
):
    store, family, version, _ = skill_store
    BudgetLedger(store.workflow).publish_policy(
        BudgetPolicy(id="controlled-episode-cap", max_owner_dispatches=3),
        reason="Explicit finite policy for episode identity checks",
    )
    source = SourceRef(kind="development_input", id="environment-1", content_hash=content_hash("x"))
    failure = store.record_experience(
        "project",
        family["id"],
        kind="provider_failure",
        source_ref=source,
        payload={"error": "Connection refused"},
        problem_key="guard-dialogue",
        version_id=version["id"],
    )
    with pytest.raises(ValueError, match="运行诊断"):
        store.begin_episode(
            "project",
            family["id"],
            failure["id"],
            "guard-dialogue",
            parent_version_id=version["id"],
        )
    source = SourceRef(
        kind="development_input", id="feedback-1", content_hash=content_hash("feedback")
    )
    feedback = store.record_experience(
        "project",
        family["id"],
        kind="author_feedback",
        source_ref=source,
        payload={"text": "Keep the guard's goal"},
        problem_key="guard-dialogue",
    )
    assert (
        store.record_experience(
            "project",
            family["id"],
            kind="author_feedback",
            source_ref=source,
            payload={"text": "Keep the guard's goal"},
            problem_key="guard-dialogue",
        )["id"]
        == feedback["id"]
    )
    episode = store.begin_episode(
        "project",
        family["id"],
        feedback["id"],
        "guard-dialogue",
        parent_version_id=version["id"],
    )
    assert (
        store.begin_episode(
            "project",
            family["id"],
            feedback["id"],
            "guard-dialogue",
            parent_version_id=version["id"],
        )["id"]
        == episode["id"]
    )
    other_feedback = store.record_experience(
        "project",
        family["id"],
        kind="technical_failure",
        source_ref=source.model_copy(update={"id": "feedback-2"}),
        payload={"issue": "wrong-referenced-character"},
        problem_key="guard-dialogue",
    )
    next_episode = store.begin_episode(
        "project",
        family["id"],
        other_feedback["id"],
        "guard-dialogue",
        parent_version_id=version["id"],
    )
    assert next_episode["id"] != episode["id"]
    assert next_episode["budget_owner"] == episode["budget_owner"]
    assert episode["max_model_dispatches"] == 3
    assert next_episode["budget_policy"] == episode["budget_policy"]


def test_one_trigger_has_one_claimed_candidate_even_with_concurrent_workers(skill_store):
    store, family, version, _ = skill_store
    feedback = store.record_experience(
        "project",
        family["id"],
        kind="author_feedback",
        source_ref=SourceRef(
            kind="development_input", id="one-trigger", content_hash=content_hash("one")
        ),
        payload={"text": "Make the motivation explicit"},
        problem_key="motivation",
    )
    episode = store.begin_episode(
        "project",
        family["id"],
        feedback["id"],
        "motivation",
        parent_version_id=version["id"],
    )
    count = 0
    lock = Lock()

    def controlled_proposal(context):
        nonlocal count
        with lock:
            count += 1
        assert context["episode"]["budget_owner"] == episode["budget_owner"]
        return SkillDelta(
            rationale="Explicit controlled-development proposal, no model was called",
            target_problem="motivation",
            upsert_entries=[GuidanceEntry(id="motivation", text="Clarify the motivation first")],
        )

    def invoke(_):
        return run_evolution_episode(
            store,
            "project",
            episode["id"],
            budgeted_propose=controlled_proposal,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(invoke, range(2)))
    assert count == 1
    completed = store.get_episode("project", episode["id"])
    assert completed["result"]["status"] == "candidate"
    candidate = store.get_version("project", completed["result"]["candidate_version_id"])
    assert candidate["state"]["technical_status"] == "compiled"
    assert candidate["state"]["author_acceptance"] == "unknown"
    assert candidate["state"]["improvement_status"] == "unassessed"
    assert len(store.versions("project", family["id"])) == 2
    assert (
        run_evolution_episode(
            store,
            "project",
            episode["id"],
            budgeted_propose=lambda _: pytest.fail("No second proposal"),
        )["result"]
        == completed["result"]
    )


def test_execution_failure_or_uncompiled_schema_never_becomes_passed_or_default(skill_store):
    store, family, version, validator = skill_store
    result, state = run_and_record(store, version, validator, fail=True)
    assert result.status == "completed_with_skips" and state["technical_status"] == "failed"
    with pytest.raises(ValueError, match="不能接受"):
        store.decide_version(
            "project",
            version["id"],
            expected_family_hash=current_token(store, family),
            decision="accepted",
            make_default=True,
        )
    schema_store = EvolutionStore(store.workflow)
    other = schema_store.create_family("project", "Schema-only plan")
    schema = schema_store.add_version(
        "project",
        other["id"],
        graph=deepcopy(version["graph"]),
        expected_family_hash=other["family_hash"],
    )
    assert schema["state"]["technical_status"] == "schema_validated"
    with pytest.raises(ValueError, match="不是该技能图版本"):
        schema_store.record_evaluation(
            "project",
            schema["id"],
            ExecutionEvidence(
                run_id="false-evidence",
                graph_hash=content_hash(schema["graph"]),
                input_hash=content_hash({}),
                status="completed",
                consumed_artifact_hashes=["0" * 64],
                source_kind="controlled_development",
            ),
        )
