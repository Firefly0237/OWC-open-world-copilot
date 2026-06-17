"""Real-LLM end-to-end for world EXPANSION: grow content on an existing world and verify grounding.

The canon (the *existing* small world) is seeded with the deterministic offline provider ($0) and
accepted into a content store — so the only real-model spend is the expansion itself, and the test
is clean: does a REAL model, handed an existing world and one focus, grow new locations / NPCs /
side quests that reference EXISTING canon ids with ZERO dangling references? How much of the batch
stays anchored to the existing world (the volume-vs-coherence question) rather than floating free?

This spends a few cents of provider tokens. Usage:
    .venv\\Scripts\\python.exe scripts\\run_real_world_expand.py
    .venv\\Scripts\\python.exe scripts\\run_real_world_expand.py --model deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from owcopilot.app.actions import (  # noqa: E402
    decide_review_action,
    run_world_expand_action,
    run_world_seed_action,
)
from owcopilot.content.models import ContentBundle, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.util import load_dotenv, use_utf8_stdout  # noqa: E402

# A deliberately small but coherent seed world: 3 opposing factions, 2 regions, a starter cast.
SEED_BRIEF: dict[str, Any] = {
    "idea": "一个靠蒸汽巨树供能的边境群岛，三股势力为能源衰竭的真相角力。",
    "world_styles": ["蒸汽朋克", "魔幻"],
    "tone": "克制、悬疑",
    "core_conflict": "能源衰竭、阶层对立、旧神信仰复苏",
    "faction_count": 3,
    "region_count": 2,
    "npc_count": 5,
    "quest_count": 3,
    "term_count": 3,
}


def _canon_ids(bundle: ContentBundle) -> dict[str, set[str]]:
    return {
        "faction": {e.id for e in bundle.entities.values() if e.type is EntityType.FACTION},
        "region": set(bundle.regions),
        "location": set(bundle.pois)
        | {e.id for e in bundle.entities.values() if e.type is EntityType.LOCATION},
        "npc": {e.id for e in bundle.entities.values() if e.type is EntityType.NPC},
        "quest": set(bundle.quests),
    }


def _member_factions(bundle: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in bundle.get("relations", []):
        if rel.get("kind") == "member_of":
            out.setdefault(rel["source"], rel["target"])
    return out


def main() -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--workspace", default=".tmp/real_world_expand")
    parser.add_argument("--output", default=".tmp/real_world_expand.json")
    parser.add_argument("--refine-rounds", type=int, default=1)
    args = parser.parse_args()

    load_dotenv()
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    root = Path(args.workspace) / run_id / "world"
    ContentStore(root).save(ContentBundle())

    # --- 1) seed the existing world OFFLINE ($0) and accept it into the store ---
    seeded = run_world_seed_action(root, brief=SEED_BRIEF, llm_mode="offline", refine_rounds=0)
    decide_review_action(
        root, item_id=seeded["review_item_id"], decision="accepted", operator="seed"
    )
    canon = ContentStore(root).load()
    canon_ids = _canon_ids(canon)
    if not canon_ids["region"]:
        raise SystemExit("seed world has no regions to expand")
    focus_region = sorted(canon_ids["region"])[0]

    # --- 2) REAL expansion on one region ---
    result = run_world_expand_action(
        root,
        brief={
            "focus_ref": f"region:{focus_region}",
            "angle": (
                "把这片区域的日常肌理和暗流铺开：新的据点、卷入冲突的次要角色、几条迫使站队的支线。"
            ),
            "poi_count": 4,
            "npc_count": 5,
            "quest_count": 4,
        },
        llm_mode="real",
        llm_model=args.model,
        refine_rounds=args.refine_rounds,
    )
    bundle = result["bundle"]
    grounding = result["grounding"]

    # --- 3) verify grounding against the EXISTING canon, by id ---
    new_loc_ids = set(bundle.get("pois", {}))
    new_npc_ids = {eid for eid, e in bundle.get("entities", {}).items() if e["type"] == "npc"}
    npc_universe = canon_ids["npc"] | new_npc_ids
    loc_universe = canon_ids["location"] | new_loc_ids
    member = _member_factions(bundle)

    pois = []
    for poi in bundle.get("pois", {}).values():
        pois.append(
            {
                "id": poi["id"],
                "name": poi["name"],
                "region_id": poi.get("region_id"),
                "region_is_canon": poi.get("region_id") in canon_ids["region"],
                "controlling_faction": poi.get("controlling_faction"),
                "faction_is_canon": poi.get("controlling_faction") in canon_ids["faction"],
            }
        )
    npcs = []
    for eid in new_npc_ids:
        faction = member.get(eid)
        npcs.append(
            {
                "id": eid,
                "name": bundle["entities"][eid]["name"],
                "faction_id": faction,
                "faction_is_canon": faction in canon_ids["faction"],
            }
        )
    quests = []
    for quest in bundle.get("quests", {}).values():
        giver = quest.get("giver_npc")
        location = quest.get("location")
        quests.append(
            {
                "id": quest["id"],
                "title": quest["title"],
                "giver_npc": giver,
                "giver_in_world": giver in npc_universe,
                "giver_is_canon": giver in canon_ids["npc"],
                "location": location,
                "location_in_world": location in loc_universe,
                "location_is_canon": location in canon_ids["location"],
                "stages": len(quest.get("stages", [])),
            }
        )

    # How much of the batch is ANCHORED to the existing world (references >=1 canon id)? The
    # volume-vs-coherence read: a high anchor ratio means the new content grows the world it
    # extends rather than drifting into a disconnected island.
    anchored_pois = sum(1 for p in pois if p["region_is_canon"] or p["faction_is_canon"])
    anchored_npcs = sum(1 for n in npcs if n["faction_is_canon"])
    anchored_quests = sum(1 for q in quests if q["giver_is_canon"] or q["location_is_canon"])
    new_total = len(pois) + len(npcs) + len(quests)
    anchored_total = anchored_pois + anchored_npcs + anchored_quests

    report = {
        "model": args.model,
        "run_id": run_id,
        "canon": {
            "factions": len(canon_ids["faction"]),
            "regions": len(canon_ids["region"]),
            "locations": len(canon_ids["location"]),
            "npcs": len(canon_ids["npc"]),
            "quests": len(canon_ids["quest"]),
        },
        "focus": result["focus_ref"],
        "focus_label": result["focus_label"],
        "angle": result["angle"],
        "expand_counts": result["counts"],
        "grounding": grounding,
        "grounding_ok": grounding["dangling_refs"] == [],
        "canon_anchor_ratio": round(anchored_total / new_total, 3) if new_total else 0,
        "pois": pois,
        "npcs": npcs,
        "quests": quests,
        "refine_trail": result["refine_trail"],
        "issues": [{"rule": i["rule_code"], "severity": i["severity"]} for i in result["issues"]],
        "cost_usd": result["cost_budget"].get("used_usd"),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
