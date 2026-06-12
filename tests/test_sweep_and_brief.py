"""Round-12 surface: theme sweep (term/judge/graph layers, work order) and world-seed
brief generality (empty fields omitted, zero-count sections, no preset filler). $0."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import run_theme_sweep_action
from owcopilot.assist.sweep import (
    OfflineSweepJudgeProvider,
    ThemeSweepService,
)
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
    Relation,
    StyleGuide,
    Term,
)
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.worldgen.models import WorldSeedBrief
from owcopilot.worldgen.service import (
    _brief_user_message,
    _bundle_from_payload,
    _section_plan,
)


def _sweep_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_dicer": Entity(
                id="npc_dicer",
                name="掷骰人",
                type=EntityType.NPC,
                description="赌坊里讨生活的老千。",
            ),
            "npc_owner": Entity(
                id="npc_owner",
                name="金三爷",
                type=EntityType.NPC,
                description="码头一带的地下钱庄主人。",  # no direct term — graph should flag
            ),
            "npc_clean": Entity(
                id="npc_clean", name="采药童", type=EntityType.NPC, description="山里采药。"
            ),
        },
        relations=[Relation(source="npc_owner", target="npc_dicer", kind="employs")],
        quests={
            "quest_debt": Quest(
                id="quest_debt",
                title="赌债风波",
                objective="替船工抹平一笔赌债。",
            )
        },
        dialogue_trees={
            "tree_bet": DialogueTree(
                id="tree_bet",
                title="码头闲谈",
                participants=["npc_dicer"],
                root_node="n1",
                nodes={
                    "n1": DialogueNode(
                        id="n1",
                        speaker_id="npc_dicer",
                        text="今晚押大还是押小？",
                        choices=[DialogueChoice(id="c1", text="不赌。", next_node=None)],
                    )
                },
            )
        },
        terms={"term_house": Term(id="term_house", canonical="天和赌坊", description="城南赌坊。")},
        style_guides={"style_guide": StyleGuide(body="市井气，避免现代词。")},
    )


def test_sweep_term_layer_hits_every_object_type() -> None:
    report = ThemeSweepService(bundle=_sweep_bundle()).sweep("赌", extra_terms=["押"])
    refs = {f.ref for f in report.hits}
    assert "entity:npc_dicer" in refs  # description
    assert "quest:quest_debt" in refs  # title/objective
    assert "dialogue_tree:tree_bet" in refs  # node/choice text
    assert "term:term_house" in refs  # canonical
    assert "entity:npc_clean" not in refs
    assert report.scanned_total == 7  # 3 entities + 1 quest + 1 tree + 1 term + 1 style guide
    evidence = next(f for f in report.hits if f.ref == "entity:npc_dicer").evidence
    assert "赌" in evidence  # evidence carries the matched term and a snippet


def test_sweep_graph_layer_flags_neighbours_for_review() -> None:
    report = ThemeSweepService(bundle=_sweep_bundle()).sweep("赌")
    review = {f.ref: f for f in report.review_suggested}
    assert "entity:npc_owner" in review  # employs the hit npc, no direct term
    assert review["entity:npc_owner"].layer == "graph"
    assert "npc_dicer" in review["entity:npc_owner"].evidence


def test_sweep_judge_layer_adds_semantic_hits_with_evidence() -> None:
    bundle = _sweep_bundle()
    gateway = LLMGateway(
        providers={"cheap": OfflineSweepJudgeProvider()},
        router=StaticRouter(mapping={"theme_sweep": "cheap"}),
        cache=NoOpCache(),
        telemetry=TelemetryCollector(),
    )
    # the full phrase appears nowhere, so the term layer finds nothing — only the judge
    # (whose double matches per-character, simulating paraphrase recall) can flag the
    # money-lender npc. This is exactly the division of labour the real judge has.
    report = ThemeSweepService(bundle=bundle, gateway=gateway).sweep(
        "地下钱庄经营", use_llm=True, max_judge=10
    )
    assert not [f for f in report.findings if f.layer == "term"]
    judge_hits = [f for f in report.findings if f.layer == "judge"]
    assert any(f.ref == "entity:npc_owner" for f in judge_hits)
    assert all("模型判定" in f.evidence for f in judge_hits)
    assert report.judged_count == report.scanned_total  # nothing pre-hit, all judged
    assert report.judge_skipped == 0


def test_sweep_work_order_markdown_and_action(tmp_path) -> None:
    root = tmp_path / "world"
    ContentStore(root).save(_sweep_bundle())
    result = run_theme_sweep_action(str(root), theme="赌", extra_terms=["押"])
    assert result["scanned_total"] == 7
    assert result["llm_used"] is False
    md = result["markdown"]
    assert "专项清查工作单" in md
    assert "- [ ]" in md and "entity:npc_dicer" in md
    assert "未启用" in md  # honesty: states the judge did not run
    assert float(result["cost_budget"]["used_usd"]) == 0.0


def test_sweep_rejects_empty_theme() -> None:
    with pytest.raises(ValueError, match="主题"):
        ThemeSweepService(bundle=_sweep_bundle()).sweep("   ")


# ------------------------------------------------------------- world seed generality
def test_brief_user_message_omits_empty_fields() -> None:
    brief = WorldSeedBrief(idea="一个会遗忘的图书馆城市")
    message = _brief_user_message(brief)
    assert "一个会遗忘的图书馆城市" in message
    # the guidance sentence mentions dimension names generically, so check the
    # labeled-field form (标签：) that only appears when a field actually has a value
    for label in ("玩家身份：", "基调：", "核心冲突：", "载体/媒介：", "玩法/类型："):
        assert label not in message, f"empty field leaked into prompt: {label}"
    assert "不要为未提及的维度强加具体设定" in message


def test_brief_user_message_includes_filled_fields_only() -> None:
    brief = WorldSeedBrief(idea="海上邮差", tone="温柔", world_styles=["治愈"])
    message = _brief_user_message(brief)
    assert "基调：温柔" in message
    assert "世界风格：治愈" in message
    assert "玩家身份：" not in message and "核心冲突：" not in message


def test_brief_new_bible_dimensions_round_trip() -> None:
    """Round-16 guided form fields (magic system / scope / content red-lines): present
    when filled, absent when not — same omission contract as every other dimension."""
    filled = WorldSeedBrief(
        idea="x",
        magic_level="低魔（稀有而危险）",
        world_scale="一城一镇",
        content_restrictions="不出现骸骨与血泊描写",
    )
    message = _brief_user_message(filled)
    assert "魔法/超自然体系：低魔（稀有而危险）" in message
    assert "世界尺度：一城一镇" in message
    assert "内容红线（必须避免）：不出现骸骨与血泊描写" in message
    empty = _brief_user_message(WorldSeedBrief(idea="x"))
    for label in ("魔法/超自然体系：", "世界尺度：", "内容红线"):
        assert label not in empty


def test_offline_seed_carries_creator_cast_through() -> None:
    """The offline double mirrors the production contract: creator-given characters must
    appear in the generated npcs (so the whole pass-through is testable at $0)."""
    from owcopilot.worldgen.offline import OfflineWorldSeedProvider
    from owcopilot.worldgen.service import _brief_user_message as build_message

    brief = WorldSeedBrief(idea="灯塔群岛", key_characters=["沈横舟：守灯二十年的老领航员"])
    raw, _, _ = OfflineWorldSeedProvider().complete(
        system="", user=build_message(brief), model="cheap"
    )
    import json as _json

    payload = _json.loads(raw)
    names = [npc["name"] for npc in payload["npcs"]]
    assert "沈横舟" in names
    assert payload["npcs"][0]["description"].startswith("守灯二十年")


def test_brief_key_characters_block_present_only_when_given() -> None:
    brief = WorldSeedBrief(idea="x", key_characters=["沈横舟：守灯二十年的老领航员", "  "])
    message = _brief_user_message(brief)
    assert "主要人物" in message and "- 沈横舟：守灯二十年的老领航员" in message
    assert "relations 中设计" in message  # the model is told to wire their relationships
    assert "主要人物" not in _brief_user_message(WorldSeedBrief(idea="x"))


def test_section_plan_zero_counts_mean_empty_arrays() -> None:
    brief = WorldSeedBrief(idea="x", quest_count=0, npc_count=0, term_count=0)
    plan = _section_plan(brief)
    assert "quests" in plan and "npcs" in plan and "terms" in plan
    assert "return [] for: npcs, quests, terms" in plan
    assert "factions=3" in plan  # requested sections keep their targets


def test_bundle_from_payload_zero_quests_and_no_preset_filler() -> None:
    brief = WorldSeedBrief(
        idea="x", faction_count=1, region_count=0, npc_count=0, quest_count=0, term_count=0
    )
    payload = {"factions": [{"name": "守夜会", "description": "看守灯塔。"}]}
    bundle = _bundle_from_payload(
        payload,
        draft_id="ws_test",
        brief=brief,
        existing=ContentBundle(),
        inspiration_pack=_empty_pack(),
        project_pack=_empty_pack(),
    )
    assert len(bundle.quests) == 0  # zero means zero, not a padded minimum
    assert len(bundle.pois) == 0  # locations follow regions: none requested, none invented
    assert [e.name for e in bundle.entities.values() if e.type is EntityType.FACTION] == ["守夜会"]


def test_bundle_from_payload_quest_fallbacks_carry_no_preset_text() -> None:
    brief = WorldSeedBrief(
        idea="x", faction_count=0, region_count=0, npc_count=0, quest_count=1, term_count=0
    )
    payload = {"quests": [{"title": "归航"}]}  # neither objective nor stages provided
    bundle = _bundle_from_payload(
        payload,
        draft_id="ws_test2",
        brief=brief,
        existing=ContentBundle(),
        inspiration_pack=_empty_pack(),
        project_pack=_empty_pack(),
    )
    quest = next(iter(bundle.quests.values()))
    assert quest.objective == "归航"  # derived from the quest itself
    assert [stage.summary for stage in quest.stages] == ["归航"]
    forbidden_presets = ("确认线索", "作出选择", "阵营后果")
    for text in (quest.objective, *(s.summary for s in quest.stages)):
        assert not any(preset in text for preset in forbidden_presets)


def _empty_pack():
    from owcopilot.retrieval.models import ContextPack

    return ContextPack(query="", budget_tokens=0)
