"""Test the ``memory-report`` CLI: it surfaces the Agent Memory / 数据闭环 loop over a workspace."""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.cli.main import main


def test_memory_report_surfaces_the_loop_on_an_empty_workspace(tmp_path: Path, capsys) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()

    assert main(["memory-report", "--content-root", str(content_root)]) == 0

    payload = json.loads(capsys.readouterr().out)
    loop = payload["agent_memory_loop"]
    # names the whole loop as one subsystem
    assert len(loop["stages"]) == 5
    # review-queue state is reported (empty workspace → zeros, but real fields)
    rq = loop["review_queue"]
    assert rq["pending"] == 0
    assert rq["resolved"] == 0
    assert rq["accepted"] == 0
    assert rq["rejected"] == 0
    # calibration is present with the honest small-sample guard
    assert "calibration" in loop
    assert loop["calibration"]["sufficient_sample"] is False
