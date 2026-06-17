"""Design-readiness assessment: deterministic completeness scoring, offline / $0.

See ``project_docs/开发全过程.md``. Readiness is completeness, not correctness — these
tests assert it never touches the audit's error stream.
"""

from __future__ import annotations

import json

from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
    QuestStage,
    RegionBrief,
    Relation,
    Reward,
)
from owcopilot.content.store import ContentStore
from owcopilot.readiness import (
    STANDARD_VERSION,
    assess_quest,
    assess_readiness,
)


def _complete_quest() -> Quest:
    return Quest(
        id="q_signal",
        title="错音之源",
        objective="追查铃声错乱的源头并作出抉择。",
        giver_npc="npc_warden",
        location="region_fog",
        stages=[QuestStage(id="s1", summary="勘察雾铃"), QuestStage(id="s2", summary="抉择")],
        rewards=[Reward(kind="item", value="静默之铃")],
    )


def test_complete_quest_is_ready_with_full_score() -> None:
    item = assess_quest(_complete_quest())
    assert item.ready is True
    assert item.score == 1.0
    assert item.missing == []
    assert item.ref == "quest:q_signal"


def test_incomplete_quest_lists_missing_checklist_items() -> None:
    stub = Quest(
        id="q_stub", title="占位任务", objective="待补"
    )  # no stages/rewards/giver/location
    item = assess_quest(stub)
    assert item.ready is False
    assert item.score < 1.0
    # objective "待补" is 2 chars (< 8), and stages/reward/giver/location all absent
    labels = {c.label: c.passed for c in item.checks}
    assert labels["奖励结构"] is False
    assert labels["任务阶段"] is False
    assert "奖励结构" in item.missing


def test_character_profile_completeness_and_connection() -> None:
    full_profile = {
        key: "x"
        for key in (
            "appearance",
            "personality",
            "backstory",
            "motivation",
            "abilities",
            "weakness",
            "voice",
        )
    }
    bundle = ContentBundle(
        entities={
            "npc_done": Entity(
                id="npc_done",
                name="雾喉",
                type=EntityType.NPC,
                description="以记忆为筹码的掮客。",
                metadata={"profile": full_profile},
            ),
            "npc_bare": Entity(id="npc_bare", name="无名", type=EntityType.NPC),
            "loc_x": Entity(id="loc_x", name="灯塔", type=EntityType.LOCATION),
        },
        relations=[Relation(source="npc_done", target="loc_x", kind="located_in")],
    )
    report = assess_readiness(bundle)
    chars = {it.ref: it for it in report.items if it.kind == "character"}
    # only NPCs are assessed for readiness, not the LOCATION entity
    assert set(chars) == {"entity:npc_done", "entity:npc_bare"}
    assert chars["entity:npc_done"].ready is True
    bare = chars["entity:npc_bare"]
    assert bare.ready is False
    assert "已接入关系网" in bare.missing  # npc_bare is in no relation
    assert "人设档案" in bare.missing


def test_region_and_dialogue_tree_readiness() -> None:
    linear = DialogueTree(
        id="dt_linear",
        title="独白",
        participants=["npc_done"],
        root_node="n1",
        nodes={"n1": DialogueNode(id="n1", text="只有旁白，没有分支。")},
    )
    branching = DialogueTree(
        id="dt_branch",
        title="抉择",
        participants=["npc_done"],
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                text="你怎么选？",
                choices=[
                    DialogueChoice(text="留下", next_node=None),
                    DialogueChoice(text="离开", next_node=None),
                ],
            )
        },
    )
    bundle = ContentBundle(
        regions={
            "r_full": RegionBrief(
                id="r_full", name="雾湾", level_min=1, level_max=10, themes=["雾"]
            ),
            "r_bare": RegionBrief(id="r_bare", name="空区"),
        },
        dialogue_trees={"dt_linear": linear, "dt_branch": branching},
    )
    report = assess_readiness(bundle)
    by_ref = {it.ref: it for it in report.items}
    assert by_ref["region:r_full"].ready is True
    assert by_ref["region:r_bare"].ready is False
    assert by_ref["dialogue_tree:dt_branch"].ready is True
    assert by_ref["dialogue_tree:dt_linear"].ready is False
    assert "存在分支选项" in by_ref["dialogue_tree:dt_linear"].missing


def test_poi_term_and_faction_are_assessed() -> None:
    # these content types used to be invisible to the readiness board (it only saw
    # quests/NPCs/regions/dialogue), so an incomplete world could read as "done".
    from owcopilot.content.models import POI, Term

    bundle = ContentBundle(
        entities={
            "fac_done": Entity(
                id="fac_done", name="盐会", type=EntityType.FACTION, description="掌控盐路的商会。"
            ),
            "fac_bare": Entity(id="fac_bare", name="孤帮", type=EntityType.FACTION),
            "npc_x": Entity(id="npc_x", name="周", type=EntityType.NPC),
        },
        relations=[Relation(source="npc_x", target="fac_done", kind="member_of")],
        pois={
            "loc_full": POI(
                id="loc_full",
                name="盐仓",
                region_id="r1",
                purpose="补给与交易",
                controlling_faction="fac_done",
            ),
            "loc_bare": POI(id="loc_bare", name="空地"),
        },
        terms={
            "t_full": Term(id="t_full", canonical="潮汐律", description="海港的潮汐法令。"),
            "t_bare": Term(id="t_bare", canonical="空词"),
        },
    )
    report = assess_readiness(bundle)
    by_ref = {it.ref: it for it in report.items}
    kinds = {it.kind for it in report.items}
    assert {"faction", "poi", "term"} <= kinds  # all three now on the board

    assert by_ref["entity:fac_done"].ready is True
    assert by_ref["entity:fac_bare"].ready is False  # no description, not connected
    assert by_ref["poi:loc_full"].ready is True
    assert by_ref["poi:loc_bare"].ready is False
    assert "所属区域" in by_ref["poi:loc_bare"].missing
    assert by_ref["term:t_full"].ready is True
    assert by_ref["term:t_bare"].ready is False
    assert "词条释义" in by_ref["term:t_bare"].missing


def test_report_aggregation_and_standard_version() -> None:
    bundle = ContentBundle(quests={"q": _complete_quest()})
    report = assess_readiness(bundle)
    assert report.standard_version == STANDARD_VERSION
    assert report.total_items == 1
    assert report.ready_items == 1
    assert report.overall_score == 1.0
    assert report.ready_rate == 1.0
    assert len(report.content_hash) == 64
    quest_summary = next(s for s in report.by_kind if s.kind == "quest")
    assert quest_summary.total == 1
    assert quest_summary.ready == 1


def test_empty_bundle_is_vacuously_ready() -> None:
    report = assess_readiness(ContentBundle())
    assert report.total_items == 0
    assert report.overall_score == 1.0
    assert report.ready_rate == 1.0
    assert report.by_kind == []


def test_view_model_builder_filters(tmp_path) -> None:
    from owcopilot.app.view_models import build_readiness_report

    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            quests={"q_full": _complete_quest(), "q_stub": Quest(id="q_stub", title="占位")},
        )
    )
    full = build_readiness_report(root)
    assert full["total_items"] == 2
    assert "cost_budget" in full
    incomplete = build_readiness_report(root, only_incomplete=True)
    assert [it["ref"] for it in incomplete["items"]] == ["quest:q_stub"]
    only_quests = build_readiness_report(root, kind="quest")
    assert all(it["kind"] == "quest" for it in only_quests["items"])


def test_workorder_markdown_groups_gaps_by_kind() -> None:
    from owcopilot.app.view_models import readiness_workorder_markdown

    bundle = ContentBundle(
        quests={
            "q_full": _complete_quest(),
            "q_stub": Quest(id="q_stub", title="占位任务", objective="待补"),
        },
    )
    report = assess_readiness(bundle).model_dump(mode="json")
    md = readiness_workorder_markdown(report)
    assert md.startswith("# 设计就绪度工作单")
    assert "总体就绪度" in md
    assert "## 任务" in md
    # the incomplete stub appears with its ref and a concrete missing checklist label
    assert "占位任务" in md
    assert "quest:q_stub" in md
    assert "奖励结构" in md
    # the complete quest is ready, so it must not be listed as work to do
    assert "错音之源" not in md


def test_workorder_markdown_when_all_ready_lists_no_work() -> None:
    from owcopilot.app.view_models import readiness_workorder_markdown

    report = assess_readiness(ContentBundle(quests={"q": _complete_quest()})).model_dump(
        mode="json"
    )
    md = readiness_workorder_markdown(report)
    assert "可量产标准" in md
    assert "##" not in md  # no per-kind sections when nothing is incomplete


def test_cli_readiness_command(tmp_path, capsys) -> None:
    from owcopilot.cli.main import main

    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle(quests={"q": _complete_quest()}))
    rc = main(["readiness", "--content-root", str(root)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["standard_version"] == STANDARD_VERSION
    assert payload["ready_items"] == 1
