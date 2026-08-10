"""Typed FastAPI contract for the deterministic dialogue-to-quest playtest loop."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.models import (  # noqa: E402
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    quest = Quest(
        id="q_gate",
        title="门后",
        stages=[QuestStage(id="s1", summary="问答"), QuestStage(id="s2", summary="进门")],
        logic=QuestLogic(
            variables=[LogicVar(id="helped", type=LogicVarType.BOOL, default=False)],
            stage_logic=[StageLogic(stage_id="s2", precondition="helped")],
            branches=[Branch(id="b_enter", from_stage="s1", to_stage="s2")],
        ),
    )
    tree = DialogueTree(
        id="dlg_gate",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                choices=[
                    DialogueChoice(
                        text="帮忙",
                        effects=[Effect(var="helped", op="set", value=True)],
                    )
                ],
            )
        },
    )
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle(quests={quest.id: quest}, dialogue_trees={tree.id: tree}))
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")
    return TestClient(create_app())


def test_playtest_route_composes_dialogue_choices_and_state(client: TestClient) -> None:
    response = client.post(
        "/projects/demo/quests/q_gate:simulate",
        json={
            "choices": ["b_enter"],
            "initial_state": {"manual": "kept"},
            "dialogue_tree_id": "dlg_gate",
            "dialogue_choices": ["n0:0"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quest_id"] == "q_gate"
    assert body["dialogue_walk"]["steps"][0]["chosen"] == "n0:0"
    assert body["dialogue_walk"]["final_state"]["helped"] is True
    assert body["run"]["status"] == "completed"
    assert body["run"]["steps"][0]["branch_taken"] == "b_enter"
    assert body["run"]["final_state"]["manual"] == "kept"
    assert body["cost_budget"]["used_usd"] == 0.0


def test_playtest_explicit_state_overrides_dialogue_and_contract_is_strict(
    client: TestClient,
) -> None:
    overridden = client.post(
        "/projects/demo/quests/q_gate:simulate",
        json={"dialogue_tree_id": "dlg_gate", "initial_state": {"helped": False}},
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["dialogue_walk"]["final_state"]["helped"] is True
    assert overridden.json()["run"]["status"] == "blocked"
    assert overridden.json()["run"]["final_state"]["helped"] is False

    no_dialogue = client.post("/projects/demo/quests/q_gate:simulate", json={})
    assert no_dialogue.status_code == 200
    assert "dialogue_walk" not in no_dialogue.json()

    nested_state = client.post(
        "/projects/demo/quests/q_gate:simulate", json={"initial_state": {"bad": [1, 2]}}
    )
    assert nested_state.status_code == 422
    orphan_choices = client.post(
        "/projects/demo/quests/q_gate:simulate", json={"dialogue_choices": ["n0:0"]}
    )
    assert orphan_choices.status_code == 422
    unknown_option = client.post("/projects/demo/quests/q_gate:simulate", json={"surprise": True})
    assert unknown_option.status_code == 422
