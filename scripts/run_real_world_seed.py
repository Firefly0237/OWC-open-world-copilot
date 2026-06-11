"""Real-LLM validation for inspiration references and world seed creation.

This script spends provider tokens. It builds a fresh temporary project, adds an inspiration
reference, generates a world seed with the configured real model, accepts the review item, audits
the saved content, and asks a grounded lore question against the resulting project.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_real_world_seed.py
    .venv\\Scripts\\python.exe scripts\\run_real_world_seed.py --model deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from owcopilot.app.actions import (  # noqa: E402
    add_reference_action,
    decide_review_action,
    run_ask_action,
    run_project_audit_action,
    run_world_seed_action,
)
from owcopilot.content.models import ContentBundle, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.util import load_dotenv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--workspace", default=".tmp/real_world_seed")
    parser.add_argument("--output", default="project_docs/reports/real_world_seed.json")
    args = parser.parse_args()

    load_dotenv()
    os.environ.setdefault("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "120")
    os.environ.setdefault("OWCOPILOT_MAX_OUTPUT_TOKENS", "5000")
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    workspace = Path(args.workspace) / run_id
    content_root = workspace / "content"
    ContentStore(content_root).save(ContentBundle())

    reference = add_reference_action(
        content_root,
        title="Real validation reference",
        text=(
            "A design reference for a three-faction open-world RPG: an industrial guild "
            "controls failing infrastructure, a forest order protects memory rituals, and a "
            "free city trades secrets between both sides. The desired structure is exploration, "
            "evidence gathering, faction choice, and visible consequences in hub locations."
        ),
        allowed_uses=["inspiration", "structure", "style"],
    )

    seed = run_world_seed_action(
        content_root,
        brief={
            "idea": "一个靠蒸汽巨树维持生命的边境群岛，玩家调查能源衰竭背后的旧战争记忆。",
            "medium": "开放世界游戏",
            "game_genre": "开放世界 RPG",
            "world_styles": ["蒸汽朋克", "魔幻"],
            "tone": "克制、悬疑、史诗",
            "era": "工业革命早期与古代仪式并存",
            "player_fantasy": "流亡调查员",
            "core_conflict": "能源衰竭、阶层对立、旧神信仰复苏",
            "reference_mode": "参考剧情结构",
            "reference_query": "three faction infrastructure forest ritual free city",
            "faction_count": 3,
            "region_count": 2,
            "npc_count": 6,
            "quest_count": 4,
            "term_count": 4,
        },
        llm_mode="real",
        llm_model=args.model,
    )
    accepted = decide_review_action(
        content_root,
        item_id=seed["review_item_id"],
        decision="accepted",
        operator="real_llm_validation",
    )
    audit = run_project_audit_action(content_root)
    saved = ContentStore(content_root).load()
    npcs = [entity for entity in saved.entities.values() if entity.type is EntityType.NPC]
    if not npcs:
        raise RuntimeError("real model world seed did not produce any NPC entities")
    ask = run_ask_action(
        content_root,
        query=f"{npcs[0].name}是谁？",
        llm_mode="real",
        llm_model=args.model,
    )

    passed = (
        bool(seed["inspiration_context_refs"])
        and bool(seed["reference_report"])
        and seed["counts"]["quests"] >= 4
        and seed["counts"]["entities"] >= 9
        and accepted["written_ref"].startswith("world_seed:")
        and audit["open_errors"] == 0
        and not ask["answer"]["refused"]
        and bool(ask["answer"]["citations"])
    )
    payload = {
        "passed": passed,
        "workspace": str(workspace),
        "reference_source": reference["source"],
        "seed": {
            "id": seed["id"],
            "summary": seed["summary"],
            "counts": seed["counts"],
            "reference_report": seed["reference_report"],
            "inspiration_context_refs": seed["inspiration_context_refs"],
            "telemetry": seed["telemetry"],
            "cost_budget": seed["cost_budget"],
        },
        "accepted": accepted,
        "audit_open_errors": audit["open_errors"],
        "ask": {
            "query": f"{npcs[0].name}是谁？",
            "answer": ask["answer"],
            "telemetry": ask["telemetry"],
            "cost_budget": ask["cost_budget"],
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
