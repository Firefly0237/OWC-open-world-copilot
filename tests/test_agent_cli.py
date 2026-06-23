from __future__ import annotations

import json

from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore


def _dirty_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


def test_cli_agent_offline_end_to_end(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)

    code = main(
        [
            "agent",
            "--content-root",
            str(content_root),
            "--goal",
            "Get this world ready to export.",
            "--llm-mode",
            "offline",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stop_reason"] == "finished"
    assert [step["action"] for step in payload["steps"]] == [
        "audit_project",
        "build_context_pack",
        "quality_harness",
    ]
    assert "audit_project" in payload["skills"]
    assert payload["llm_mode"] == "offline"
    # Agent reasoning went through the gateway (telemetry recorded its calls); the tool actions
    # themselves are deterministic and $0.
    assert payload["telemetry"]["calls"] >= 4


def test_cli_agent_rejects_missing_content_root(tmp_path) -> None:
    code = main(
        [
            "agent",
            "--content-root",
            str(tmp_path / "missing"),
            "--goal",
            "anything",
            "--llm-mode",
            "offline",
        ]
    )
    # main() catches the FileNotFoundError, prints a JSON error, and returns 2.
    assert code == 2
