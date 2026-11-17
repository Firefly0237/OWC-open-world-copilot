"""Author-facing usage views must distinguish intent, dispatch and response."""

import json

import pytest
from fastapi.testclient import TestClient

from owcopilot.content.hash import content_hash
from owcopilot.service.api import create_app
from owcopilot.workflow import v2_service
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.skill_usage import run_skill_usage
from tests.platform.test_skill_cards_v2 import PREFIX, ControlledThreeChoiceProvider, _selection
from tests.platform.test_skill_cards_v2 import studio as studio
from tests.platform.test_skill_summary_schedule import card_case


def prepared(studio):
    root, store, task = studio
    card = card_case(store)
    state = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph("constrained").model_dump(mode="json"),
        machine_requirements={"decision_option_count": 2},
        skill_card_selections=[_selection(card)],
        apply_project_skills=False,
    )
    return root, store, state


def test_read_only_usage_tracks_actual_bound_dispatched_and_response(studio):
    root, store, state = prepared(studio)
    with store.connection() as db:
        before = list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
    initial = run_skill_usage(store, "demo", state["run_id"])
    assert initial["skills"][0]["steps"][0]["status"] == "bound"
    assert initial["measurements"]["dispatch_count"] == 0
    seen = []

    class ObservedProvider(ControlledThreeChoiceProvider):
        def complete(self, **kwargs):
            if json.loads(kwargs["user"])["requirements"]["operation"] == "draft_quest":
                seen.append(run_skill_usage(store, "demo", state["run_id"]))
            return super().complete(**kwargs)

    result = v2_service.execute_v2_run(
        store, root, "demo", state["run_id"], provider=ObservedProvider()
    )
    assert result["run"]["status"] == "completed"
    assert seen[0]["skills"][0]["steps"][0]["status"] == "dispatched"
    final = run_skill_usage(store, "demo", state["run_id"])
    assert final["skills"][0]["steps"][0]["status"] == "response_received"
    assert final["coverage"] == "complete"
    assert final["semantic_compliance"] == "unknown"
    requests_before = len(HarnessStore(store).requests(state["run_id"]))
    with TestClient(create_app()) as client:
        response = client.get(PREFIX + f"/runs/{state['run_id']}/skill-usage")
        assert response.status_code == 200, response.text
        assert response.json() == final
        assert (
            client.get(
                "/projects/alias/creative/v2/runs/" + state["run_id"] + "/skill-usage"
            ).status_code
            == 404
        )
    assert len(HarnessStore(store).requests(state["run_id"])) == requests_before
    # A subsequent read neither migrates tables nor dispatches another request.
    with store.connection() as db:
        schema_before = list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
    run_skill_usage(store, "demo", state["run_id"])
    with store.connection() as db:
        assert list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == schema_before
    assert before


def test_usage_rejects_tags_without_the_actual_selected_guidance(studio):
    root, store, state = prepared(studio)
    result = v2_service.execute_v2_run(
        store, root, "demo", state["run_id"], provider=ControlledThreeChoiceProvider()
    )
    assert result["run"]["status"] == "completed"
    request = next(
        row
        for row in HarnessStore(store).requests(state["run_id"])
        if row["request"]["role_id"] == "draft_quest"
    )
    request["request"]["user"] = "{}"
    request["request_hash"] = content_hash(request["request"])
    with store.connection() as db:
        db.execute(
            "UPDATE creative_harness_dispatches SET request=? WHERE id=?",
            (json.dumps(request), request["id"]),
        )
    value = run_skill_usage(store, "demo", state["run_id"])
    assert value["coverage"] == "partial"
    assert value["skills"][0]["steps"][0]["status"] == "unknown"


def test_usage_does_not_silently_initialize_an_empty_database(studio, tmp_path):
    import sqlite3
    from types import SimpleNamespace

    path = tmp_path / "untouched.sqlite3"
    with sqlite3.connect(path):
        pass
    with pytest.raises(KeyError):
        run_skill_usage(SimpleNamespace(path=path), "demo", "missing")
    with sqlite3.connect(path) as db:
        assert list(db.execute("SELECT name FROM sqlite_master")) == []
