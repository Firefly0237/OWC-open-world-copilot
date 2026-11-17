"""Played-state continuity and refusal of semantic rewrites; development fixtures only."""

import copy
import json

import pytest

from tests.domain import test_workflow_project_v2 as project_fixtures
from tests.domain.test_workflow_project_v2 import (
    anchor,
    artifact,
    outline,
    source,
    token,  # noqa: F401
)

store = project_fixtures.store


def record_actual_generation_context(store, project, value, scene_id, **kwargs):
    context = store.generation_context("demo", project["id"], scene_id, **kwargs)
    with store.workflow.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS creative_v2_runs("
            "run_id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO creative_v2_runs VALUES(?,?,?)",
            (
                value.run_id,
                "demo",
                json.dumps(
                    {
                        "narrative_project_id": project["id"],
                        "narrative_scene_id": scene_id,
                        "narrative_context": context,
                    }
                ),
            ),
        )
    return context


def test_generation_context_cannot_be_relabelled_or_used_from_other_state(store):
    project = store.create("demo", "Frozen production input")
    store.save_outline(
        "demo", project["id"], outline(), expected_project_hash=token(store, project)
    )
    value = artifact(store, "frozen")
    record_actual_generation_context(store, project, value, "start")
    source(store, project, "Unrelated new material")
    with pytest.raises(ValueError, match="重新标记"):
        store.bind_scene_artifact(
            "demo",
            project["id"],
            "start",
            value.id,
            expected_project_hash=token(store, project),
            expected_artifact_hash=value.content_hash,
        )
    fresh = artifact(store, "fresh")
    record_actual_generation_context(store, project, fresh, "start")
    store.bind_scene_artifact(
        "demo",
        project["id"],
        "start",
        fresh.id,
        expected_project_hash=token(store, project),
        expected_artifact_hash=fresh.content_hash,
    )
    with pytest.raises(ValueError, match="另一条路径"):
        store.create_preview(
            "demo",
            project["id"],
            expected_project_hash=token(store, project),
            initial_state={"trust": 4},
        )
    assert (
        store.create_preview("demo", project["id"], expected_project_hash=token(store, project))[
            "state"
        ]["trust"]
        == 0
    )


def setup_preview(store):
    project = store.create("demo", "Snapshot development case")
    store.save_outline(
        "demo", project["id"], outline(), expected_project_hash=token(store, project)
    )
    value = artifact(store, "start")
    store.bind_scene_artifact(
        "demo",
        project["id"],
        "start",
        value.id,
        expected_project_hash=token(store, project),
        expected_artifact_hash=value.content_hash,
    )
    preview = store.create_preview(
        "demo", project["id"], expected_project_hash=token(store, project)
    )
    played = store.step_preview(
        "demo", project["id"], preview["preview_id"], action_id="do", expected_seq=0, choice_id="do"
    )
    return project, played


def bind_scene(store, project, name, *, delta=0):
    value = artifact(store, name, delta=delta)
    store.bind_scene_artifact(
        "demo",
        project["id"],
        name,
        value.id,
        expected_project_hash=token(store, project),
        expected_artifact_hash=value.content_hash,
    )


def rebase(store, project, preview):
    return store.rebase_preview(
        "demo",
        project["id"],
        preview["preview_id"],
        expected_project_hash=token(store, project),
        expected_preview_hash=preview["preview_hash"],
    )


def test_prospective_bind_rebase_archive_preserve_path_and_original(store):
    project, played = setup_preview(store)
    saved = store.save_archive(
        "demo", project["id"], played["preview_id"], name="before B", expected_seq=1
    )
    future = store.generation_context(
        "demo", project["id"], "left", preview_id=played["preview_id"], transition_id="left"
    )
    assert future["path_state"]["trust"] == 2
    assert future["generation_binding"]["prospective"] is True
    bind_scene(store, project, "left", delta=1)
    historical = store.get_preview("demo", project["id"], played["preview_id"])
    assert historical["snapshot_status"] == "historical_requires_rebase"
    assert historical["preview_hash"] == played["preview_hash"]
    fork = rebase(store, project, played)
    assert fork["preview_id"] != played["preview_id"]
    assert fork["state"] == played["state"] and fork["action_seq"] == 1
    assert rebase(store, project, played)["preview_id"] == fork["preview_id"]
    continued = store.step_preview(
        "demo",
        project["id"],
        fork["preview_id"],
        action_id="to B",
        expected_seq=1,
        transition_id="left",
    )
    assert continued["scene_id"] == "left" and continued["state"]["trust"] == 2
    result = store.step_preview(
        "demo",
        project["id"],
        fork["preview_id"],
        action_id="B done",
        expected_seq=2,
        choice_id="do",
    )
    assert result["state"]["trust"] == 3
    restored = store.restore_archive(
        "demo",
        project["id"],
        saved["id"],
        expected_project_hash=token(store, project),
        operation_id="restore once",
    )
    assert restored["state"] == played["state"] and restored["branch_history"] == []
    assert restored["snapshot_migration"]["source_ref"]["kind"] == "archive"
    assert store.archives("demo", project["id"])[0] == saved
    assert len(store.export_project("demo", project["id"])["snapshot_migrations"]) == 2


@pytest.mark.parametrize("change", ["visited_binding", "outgoing", "variables"])
def test_rebase_rejects_changed_played_contract(store, change):
    project, played = setup_preview(store)
    if change == "visited_binding":
        bind_scene(store, project, "start", delta=7)
    else:
        revised = copy.deepcopy(store.get("demo", project["id"])["outline"])
        if change == "outgoing":
            revised["branches"][0]["effects"] = [{"var": "trust", "op": "inc", "value": 8}]
        else:
            revised["variables"][0]["default"] = 5
        store.save_outline(
            "demo", project["id"], revised, expected_project_hash=token(store, project)
        )
    with pytest.raises(ValueError, match="改变|发生变化|失效"):
        rebase(store, project, played)


def test_action_receipt_same_parameters_survives_project_change_without_reexecution(store):
    project, played = setup_preview(store)
    bind_scene(store, project, "left")
    replay = store.step_preview(
        "demo", project["id"], played["preview_id"], action_id="do", expected_seq=0, choice_id="do"
    )
    assert replay["idempotent_replay"] is True
    assert {key: value for key, value in replay.items() if key != "idempotent_replay"} == played
    with pytest.raises(ValueError, match="不同参数"):
        store.step_preview(
            "demo",
            project["id"],
            played["preview_id"],
            action_id="do",
            expected_seq=1,
            choice_id="do",
        )


def test_unrelated_future_source_edit_safe_but_played_source_revocation_rejected(store):
    project, played = setup_preview(store)
    future_source = source(store, project, "Future scene draft")
    revised = copy.deepcopy(store.get("demo", project["id"])["outline"])
    revised["scenes"][1]["sources"] = [anchor(future_source)]
    store.save_outline("demo", project["id"], revised, expected_project_hash=token(store, project))
    fork = rebase(store, project, played)
    assert fork["state"] == played["state"]
    # Replacing an unvisited source does not invalidate A, but B remains stale
    # until explicitly rebound. Revoking A's adopted factual basis is different.
    basis = source(store, project, "Accepted A basis")
    claim = store.record_claim(
        "demo",
        project["id"],
        {
            "subject": "A",
            "predicate": "basis",
            "value": "known",
            "anchors": [anchor(basis)],
            "scope": {"scene_ids": ["start"]},
            "interpretation": "deterministic_extraction",
        },
        expected_project_hash=token(store, project),
    )
    store.decide_claim(
        "demo",
        project["id"],
        claim["claim"]["id"],
        accept=True,
        expected_project_hash=token(store, project),
    )
    with pytest.raises(ValueError, match="失效"):
        rebase(store, project, fork)
