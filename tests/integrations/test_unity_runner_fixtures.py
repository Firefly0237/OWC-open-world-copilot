"""Golden gate for the Unity quest-runner cross-language differential fixtures.

Locks the fixtures under
``integrations/unity/com.owcopilot.content-import/Tests/Editor/Fixtures/`` that the
C# ``QuestRunnerCrossLangTests`` runs against, exactly like the E2 ``content_bundle.json`` gate:

* **byte gate** — committed ``runner_bundle.json`` / ``runner_traces.json`` byte-identical to
  a fresh run of ``scripts/gen_runner_traces.py`` (so the C# runs against genuine wire bytes, and a
  drift on either side is caught);
* **oracle consistency** — every stored trace equals a FRESH production ``simulate_quest`` /
  ``walk_dialogue_tree`` over that scenario's stored ``choices`` + ``initial_state`` (proving the
  committed traces are real oracle output, never hand-edited);
* **coverage** — all four simulate statuses + all four walk statuses + a terminal-branch outcome + a
  dialogue→quest closure + the two production-generated support quests are present.

No business logic is duplicated: the bundle/scenarios come from the generator and the expected
results come from the production simulate/walk paths; the test only assembles inputs and asserts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gen_runner_traces as gen

from owcopilot.logic.dialogue import walk_dialogue_tree
from owcopilot.logic.simulate import simulate_quest

_FIXTURES = (
    _REPO_ROOT
    / "integrations"
    / "unity"
    / "com.owcopilot.content-import"
    / "Tests"
    / "Editor"
    / "Fixtures"
)
_FIXTURE_NAMES = ("runner_bundle.json", "runner_traces.json")


def _committed_traces() -> dict:
    return json.loads((_FIXTURES / "runner_traces.json").read_text(encoding="utf-8"))


# --- byte gate + determinism --------------------------------------------------------------------


def test_generator_output_is_byte_identical_to_committed(tmp_path: Path) -> None:
    gen.write_fixtures(tmp_path)
    for name in _FIXTURE_NAMES:
        produced = (tmp_path / name).read_text(encoding="utf-8")
        committed = (_FIXTURES / name).read_text(encoding="utf-8")
        assert committed == produced, (
            f"{name} is stale vs scripts/gen_runner_traces.py. "
            "Regenerate it (python scripts/gen_runner_traces.py); the C# runner tests run against "
            "these exact bytes."
        )


def test_generator_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    gen.write_fixtures(first)
    gen.write_fixtures(second)
    for name in _FIXTURE_NAMES:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_support_selection_is_the_first_two_support_quests() -> None:
    assert gen.support_quest_ids() == ["q0000", "q0001"]


# --- oracle consistency (each stored trace == a fresh production run) ----------------------------


def test_quest_traces_agree_with_the_production_simulator() -> None:
    bundle = gen.build_bundle()
    for scenario in _committed_traces()["quest_scenarios"]:
        run = simulate_quest(
            bundle.quests[scenario["quest_id"]],
            choices=list(scenario["choices"]),
            initial_state=dict(scenario["initial_state"]),
        )
        assert run.model_dump(mode="json") == scenario["expected"], scenario["name"]


def test_dialogue_traces_agree_with_the_production_walker() -> None:
    bundle = gen.build_bundle()
    for scenario in _committed_traces()["dialogue_scenarios"]:
        walk = walk_dialogue_tree(
            bundle.dialogue_trees[scenario["tree_id"]],
            choices=list(scenario["choices"]),
            initial_state=dict(scenario["initial_state"]),
        )
        assert walk.model_dump(mode="json") == scenario["expected"], scenario["name"]


def test_closure_traces_chain_dialogue_final_state_into_the_quest() -> None:
    bundle = gen.build_bundle()
    for scenario in _committed_traces()["closure_scenarios"]:
        walk = walk_dialogue_tree(
            bundle.dialogue_trees[scenario["tree_id"]],
            choices=list(scenario["dialogue_choices"]),
            initial_state=dict(scenario["dialogue_initial_state"]),
        )
        assert walk.model_dump(mode="json") == scenario["dialogue_expected"], scenario["name"]
        run = simulate_quest(
            bundle.quests[scenario["quest_id"]],
            choices=list(scenario["quest_choices"]),
            initial_state=dict(walk.final_state),
        )
        assert run.model_dump(mode="json") == scenario["quest_expected"], scenario["name"]


# --- coverage -----------------------------------------------------------------------------------


def test_corpus_covers_every_simulate_and_walk_status() -> None:
    traces = _committed_traces()
    sim_statuses = {s["expected"]["status"] for s in traces["quest_scenarios"]}
    assert {"completed", "deadlock", "blocked", "cycle", "unavailable"} <= sim_statuses
    walk_statuses = {s["expected"]["status"] for s in traces["dialogue_scenarios"]}
    assert {"completed", "dangling", "deadlock", "cycle"} <= walk_statuses

    # a terminal-branch outcome = a completed run whose last step took a branch (empty to_stage).
    assert any(
        s["expected"]["status"] == "completed"
        and s["expected"]["steps"]
        and s["expected"]["steps"][-1]["branch_taken"]
        for s in traces["quest_scenarios"]
    )
    assert len(traces["closure_scenarios"]) >= 1


def test_corpus_includes_the_two_support_quests() -> None:
    traces = _committed_traces()
    quest_ids = {s["quest_id"] for s in traces["quest_scenarios"]}
    assert {"q0000", "q0001"} <= quest_ids
    assert traces["meta"]["support_quests"] == ["q0000", "q0001"]

    bundle_data = json.loads((_FIXTURES / "runner_bundle.json").read_text(encoding="utf-8"))
    assert {"q0000", "q0001"} <= set(bundle_data["quests"])  # real exporter output carries them
    assert bundle_data["dialogue_trees"], "runner_bundle.json must carry the dialogue trees"
