"""HTTP metadata round trip without model calls or user-origin inference."""

import json

from fastapi.testclient import TestClient

from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.service.api import create_app


def test_creator_names_round_trip_outside_the_unchanged_generation_request(tmp_path, monkeypatch):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root)}))
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    prefix = "/projects/demo/creative/v2/tasks"
    with TestClient(create_app()) as client:
        response = client.post(prefix, json={
            "brief": "Keep this complete raw creative requirement.",
            "display_title": "A letter after midnight",
            "display_synopsis": "A messenger returns with an unopened letter.",
            "origin_kind": "author",
        })
        assert response.status_code == 201, response.text
        created = response.json()["task"]
        for actual in [
            client.get(prefix + "/" + created["id"]).json()["task"],
            client.get(prefix).json()["tasks"][0],
        ]:
            assert actual["display_title"] == "A letter after midnight"
            assert actual["origin_kind"] == "author"
            assert actual["request"]["brief"] == "Keep this complete raw creative requirement."
            assert "display_title" not in actual["request"]
        old_shape = client.post(prefix, json={"brief": "Development input is still preserved."})
        assert old_shape.status_code == 201
        assert old_shape.json()["task"]["display_title"] == "未命名故事"
        assert old_shape.json()["task"]["origin_kind"] == "unspecified"
