"""Deterministic offline provider for world EXPANSION.

Expansion is a grounded chain (focus → pois → cast → quests) over an *existing* world, so unlike the
creation double — which returns one fixed canned world — this double cannot hardcode ids: it must
reference whatever canon the grounding block actually carries. So it parses the SAME grounding lines
the real model reads (``- 阵营 <id>（…）`` / ``- 区域 <id>`` / ``- 地点 <id>`` / ``- 角色 <id>``,
existing-then-new) and grows new content that points only at those real ids. That keeps the $0 path
honest — the offline batch grounds on real canon exactly the way a real model's would, with zero
dangling references — and keeps the double on the identical multi-call, stage-marked contract.

Two behaviours mirror the creation double so the whole loop stays exercisable at $0:
  * the quests stage returns a thin batch (one stage per quest) by default and a deepened,
    multi-stage batch when the user carries the ``[REFINE]`` marker;
  * the QUEST_CRITIQUE stage reuses the creation double's critique, flipping "revise"→"pass" once
    the deterministic grounding check (reported in the critic message) finds nothing left to fix.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .offline import _offline_critique
from .stages import (
    EXPAND_CAST,
    EXPAND_FOCUS,
    EXPAND_POIS,
    EXPAND_QUESTS,
    QUEST_CRITIQUE,
    stage_from_system,
)

_FACTION_RE = re.compile(r"^- 阵营 ([^\s（(]+)", re.M)
_REGION_RE = re.compile(r"^- 区域 ([^\s（(]+)", re.M)
_LOCATION_RE = re.compile(r"^- 地点 ([^\s（(]+)", re.M)
_NPC_RE = re.compile(r"^- 角色 ([^\s（(]+)", re.M)
_NEW_HEADER = "本批已新增"


class OfflineWorldExpandProvider:
    """Return a compact, canon-grounded expansion slice for tests and local dry-runs."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        stage = stage_from_system(system)
        if stage is None:
            raise ValueError(
                "offline world-expand double called without a stage marker; production stamps one "
                "on every stage prompt (see worldgen/stages.py)"
            )
        if stage == QUEST_CRITIQUE:
            text = _offline_critique(user)
        else:
            refined = "[REFINE]" in user
            text = json.dumps(_slice_for_stage(stage, system, refined=refined), ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _slice_for_stage(stage: str, system: str, *, refined: bool) -> dict[str, Any]:
    canon, new = _split_grounding(system)
    factions = _ids(_FACTION_RE, canon)
    regions = _ids(_REGION_RE, canon)
    if stage == EXPAND_FOCUS:
        return {
            "angle": (
                "在既有冲突之上深化该焦点：补充可探索地点、卷入冲突的次要角色，"
                "以及把玩家推入两难抉择的支线，与现有主轴一致而不矛盾。"
            ),
            "central_ids": (factions[:1] + regions[:1]),
        }
    if stage == EXPAND_POIS:
        return {"pois": _pois(_count(system, "pois"), regions, factions)}
    if stage == EXPAND_CAST:
        locations = _ids(_LOCATION_RE, new) or _ids(_LOCATION_RE, canon)
        return {"npcs": _cast(_count(system, "npcs"), factions, locations)}
    if stage == EXPAND_QUESTS:
        npcs = _ids(_NPC_RE, new) or _ids(_NPC_RE, canon)
        locations = _ids(_LOCATION_RE, new) or _ids(_LOCATION_RE, canon)
        return {"quests": _quests(_count(system, "quests"), npcs, locations, refined=refined)}
    raise ValueError(f"offline world-expand double: unhandled stage {stage!r}")


def _pois(count: int, regions: list[str], factions: list[str]) -> list[dict[str, Any]]:
    region = regions[0] if regions else None
    faction = factions[0] if factions else None
    out: list[dict[str, Any]] = []
    for index in range(count):
        poi: dict[str, Any] = {
            "id": f"loc_exp_{index + 1}",
            "name": f"扩写据点{index + 1}",
            "description": "焦点区域内新增的可探索据点，揭示一处当下的张力。",
            "purpose": "把焦点冲突落到一个可交互的地点上。",
            "tags": ["扩写"],
        }
        if region:
            poi["region_id"] = region
        if faction:
            poi["controlling_faction"] = faction
        out.append(poi)
    return out


def _cast(count: int, factions: list[str], locations: list[str]) -> list[dict[str, Any]]:
    faction = factions[0] if factions else None
    location = locations[0] if locations else None
    out: list[dict[str, Any]] = []
    for index in range(count):
        npc: dict[str, Any] = {
            "id": f"npc_exp_{index + 1}",
            "name": f"扩写人物{index + 1}",
            "description": "卷入焦点冲突的次要角色，有自己想要的东西和当下的撕扯。",
        }
        if faction:
            npc["faction_id"] = faction
        if location:
            npc["location_id"] = location
        out.append(npc)
    return out


def _quests(
    count: int, npcs: list[str], locations: list[str], *, refined: bool
) -> list[dict[str, Any]]:
    giver = npcs[0] if npcs else None
    location = locations[0] if locations else None
    deep = ["接触线索并表态", "在焦点地点查证", "在两派张力间作出抉择"]
    out: list[dict[str, Any]] = []
    for index in range(count):
        quest: dict[str, Any] = {
            "id": f"quest_exp_{index + 1}",
            "title": f"扩写支线{index + 1}",
            "objective": "围绕焦点冲突展开一条支线，把玩家推进一次站队抉择。",
            "stages": list(deep) if refined else deep[:1],
            "tags": ["扩写", "支线"],
        }
        if giver:
            quest["giver_npc"] = giver
        if location:
            quest["location"] = location
        out.append(quest)
    return out


def _split_grounding(system: str) -> tuple[str, str]:
    """Split the grounding block into (existing-canon part, this-batch's-new part). Downstream
    stages may reference both; the new part is preferred so new cast/quests wire to new places."""
    marker = system.find(_NEW_HEADER)
    if marker < 0:
        return system, ""
    return system[:marker], system[marker:]


def _ids(pattern: re.Pattern[str], text: str) -> list[str]:
    seen: list[str] = []
    for match in pattern.findall(text):
        if match not in seen:
            seen.append(match)
    return seen


def _count(system: str, key: str) -> int:
    """The requested count for a section, read from the plan line 'Target new-content counts: …'."""
    match = re.search(rf"\b{key}=(\d+)", system)
    if match:
        return int(match.group(1))
    match = re.search(r"Design exactly (\d+)", system)
    return int(match.group(1)) if match else 1
