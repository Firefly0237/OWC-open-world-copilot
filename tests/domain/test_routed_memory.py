"""Controlled storage checks; no live requests, human judgments or learned rewards."""

import json

import pytest

from owcopilot.app.actions.workflow import create_task_action
from owcopilot.content.hash import content_hash
from owcopilot.workflow.harness import Harness, HarnessStore
from owcopilot.workflow.models import TaskInput
from owcopilot.workflow.routed_memory import MemoryClusters
from owcopilot.workflow.skill_cards import SkillCards
from tests.platform.test_workflow_v2_service import _execute
from tests.platform.test_workflow_v2_service import studio as studio

CONTRACT = {
    "model": "controlled",
    "roles": "fixed-role-schema",
    "schema_version": 1,
    "adapter_protocol": "test-1",
}


def new_task(root, store, brief="A new controlled scene"):
    task = create_task_action(root, "demo", TaskInput(brief=brief))["task"]
    return store.get_task(task["id"])


def bind_receipt(store, run_id, snapshot):
    receipt = {
        key: snapshot[key]
        for key in ("cluster_id", "cluster_hash", "backend", "state_hash", "contract")
    }
    receipt["read_snapshot"] = snapshot
    receipt["receipt_hash"] = content_hash(receipt)
    with store.connection() as db:
        row = db.execute(
            "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        state = json.loads(row[0])
        state["memory_route"] = receipt
        db.execute(
            "UPDATE creative_v2_runs SET payload=? WHERE run_id=?", (json.dumps(state), run_id)
        )


def source(studio, memory, cluster, task=None, **kwargs):
    root, store, default = studio
    task = task or default
    snapshot = memory.read_snapshot(
        "demo",
        cluster["id"],
        root=root,
        task=task,
        expected_contract=CONTRACT,
        explicit_skill_selections=kwargs.get("skill_card_selections", []),
    )
    result, _ = _execute(root, store, task, **kwargs)
    assert result["run"]["status"] == "completed"
    bind_receipt(store, result["run_id"], snapshot)
    return result, snapshot


def test_cluster_definition_is_immutable_and_catalog_is_project_contract_scoped(studio):
    _, store, _ = studio
    memory = MemoryClusters(store)
    cluster = memory.create(
        "demo", "trajectory", "Only choice scenes", contract=CONTRACT, cluster_id="stable"
    )
    assert (
        memory.create(
            "demo", "trajectory", "Only choice scenes", contract=CONTRACT, cluster_id="stable"
        )
        == cluster
    )
    assert memory.catalog("other") == []
    assert memory.catalog("demo", contract={**CONTRACT, "schema_version": 2}) == []
    with pytest.raises(ValueError, match="不可改写"):
        memory.create("demo", "skill", "Changed mechanism", contract=CONTRACT, cluster_id="stable")
    with pytest.raises(KeyError):
        memory.read_snapshot("other", cluster["id"], root=studio[0], task=studio[2])


def test_budget_skips_oversized_complete_units_without_cutting_json_or_reordering():
    first = {
        "source_request": {"id": "first", "user": "small"},
        "observed_results": [{"response": "one"}],
    }
    second = {
        "source_request": {"id": "second", "user": "small"},
        "observed_results": [{"response": "two"}],
    }
    latest = {
        "source_request": {"id": "large", "user": "x" * 3000},
        "observed_results": [{"response": "three"}],
    }
    units, omitted = MemoryClusters._bounded([first, second, latest], 1024)
    assert units == [first, second]
    assert omitted == 1


def test_two_generation_view_removes_only_prior_advisory_and_keeps_original_harness(
    studio, monkeypatch
):
    root, store, _ = studio
    memory = MemoryClusters(store)
    cluster = memory.create("demo", "trajectory", "Choice native task views", contract=CONTRACT)
    first, initial = source(studio, memory, cluster)
    memory.append_run(
        "demo", cluster["id"], first["run_id"], expected_state_hash=initial["state_hash"], root=root
    )
    second_task = new_task(
        root, store, "Second generation keeps current requirements and explicit guidance"
    )
    first_generation = memory.read_snapshot("demo", cluster["id"], root=root, task=second_task)
    original_complete = Harness.complete

    def complete_with_prior(self, **kwargs):
        context = json.loads(kwargs["user"])
        context["past_experience"] = first_generation["payload"]
        kwargs["user"] = json.dumps(context)
        kwargs["context_manifest"] = {
            **kwargs["context_manifest"],
            "memory_route_hash": "controlled-prior-route",
            "memory_snapshot_hash": first_generation["snapshot_hash"],
        }
        return original_complete(self, **kwargs)

    monkeypatch.setattr(Harness, "complete", complete_with_prior)
    second, read = source(
        studio,
        memory,
        cluster,
        second_task,
        plan_guidance={"logic": "Keep explicit current guidance"},
    )
    raw_before = HarnessStore(store).requests(second["run_id"])
    assert all("past_experience" in json.loads(row["request"]["user"]) for row in raw_before)
    memory.append_run(
        "demo", cluster["id"], second["run_id"], expected_state_hash=read["state_hash"], root=root
    )
    third_task = new_task(root, store, "Third generation reads the latest native task")
    second_generation = memory.read_snapshot(
        "demo", cluster["id"], root=root, task=third_task, max_chars=128000
    )
    native_units = [
        unit
        for unit in second_generation["payload"]["units"]
        if unit["source_run_id"] == second["run_id"]
    ]
    assert native_units
    originals = {row["id"]: row for row in raw_before}
    for unit in native_units:
        native = unit["source_request"]
        original = originals[native["id"]]
        original_context = json.loads(original["request"]["user"])
        expected = {
            key: value for key, value in original_context.items() if key != "past_experience"
        }
        assert json.loads(native["request"]["user"]) == expected
        projection = native["projection"]
        assert projection["applied"] is True
        assert (
            projection["original_request_hash"]
            == original["request_hash"]
            == native["request_hash"]
        )
        assert projection["projected_request_hash"] == content_hash(native["request"])
        assert projection["projected_request_hash"] != projection["original_request_hash"]
        assert projection["removed_past_experience_hash"] == content_hash(
            first_generation["payload"]
        )
        assert projection["memory_route_hash"] == "controlled-prior-route"
        assert projection["memory_snapshot_hash"] == first_generation["snapshot_hash"]
        assert projection["source_ref"]["request_id"] == original["id"]
        assert unit["observed_results"]
    assert HarnessStore(store).requests(second["run_id"]) == raw_before
    assert (
        memory.assert_snapshot("demo", second_generation, root=root, task=third_task)
        == second_generation
    )


def test_append_keeps_exact_request_results_and_discloses_whole_unit_budget(studio):
    root, store, _ = studio
    memory = MemoryClusters(store)
    cluster = memory.create("demo", "trajectory", "Choice production", contract=CONTRACT)
    result, empty = source(studio, memory, cluster)
    appended = memory.append_run(
        "demo", cluster["id"], result["run_id"], expected_state_hash=empty["state_hash"], root=root
    )
    assert appended["completed_count"] == appended["state_version"] == 1
    repeated = memory.append_run(
        "demo", cluster["id"], result["run_id"], expected_state_hash=empty["state_hash"], root=root
    )
    assert repeated["status"] == "already_recorded"
    assert repeated["state_hash"] == appended["state_hash"]
    task = new_task(root, store)
    snapshot = memory.read_snapshot(
        "demo", cluster["id"], root=root, task=task, max_chars=128000, expected_contract=CONTRACT
    )
    assert snapshot["payload"]["units"]
    first = snapshot["payload"]["units"][0]
    assert (
        content_hash(first["source_request"]["request"]) == first["source_request"]["request_hash"]
    )
    assert first["observed_results"][0]["request_id"] == first["source_request"]["id"]
    bounded = memory.read_snapshot("demo", cluster["id"], root=root, task=task, max_chars=1024)
    assert bounded["omitted_units"] > 0
    assert bounded["payload"]["units"] == []
    assert bounded["budget_unit"] == "unicode_characters_not_tokens"


def test_frozen_read_survives_append_but_stale_append_requires_reconciliation(studio):
    root, store, _ = studio
    memory = MemoryClusters(store)
    cluster = memory.create("demo", "trajectory", "Independent choices", contract=CONTRACT)
    first, initial = source(studio, memory, cluster)
    memory.append_run(
        "demo", cluster["id"], first["run_id"], expected_state_hash=initial["state_hash"], root=root
    )
    second_task = new_task(root, store, "Second choice")
    second, read = source(studio, memory, cluster, second_task)
    third_task = new_task(root, store, "Third choice")
    third, stale_read = source(studio, memory, cluster, third_task)
    memory.append_run(
        "demo", cluster["id"], second["run_id"], expected_state_hash=read["state_hash"], root=root
    )
    assert memory.assert_snapshot("demo", read, root=root, task=second_task) == read
    with pytest.raises(ValueError, match="新增结果"):
        memory.append_run(
            "demo",
            cluster["id"],
            third["run_id"],
            expected_state_hash=stale_read["state_hash"],
            root=root,
        )


def test_only_selected_cluster_may_receive_a_run_and_revocation_blocks_old_reads(studio):
    root, store, _ = studio
    memory = MemoryClusters(store)
    selected = memory.create("demo", "trajectory", "Selected", contract=CONTRACT)
    other = memory.create("demo", "trajectory", "Not selected", contract=CONTRACT)
    result, initial = source(studio, memory, selected)
    with pytest.raises(ValueError, match="正式路由"):
        memory.append_run(
            "demo",
            other["id"],
            result["run_id"],
            expected_state_hash=initial["state_hash"],
            root=root,
        )
    memory.append_run(
        "demo",
        selected["id"],
        result["run_id"],
        expected_state_hash=initial["state_hash"],
        root=root,
    )
    task = new_task(root, store)
    read = memory.read_snapshot("demo", selected["id"], root=root, task=task)
    store.update_run(result["run_id"], cancel_requested=True)
    with pytest.raises(ValueError, match="撤回"):
        memory.assert_snapshot("demo", read, root=root, task=task)
    with pytest.raises(ValueError, match="撤回"):
        memory.assert_cluster_sources("demo", selected["id"], root=root, task=task)
    assert (
        memory.assert_cluster_sources("demo", other["id"], root=root, task=task)["completed_count"]
        == 0
    )
    assert (
        next(item for item in memory.catalog("demo") if item["id"] == other["id"])[
            "completed_count"
        ]
        == 0
    )


def test_skill_backend_does_not_promote_candidates_and_rebinds_approved_parameters(studio):
    root, store, _ = studio
    cards = SkillCards(store)
    card, _ = cards.add_candidate(
        "demo",
        {
            "name": "Current choices",
            "purpose": "Keep the current choice count",
            "steps": [
                {
                    "id": "choices",
                    "title": "Use current count",
                    "operation": "draft_quest",
                    "instruction": "Use {{count}} choices.",
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
        {"kind": "controlled_test"},
    )
    version = card["versions"][0]
    selection = {
        "card_id": card["id"],
        "version_id": version["id"],
        "version_hash": version["version_hash"],
    }
    memory = MemoryClusters(store)
    cluster = memory.create("demo", "skill", "Choice methods", contract=CONTRACT)
    result, initial = source(
        studio,
        memory,
        cluster,
        skill_card_selections=[selection],
        machine_requirements={"decision_option_count": 2},
    )
    memory.append_run(
        "demo",
        cluster["id"],
        result["run_id"],
        expected_state_hash=initial["state_hash"],
        root=root,
    )
    task = new_task(root, store)
    read = memory.read_snapshot(
        "demo",
        cluster["id"],
        root=root,
        task=task,
        machine_requirements={"decision_option_count": 3},
    )
    assert read["payload"]["skills"] == []
    assert read["omitted_units"] == 1
    assert cards.detail("demo", card["id"])["active_version_id"] is None
    card = cards.decide("demo", card["id"], card["card_hash"], version["id"], make_default=False)
    read = memory.read_snapshot(
        "demo",
        cluster["id"],
        root=root,
        task=task,
        machine_requirements={"decision_option_count": 3},
    )
    assert read["payload"]["skills"][0]["steps"][0]["instruction"] == "Use 3 choices."
    duplicate = memory.read_snapshot(
        "demo",
        cluster["id"],
        root=root,
        task=task,
        machine_requirements={"decision_option_count": 3},
        explicit_skill_selections=[selection],
    )
    assert duplicate["payload"]["skills"] == []
    assert duplicate["already_bound_skill_versions"] == [version["id"]]
    cards.enable("demo", card["id"], card["card_hash"], False)
    with pytest.raises(ValueError, match="Skill|选择"):
        memory.assert_snapshot("demo", read, root=root, task=task)


def test_snapshot_task_contract_and_hash_changes_are_rejected(studio):
    root, store, task = studio
    memory = MemoryClusters(store)
    cluster = memory.create("demo", "trajectory", "Contracted", contract=CONTRACT)
    read = memory.read_snapshot("demo", cluster["id"], root=root, task=task)
    with pytest.raises(ValueError, match="合同"):
        memory.assert_snapshot(
            "demo", read, root=root, task=task, expected_contract={**CONTRACT, "schema_version": 2}
        )
    with pytest.raises(ValueError, match="当前任务"):
        memory.assert_snapshot("demo", read, root=root, task=new_task(root, store))
    changed = {**read, "payload": {"backend": "trajectory", "units": ["invented"]}}
    with pytest.raises(ValueError, match="完整性"):
        memory.assert_snapshot("demo", changed, root=root, task=task)


ACTIVE_CONTRACT = {**CONTRACT, "adapter_protocol": "memory-routing-v3"}


def test_new_history_retains_real_first_node_bad_response_and_rejects_resealed_payload(
    studio, monkeypatch
):
    from owcopilot.workflow import memory_routing
    from owcopilot.workflow.routed_memory import read_memory_source
    from tests.domain.test_memory_history import ControlledCounter

    class BadFirstResponse:
        model = "controlled-bad-response"

        def complete(self, **kwargs):
            return "not a JSON object", 10, 5, 0, self.model

    root, store, task = studio
    memory = MemoryClusters(store)
    cluster = memory.create(
        "demo", "trajectory", "Actual format failures", contract=ACTIVE_CONTRACT
    )
    counter = ControlledCounter()
    snapshot = memory.read_snapshot(
        "demo", cluster["id"], root=root, task=task, max_tokens=12000, counter=counter
    )
    ended, _ = _execute(root, store, task, provider=BadFirstResponse())
    assert ended["result"]["outcomes"]["plan"]["status"] == "failed"
    with store.connection() as db:
        _, raw = read_memory_source(db, "demo", ended["run_id"])
    assert raw["current_observation_classification"] == "observed_failure"
    assert "not a JSON object" in json.dumps(raw["visible_messages"])
    assert "graph.node.failed" in json.dumps(raw["visible_messages"])
    # Isolate storage; the persisted-decision guard is separately asserted below.
    monkeypatch.setattr(memory_routing, "assert_memory_route", lambda *args, **kwargs: None)
    bind_receipt(store, ended["run_id"], snapshot)
    update = memory.append_run(
        "demo",
        cluster["id"],
        ended["run_id"],
        root=root,
        expected_state_hash=snapshot["state_hash"],
        counter=counter,
    )
    assert update["status"] == "appended"
    assert update["observation_kind"] == "observed_failure"
    next_task = new_task(root, store)
    read = memory.read_snapshot(
        "demo", cluster["id"], root=root, task=next_task, max_tokens=12000, counter=counter
    )
    assert read["payload"]["messages"]
    assert memory.assert_snapshot("demo", read, root=root, task=next_task, counter=counter) == read
    changed = json.loads(json.dumps(read))
    changed["payload"]["messages"][-1]["content"] = "made up successful feedback"
    changed["snapshot_hash"] = content_hash(
        {k: v for k, v in changed.items() if k != "snapshot_hash"}
    )
    with pytest.raises(ValueError, match="真实来源"):
        memory.assert_snapshot("demo", changed, root=root, task=next_task, counter=counter)


def test_new_append_requires_persisted_formal_route_and_does_not_treat_a_hash_as_authority(studio):
    from tests.domain.test_memory_history import ControlledCounter

    root, store, task = studio
    memory, counter = MemoryClusters(store), ControlledCounter()
    cluster = memory.create(
        "demo", "trajectory", "Formal decision required", contract=ACTIVE_CONTRACT
    )
    snapshot = memory.read_snapshot("demo", cluster["id"], root=root, task=task, counter=counter)
    ended, _ = _execute(root, store, task)
    bind_receipt(store, ended["run_id"], snapshot)
    with pytest.raises((ValueError, KeyError)):
        memory.append_run(
            "demo",
            cluster["id"],
            ended["run_id"],
            root=root,
            expected_state_hash=snapshot["state_hash"],
            counter=counter,
        )
    assert memory.catalog("demo")[0]["completed_count"] == 0


def test_new_pure_reuse_has_no_new_observation_and_does_not_add_member(studio, monkeypatch):
    from owcopilot.workflow import memory_routing
    from owcopilot.workflow.routed_memory import read_memory_source
    from tests.domain.test_memory_history import ControlledCounter

    root, store, task = studio
    first, _ = _execute(root, store, task)
    repeated, provider = _execute(root, store, task, prior_run_id=first["run_id"])
    assert provider.calls == []
    with store.connection() as db:
        _, source_record = read_memory_source(db, "demo", repeated["run_id"])
    assert source_record["current_observation_classification"] == "reused_only"
    assert len(source_record["source_chain"]) == 2
    memory, counter = MemoryClusters(store), ControlledCounter()
    cluster = memory.create("demo", "trajectory", "Reuse is not learning", contract=ACTIVE_CONTRACT)
    snapshot = memory.read_snapshot("demo", cluster["id"], root=root, task=task, counter=counter)
    bind_receipt(store, repeated["run_id"], snapshot)
    monkeypatch.setattr(memory_routing, "assert_memory_route", lambda *args, **kwargs: None)
    update = memory.append_run(
        "demo",
        cluster["id"],
        repeated["run_id"],
        root=root,
        expected_state_hash=snapshot["state_hash"],
        counter=counter,
    )
    assert update["status"] == "no_change"
    assert update["observation_kind"] == "reused_only"
    assert update["completed_count"] == 0


def test_observation_failure_categories_do_not_confuse_provider_return_with_valid_artifact():
    classify = MemoryClusters._observation_classification
    request = {"id": "call", "node_id": "plan"}
    source_record = {"requests": [request], "results": [], "result": {"outcomes": {}}}
    assert classify(source_record) == "outcome_unknown"
    source_record["results"] = [
        {
            "payload": {
                "request_id": "call",
                "outcome_known": True,
                "status": "failed",
                "error": "service rejected",
            }
        }
    ]
    assert classify(source_record) == "provider_failure"
    source_record["results"][0]["payload"]["outcome_known"] = False
    assert classify(source_record) == "outcome_unknown"
    source_record["results"] = [
        {"payload": {"request_id": "call", "status": "done", "response": {"text": "malformed"}}}
    ]
    source_record["result"]["outcomes"] = {"plan": {"status": "failed"}}
    assert classify(source_record) == "observed_failure"


def test_new_source_reconstructs_actual_repair_chain_and_refuses_cycle(studio):
    from owcopilot.workflow import repair_v2
    from owcopilot.workflow.routed_memory import read_memory_source
    from tests.platform.test_workflow_method_episode_v2 import VersionedNarrativeTransport
    from tests.platform.test_workflow_targeted_repair import broken_source

    root, store, broken, edit = broken_source(studio)
    diagnosis = repair_v2.diagnose_repair(store, "demo", broken["run_id"])
    prepared = repair_v2.prepare_repair_run(
        store,
        root,
        "demo",
        broken["run_id"],
        edit,
        expected_diagnosis_hash=diagnosis["diagnosis_hash"],
        command_id="source-chain-controlled",
    )
    repaired = repair_v2.execute_repair_run(
        store, root, "demo", prepared["repair_id"], provider=VersionedNarrativeTransport()
    )
    run_id = repaired["outcome"]["run_id"]
    with store.connection() as db:
        _, source_record = read_memory_source(db, "demo", run_id)
    assert [part["run_id"] for part in source_record["source_chain"]] == [broken["run_id"], run_id]
    assert source_record["source_chain"][-1]["predecessor"]["kind"] == "repair"
    assert {message["source_run_id"] for message in source_record["visible_messages"]} == {
        broken["run_id"],
        run_id,
    }
    assert "graph.node.failed" in json.dumps(source_record["visible_messages"])
    with store.connection() as db:
        row = db.execute(
            "SELECT payload FROM creative_v2_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        state = json.loads(row[0])
        state["prior_run_id"] = run_id
        db.execute(
            "UPDATE creative_v2_runs SET payload=? WHERE run_id=?", (json.dumps(state), run_id)
        )
    with store.connection() as db, pytest.raises(ValueError, match="循环"):
        read_memory_source(db, "demo", run_id)


def test_real_stage_supersession_is_not_mistaken_for_readable_unrevoked_history(studio):
    from owcopilot.workflow import v2_service
    from owcopilot.workflow.routed_memory import read_memory_source
    from tests.platform.test_workflow_v2_service import ControlledNarrativeProvider

    root, store, _ = studio
    created = create_task_action(
        root,
        "demo",
        TaskInput(brief="Pause before continuing a controlled scene", participation="direction"),
    )["task"]
    task = store.get_task(created["id"])
    waiting, provider = _execute(root, store, task, provider=ControlledNarrativeProvider())
    assert waiting["run"]["status"] == "waiting_user"
    gate = waiting["collaboration"]["pending_gate"]
    successor = v2_service.revise_stage_run(
        store,
        root,
        "demo",
        waiting["run_id"],
        expected_gate_hash=gate["gate_hash"],
        expected_policy_hash=waiting["collaboration"]["policy_hash"],
        collaboration_policy={"mode": "delegate"},
        guidance={},
        actor_kind="development_operator",
    )
    finished = v2_service.execute_v2_run(
        store, root, "demo", successor["run_id"], provider=provider
    )
    assert finished["run"]["status"] == "completed"
    assert store.get_run(waiting["run_id"]).cancel_requested is True
    # Cancellation still lacks an independent revocation identity; the source stays closed.
    with store.connection() as db, pytest.raises(ValueError, match="撤回"):
        read_memory_source(db, "demo", finished["run_id"])
