# 生成 Unity 任务状态机运行器的跨语言差分测试夹具（runner_bundle.json + runner_traces.json）。
"""Generate the golden fixtures for the Unity quest state-machine runner cross-language test.

This is the Python side of a **differential test** (the reference-oracle method, Wikipedia
"Differential testing"; cross-language conformance corpus per ACM AsiaCCS 2024 "Cross-Language
Differential Testing of JSON Parsers"): the production ``simulate_quest`` / ``walk_dialogue_tree``
is the reference oracle, the C# ``QuestRunner`` / ``DialogueWalker`` is the second implementation,
and the two files written here are the shared corpus that drives both.

* ``runner_bundle.json`` — the **real** ``export_content_bundle`` serialization of a
  :class:`ContentBundle`. The C# parses these exact wire bytes into runner models (never a
  hand-rolled shape), exactly like the E2 ``content_bundle.json`` fixture gate.
* ``runner_traces.json`` — for each scenario, the stored ``choices`` + ``initial_state`` (so the C#
  replays a FIXED script — no explore/oracle logic on the C# side) plus the production oracle's
  full result. The C# re-runs its own runner with the same inputs and asserts agreement on
  ``status`` / ``path`` / ``steps`` / ``final_state`` (``message`` is excluded — Chinese vs any C#
  text).

The bundle mixes hand-authored scenario quests/trees that force every terminal condition — all five
simulate statuses (``completed`` / ``deadlock`` / ``blocked`` / ``cycle`` / ``unavailable``,
including the quest-level precondition gate proven state-dependent by the same quest going both
``unavailable`` and ``completed``), all four walk statuses (``completed`` / ``dangling`` /
``deadlock`` / ``cycle``), a terminal-branch outcome and a dialogue→quest closure — with two
**production-generated** support-tier quests (the first two of the
N2b support corpus), so the runner is exercised against genuine generated complexity, not only toy
graphs. Deterministic and ``$0`` (no model, no network): same code => byte-identical fixtures.

    python scripts/gen_runner_traces.py            # (re)write the committed fixtures
    python scripts/gen_runner_traces.py --out DIR  # write to DIR instead (used by the byte gate)
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from owcopilot.content.models import (
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.evaluation.complexity_corpus import (
    generate_complexity_bundle,
    load_complexity_profiles,
)
from owcopilot.exporters import export_content_bundle
from owcopilot.logic.dialogue import walk_dialogue_tree
from owcopilot.logic.simulate import Value, simulate_quest

_FIXTURES_DIR = (
    Path(__file__).resolve().parents[1]
    / "unity"
    / "com.owcopilot.content-import"
    / "Tests"
    / "Editor"
    / "Fixtures"
)

# The support tier of the N2b structural-complexity corpus, at the committed benchmark seed. The
# first two quests (sorted) are folded into the runner bundle as real generated-complexity cases.
_SUPPORT_SEED = 20260711
_SUPPORT_TIER = "support"
_SUPPORT_COUNT = 2


# --------------------------------------------------------------------------------------------------
# Hand-authored scenario content — each quest/tree forces exactly one terminal condition.
# --------------------------------------------------------------------------------------------------


def _bool_var(var_id: str) -> LogicVar:
    return LogicVar(id=var_id, name=var_id, type=LogicVarType.BOOL, default=False)


def _quest_linear() -> Quest:
    """Linear two-stage quest with an on-complete effect: simulates to ``completed``."""
    return Quest(
        id="q_linear",
        title="Linear",
        stages=[QuestStage(id="s0", summary="start"), QuestStage(id="s1", summary="end")],
        logic=QuestLogic(
            variables=[_bool_var("v_started")],
            stage_logic=[
                StageLogic(
                    stage_id="s0",
                    effects_on_complete=[Effect(var="v_started", op="set", value=True)],
                )
            ],
        ),
    )


def _quest_terminal() -> Quest:
    """Single stage whose only branch is an always-true terminal outcome carrying a rep effect:
    simulates to ``completed`` with the last step taking the terminal branch."""
    return Quest(
        id="q_terminal",
        title="Terminal",
        stages=[QuestStage(id="s0", summary="decide")],
        logic=QuestLogic(
            branches=[
                Branch(
                    id="br_win",
                    from_stage="s0",
                    to_stage="",
                    outcome="win",
                    effects=[Effect(var="rep:fac_watch", op="inc", value=1)],
                )
            ]
        ),
    )


def _quest_deadlock() -> Quest:
    """Decision stage whose only branch is gated off by a false flag: simulates to ``deadlock``."""
    return Quest(
        id="q_deadlock",
        title="Deadlock",
        stages=[QuestStage(id="s0", summary="stuck")],
        logic=QuestLogic(
            variables=[_bool_var("gate")],
            branches=[Branch(id="br_gated", from_stage="s0", condition="gate", to_stage="")],
        ),
    )


def _quest_blocked() -> Quest:
    """Second stage has a false precondition: simulates to ``blocked`` after entering it."""
    return Quest(
        id="q_blocked",
        title="Blocked",
        stages=[QuestStage(id="s0", summary="start"), QuestStage(id="s1", summary="locked")],
        logic=QuestLogic(
            variables=[_bool_var("key")],
            stage_logic=[StageLogic(stage_id="s1", precondition="key")],
        ),
    )


def _quest_cycle() -> Quest:
    """Two stages branch into each other: the stage-space visited set trips ``cycle``."""
    return Quest(
        id="q_cycle",
        title="Cycle",
        stages=[QuestStage(id="s0", summary="a"), QuestStage(id="s1", summary="b")],
        logic=QuestLogic(
            branches=[
                Branch(id="b_s0_s1", from_stage="s0", to_stage="s1"),
                Branch(id="b_s1_s0", from_stage="s1", to_stage="s0"),
            ]
        ),
    )


def _quest_gated() -> Quest:
    """Quest-level precondition ``has_writ`` gates ENTRY: false in the seeded state simulates to
    ``unavailable`` (no stage entered); the SAME quest simulates to ``completed`` once the initial
    state satisfies it — proving the gate is state-dependent, not a static quest property."""
    return Quest(
        id="q_gated",
        title="Gated",
        stages=[
            QuestStage(id="s0", summary="present the writ"),
            QuestStage(id="s1", summary="enter"),
        ],
        logic=QuestLogic(
            variables=[_bool_var("has_writ")],
            precondition="has_writ",
        ),
    )


def _quest_open_gate() -> Quest:
    """Explicit empty quest-level precondition = always enterable (the field's documented ""
    semantics): a regression sentinel that the quest gate never treats "" as false."""
    return Quest(
        id="q_open_gate",
        title="OpenGate",
        stages=[QuestStage(id="s0", summary="walk in")],
        logic=QuestLogic(
            variables=[_bool_var("noted")],
            precondition="",
            stage_logic=[
                StageLogic(
                    stage_id="s0",
                    effects_on_complete=[Effect(var="noted", op="set", value=True)],
                )
            ],
        ),
    )


def _quest_closure() -> Quest:
    """Second stage is gated on ``door_open``; only reachable when a dialogue choice set it. Drives
    the dialogue→quest closure scenario (walk feeds ``final_state`` into simulate as initial)."""
    return Quest(
        id="q_closure",
        title="Closure",
        stages=[QuestStage(id="s0", summary="approach"), QuestStage(id="s1", summary="enter")],
        logic=QuestLogic(
            variables=[_bool_var("door_open")],
            stage_logic=[StageLogic(stage_id="s1", precondition="door_open")],
        ),
    )


def _tree_completed() -> DialogueTree:
    """Root choice applies an effect and links to a leaf: walk reaches ``completed``."""
    return DialogueTree(
        id="t_completed",
        quest_id="q_closure",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                text="greet",
                choices=[
                    DialogueChoice(
                        text="say hello",
                        next_node="n1",
                        effects=[Effect(var="flag_met", op="set", value=True)],
                    )
                ],
            ),
            "n1": DialogueNode(id="n1", text="bye"),
        },
    )


def _tree_dangling() -> DialogueTree:
    """Root linear ``next_node`` points at a missing node: the walk reports ``dangling``."""
    return DialogueTree(
        id="t_dangling",
        root_node="n0",
        nodes={"n0": DialogueNode(id="n0", text="lost", next_node="ghost")},
    )


def _tree_deadlock() -> DialogueTree:
    """Root's only choice is gated off and it has no ``next_node`` fallback: walk ``deadlock``."""
    return DialogueTree(
        id="t_deadlock",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                text="closed",
                choices=[DialogueChoice(text="locked", next_node="n1", condition="gate")],
            ),
            "n1": DialogueNode(id="n1", text="unreachable"),
        },
    )


def _tree_cycle() -> DialogueTree:
    """Two linear nodes point at each other: the visited set trips ``cycle``."""
    return DialogueTree(
        id="t_cycle",
        root_node="n0",
        nodes={
            "n0": DialogueNode(id="n0", text="a", next_node="n1"),
            "n1": DialogueNode(id="n1", text="b", next_node="n0"),
        },
    )


def _tree_closure() -> DialogueTree:
    """Root choice sets ``door_open``: its walk ``final_state`` unlocks ``q_closure``."""
    return DialogueTree(
        id="t_closure",
        quest_id="q_closure",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                text="the door",
                choices=[
                    DialogueChoice(
                        text="open it",
                        next_node="n1",
                        effects=[Effect(var="door_open", op="set", value=True)],
                    )
                ],
            ),
            "n1": DialogueNode(id="n1", text="through"),
        },
    )


def build_bundle() -> ContentBundle:
    """Assemble the runner bundle: hand-authored scenario quests/trees + the two support quests.

    Deterministic — the support quests come from ``generate_complexity_bundle`` at a fixed
    (profile, seed), so the same code always produces the identical bundle."""
    bundle = ContentBundle()
    for quest in (
        _quest_linear(),
        _quest_terminal(),
        _quest_deadlock(),
        _quest_blocked(),
        _quest_cycle(),
        _quest_gated(),
        _quest_open_gate(),
        _quest_closure(),
    ):
        bundle.quests[quest.id] = quest
    for tree in (
        _tree_completed(),
        _tree_dangling(),
        _tree_deadlock(),
        _tree_cycle(),
        _tree_closure(),
    ):
        bundle.dialogue_trees[tree.id] = tree

    for qid in support_quest_ids():
        bundle.quests[qid] = _support_bundle().quests[qid]
    return bundle


def _support_bundle() -> ContentBundle:
    profile = load_complexity_profiles()[_SUPPORT_TIER]
    return generate_complexity_bundle(profile, _SUPPORT_SEED)


def support_quest_ids() -> list[str]:
    """The first two quest ids of the support corpus, in sorted order (``[q0000, q0001]``)."""
    return sorted(_support_bundle().quests)[:_SUPPORT_COUNT]


# --------------------------------------------------------------------------------------------------
# Scenario table — (name, target id, fixed choices, fixed initial state).
# --------------------------------------------------------------------------------------------------

QuestScenario = tuple[str, str, list[str], dict[str, Value]]
DialogueScenario = tuple[str, str, list[str], dict[str, Value]]
# (name, tree id, dialogue choices, dialogue initial, quest id, quest choices)
ClosureScenario = tuple[str, str, list[str], dict[str, Value], str, list[str]]


def quest_scenarios() -> list[QuestScenario]:
    return [
        ("linear_completed", "q_linear", [], {}),
        ("terminal_outcome", "q_terminal", [], {}),
        ("deadlock", "q_deadlock", [], {}),
        ("blocked_precondition", "q_blocked", [], {}),
        ("cycle", "q_cycle", [], {}),
        ("quest_gate_unavailable", "q_gated", [], {}),
        ("quest_gate_satisfied", "q_gated", [], {"has_writ": True}),
        ("quest_gate_empty_is_open", "q_open_gate", [], {}),
        ("support_q0000", "q0000", [], {}),
        ("support_q0001", "q0001", [], {}),
    ]


def dialogue_scenarios() -> list[DialogueScenario]:
    return [
        ("walk_completed", "t_completed", [], {}),
        ("walk_dangling", "t_dangling", [], {}),
        ("walk_deadlock", "t_deadlock", [], {}),
        ("walk_cycle", "t_cycle", [], {}),
    ]


def closure_scenarios() -> list[ClosureScenario]:
    return [
        ("dialogue_unlocks_stage", "t_closure", ["n0:0"], {}, "q_closure", []),
    ]


# --------------------------------------------------------------------------------------------------
# Trace generation — run the production oracle over each scenario.
# --------------------------------------------------------------------------------------------------


def build_traces(bundle: ContentBundle) -> dict[str, Any]:
    """Run the production simulate/walk over every scenario and record inputs + full result."""
    quests: list[dict[str, Any]] = []
    for name, quest_id, choices, initial in quest_scenarios():
        run = simulate_quest(
            bundle.quests[quest_id], choices=list(choices), initial_state=dict(initial)
        )
        quests.append(
            {
                "name": name,
                "quest_id": quest_id,
                "choices": list(choices),
                "initial_state": dict(initial),
                "expected": run.model_dump(mode="json"),
            }
        )

    dialogues: list[dict[str, Any]] = []
    for name, tree_id, choices, initial in dialogue_scenarios():
        walk = walk_dialogue_tree(
            bundle.dialogue_trees[tree_id], choices=list(choices), initial_state=dict(initial)
        )
        dialogues.append(
            {
                "name": name,
                "tree_id": tree_id,
                "choices": list(choices),
                "initial_state": dict(initial),
                "expected": walk.model_dump(mode="json"),
            }
        )

    closures: list[dict[str, Any]] = []
    for name, tree_id, dchoices, dinitial, quest_id, qchoices in closure_scenarios():
        walk = walk_dialogue_tree(
            bundle.dialogue_trees[tree_id], choices=list(dchoices), initial_state=dict(dinitial)
        )
        run = simulate_quest(
            bundle.quests[quest_id],
            choices=list(qchoices),
            initial_state=dict(walk.final_state),
        )
        closures.append(
            {
                "name": name,
                "tree_id": tree_id,
                "dialogue_choices": list(dchoices),
                "dialogue_initial_state": dict(dinitial),
                "quest_id": quest_id,
                "quest_choices": list(qchoices),
                "dialogue_expected": walk.model_dump(mode="json"),
                "quest_expected": run.model_dump(mode="json"),
            }
        )

    return {
        "meta": {
            "description": (
                "Differential-test corpus: production simulate_quest/walk_dialogue_tree is the "
                "oracle; the C# QuestRunner/DialogueWalker replays these fixed inputs and must "
                "agree on status/path/steps/final_state (message excluded)."
            ),
            "support_seed": _SUPPORT_SEED,
            "support_tier": _SUPPORT_TIER,
            "support_quests": support_quest_ids(),
        },
        "quest_scenarios": quests,
        "dialogue_scenarios": dialogues,
        "closure_scenarios": closures,
    }


def _serialize_bundle(bundle: ContentBundle) -> str:
    """The exact bytes the real exporter emits for ``content_bundle.json`` (a system temp dir is
    used only as the exporter's scratch output; nothing is written into the repo workspace)."""
    with tempfile.TemporaryDirectory() as scratch:
        export_content_bundle(bundle, scratch)
        return (Path(scratch) / "content_bundle.json").read_text(encoding="utf-8")


def _json(value: object) -> str:
    """Deterministic serialization, exporter-shaped (sorted keys, trailing newline)."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_fixtures(out_dir: Path) -> tuple[Path, Path]:
    """Write ``runner_bundle.json`` + ``runner_traces.json`` into ``out_dir``, return paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle()
    bundle_path = out_dir / "runner_bundle.json"
    traces_path = out_dir / "runner_traces.json"
    bundle_path.write_text(_serialize_bundle(bundle), encoding="utf-8")
    traces_path.write_text(_json(build_traces(bundle)), encoding="utf-8")
    return bundle_path, traces_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_FIXTURES_DIR,
        help="output directory for the two fixtures (default: the Unity package Fixtures dir)",
    )
    args = parser.parse_args()
    bundle_path, traces_path = write_fixtures(args.out)
    print(f"[written] {bundle_path}")
    print(f"[written] {traces_path}")


if __name__ == "__main__":
    main()
