"""E6 · quest-simulate CLI + the end-to-end demo line.

Tests assemble inputs and drive production code (the CLI, scripts/run_demo_pipeline.py) — the demo
world builder is imported from the script itself so the smoke test exercises the exact demo path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from owcopilot.cli.main import main

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))
from run_demo_pipeline import build_demo_world  # noqa: E402 — the script IS the fixture source


# --- quest-simulate CLI ---------------------------------------------------------------------------
def test_cli_quest_simulate_with_choices_and_initial_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    build_demo_world(root)
    state = tmp_path / "state.json"
    state.write_text('{"helped": true}', encoding="utf-8")

    code = main(
        [
            "quest-simulate",
            "--content-root",
            str(root),
            "quest_gate",
            "--choices",
            "b_peace",
            "--initial-state",
            str(state),
        ]
    )
    assert code == 0
    run = json.loads(capsys.readouterr().out)
    assert run["status"] == "completed" and run["path"] == ["s_talk", "s_act"]
    assert run["final_state"]["rep"] == 1  # branch effects settled


def test_cli_quest_simulate_missing_quest_is_guided(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    build_demo_world(root)
    code = main(["quest-simulate", "--content-root", str(root), "ghost_quest"])
    assert code == 2
    assert "不存在" in capsys.readouterr().err  # guided error, no traceback


# --- the demo line, end to end --------------------------------------------------------------------
def test_demo_pipeline_runs_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import run_demo_pipeline

    monkeypatch.setattr(sys, "argv", ["run_demo_pipeline.py", "--out", str(tmp_path / "demo")])
    run_demo_pipeline.main()  # raises SystemExit on any failing step
    out = capsys.readouterr().out
    assert "4/4" in out
    ue_dir = tmp_path / "demo" / "export" / "unreal" / "unreal"
    for name in ("Quests.json", "Entities.json", "Dialogues.json", "OWCopilotRows.h"):
        assert (ue_dir / name).exists(), name
