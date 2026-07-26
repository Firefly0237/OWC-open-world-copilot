"""E6 · quest-simulate CLI + the end-to-end demo line + doc anti-rot checks.

Tests assemble inputs and drive production code (the CLI, scripts/run_demo_pipeline.py) — the demo
world builder is imported from the script itself so the smoke test exercises the exact demo path.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from owcopilot.cli.main import build_parser, main

_REPO = Path(__file__).resolve().parents[1]
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


# --- doc anti-rot: 演示动线.md must reference real commands and real files -----------------------
def test_demo_doc_references_resolve() -> None:
    doc = (_REPO / "docs" / "演示动线.md").read_text(encoding="utf-8")
    # every repo path mentioned must exist
    pattern = r"(?<![\w/])(?:docs|scripts|unity|integrations|benchmarks)/[\w./\-]+"
    for rel in re.findall(pattern, doc):
        assert (_REPO / rel).exists(), f"演示动线.md 引用了不存在的路径:{rel}"
    # every `owcopilot <sub>` command mentioned must be a registered subcommand
    parser = build_parser()
    subactions = next(
        a for a in parser._actions if isinstance(a, type(parser._subparsers._group_actions[0]))
    )
    registered = set(subactions.choices.keys())
    for sub in set(re.findall(r"owcopilot ([a-z][a-z\-]+)", doc)):
        assert sub in registered, f"演示动线.md 引用了未注册的子命令:{sub}"
