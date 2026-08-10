# 端到端演示动线（离线段）：建世界→审计→全路径探索→对白闭环→双引擎导出，每步一条真实 CLI。
"""E6 · the offline demo line, runnable in one command:

    python scripts/run_demo_pipeline.py --out <dir>

Builds a small branching demo world (a dialogue choice sets a flag; a quest branch pays
reputation; two endings), then drives the REAL CLI for every step — the same commands a user
types, so this script is also an executable walkthrough of the delivery sequence:

    1. audit         — deterministic rules, expect zero open errors
    2. quest-explore — narrative model checking: every ending reachable? dead branches?
    3. dialogue walk + quest-simulate --initial-state — the dialogue-choice -> quest-path loop
    4. export --target-engine unreal — generic bundle + UE DataTable projection + preflight

Deterministic and $0 (no model, no network). Prints each step's command, then the JSON the CLI
emitted; exits non-zero on the first failing step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owcopilot.cli.main import main as cli_main
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueRef,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    LocalizedText,
    LogicVar,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.content.store import ContentStore
from owcopilot.logic import walk_dialogue_tree
from owcopilot.util import use_utf8_stdout


def build_demo_world(root: Path) -> None:
    """A minimal but REAL branching world: dialogue effects, branch effects, two endings."""
    bundle = ContentBundle(
        entities={
            "npc_mara": Entity(id="npc_mara", name="玛拉", type=EntityType.NPC, description="斥候"),
            "loc_gate": Entity(id="loc_gate", name="边境哨门", type=EntityType.LOCATION),
        },
        quests={
            "quest_gate": Quest(
                id="quest_gate",
                title="哨门风波",
                giver_npc="npc_mara",
                location="loc_gate",
                objective="平息哨门的争执。",
                localization_keys=["quest.quest_gate.objective"],
                stages=[
                    QuestStage(id="s_talk", summary="与玛拉交谈"),
                    QuestStage(id="s_act", summary="选择立场"),
                ],
                dialogue_refs=["dlg_line_ask"],
                logic=QuestLogic(
                    variables=[
                        LogicVar(id="helped", type="bool", default=False),
                        LogicVar(id="rep", type="int", default=0),
                    ],
                    stage_logic=[StageLogic(stage_id="s_act", precondition="helped")],
                    branches=[
                        {
                            "id": "b_peace",
                            "from_stage": "s_act",
                            "condition": "rep >= 0",
                            "to_stage": "",
                            "outcome": "peaceful",
                            "effects": [{"var": "rep", "op": "inc", "value": 1}],
                        },
                        {
                            "id": "b_force",
                            "from_stage": "s_act",
                            "to_stage": "",
                            "outcome": "forceful",
                            "effects": [{"var": "rep", "op": "dec", "value": 1}],
                        },
                    ],
                ),
            )
        },
        dialogues={
            "dlg_line_ask": DialogueRef(
                id="dlg_line_ask",
                text_key="dlg.gate.ask",
                speaker_id="npc_mara",
                quest_id="quest_gate",
                text="你愿意帮忙吗？",
                locale="zh-CN",
            )
        },
        localized_texts={
            "loc_gate_objective": LocalizedText(
                id="loc_gate_objective",
                text_key="quest.quest_gate.objective",
                locale="zh-CN",
                text="平息哨门的争执。",
            )
        },
        dialogue_trees={
            "dlg_gate": DialogueTree(
                id="dlg_gate",
                quest_id="quest_gate",
                root_node="n0",
                participants=["npc_mara"],
                nodes={
                    "n0": DialogueNode(
                        id="n0",
                        speaker_id="npc_mara",
                        text="你愿意帮忙吗？",
                        choices=[
                            DialogueChoice(
                                text="我来帮你。",
                                next_node="n_end",
                                effects=[Effect(var="helped", op="set", value=True)],
                            ),
                            DialogueChoice(text="与我无关。", next_node="n_end"),
                        ],
                    ),
                    "n_end": DialogueNode(id="n_end", speaker_id="npc_mara", text="……"),
                },
            )
        },
    )
    ContentStore(root).save(bundle)


def _step(title: str, argv: list[str]) -> None:
    print(f"\n== {title} ==\n$ owcopilot {' '.join(argv)}")
    code = cli_main(argv)
    if code != 0:
        print(f"[demo] 步骤失败（exit {code}），中止。", file=sys.stderr)
        raise SystemExit(code)


def main() -> None:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Working dir for the demo world + exports.")
    args = parser.parse_args()

    out = Path(args.out)
    root = out / "content"
    build_demo_world(root)
    print(f"[demo] 世界已生成：{root}")

    _step("1/4 确定性审计", ["audit", "--content-root", str(root)])
    print(
        "\n[demo] 步骤 2 预期读法：两个结局都报 unreachable、死分支两条——探索器在告诉你"
        "『不做对白,这个任务玩不通』（s_act 的前置 helped 只有对白选择能置真）。这不是失败,"
        "是模型检查暴露了对白依赖;步骤 3 用对白状态闭环它。"
    )
    _step(
        "2/4 叙事模型检查（每个结局可达吗）",
        ["quest-explore", "--content-root", str(root), "quest_gate"],
    )

    # 3/4 对白闭环：走树得状态（帮忙 -> helped=True），喂回模拟器
    walk = walk_dialogue_tree(
        ContentStore(root).load().dialogue_trees["dlg_gate"], choices=["n0:0"]
    )
    state_path = out / "dialogue_state.json"
    state_path.write_text(json.dumps(walk.final_state, ensure_ascii=False), encoding="utf-8")
    print(f"\n[demo] 对白行走完成：status={walk.status} final_state={walk.final_state}")
    _step(
        "3/4 对白选择改变任务走向（quest-simulate 消费对白状态）",
        [
            "quest-simulate",
            "--content-root",
            str(root),
            "quest_gate",
            "--choices",
            "b_peace",
            "--initial-state",
            str(state_path),
        ],
    )

    _step(
        "4/4 双形态导出（generic bundle + UE DataTable 投影 + preflight）",
        [
            "export",
            "--content-root",
            str(root),
            "--output-dir",
            str(out / "export"),
            "--target-engine",
            "unreal",
        ],
    )

    ue_dir = out / "export" / "unreal" / "unreal"
    print("\n[demo] 完成。关键产物：")
    for name in ("Quests.json", "Entities.json", "Dialogues.json", "OWCopilotRows.h"):
        path = ue_dir / name
        print(f"  - {path} {'✓' if path.exists() else '（未生成）'}")
    print(f"  - {out / 'export' / 'unreal' / 'content_bundle.json'} ✓")
    print(
        "[demo] 下一步（真机段）：按 integrations/README.md 与引擎包 README 的核对表，"
        "把 unreal/ 拷进 UE 工程导入。"
    )


if __name__ == "__main__":
    main()
