"""Skill lineage, actual supplied guidance and scoped cost export with controlled transports."""

import json
import sqlite3
from copy import deepcopy

import pytest

from owcopilot.workflow import v2_evidence
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.skill_cards import SkillCards
from owcopilot.workflow.skill_learning import learn_skill
from tests.platform.test_skill_cards_v2 import (
    ControlledSkillSummary,
    ControlledThreeChoiceProvider,
    _new_task,
    _selection,
)
from tests.platform.test_workflow_evidence_export_v2 import _case, _snapshot
from tests.platform.test_workflow_v2_service import _execute
from tests.platform.test_workflow_v2_service import studio as studio


@pytest.fixture
def learned_case(studio):
    root, store, task = studio
    source, _provider = _execute(
        root,
        store,
        task,
        provider=ControlledThreeChoiceProvider(),
        machine_requirements={"decision_option_count": 2},
    )
    assert source["run"]["status"] == "completed"
    learned = learn_skill(
        store,
        root,
        "demo",
        source["run_id"],
        operation_id="evidence-summary",
        provider=ControlledSkillSummary(),
    )
    assert learned["status"] == "candidate"
    card = SkillCards(store).detail("demo", learned["card_id"])
    return root, store, task, source, learned, card


def test_current_task_summary_is_paged_costed_and_exported_without_writes(
    learned_case,
    monkeypatch,
):
    root, store, task, source, learned, card = learned_case
    harness = HarnessStore(store)
    with store.connection() as db:
        for index in range(1003):
            harness._event(db, learned["summary_run_id"], "controlled_pagination", {"index": index})
        before = list(db.execute("SELECT name FROM sqlite_master ORDER BY name"))
    connect, writes = sqlite3.connect, []

    def read_only_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, _a, _b, _c, _d):
            if action in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
            }:
                writes.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(v2_evidence.sqlite3, "connect", read_only_connect)
        exported = v2_evidence.export_v2_evidence(store, root, "demo", source["run_id"])
    assert writes == []
    with store.connection() as db:
        assert list(db.execute("SELECT name FROM sqlite_master ORDER BY name")) == before
    skills = exported["procedural_skills"]
    assert skills["bindings"] == skills["consumption"] == []
    assert [row["id"] for row in skills["versions"]] == [learned["version_id"]]
    assert skills["cards"][0]["id"] == card["id"]
    summary = skills["summary_traces"][0]
    assert summary["run"]["id"] == learned["summary_run_id"] and len(summary["events"]) > 1000
    assert skills["summary_sources"][0]["cost_scope"] == "current_task_learning"
    assert skills["summary_sources"][0]["trace_included"] is True
    expected = {
        row["id"]
        for run_id in [source["run_id"], learned["summary_run_id"]]
        for row in harness.requests(run_id)
    }
    measured = exported["recording"]["measurements"]
    assert {row["request_id"] for row in measured["dispatches"]} == expected
    assert measured["dispatch_count"] == len(expected)
    assert exported["recording"]["current_task_measurements"]["dispatch_count"] == len(expected)
    receipt_id = card["versions"][0]["source"]["summary_execution_receipt_id"]
    assert receipt_id in {row["id"] for row in exported["recording"]["budget"]["attempt_receipts"]}
    assert skills["author_quality"] == skills["transfer_benefit"] == "unknown"
    assert exported["manifest"]["training_export"]["eligible"] is False
    assert exported["manifest"]["schema_versions"]["export"] == 4


def test_selected_cross_task_skill_exports_consumption_without_source_cost_or_unrelated_versions(
    learned_case,
):
    root, store, _task, _source, learned, card = learned_case
    next_task = _new_task(root, store, "Controlled follow-up scene")
    target, _provider = _execute(
        root,
        store,
        next_task,
        provider=ControlledThreeChoiceProvider(),
        machine_requirements={"decision_option_count": 3},
        skill_card_selections=[_selection(card)],
        apply_project_skills=False,
    )
    assert target["run"]["status"] == "completed"
    cards = SkillCards(store)
    definition = deepcopy(card["versions"][0]["definition"])
    definition["steps"][0]["instruction"] += " private-future-instruction"
    future, _created = cards.add_candidate(
        "demo",
        definition,
        {"kind": "import"},
        card_id=card["id"],
        expected_card_hash=card["card_hash"],
        parent_version_id=learned["version_id"],
    )
    unrelated = deepcopy(definition)
    unrelated["name"] = "private-unrelated-card"
    cards.add_candidate("demo", unrelated, {"kind": "import"})
    exported = v2_evidence.export_v2_evidence(store, root, "demo", target["run_id"])
    skills = exported["procedural_skills"]
    assert len(skills["bindings"]) == 1 and skills["binding_is_consumption"] is False
    assert [row["id"] for row in skills["versions"]] == [learned["version_id"]]
    assert {event["payload"]["node_id"] for event in skills["consumption"]} == {"quest"}
    assert skills["summary_traces"] == []
    assert skills["summary_sources"][0]["cost_scope"] == "cross_task_provenance_only"
    assert skills["summary_sources"][0]["trace_included"] is False
    assert skills["head_version_scope"][0]["current_version_included"] is False
    assert skills["cards"][0]["current_version_id"] == future["current_version_id"]
    encoded = json.dumps(exported)
    assert "private-future-instruction" not in encoded and "private-unrelated-card" not in encoded
    current = exported["recording"]["current_task_measurements"]
    assert current["dispatch_count"] == len(HarnessStore(store).requests(target["run_id"]))
    assert current["excluded_cross_task_skill_source_traces"] == [learned["summary_run_id"]]
    exported_runs = {row["run_id"] for row in exported["manifest"]["run_ranges"]}
    assert learned["summary_run_id"] not in exported_runs


def _definition():
    return {
        "name": "Check current requirements",
        "purpose": "Use the current declared requirements",
        "steps": [
            {
                "id": "check",
                "title": "Check",
                "operation": "draft_quest",
                "instruction": "Use the current declared requirements.",
            }
        ],
        "applies_to": ["A task with declared requirements"],
    }


def test_failed_and_no_change_summaries_without_cards_are_not_dropped_from_cost(studio):
    root, store, task = studio
    source, _provider = _execute(
        root,
        store,
        task,
        provider=ControlledThreeChoiceProvider(),
        machine_requirements={"decision_option_count": 2},
    )

    class InvalidRole(ControlledSkillSummary):
        def complete(self, **kwargs):
            text, *usage = super().complete(**kwargs)
            value = json.loads(text)
            value["steps"][0]["method"]["operation"] = "validate_decisions"
            return json.dumps(value), *usage

    with pytest.raises(ValueError):
        learn_skill(
            store,
            root,
            "demo",
            source["run_id"],
            operation_id="invalid-summary",
            provider=InvalidRole(),
        )

    class NoChange(ControlledSkillSummary):
        def complete(self, **kwargs):
            text, *usage = super().complete(**kwargs)
            value = json.loads(text)
            value.update(should_save=False, steps=[], applies_to=[])
            return json.dumps(value), *usage

    unchanged = learn_skill(
        store,
        root,
        "demo",
        source["run_id"],
        operation_id="no-change-summary",
        feedback="Check whether a portable change exists",
        provider=NoChange(),
    )
    assert unchanged["status"] == "no_change"
    assert SkillCards(store).list("demo") == []
    exported = v2_evidence.export_v2_evidence(store, root, "demo", source["run_id"])
    skills = exported["procedural_skills"]
    assert skills["cards"] == skills["versions"] == []
    assert len(skills["summary_attempts"]) == len(skills["summary_traces"]) == 2
    assert {row["summary_status"] for row in skills["summary_attempts"]} == {"failed", "completed"}
    assert all(not row["candidate_version_ids"] for row in skills["summary_attempts"])
    actual_summary_requests = {
        request["id"] for trace in skills["summary_traces"] for request in trace["requests"]
    }
    assert len(actual_summary_requests) == 2
    measurements = exported["recording"]["current_task_measurements"]
    assert actual_summary_requests <= {row["request_id"] for row in measurements["dispatches"]}
    summary_dispatches = [
        row for row in measurements["dispatches"] if row["request_id"] in actual_summary_requests
    ]
    assert sum(row["measurement"]["input_tokens"] for row in summary_dispatches) == 34
    assert sum(row["measurement"]["output_tokens"] for row in summary_dispatches) == 18
    receipts = {row["id"] for row in exported["recording"]["budget"]["attempt_receipts"]}
    assert all(
        row["summary_execution_receipt_id"] in receipts for row in skills["summary_attempts"]
    )


def test_missing_summary_and_tampered_version_remain_explicit(tmp_path):
    store, task, run, _harness = _case(tmp_path)
    _snapshot(store, task, run)
    card, _created = SkillCards(store).add_candidate(
        task.project,
        _definition(),
        {
            "kind": "run",
            "run_id": run.id,
            "task_id": task.id,
            "summary_run_id": "missing-summary",
        },
    )
    exported = v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)
    skills = exported["procedural_skills"]
    assert skills["summary_sources"][0]["trace_included"] is False
    missing = {"section": "trace", "id": "missing-summary", "reason": "linked_run_missing"}
    assert missing in exported["manifest"]["missing_records"]
    assert exported["manifest"]["retrieval_complete"] is False
    assert exported["recording"]["current_task_measurements"]["dispatch_count"] == 0
    assert exported["recording"]["current_task_measurements"]["cost_status"] != "priced"
    with store.connection() as db:
        row = db.execute(
            "SELECT payload FROM skill_card_versions WHERE card_id=?", (card["id"],)
        ).fetchone()
        version = json.loads(row[0])
        version["definition"]["purpose"] = "Changed without a matching hash"
        db.execute(
            "UPDATE skill_card_versions SET payload=? WHERE id=?",
            (json.dumps(version), version["id"]),
        )
    with pytest.raises(ValueError, match="校验失败"):
        v2_evidence.export_v2_evidence(store, tmp_path, task.project, run.id)
