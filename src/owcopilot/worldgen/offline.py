"""Deterministic offline world-seed provider."""

from __future__ import annotations

import json
import re
from typing import Any


class OfflineWorldSeedProvider:
    """Return a compact structured world seed for tests and local dry-runs."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        brief: dict[str, Any] = json.loads(user)
        idea = str(brief.get("idea") or "未命名世界")
        styles = brief.get("world_styles") or ["原创奇幻"]
        style_text = "、".join(str(item) for item in styles if str(item).strip()) or "原创奇幻"
        refs = _reference_rows(system)
        reference_report = [
            {
                "source_ref": ref,
                "source_title": title,
                "used_for": f"{brief.get('reference_mode') or '灵感参考'}：主题、节奏和冲突结构",
                "transformation": (
                    "转化为能源、阵营和区域之间的新冲突，没有把参考材料当作正式设定事实。"
                ),
                "excluded": ["未复用参考材料中的专有名词", "未复用长段原文"],
            }
            for ref, title, _body in refs[:4]
        ]
        payload = {
            "summary": (
                f"围绕“{idea}”展开的{style_text}开放世界，玩家在资源危机和阵营博弈中选择秩序。"
            ),
            "style_guide": {
                "body": f"世界风格：{style_text}。\n核心体验：探索、抉择、阵营后果。",
                "rules": ["所有任务必须指向阵营关系变化", "地点描述要带可交互的叙事钩子"],
            },
            "factions": [
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
            ],
            "regions": [
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
            ],
            "locations": [
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
            ],
            "npcs": [
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
            ],
            "quests": [
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
            ],
            "terms": [
                {
                    "id": "term_hearth_core",
                    "canonical": "炉心",
                    "description": "驱动城市和矿井的核心能源。",
                },
                {
                    "id": "term_ash_oath",
                    "canonical": "灰誓",
                    "description": "旧林盟誓守护生态平衡的誓约。",
                },
                {
                    "id": "term_tide_script",
                    "canonical": "潮汐字",
                    "description": "档案馆保存的前时代文字。",
                },
            ],
            "relations": [
                {"source": "fac_charter", "target": "fac_wilds", "kind": "enemy_of"},
                {"source": "fac_freeport", "target": "fac_charter", "kind": "rival_of"},
                {"source": "fac_freeport", "target": "fac_wilds", "kind": "trades_with"},
            ],
            "reference_report": reference_report,
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _reference_rows(system: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    pattern = r"^- \[(reference_chunk:[^\]]+)\]\s*([^:：]*?)[:：]\s*(.*)$"
    for ref, title, body in re.findall(pattern, system, re.M):
        rows.append((ref, title.strip(), body.strip()))
    return rows
