from __future__ import annotations

from pathlib import Path

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.snapshot import (
    bundle_diff,
    list_snapshots,
    load_snapshot,
    write_snapshot,
)
from owcopilot.content.store import ContentStore


def _v1() -> ContentBundle:
    return ContentBundle(
        entities={
            "fac_a": Entity(id="fac_a", name="宪章会", type=EntityType.FACTION),
            "npc_old": Entity(id="npc_old", name="旧人", type=EntityType.NPC),
        },
        quests={"q1": Quest(id="q1", title="任务一", objective="旧目标")},
    )


def _v2() -> ContentBundle:
    return ContentBundle(
        entities={"fac_a": Entity(id="fac_a", name="宪章议会", type=EntityType.FACTION)},
        quests={
            "q1": Quest(id="q1", title="任务一", objective="新目标"),
            "q2": Quest(id="q2", title="任务二"),
        },
    )


def test_snapshot_round_trips_the_bundle(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    store.save(_v1())

    meta = write_snapshot(store, label="第一版")
    assert meta.label == "第一版"
    assert [m.id for m in list_snapshots(store)] == [meta.id]

    restored = load_snapshot(store, meta.id)
    assert restored is not None
    assert content_hash(restored) == content_hash(_v1())
    assert load_snapshot(store, "nope") is None


def test_diff_reports_added_removed_and_field_changes(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    store.save(_v1())
    meta = write_snapshot(store)
    store.save(_v2())  # the live world is now v2

    old = load_snapshot(store, meta.id)
    assert old is not None
    diff = bundle_diff(old, store.load())

    assert ("quest", "q2") in {(c.kind, c.id) for c in diff.added}
    assert ("entity", "npc_old") in {(c.kind, c.id) for c in diff.removed}

    changed = {(c.kind, c.id): c for c in diff.changed}
    assert ("entity", "fac_a") in changed
    assert ("quest", "q1") in changed
    fac_fields = {fc.field: (fc.before, fc.after) for fc in changed[("entity", "fac_a")].changes}
    assert fac_fields["name"] == ("宪章会", "宪章议会")
    quest_fields = {fc.field for fc in changed[("quest", "q1")].changes}
    assert "objective" in quest_fields

    assert diff.summary == {"added": 1, "removed": 1, "changed": 2}


def test_diff_tracks_style_guide_edits(tmp_path: Path) -> None:
    # a style guide steers every generation stage, so editing it is a real canon change the
    # version history must surface — it was silently absent from the diff's collections.
    from owcopilot.content.models import StyleGuide

    old = ContentBundle(
        style_guides={"sg": StyleGuide(id="sg", body="市井气，避免现代词。", rules=["不用网络梗"])}
    )
    new = ContentBundle(
        style_guides={
            "sg": StyleGuide(id="sg", body="史诗庄重。", rules=["不用网络梗", "多用古语"])
        }
    )
    diff = bundle_diff(old, new)
    changed = {(c.kind, c.id): c for c in diff.changed}
    assert ("style_guide", "sg") in changed
    fields = {fc.field for fc in changed[("style_guide", "sg")].changes}
    assert "body" in fields and "rules" in fields
    # and a brand-new / removed style guide shows as added/removed
    added = bundle_diff(ContentBundle(), new)
    assert ("style_guide", "sg") in {(c.kind, c.id) for c in added.added}


def test_identical_world_has_empty_diff(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    store.save(_v1())
    meta = write_snapshot(store)
    old = load_snapshot(store, meta.id)
    assert old is not None

    diff = bundle_diff(old, store.load())
    assert diff.summary == {"added": 0, "removed": 0, "changed": 0}
