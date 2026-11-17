"""Complete read-only evidence retrieval; no provider or human evaluation is simulated."""

from __future__ import annotations

import json
import sqlite3

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow import v2_evidence
from owcopilot.workflow.evolution import EvolutionStore, SourceRef
from owcopilot.workflow.execution_records import DecisionReceipt, ExecutionRecords, SourceAxes
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import BudgetOwner, HarnessStore
from owcopilot.workflow.models import RunRecord, TaskInput, TaskRecord
from owcopilot.workflow.store import WorkflowStore


def _case(tmp_path):
    store = WorkflowStore(tmp_path / "records.sqlite")
    task = TaskRecord(
        project="local",
        request=TaskInput(brief="A short scene"),
        base_version="v1",
        base_hash="0" * 64,
        base_bundle={},
    )
    store.save_task(task)
    run, _ = store.create_run(task)
    return store, task, run, HarnessStore(store)


def _another_run(store, task):
    run = RunRecord(task_id=task.id, revision=task.revision)
    with store.connection() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?)", (run.id, task.id, run.model_dump_json()))
    return run


def _snapshot(store, task, run, *, planning_id=None, version_id=None):
    graph = create_narrative_graph().model_dump(mode="json")
    if planning_id:
        graph["nodes"][0]["parameters"]["initial_planning"] = {"run_id": planning_id}
    state = {
        "run_id": run.id,
        "project": task.project,
        "task_id": task.id,
        "revision": run.revision,
        "graph": graph,
        "graph_hash": content_hash(graph),
        "references": [],
        "skill_version_id": version_id,
        "asset_binding": {"source_version_id": version_id, "bound_nodes": ["plan"]},
    }
    with store.connection() as db:
        db.execute(
            "CREATE TABLE creative_v2_runs(run_id TEXT PRIMARY KEY,project TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO creative_v2_runs VALUES(?,?,?)", (run.id, task.project, json.dumps(state))
        )


def _burst(store, harness, run_id, *, episode_id=None):
    with store.connection() as db:
        for index in range(1003):
            harness._event(db, run_id, "observation", {"index": index})
        if episode_id:
            harness._event(db, run_id, "evolution/completed", {"episode_id": episode_id})


def test_pages_all_linked_trace_types_and_detects_late_episode(tmp_path):
    store, task, run, harness = _case(tmp_path)
    planner = _another_run(store, task)
    proposal = _another_run(store, task)
    evolution_run = _another_run(store, task)
    skills = EvolutionStore(store)
    family = skills.create_family(task.project, "scene method")
    version = skills.add_version(
        task.project,
        family["id"],
        graph=create_narrative_graph().model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
    )
    experience = skills.record_experience(
        task.project,
        family["id"],
        kind="technical_failure",
        source_ref=SourceRef(kind="run", id=run.id, content_hash=content_hash(run)),
        payload={"reason": "declared state mismatch"},
        problem_key="state",
    )
    episode = skills.begin_episode(
        task.project, family["id"], experience["id"], "state", parent_version_id=version["id"]
    )
    _snapshot(store, task, run, planning_id=planner.id, version_id=version["id"])
    for item in (run, planner, proposal):
        _burst(store, harness, item.id)
    _burst(store, harness, evolution_run.id, episode_id=episode["id"])
    with store.connection() as db:
        db.execute(
            "CREATE TABLE creative_intent_ai_proposals(proposal_key TEXT,project TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO creative_intent_ai_proposals VALUES(?,?,?)",
            ("p", task.project, json.dumps({"task_id": task.id, "trace_run_id": proposal.id})),
        )
    result = v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)
    assert len(result["harness"]["events"]) == 1003
    assert len(result["harness"]["initial_planning"][0]["events"]) == 1003
    assert len(result["intent"]["proposal_traces"][0]["events"]) == 1003
    assert len(result["skill"]["evolution_traces"][0]["events"]) == 1004
    assert result["manifest"]["counts"]["events"] == 4013
    assert result["manifest"]["truncated"] is False
    assert result["execution"]["asset_binding"]["source_version_id"] == version["id"]
    assert result["harness"]["replay_model_dispatches"] == 0
    assert result["export_hash"] == content_hash(
        {k: v for k, v in result.items() if k != "export_hash"}
    )


def test_missing_links_usage_and_sources_remain_visible_without_writes(tmp_path, monkeypatch):
    store, task, run, harness = _case(tmp_path)
    _snapshot(store, task, run, planning_id="missing-planner")
    known = harness.claim(
        BudgetOwner(project=task.project, task_id=task.id, product_key="draft", problem_key="one"),
        run.id,
        "draft",
        {"role_id": "draft", "config": {"execution_origin": "controlled_transport"}},
        {},
    )
    harness.finish(
        known,
        {
            "request_id": known["id"],
            "status": "completed",
            "measurement": {
                "input_tokens": 12,
                "output_tokens": 3,
                "cached_input_tokens": 4,
            },
        },
        "draft",
        settlement="succeeded",
    )
    unknown = harness.claim(
        BudgetOwner(project=task.project, task_id=task.id, product_key="check", problem_key="two"),
        run.id,
        "check",
        {"role_id": "check", "config": {}},
        {},
    )
    with store.connection() as db:
        before = list(db.execute("SELECT name FROM sqlite_master ORDER BY name"))
    connect = sqlite3.connect
    changes = []

    def guarded_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, _a, _b, _c, _d):
            if action in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
            }:
                changes.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(v2_evidence.sqlite3, "connect", guarded_connect)
    result = v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)
    assert changes == []
    assert result["manifest"]["read_consistency"] == "single_sqlite_read_transaction"
    assert {"section": "trace", "id": "missing-planner", "reason": "linked_run_missing"} in result[
        "manifest"
    ]["missing_records"]
    assert "creative_method_decisions" in result["manifest"]["missing_tables"]
    totals = result["recording"]["measurements"]
    assert totals["known_totals"]["input_tokens"] == 12
    assert totals["total_tokens"] is None
    assert totals["unknown_usage_request_ids"] == [unknown["id"]]
    assert totals["cost_usd"] is None
    assert result["manifest"]["training_export"]["exported_samples"] == 0
    assert result["manifest"]["source_exclusions"][0]["execution_origin"] == "controlled_transport"
    monkeypatch.setattr(v2_evidence.sqlite3, "connect", connect)
    with store.connection() as db:
        assert list(db.execute("SELECT name FROM sqlite_master ORDER BY name")) == before


@pytest.mark.parametrize("target", ["missing", "foreign"])
def test_root_run_is_hidden_before_reading_trace_for_missing_or_other_project(
    tmp_path,
    monkeypatch,
    target,
):
    store, task, _run, _harness = _case(tmp_path)
    outsider = task.model_copy(update={"id": "outsider", "project": "other"})
    store.save_task(outsider)
    foreign = _another_run(store, outsider)

    def do_not_read(*_args, **_kwargs):
        raise AssertionError("Root object admission must happen before reading its trace")

    monkeypatch.setattr(v2_evidence, "_read_trace", do_not_read)
    with pytest.raises(KeyError, match="运行不存在"):
        v2_evidence.export_v2_evidence(
            store, tmp_path, task.project, foreign.id if target == "foreign" else "missing-run"
        )


def test_cross_project_link_and_tampered_event_fail_closed(tmp_path):
    store, task, run, harness = _case(tmp_path)
    outsider = task.model_copy(update={"id": "outsider", "project": "other"})
    store.save_task(outsider)
    foreign = _another_run(store, outsider)
    _snapshot(store, task, run, planning_id=foreign.id)
    with pytest.raises(ValueError, match="当前项目"):
        v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)
    with store.connection() as db:
        state = json.loads(db.execute("SELECT payload FROM creative_v2_runs").fetchone()[0])
        state["graph"]["nodes"][0]["parameters"] = {}
        state["graph_hash"] = content_hash(state["graph"])
        db.execute("UPDATE creative_v2_runs SET payload=?", (json.dumps(state),))
        harness._event(db, run.id, "observation", {"value": 1})
        row = db.execute("SELECT seq,payload FROM creative_harness_events").fetchone()
        event = json.loads(row[1])
        event["payload"]["value"] = 2
        db.execute(
            "UPDATE creative_harness_events SET payload=? WHERE seq=?", (json.dumps(event), row[0])
        )
    with pytest.raises(ValueError, match="校验失败"):
        v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)


def test_decision_links_and_concurrent_append_share_one_read_snapshot(tmp_path, monkeypatch):
    store, task, run, harness = _case(tmp_path)
    _snapshot(store, task, run)
    ledger = ExecutionRecords(store)
    receipt = ledger.append_decision(
        DecisionReceipt(
            project=task.project,
            task_id=task.id,
            task_revision=task.revision,
            input_hash=content_hash(task.request),
            policy_id="rules",
            policy_version="1",
            observation={},
            candidates=[{"id": "fresh"}],
            selected_action_id="fresh",
            reason="No eligible asset",
            budget={},
            source=SourceAxes(
                task_origin="development",
                execution_origin="no_model",
                actor_origin="developer",
                collection_condition="synthetic_protocol",
            ),
        )
    )
    ledger.bind_run(task.project, receipt["id"], run.id)
    harness.append(run.id, "observation", {"value": "before"})
    original = v2_evidence._pages
    wrote = False

    def append_after_read_started(db, sql, parameters=()):
        nonlocal wrote
        for row in original(db, sql, parameters):
            if "SELECT seq,payload FROM creative_harness_events" in sql and not wrote:
                wrote = True
                harness.append(run.id, "observation", {"value": "after"})
            yield row

    monkeypatch.setattr(v2_evidence, "_pages", append_after_read_started)
    result = v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)
    assert wrote
    assert len(result["harness"]["events"]) == 1
    assert len(harness.events(run.id)) == 2
    assert result["recording"]["decisions"][0]["run_ids"] == [run.id]
    assert result["manifest"]["policy_assignments"] == [
        {
            "decision_id": receipt["id"],
            "policy_id": "rules",
            "policy_version": "1",
        }
    ]
    assert result["manifest"]["training_export"]["eligible"] is False
