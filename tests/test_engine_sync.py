"""Engine back-sync (the kept import direction): classify engine-edited quest rows vs canon and
stage only the new/changed ones for human review. Engine edits never auto-land."""

from owcopilot.app.engine_sync import plan_engine_import, staged_bundle
from owcopilot.content.models import ContentBundle, Quest


def _canon() -> ContentBundle:
    return ContentBundle(
        quests={
            "q_keep": Quest(id="q_keep", title="Keep", objective="unchanged objective"),
            "q_edit": Quest(id="q_edit", title="Edit", objective="old objective"),
        }
    )


def test_plan_engine_import_classifies_new_changed_unchanged() -> None:
    incoming = [
        {"id": "q_keep", "title": "Keep", "objective": "unchanged objective"},  # unchanged
        {"id": "q_edit", "title": "Edit", "objective": "a rewritten objective"},  # changed
        {"id": "q_new", "title": "New", "objective": "brand new quest"},  # new
    ]
    plan = plan_engine_import(incoming, _canon())
    assert plan["new"] == ["q_new"]
    assert plan["changed"] == ["q_edit"]
    assert plan["unchanged"] == ["q_keep"]


def test_staged_bundle_holds_only_new_and_changed() -> None:
    incoming = [
        {"id": "q_keep", "title": "Keep", "objective": "unchanged objective"},
        {"id": "q_edit", "title": "Edit", "objective": "a rewritten objective"},
        {"id": "q_new", "title": "New", "objective": "brand new quest"},
    ]
    plan = plan_engine_import(incoming, _canon())
    staged = staged_bundle(plan)
    assert set(staged.quests) == {"q_new", "q_edit"}  # the unchanged one is not re-queued


def test_coerce_folds_legacy_reward_string() -> None:
    # The engine-import path coerces a pre-v2 quest dict (single `reward` string) into a v2 Quest.
    legacy = [{"id": "q_legacy", "title": "Legacy", "reward": "75 gold"}]
    plan = plan_engine_import(legacy, _canon())
    quest = plan["_quests"]["q_legacy"]
    assert quest.rewards and quest.rewards[0].value == "75 gold"
