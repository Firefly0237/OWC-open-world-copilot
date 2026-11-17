"""WS-A S4 · edit a quest's logic layer through the human-edit pipeline (action level)."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    run_project_audit_action,
    update_quest_logic_action,
)
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore


def _write(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            quests={
                "q1": Quest(
                    id="q1",
                    title="盐风驰援",
                    stages=[
                        QuestStage(id="s1", summary="出发"),
                        QuestStage(id="s2", summary="抵达"),
                    ],
                )
            }
        )
    )


def test_set_logic_persists_and_returns_issues(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    result = update_quest_logic_action(
        root,
        quest_id="q1",
        logic={
            "variables": [{"id": "flag", "type": "bool", "default": False}],
            "stage_logic": [{"stage_id": "s2", "precondition": "flag"}],
        },
    )
    assert result["quest"]["logic"]["variables"][0]["id"] == "flag"
    assert result["logic_issues"] == []  # clean logic

    # it persisted into canon: a fresh load sees the logic
    reloaded = ContentStore(root).load().quests["q1"]
    assert reloaded.logic is not None and reloaded.logic.stage_logic[0].precondition == "flag"


def test_malformed_expression_is_rejected_cleanly(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    with pytest.raises(ValueError, match="无法解析"):
        update_quest_logic_action(
            root,
            quest_id="q1",
            logic={"stage_logic": [{"stage_id": "s2", "precondition": "flag =="}]},
        )


def test_logic_issues_flow_into_the_project_audit(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    update_quest_logic_action(
        root,
        quest_id="q1",
        logic={"stage_logic": [{"stage_id": "s2", "precondition": "undefined_flag"}]},
    )
    audit = run_project_audit_action(root)
    blob = " ".join(issue["message"] for issue in audit["issues"])
    assert "LOGIC_UNDEFINED_VAR" in blob  # the logic rule is part of the standard audit


def test_clearing_logic_sets_none(tmp_path) -> None:
    root = tmp_path / "content"
    _write(root)
    update_quest_logic_action(root, quest_id="q1", logic={"variables": []})
    cleared = update_quest_logic_action(root, quest_id="q1", logic=None)
    assert cleared["quest"]["logic"] is None
