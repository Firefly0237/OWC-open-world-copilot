from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.patches.search import plan_repairs
from owcopilot.patches.shadow import apply_patch_shadow


def _runner() -> AuditRunner:
    return AuditRunner(build_default_rule_registry())


def _dirty_bundle() -> ContentBundle:
    # Three open errors on q1: an unknown giver_npc ref + missing localization key (both have a
    # deterministic fixer) and a missing objective (no deterministic fixer).
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"
            )
        },
        quests={"q1": Quest(id="q1", title="The Lost Caravan", giver_npc="npc_missing")},
    )


def _fixable_bundle() -> ContentBundle:
    # A world whose every error is deterministically fixable: a dangling giver_npc ref (fixed by
    # removing it) plus a missing localization key (fixed by adding it). The search can therefore
    # drive it all the way to 0 open errors — the shape the anytime early-stop needs.
    return ContentBundle(
        quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_ghost", objective="Do the thing.")}
    )


def _open_error_count(bundle: ContentBundle, runner: AuditRunner) -> int:
    return len(runner.run(AuditContext.from_bundle(bundle)).open_errors)


def test_plan_repairs_clean_project_is_empty() -> None:
    clean = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"
            )
        },
        quests={
            "q1": Quest(
                id="q1",
                title="Q1",
                giver_npc="npc_aldric",
                objective="Help Aldric.",
                localization_keys=["quest.q1.objective"],
            )
        },
    )
    plan = plan_repairs(clean, _runner())
    assert plan.initial_open_errors == 0
    assert plan.final_open_errors == 0
    assert plan.moves == []
    assert plan.iterations == 0


def test_plan_repairs_finds_the_fixable_sequence() -> None:
    bundle = _dirty_bundle()
    runner = _runner()
    assert _open_error_count(bundle, runner) == 3  # sanity: the fixture really has 3 errors

    plan = plan_repairs(bundle, runner, seed=0)

    # Two of the three errors have a deterministic fix; the missing-objective one does not.
    assert plan.initial_open_errors == 3
    assert plan.resolved_errors == 2
    assert plan.final_open_errors == 1
    assert len(plan.moves) == 2
    assert {move.rule_code for move in plan.moves} == {
        "UNKNOWN_ENTITY_REF",
        "MISSING_LOCALIZATION_KEY",
    }


def test_plan_is_executable_and_reaches_the_predicted_state() -> None:
    # Applying the plan's moves in order must really drive the audit to the predicted error count —
    # proof the search's reward signal matches what the patches actually do.
    bundle = _dirty_bundle()
    runner = _runner()
    plan = plan_repairs(bundle, runner, seed=0)

    patched = bundle
    for move in plan.moves:
        patched = apply_patch_shadow(patched, move.ops)
    assert _open_error_count(patched, runner) == plan.final_open_errors


def test_plan_repairs_is_reproducible_for_a_seed() -> None:
    bundle = _dirty_bundle()
    runner = _runner()
    first = plan_repairs(bundle, runner, seed=7)
    second = plan_repairs(bundle, runner, seed=7)
    assert first.model_dump() == second.model_dump()


def test_plan_repairs_never_increases_errors() -> None:
    bundle = _dirty_bundle()
    runner = _runner()
    plan = plan_repairs(bundle, runner, seed=0)
    assert plan.final_open_errors <= plan.initial_open_errors


def test_stop_on_clean_early_terminates_and_reports_actual_iterations() -> None:
    # A fully-fixable world reaches 0 open errors, so the anytime stop (default) fires and the loop
    # returns without burning the whole budget — and iterations reports the true executed count.
    bundle = _fixable_bundle()
    runner = _runner()
    assert _open_error_count(bundle, runner) == 2  # sanity: two deterministically-fixable errors

    plan = plan_repairs(bundle, runner, seed=0, max_iterations=200)

    assert plan.final_open_errors == 0
    assert plan.resolved_errors == 2
    assert 0 < plan.iterations < 200  # stopped on the clean goal, not at the full budget


def test_stop_on_clean_false_exhausts_the_budget() -> None:
    # Opting out of the anytime stop keeps hunting until the budget is spent, even after a clean
    # plan is already in hand — iterations then equals the full budget.
    bundle = _fixable_bundle()
    runner = _runner()

    plan = plan_repairs(bundle, runner, seed=0, max_iterations=25, stop_on_clean=False)

    assert plan.final_open_errors == 0  # still finds the fully-resolving plan
    assert plan.iterations == 25  # but runs every iteration of the budget


def test_iterations_equal_budget_when_the_clean_goal_is_unreachable() -> None:
    # Backward-compat: when no plan can reach 0 errors, the anytime stop never fires, so the loop
    # runs the full budget and iterations equals max_iterations — exactly the old reported value.
    bundle = _dirty_bundle()
    runner = _runner()

    plan = plan_repairs(bundle, runner, seed=0, max_iterations=50)

    assert plan.final_open_errors == 1  # the missing-objective error has no deterministic fix
    assert plan.iterations == 50
