"""Deterministic offline world-seed provider.

Production builds a world in a *grounded chain* of calls — premise → factions → regions → cast →
quests, plus an optional quests critique — so this double must follow the SAME multi-call contract,
or it would silently hide drift between the two. It dispatches on the stage marker that
``worldgen/stages.py`` stamps onto every stage's system prompt and returns just that stage's slice
of one internally-consistent canned world (so cross-stage ids — ``fac_charter`` referenced by a
location, ``npc_sera`` referenced by a quest — wire up exactly as a real model's would).

Two behaviours beyond the plain stages keep the whole loop exercisable at $0:
  * the quests stage returns a thin batch (one stage per quest) by default and a deepened,
    grounded batch (multiple stages) when the user carries the ``[REFINE]`` marker;
  * the critique stage flips from "revise" to "pass" once the deterministic grounding check (which
    the critic reports in its message) finds nothing left to fix.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .stages import (
    CAST,
    FACTIONS,
    PREMISE,
    QUEST_CRITIQUE,
    QUESTS,
    REGIONS,
    stage_from_system,
)


class OfflineWorldSeedProvider:
    """Return a compact structured world-seed slice for tests and local dry-runs."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        stage = stage_from_system(system)
        if stage is None:
            raise ValueError(
                "offline world-seed double called without a stage marker; production stamps one "
                "on every stage prompt (see worldgen/stages.py)"
            )
        # The production user message is labeled plain text with empty fields omitted (never a
        # full-brief JSON dump — see worldgen.service._brief_user_message); parsing the same format
        # here means tests exercise the real contract.
        brief = _parse_brief_message(user)
        if stage == QUEST_CRITIQUE:
            text = _offline_critique(user)
        else:
            refined = "[REFINE]" in user
            text = json.dumps(
                _slice_for_stage(stage, brief, system, refined=refined), ensure_ascii=False
            )
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _slice_for_stage(
    stage: str, brief: dict[str, Any], system: str, *, refined: bool
) -> dict[str, Any]:
    idea = str(brief.get("idea") or "未命名世界")
    styles = brief.get("world_styles") or ["原创奇幻"]
    style_text = "、".join(str(item) for item in styles if str(item).strip()) or "原创奇幻"
    if stage == PREMISE:
        # Follows the production premise contract: a dramatic spine, not just a summary.
        return {
            "summary": (
                f"围绕“{idea}”展开的{style_text}开放世界，玩家在资源危机和阵营博弈中选择秩序。"
            ),
            "central_conflict": (
                "炉心公约垄断核心能源以维持城市秩序，旧林盟誓要夺回被开采的圣地，"
                "自由港议会在两者之间贩卖情报与退路——能源耗竭迫使三方此刻摊牌。"
            ),
            "themes": ["秩序与自由的代价", "谁有权决定牺牲谁"],
            "dramatic_question": "当秩序要以他人为燃料，你站在哪一边？",
            "faction_axes": ["集权秩序 vs 旧生态守护", "垄断能源 vs 自由流通"],
            "stakes": "炉心即将枯竭，维持城市的代价正转嫁到边境与难民身上。",
            "style_guide": {
                "body": f"世界风格：{style_text}。\n核心体验：探索、抉择、阵营后果。",
                "rules": ["所有任务必须指向阵营关系变化", "地点描述要带可交互的叙事钩子"],
            },
            "terms": _TERMS,
            "reference_report": _reference_report(system),
        }
    if stage == FACTIONS:
        return {"factions": _FACTIONS, "relations": _FACTION_RELATIONS}
    if stage == REGIONS:
        return {"regions": _REGIONS, "locations": _LOCATIONS}
    if stage == CAST:
        return {"npcs": _cast(brief)}
    if stage == QUESTS:
        return {"quests": _quests(refined=refined)}
    raise ValueError(f"offline world-seed double: unhandled stage {stage!r}")


_FACTIONS = [
    {
        "id": "fac_charter",
        "name": "炉心公约",
        "description": "控制核心能源与城市通行权的工程共同体。",
    },
    {
        "id": "fac_wilds",
        "name": "旧林盟誓",
        "description": "守护旧生态和失落仪式的边境联盟。",
    },
    {
        "id": "fac_freeport",
        "name": "自由港议会",
        "description": "靠贸易、情报和走私维持独立的港口势力。",
    },
]

_FACTION_RELATIONS = [
    {"source": "fac_charter", "target": "fac_wilds", "kind": "enemy_of"},
    {"source": "fac_freeport", "target": "fac_charter", "kind": "rival_of"},
    {"source": "fac_freeport", "target": "fac_wilds", "kind": "trades_with"},
]

_REGIONS = [
    {
        "id": "region_crown_city",
        "name": "冠城环带",
        "themes": ["工业中心", "阶层矛盾"],
        "level_min": 1,
        "level_max": 12,
    },
    {
        "id": "region_ashen_wilds",
        "name": "灰烬旧林",
        "themes": ["荒野探索", "古老信仰"],
        "level_min": 8,
        "level_max": 24,
    },
]

_LOCATIONS = [
    {
        "id": "loc_crown_gate",
        "name": "冠门车站",
        "region_id": "region_crown_city",
        "purpose": "新手枢纽，展示能源垄断和难民流动。",
        "controlling_faction": "fac_charter",
    },
    {
        "id": "loc_slate_market",
        "name": "黑板集市",
        "region_id": "region_crown_city",
        "purpose": "情报、黑市和阵营招募交汇点。",
        "controlling_faction": "fac_freeport",
    },
    {
        "id": "loc_root_shrine",
        "name": "根须圣坛",
        "region_id": "region_ashen_wilds",
        "purpose": "揭示旧林盟誓的世界规则。",
        "controlling_faction": "fac_wilds",
    },
    {
        "id": "loc_ember_mine",
        "name": "余烬矿井",
        "region_id": "region_ashen_wilds",
        "purpose": "核心资源争夺地，连接主线冲突。",
        "controlling_faction": "fac_charter",
    },
    {
        "id": "loc_tide_archive",
        "name": "潮汐档案馆",
        "region_id": "region_crown_city",
        "purpose": "保存旧世界真相和可选结局线索。",
        "controlling_faction": "fac_freeport",
    },
]

_TERMS = [
    {"id": "term_hearth_core", "canonical": "炉心", "description": "驱动城市和矿井的核心能源。"},
    {"id": "term_ash_oath", "canonical": "灰誓", "description": "旧林盟誓守护生态平衡的誓约。"},
    {"id": "term_tide_script", "canonical": "潮汐字", "description": "档案馆保存的前时代文字。"},
]

_BASE_CAST = [
    {
        "id": "npc_sera",
        "name": "瑟拉",
        "description": "炉心公约的年轻工程官，相信秩序必须先于自由。",
        "faction_id": "fac_charter",
        "location_id": "loc_crown_gate",
    },
    {
        "id": "npc_ren",
        "name": "任",
        "description": "旧林盟誓的向导，能读懂矿脉中的古老回声。",
        "faction_id": "fac_wilds",
        "location_id": "loc_root_shrine",
    },
    {
        "id": "npc_mave",
        "name": "梅芙",
        "description": "自由港议会的情报掮客，出售真相也出售退路。",
        "faction_id": "fac_freeport",
        "location_id": "loc_slate_market",
    },
    {
        "id": "npc_orlan",
        "name": "奥兰",
        "description": "矿井守卫队长，夹在命令和矿工生计之间。",
        "faction_id": "fac_charter",
        "location_id": "loc_ember_mine",
    },
]


def _cast(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Creator-given key characters lead the cast (mirroring the production contract
    "必须保留并深化"), then the canned supporting cast."""
    creator = [
        {
            "id": f"npc_cast_{index + 1}",
            "name": entry.split("：", 1)[0].strip() or f"主要人物{index + 1}",
            "description": (entry.split("：", 1)[1].strip() if "：" in entry else entry),
            "faction_id": "fac_charter",
            "location_id": "loc_crown_gate",
        }
        for index, entry in enumerate(brief.get("key_characters") or [])
    ]
    return creator + _BASE_CAST


# Quest backbone: each grounded in cast + places. The default batch carries one stage per quest
# (a thin first draft); a [REFINE] regeneration deepens every quest to a multi-stage arc — that is
# the genuine improvement the deterministic grounding check measures and the refine loop closes.
_QUEST_SPECS = [
    {
        "id": "quest_first_spark",
        "title": "第一枚火花",
        "giver_npc": "npc_sera",
        "location": "loc_crown_gate",
        "objective": "调查冠门车站能源中断，并决定是否公开事故原因。",
        "stages": ["访问车站控制室", "询问难民工人", "向一个阵营交付证据"],
    },
    {
        "id": "quest_roots_below",
        "title": "根须之下",
        "giver_npc": "npc_ren",
        "location": "loc_root_shrine",
        "objective": "进入旧林圣坛，确认矿脉开采是否唤醒了失落仪式。",
        "stages": ["穿越灰烬旧林", "解读圣坛刻痕", "选择封印或公开仪式"],
    },
    {
        "id": "quest_blackboard_bargain",
        "title": "黑板交易",
        "giver_npc": "npc_mave",
        "location": "loc_slate_market",
        "objective": "在黑板集市交换情报，找出谁在操纵三方冲突。",
        "stages": ["追踪假账本", "保护线人", "决定情报流向"],
    },
]


def _quests(*, refined: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in _QUEST_SPECS:
        stages_value = spec["stages"] if refined else spec["stages"][:1]
        out.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "giver_npc": spec["giver_npc"],
                "location": spec["location"],
                "objective": spec["objective"],
                "stages": list(stages_value),
            }
        )
    return out


def _offline_critique(user: str) -> str:
    """The world critic only lists "待修正问题" when the deterministic grounding check found gaps;
    their absence means the quest batch is grounded and complete enough to pass."""
    if "待修正问题" in user:
        result = {
            "verdict": "revise",
            "score": 0.5,
            "summary": "任务批次尚未充分接地或阶段不足。",
            "dimensions": [
                {
                    "dimension": "grounding",
                    "severity": "blocker",
                    "issue": "部分任务阶段不足或未接地到已确立的角色/地点。",
                    "fix": "为每个任务补足至少两个阶段，并把 giver_npc 与 location 接地到既有 id。",
                }
            ],
        }
    else:
        result = {
            "verdict": "pass",
            "score": 0.9,
            "summary": "任务结构完整、已接地到既有角色与地点。",
            "dimensions": [{"dimension": "completeness", "severity": "ok", "issue": "", "fix": ""}],
        }
    return json.dumps(result, ensure_ascii=False)


def _parse_brief_message(user: str) -> dict[str, Any]:
    """Parse the labeled-lines user message (核心想法：…/世界风格：…/主要人物 block). Downstream
    stages append grounding/refine sections after the brief; this only reads the labeled brief
    lines and ignores the rest."""
    brief: dict[str, Any] = {}
    characters: list[str] = []
    in_cast = False
    for line in user.splitlines():
        stripped = line.strip()
        if in_cast:
            if stripped.startswith("- "):
                characters.append(stripped[2:].strip())
                continue
            in_cast = False
        if stripped.startswith("主要人物"):
            in_cast = True
            continue
        label, separator, value = stripped.partition("：")
        if not separator:
            continue
        label, value = label.strip(), value.strip()
        if label == "核心想法":
            brief["idea"] = value
        elif label == "世界风格":
            brief["world_styles"] = [s.strip() for s in value.split("、") if s.strip()]
    if characters:
        brief["key_characters"] = characters
    return brief


def _reference_report(system: str) -> list[dict[str, Any]]:
    return [
        {
            "source_ref": ref,
            "source_title": title,
            "used_for": "灵感参考：主题、节奏和冲突结构",
            "transformation": (
                "转化为能源、阵营和区域之间的新冲突，没有把参考材料当作正式设定事实。"
            ),
            "excluded": ["未复用参考材料中的专有名词", "未复用长段原文"],
        }
        for ref, title, _body in _reference_rows(system)[:4]
    ]


def _reference_rows(system: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    pattern = r"^- \[(reference_chunk:[^\]]+)\]\s*([^:：]*?)[:：]\s*(.*)$"
    for ref, title, body in re.findall(pattern, system, re.M):
        rows.append((ref, title.strip(), body.strip()))
    return rows
