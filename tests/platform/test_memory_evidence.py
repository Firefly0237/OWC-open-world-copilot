"""Read-only memory attestation using actual product paths and controlled transports."""

import json
from copy import deepcopy

import pytest

from owcopilot.app.actions.workflow import create_task_action
from owcopilot.content.hash import content_hash
from owcopilot.workflow import memory_history, routed_memory, v2_service
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.memory_routing import route_task_memory
from owcopilot.workflow.models import TaskInput
from owcopilot.workflow.routed_memory import MemoryClusters
from owcopilot.workflow.v2_evidence import _rebuild_memory_read, export_v2_evidence
from tests.platform.test_memory_lessons import ControlledLessonEncoder, ControlledLessonProvider
from tests.platform.test_memory_lessons import strategy_case as strategy_case
from tests.platform.test_routed_memory_integration import router_for_stream
from tests.platform.test_workflow_v2_service import _execute
from tests.platform.test_workflow_v2_service import studio as studio


@pytest.fixture(autouse=True)
def controlled_token_units(monkeypatch):
    class Counter:
        identity = {"kind": "controlled_test_units", "version": 1}

        def count(self, text):
            return (len(text) + 3) // 4

    counter = Counter()
    monkeypatch.setattr(memory_history, "get_memory_token_counter", lambda: counter)
    monkeypatch.setattr(routed_memory, "get_memory_token_counter", lambda: counter)


def _route(studio):
    root, store, task = studio
    route = route_task_memory(store, root, task, mode="routed", provider=router_for_stream())
    state, _ = _execute(root, store, task, memory_route=route)
    assert state["run"]["status"] == "completed", state
    return root, store, task, state


@pytest.fixture
def reused(studio):
    root, store, _, first = _route(studio)
    first = v2_service.finalize_memory_run(store, root, "demo", first)
    assert first["memory_update"]["status"] == "recorded", first
    task = store.get_task(
        create_task_action(root, "demo", TaskInput(brief="A second controlled choice scene"))[
            "task"
        ]["id"]
    )
    route = route_task_memory(store, root, task, mode="routed", provider=router_for_stream())
    state, _ = _execute(root, store, task, memory_route=route)
    assert state["run"]["status"] == "completed", state
    assert route["read_snapshot"]["payload"]["messages"]
    return root, store, task, first, state


def _write_state(store, state):
    with store.connection() as db:
        db.execute(
            "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
            (json.dumps(state, ensure_ascii=False), state["run_id"]),
        )


def test_exact_provider_payload_and_response_attestation_is_read_only_and_cost_is_unique(
    reused, monkeypatch
):
    root, store, _, first, state = reused
    # Moving the head forward must not invalidate the earlier frozen read.
    result = v2_service.finalize_memory_run(store, root, "demo", state)
    assert result["memory_update"]["state_version"] == 2
    monkeypatch.setattr(
        MemoryClusters, "__init__", lambda *args: pytest.fail("export must not initialize storage")
    )
    before = store.path.read_bytes()
    evidence = export_v2_evidence(store, root, "demo", state["run_id"])
    assert store.path.read_bytes() == before
    memory = evidence["routed_memory"]
    assert memory["status"] == "provided_with_response"
    assert memory["frozen_state"]["version"] == 1
    assert memory["provided_source_run_ids"] == [first["run_id"]]
    assert len(memory["production_dispatches"]) == 4
    assert all(row["response_received"] for row in memory["production_dispatches"])
    assert memory["semantic_compliance"] == "not_established"
    assert evidence["recording"]["current_task_measurements"]["dispatch_count"] == 5
    assert evidence["recording"]["measurements"]["dispatch_count"] == 5


def test_cold_start_is_empty_not_claimed_as_reused_experience(studio):
    root, store, _, state = _route(studio)
    memory = export_v2_evidence(store, root, "demo", state["run_id"])["routed_memory"]
    assert memory["status"] == "empty"
    assert memory["provided_source_run_ids"] == []
    assert len(memory["production_dispatches"]) == 4
    assert all(row["status"] == "empty" for row in memory["production_dispatches"])


def test_legacy_without_route_remains_compatible(studio):
    root, store, task = studio
    state, _ = _execute(root, store, task)
    memory = export_v2_evidence(store, root, "demo", state["run_id"])["routed_memory"]
    assert memory == {"schema_version": 1, "mode": "existing", "status": "not_enabled"}


def test_assigned_but_unstarted_production_is_not_dispatched(studio):
    root, store, task = studio
    route = route_task_memory(store, root, task, mode="routed", provider=router_for_stream())
    prepared = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph("constrained").model_dump(mode="json"),
        memory_route=route,
    )
    memory = export_v2_evidence(store, root, "demo", prepared["run_id"])["routed_memory"]
    assert memory["status"] == "not_dispatched"
    assert memory["production_dispatches"] == []


@pytest.mark.parametrize("change", ["receipt", "snapshot", "decision", "state", "route_removed"])
def test_missing_or_changed_frozen_identity_is_rejected(studio, change):
    root, store, _, state = _route(studio)
    broken = deepcopy(state)
    route = broken["memory_route"]
    if change == "receipt":
        route["reason"] = "Unrecorded replacement"
    elif change == "snapshot":
        route["read_snapshot"]["payload"]["units"] = [{"source_run_id": "invented"}]
        route["receipt_hash"] = content_hash(
            {key: item for key, item in route.items() if key != "receipt_hash"}
        )
    elif change == "decision":
        route["reason"] = "A re-sealed but unrecorded replacement"
        route["receipt_hash"] = content_hash(
            {key: item for key, item in route.items() if key != "receipt_hash"}
        )
    elif change == "state":
        with store.connection() as db:
            db.execute(
                "DELETE FROM creative_memory_states WHERE cluster_id=? AND version=0",
                (route["cluster_id"],),
            )
    else:
        broken.pop("memory_route")
    _write_state(store, broken)
    with pytest.raises(ValueError, match="经历"):
        export_v2_evidence(store, root, "demo", state["run_id"])


def test_resealed_actual_request_cannot_claim_supplied_memory(reused):
    root, store, _, _, state = reused
    with store.connection() as db:
        row = db.execute(
            "SELECT id,request FROM creative_harness_dispatches "
            "WHERE run_id=? ORDER BY rowid LIMIT 1",
            (state["run_id"],),
        ).fetchone()
        record = json.loads(row[1])
        user = json.loads(record["request"]["user"])
        user["past_experience"] = {"backend": "trajectory", "units": []}
        record["request"]["user"] = json.dumps(user)
        record["request_hash"] = content_hash(record["request"])
        db.execute(
            "UPDATE creative_harness_dispatches SET request=? WHERE id=?",
            (json.dumps(record), row[0]),
        )
    with pytest.raises(ValueError, match="实际制作请求"):
        export_v2_evidence(store, root, "demo", state["run_id"])


def test_message_body_order_lineage_and_token_count_must_rebuild_from_the_source(reused):
    _, store, task, _, state = reused
    route = state["memory_route"]
    with store.connection() as db:
        cluster = json.loads(
            db.execute(
                "SELECT payload FROM creative_memory_clusters WHERE id=?", (route["cluster_id"],)
            ).fetchone()[0]
        )
        members = []
        for raw in db.execute(
            "SELECT payload FROM creative_memory_members WHERE cluster_id=?", (route["cluster_id"],)
        ):
            member = json.loads(raw[0])
            members.append((member, None, member["source"]))
    for change in ("body", "order", "lineage", "budget"):
        broken = deepcopy(route["read_snapshot"])
        if change == "body":
            broken["payload"]["messages"][-1]["content"] = "An invented successful check"
        elif change == "order":
            broken["payload"]["messages"].reverse()
        elif change == "lineage":
            broken["lineage"]["provided_message_refs"] = []
        else:
            broken["token_budget"]["token_count"] += 1
        with pytest.raises(ValueError, match="冻结经历"):
            _rebuild_memory_read(broken, cluster, members, task.model_dump(mode="json"))


def test_strategy_formation_cost_and_actual_read_are_audited_without_an_encoder_call(
    strategy_case, monkeypatch
):
    root, store, _, first, _ = strategy_case
    provider, encoder = ControlledLessonProvider(), ControlledLessonEncoder()
    route = first["memory_route"]
    MemoryClusters(store).append_run(
        "demo",
        route["cluster_id"],
        first["run_id"],
        root=root,
        expected_state_hash=route["state_hash"],
        lesson_provider=provider,
        encoder=encoder,
    )
    from owcopilot.workflow import memory_lessons

    monkeypatch.setattr(memory_lessons, "get_lesson_encoder", lambda: encoder)
    task = store.get_task(
        create_task_action(root, "demo", TaskInput(brief="A new independent constrained scene"))[
            "task"
        ]["id"]
    )
    route = route_task_memory(store, root, task, mode="routed", provider=router_for_stream())
    second, _ = _execute(root, store, task, memory_route=route)
    calls = len(encoder.calls)
    monkeypatch.setattr(
        MemoryClusters,
        "__init__",
        lambda *args: pytest.fail("read-only export cannot initialize memory storage"),
    )
    before = store.path.read_bytes()
    evidence = export_v2_evidence(store, root, "demo", first["run_id"])
    later = export_v2_evidence(store, root, "demo", second["run_id"])
    assert store.path.read_bytes() == before
    assert len(encoder.calls) == calls
    assert evidence["recording"]["current_task_measurements"]["dispatch_count"] == 7
    assert len(evidence["routed_memory"]["formations"]) == 1
    assert (
        evidence["routed_memory"]["formations"][0]["model_proxy"]["assessment_kind"]
        == "model_proxy"
    )
    assert evidence["routed_memory"]["author_acceptance"] == "unknown"
    assert later["recording"]["current_task_measurements"]["dispatch_count"] == 5
    assert later["routed_memory"]["provided_source_run_ids"] == [first["run_id"]]
    assert len(later["routed_memory"]["source_formations"]) == 1
