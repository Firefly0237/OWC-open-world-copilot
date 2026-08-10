"""``prose-check`` 子命令：自由文本一致性检查（吃书检测）接进 CLI 产品入口（N8.3）。

``check_prose`` 逻辑本身早已完整（见 test_creator_features.py）；这里只验证新的产品入口
（CLI 子命令 -> ``run_prose_check_action``）真的把请求送到了那条逻辑上，不重写判定规则。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_shen": Entity(
                id="npc_shen",
                name="沈青澜",
                type=EntityType.NPC,
                description="边军斥候出身的调查员。",
            ),
        }
    )


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, *extra: str) -> dict:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle())
    input_path = tmp_path / "chapter.txt"
    input_path.write_text(text, encoding="utf-8")
    code = main(["prose-check", "--content-root", str(root), "--input", str(input_path), *extra])
    assert code == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_prose_check_resolves_known_and_flags_an_unregistered_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run(tmp_path, capsys, "沈青澜遇到了不认识的洛千山，洛千山说道。")

    assert any(m["name"] == "沈青澜" for m in payload["resolved_mentions"])
    unknown = [i for i in payload["issues"] if i["kind"] == "unknown_mention"]
    assert any("洛千山" in i["message"] for i in unknown)
    assert payload["stats"]["unknown_mentions"] >= 1
    assert payload["cost_budget"]["used_usd"] == 0.0


def test_prose_check_clean_text_reports_no_issues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run(tmp_path, capsys, "沈青澜检查了城门。")
    assert payload["issues"] == []


def test_prose_check_output_is_byte_stable_across_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle())
    input_path = tmp_path / "chapter.txt"
    input_path.write_text("沈青澜遇到了洛千山。", encoding="utf-8")
    args = ["prose-check", "--content-root", str(root), "--input", str(input_path)]

    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    second = capsys.readouterr().out
    assert first == second
