"""``flag-xref`` 子命令与同名 skill：世界级 flag 的 where-used 报表接进产品入口。

测试组装内容、驱动真实 CLI / skill 注册表，不重写交叉引用逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.cli.main import main
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.content.store import ContentStore
from owcopilot.skills import default_skill_registry


def _quest(quest_id: str, logic: QuestLogic) -> Quest:
    return Quest(id=quest_id, title=quest_id, stages=[QuestStage(id="s0", summary="")], logic=logic)


def _shared_flag_world(root: Path) -> None:
    """``shared`` 由 q_writer 写、被 q_reader 读——最常见的跨任务共享形态。"""
    writer = _quest(
        "q_writer",
        QuestLogic(
            variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s0", effects_on_complete=[Effect(var="shared", op="set", value=True)]
                )
            ],
        ),
    )
    reader = _quest(
        "q_reader",
        QuestLogic(
            variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
            branches=[Branch(id="b0", from_stage="s0", condition="shared == true", outcome="done")],
        ),
    )
    ContentStore(root).save(ContentBundle(quests={"q_writer": writer, "q_reader": reader}))


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:
    root = tmp_path / "content"
    _shared_flag_world(root)
    code = main(["flag-xref", "--content-root", str(root), *extra])
    assert code == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_flag_xref_lists_writers_and_readers_across_quests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run(tmp_path, capsys, "--flag", "shared")

    entry = payload["flags"][0]
    assert entry["flag"] == "shared"
    assert entry["declared_in"] == ["q_reader", "q_writer"]
    assert [w["owner_id"] for w in entry["writers"]] == ["q_writer"]
    assert [r["owner_id"] for r in entry["readers"]] == ["q_reader"]
    assert entry["verdicts"] == []


def test_flag_xref_without_a_filter_lists_every_flag_and_a_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run(tmp_path, capsys)

    assert {entry["flag"] for entry in payload["flags"]} >= {"shared"}
    assert payload["reads_complete"] is True
    assert payload["summary"]["declared"] == 1
    assert payload["cost_budget"]["used_usd"] == 0.0


def test_flag_xref_output_is_byte_stable_across_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    _shared_flag_world(root)

    main(["flag-xref", "--content-root", str(root)])
    first = capsys.readouterr().out
    main(["flag-xref", "--content-root", str(root)])
    second = capsys.readouterr().out

    assert first == second


def test_unknown_flag_gets_a_chinese_guiding_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    _shared_flag_world(root)

    code = main(["flag-xref", "--content-root", str(root), "--flag", "no_such_flag"])

    assert code == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert error["type"] == "ValueError"
    assert "no_such_flag" in error["error"]
    assert "拼写" in error["error"]  # 引导下一步，而不是甩一句原始报错


def test_flag_xref_skill_returns_the_same_report_as_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """agent 侧与人侧看到的必须是同一份事实。"""
    root = tmp_path / "content"
    _shared_flag_world(root)
    registry = default_skill_registry(content_root=str(root))

    from_skill = registry.run("flag_xref", {"flag": "shared"})
    main(["flag-xref", "--content-root", str(root), "--flag", "shared"])
    from_cli = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert from_skill["flags"] == from_cli["flags"]
    assert from_skill["summary"] == from_cli["summary"]
