from __future__ import annotations

from pathlib import Path

import pytest

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


# --- snapshot_id path-traversal hardening (R5 HIGH) -------------------------
# load_snapshot is the single read boundary both the diff and restore API entry
# points funnel through; an externally controlled snapshot_id must not escape the
# .snapshots dir when interpolated into ``{id}.json``. Reuses the store-side
# id-invariant (_validate_id_chars / _FORBIDDEN_ID_CHARS), not a private copy.

_MALICIOUS_IDS = [
    "../secret",  # relative parent traversal
    "..\\secret",  # Windows backslash traversal
    "../../r5_outside_secret",  # multi-level escape
    "..\\..\\r5_outside_secret",
    "C:/Windows/win.ini",  # absolute path / drive colon (pathlib would swallow root)
    "/etc/passwd",  # POSIX absolute
    "a/b/c",  # nested path separators
    "bad\x00id",  # NUL control char
    "tab\tid",  # control char
]


@pytest.mark.parametrize("bad_id", _MALICIOUS_IDS)
def test_load_snapshot_rejects_traversal_ids(tmp_path: Path, bad_id: str) -> None:
    store = ContentStore(tmp_path)
    store.save(_v1())
    # a sentinel .json the traversal would otherwise read
    (tmp_path / "secret.json").write_text('{"bundle": {}}', encoding="utf-8")
    (tmp_path.parent / "r5_outside_secret.json").write_text('{"bundle": {}}', encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        load_snapshot(store, bad_id)
    # guided error, never silent: message names the offending id + context
    assert "context" in str(exc.value)
    assert "snapshot_id" in str(exc.value)


def test_diff_view_model_rejects_traversal_from_id(tmp_path: Path) -> None:
    from owcopilot.app.view_models import build_diff_view_model

    store = ContentStore(tmp_path)
    store.save(_v1())
    (tmp_path / "secret.json").write_text('{"bundle": {}}', encoding="utf-8")

    with pytest.raises(ValueError):
        build_diff_view_model(tmp_path, from_id="../secret")


def test_restore_action_rejects_traversal_snapshot_id(tmp_path: Path) -> None:
    from owcopilot.app.actions import restore_snapshot_action

    store = ContentStore(tmp_path)
    store.save(_v1())
    (tmp_path / "secret.json").write_text('{"bundle": {}}', encoding="utf-8")

    with pytest.raises(ValueError):
        restore_snapshot_action(tmp_path, snapshot_id="../../secret")


def test_legal_snapshot_roundtrip_unaffected(tmp_path: Path) -> None:
    # save -> load -> restore -> diff with the internally-generated (timestamp) id must
    # all pass: the validation only rejects attacker-supplied ids, never legitimate ones.
    from owcopilot.app.actions import restore_snapshot_action
    from owcopilot.app.view_models import build_diff_view_model

    store = ContentStore(tmp_path)
    store.save(_v1())
    meta = write_snapshot(store, label="基线")

    # load via the validated read boundary
    restored = load_snapshot(store, meta.id)
    assert restored is not None
    assert content_hash(restored) == content_hash(_v1())

    # mutate the live world, then diff the snapshot against it through the API entry point
    store.save(_v2())
    diff_vm = build_diff_view_model(tmp_path, from_id=meta.id)
    assert diff_vm is not None
    assert diff_vm["from_id"] == meta.id

    # restore the baseline snapshot through the action entry point
    result = restore_snapshot_action(tmp_path, snapshot_id=meta.id)
    assert result["restored"] == meta.id
    assert content_hash(ContentStore(tmp_path).load()) == content_hash(_v1())


def test_identical_world_has_empty_diff(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    store.save(_v1())
    meta = write_snapshot(store)
    old = load_snapshot(store, meta.id)
    assert old is not None

    diff = bundle_diff(old, store.load())
    assert diff.summary == {"added": 0, "removed": 0, "changed": 0}


# --- second-layer container assertion (resolve_under_root) -------------------
# load_snapshot / write_snapshot keep the friendly first layer (_validate_id_chars),
# and now also assert the FINAL `{id}.json` path stays under the store root with the
# shared canon helper (resolve_under_root). To prove the second layer is wired
# independently of the first, we neuter the char blacklist and confirm an escaping id
# is still rejected with a guided PathSecurityError (a ValueError subclass).


def test_load_snapshot_second_layer_blocks_escape_even_if_first_layer_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from owcopilot.trust.security import PathSecurityError

    store = ContentStore(tmp_path)
    store.save(_v1())
    (tmp_path.parent / "outside.json").write_text('{"bundle": {}}', encoding="utf-8")

    # Disable the friendly char layer so only the container assertion can catch the escape.
    # The final path is `<root>/.snapshots/<id>.json`, so an id needs `../../` to truly
    # leave the store root (`../` alone only climbs out of `.snapshots`, still inside root).
    monkeypatch.setattr(
        "owcopilot.content.snapshot._validate_id_chars", lambda value, **_: value
    )
    with pytest.raises(PathSecurityError) as exc:
        load_snapshot(store, "../../outside")
    assert "escapes allowed root" in str(exc.value)  # guided, never silent


def test_write_snapshot_second_layer_blocks_escape_even_if_first_layer_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from owcopilot.trust.security import PathSecurityError

    store = ContentStore(tmp_path)
    store.save(_v1())

    # Force write_snapshot to derive an escaping id (it normally uses a safe timestamp).
    class _EscapingNow:
        def strftime(self, _fmt: str) -> str:
            # `<root>/.snapshots/<id>.json`: needs `../../` to escape the store root.
            return "../../escape"

        def isoformat(self) -> str:
            return "2026-06-29T00:00:00+00:00"

    class _EscapingDatetime:
        @staticmethod
        def now(_tz: object = None) -> _EscapingNow:
            return _EscapingNow()

    monkeypatch.setattr("owcopilot.content.snapshot.datetime", _EscapingDatetime)
    with pytest.raises(PathSecurityError):
        write_snapshot(store)


def test_legal_ids_and_synthetic_colon_id_not_misflagged(tmp_path: Path) -> None:
    # Regression: legal slug ids, the timestamp snapshot id, AND the colon-bearing
    # synthetic quest_event_ref id (which lives in event_refs.jsonl, not `{id}.json`)
    # must all round-trip without the new container assertion misflagging them.
    from owcopilot.content.models import QuestEventReference, QuestEventRefKind

    bundle = _v1()
    synthetic_id = "q1:e1:mentions_event"
    bundle.quest_event_refs[synthetic_id] = QuestEventReference(
        id=synthetic_id,
        quest_id="q1",
        event_id="e1",
        ref_kind=QuestEventRefKind.MENTIONS_EVENT,
    )

    store = ContentStore(tmp_path)
    store.save(bundle)  # legal slug ids + colon qer id: must not raise
    reloaded = store.load()
    assert synthetic_id in reloaded.quest_event_refs
    assert "fac_a" in reloaded.entities

    # timestamp snapshot id round-trips through both write and validated read boundaries
    meta = write_snapshot(store)
    restored = load_snapshot(store, meta.id)
    assert restored is not None
    assert synthetic_id in restored.quest_event_refs
