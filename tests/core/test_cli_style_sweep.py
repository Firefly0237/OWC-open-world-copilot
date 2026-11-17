"""``style-sweep`` 子命令：StyleRubric 风格核对级联（RubricSweepService）接进 CLI 产品入口（N8.3）。

级联本身早已完整（见 tests/evaluation/test_rubric_cascade.py）；这里只验证新的产品入口
（CLI 子命令）真的把请求送到了那条逻辑上，不重写 L1/L2/L3 的判定规则。选型理由（独立子命令，
非 review 家族/非挂进
既有 sweep REST 路由）留档在 ``owcopilot.cli.main._cmd_style_sweep`` 的 docstring 里。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.cli.main import main
from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    StyleElementKind,
    StyleElementPolarity,
    StyleRubric,
    StyleRubricElement,
)
from owcopilot.content.store import ContentStore


def _rubric() -> StyleRubric:
    return StyleRubric(
        id="wafu",
        label="和风浓度 v1",
        abstract_tag="日式风格",
        version="1",
        elements=[
            StyleRubricElement(
                id="je_weapon_katana",
                text="出现日本刀剑器物（武士刀/太刀）",
                kind=StyleElementKind.LEXICAL,
                terms=["武士刀", "太刀"],
                polarity=StyleElementPolarity.INCLUDE,
            ),
            StyleRubricElement(
                id="je_no_cross",
                text="不应出现十字架/教堂等西方宗教符号",
                kind=StyleElementKind.LEXICAL,
                terms=["十字架", "教堂"],
                polarity=StyleElementPolarity.EXCLUDE,
            ),
        ],
    )


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_samurai": Entity(
                id="npc_samurai",
                name="佐藤剑客",
                type=EntityType.NPC,
                description="腰间挂着一把武士刀。",
            ),
            "npc_priest": Entity(
                id="npc_priest",
                name="西境神父",
                type=EntityType.NPC,
                description="教堂钟声响起，十字架高悬于祭坛。",
            ),
        },
    )


def _saved_world(root: Path) -> None:
    bundle = _bundle()
    bundle.style_rubrics["wafu"] = _rubric()
    ContentStore(root).save(bundle)


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:
    root = tmp_path / "content"
    _saved_world(root)
    code = main(["style-sweep", "--content-root", str(root), "--rubric-id", "wafu", *extra])
    assert code == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_style_sweep_recalls_lexical_elements_with_evidence_and_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run(tmp_path, capsys)

    assert payload["rubric_id"] == "wafu"
    assert payload["rubric_version"] == "1"
    assert payload["scanned_total"] >= 2
    by_id = {e["element_id"]: e for e in payload["elements"]}
    assert "entity:npc_samurai" in by_id["je_weapon_katana"]["l1_refs"]
    # taboo element hit is a violation, surfaced in the flat "hits" list
    cross_finding = next(f for f in payload["hits"] if f["ref"] == "entity:npc_priest")
    assert "禁忌命中" in cross_finding["evidence"] and "十字架" in cross_finding["evidence"]
    assert "风格核对工作单" in payload["markdown"]
    assert payload["llm_used"] is False
    assert payload["cost_budget"]["used_usd"] == 0.0


def test_unknown_rubric_id_gets_a_chinese_guiding_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    _saved_world(root)

    code = main(["style-sweep", "--content-root", str(root), "--rubric-id", "no_such_rubric"])

    assert code == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert error["type"] == "ValueError"
    assert "no_such_rubric" in error["error"]
    assert "wafu" in error["error"]  # guided toward the actually-available rubric id


def test_style_sweep_output_is_byte_stable_across_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    _saved_world(root)
    args = ["style-sweep", "--content-root", str(root), "--rubric-id", "wafu"]

    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    second = capsys.readouterr().out
    assert first == second
