"""Collect real complete preference-data rows from live critique→refine runs.

Runs QuestDraftService with a real LLM (generator + critic) and a refine budget on
deliberately demanding briefs, saves the drafts (with their snapshot-carrying refine trails)
into the demo world's review store, then exports the preference dataset. The complete pairs
in ``benchmarks/preference_pairs/complete_pairs.jsonl`` are a downstream handoff artifact:
top-level prompt/chosen/rejected strings compatible with DPO-style trainers. This script does
not train, fine-tune, or produce a model; it only collects the data.

Honest note: a complete pair only exists when the critic actually sent a round back for
revision — a first-round pass yields none. Demanding briefs raise that probability; they do
not fabricate anything.

Usage:  python scripts/collect_preference_pairs.py [--model deepseek-v4-flash]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from owcopilot.assist.critic import QuestCritic  # noqa: E402
from owcopilot.assist.dpo_export import export_preference_dataset  # noqa: E402
from owcopilot.assist.drafts import QuestDraftService  # noqa: E402
from owcopilot.assist.review_queue import ReviewQueue  # noqa: E402
from owcopilot.audit.baseline import issue_fingerprint  # noqa: E402
from owcopilot.llm.cache import NoOpCache  # noqa: E402
from owcopilot.llm.gateway import LLMGateway  # noqa: E402
from owcopilot.llm.resilience import build_real_provider  # noqa: E402
from owcopilot.llm.router import StaticRouter  # noqa: E402
from owcopilot.llm.telemetry import TelemetryCollector  # noqa: E402
from owcopilot.pipeline.project import ProjectContext  # noqa: E402
from owcopilot.util import load_dotenv  # noqa: E402

DEMO_WORLD = Path.home() / ".owcopilot" / "worlds" / "演示世界"

#: Demanding briefs — multi-stage, cross-faction, hard grounding constraints — so the critic
#: has real substance to send back. Complete pairs require an actual revision round.
BRIEFS = [
    "设计一个三阶段任务：玩家先赢得雾隐盟的信任，再为换取商路公会的通行证背叛他们，"
    "最后在两派夹缝中营救人质。发放人必须是现有 NPC，地点须跨两个区域，"
    "要有道德困境与两个互斥结局，每阶段写明地点与玩家行动。",
    "设计一个调查类任务：黑沙戈壁的商队接连失踪，线索指向一名表面正派的现有 NPC。"
    "要求三条可交叉验证的线索链、一个红鲱鱼误导、以及依赖时间线顺序的证据揭示，"
    "结局必须影响该 NPC 与其所属势力的关系。",
    "设计一个护送任务的黑色变体：护送目标中途暴露为通缉犯，玩家须在按约完成护送与"
    "移交官府之间抉择。发放人与目标都必须引用现有 NPC，途经的每个地点都要有事件，"
    "两种抉择都要有后续钩子。",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--refine-rounds", type=int, default=2)
    args = parser.parse_args()

    load_dotenv()
    telemetry = TelemetryCollector()
    # Demanding briefs make long generations: raise the per-request timeout and allow one
    # retry so a single slow call does not kill the whole collection run.
    provider = build_real_provider(
        model=args.model, json_mode=True, timeout=180.0, max_output_tokens=6000
    )
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"quest_draft": "cheap", "quest_critique": "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
        max_retries=1,
    )

    sqlite_path = DEMO_WORLD / ".owcopilot" / "runtime.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    project = ProjectContext.open(DEMO_WORLD, sqlite_path=sqlite_path)
    try:
        service = QuestDraftService(
            gateway=gateway,
            context_builder=project.context_builder,
            audit_runner=project.audit_runner,
            bundle=project.bundle,
            critic=QuestCritic(gateway=gateway),
            max_refine_rounds=args.refine_rounds,
        )
        queue = ReviewQueue(project.sqlite_store)
        summary = []
        for brief in BRIEFS:
            # One bad model reply (truncated JSON, timeout) must not kill the whole collection
            # run — record the failure honestly and move to the next brief.
            try:
                result = service.draft_quest(brief, budget_tokens=1200)
            except Exception as exc:  # noqa: BLE001 — collection is best-effort per brief
                summary.append({"brief": brief[:30] + "…", "failed": str(exc)[:120]})
                continue
            payload = result.quest.model_dump(mode="json", exclude_none=True)
            payload["refine_trail"] = [r.model_dump(mode="json") for r in result.refine_trail]
            queue.add_quest_draft(
                payload,
                issue_refs=[issue_fingerprint(issue) for issue in result.issues],
            )
            snap_rounds = sum(1 for r in result.refine_trail if r.artifact_before is not None)
            summary.append(
                {
                    "brief": brief[:30] + "…",
                    "rounds": len(result.refine_trail),
                    "verdicts": [r.verdict for r in result.refine_trail],
                    "snapshot_rounds": snap_rounds,
                }
            )

        out_dir = REPO / "benchmarks" / "preference_pairs"
        dataset = export_preference_dataset(project.sqlite_store, output_dir=out_dir)
        print(
            json.dumps(
                {
                    "runs": summary,
                    "complete_pairs": dataset.metadata.total_complete_pairs,
                    "incomplete_pairs": dataset.metadata.total_pairs,
                    "samples": dataset.metadata.total_samples,
                    "cost_usd": round(telemetry.summary().get("total_cost_usd", 0.0), 4),
                    "output": str(out_dir),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        project.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
