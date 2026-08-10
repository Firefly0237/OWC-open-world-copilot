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
    assert payload["run_budget"]["planning_turn_limit"] == 6
    assert payload["run_budget"]["reservation_semantics"] == "durable_pre_dispatch"
    # Agent reasoning went through the gateway (telemetry recorded its calls); the tool actions
    # themselves are deterministic and $0.
    assert payload["telemetry"]["calls"] >= 4


def test_cli_agent_default_early_stopping_is_force(tmp_path, capsys) -> None:
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
            "--max-steps",
            "2",  # too small to finish -> exercises the budget-exhaustion path
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stop_reason"] == "max_steps"  # --early-stopping omitted -> default "force"


def test_cli_agent_max_tool_calls_is_independent_and_typed(tmp_path, capsys) -> None:
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
            "--max-steps",
            "6",
            "--max-tool-calls",
            "1",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stop_reason"] == "finished"
    assert payload["steps"][0]["is_error"] is False
    assert json.loads(payload["steps"][1]["observation"])["error_code"] == "BUDGET_EXCEEDED"


def test_cli_agent_total_model_call_budget_is_independent_and_hard(tmp_path, capsys) -> None:
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
            "--max-steps",
            "6",
            "--max-model-calls",
            "1",
            "--max-tool-calls",
            "6",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stop_reason"] == "max_model_calls"
    assert payload["run_budget"]["planning_turn_limit"] == 6
    assert payload["run_budget"]["total_model_call_limit"] == 1
    assert payload["run_budget"]["tool_call_limit"] == 6
    assert payload["run_budget"]["total_model_calls_reserved"] == 1


def test_cli_agent_early_stopping_generate_flag_wires_through(tmp_path, capsys) -> None:
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
            "--max-steps",
            "2",
            "--early-stopping",
            "generate",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stop_reason"] == "max_steps_generate"
    assert payload["step_count"] == 2
    # One extra planning call beyond the 2 exhausted tool-step turns: the generate-mode finish call.
    assert payload["telemetry"]["calls"] >= 3


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
