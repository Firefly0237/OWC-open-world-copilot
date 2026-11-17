"""P1 batch-2: lesson generalization wiring + lesson revocation (soft delete).

Generalization: the cross-session lesson archive was only wired for the quest draft path. These
tests drive character + flavor end-to-end through the action layer and assert lessons reach the
generation AND critic prompts when injection is on, and that the default (off) prompt is byte-for-
byte unchanged.

Revocation: a wrong/harmful lesson could previously be injected forever. These tests cover the new
revoke_lesson store method (soft delete + idempotent), the guided error for a missing id, and the
legacy-schema migration (a DB without the revoked_at column opens and revokes fine).
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from owcopilot.app.actions import (
    revoke_lesson_action,
    run_character_action,
    run_flavor_action,
)
from owcopilot.assist.characters import OfflineCharacterProvider
from owcopilot.assist.critic import CHARACTER_CRITIQUE_MARKER, FLAVOR_CRITIQUE_MARKER
from owcopilot.assist.flavor import OfflineFlavorProvider
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.storage.sqlite import SQLiteStore

# A recognizable lesson body so a marker check also proves it is THIS lesson that got injected.
_LESSON_TEXT = "此类内容在「voice」维度历史被人审拒绝，请着重提高质量。"
_ALLOW_OFFLINE = {"OWCOPILOT_ALLOW_OFFLINE_LLM": "1"}


def _spy_provider(base_cls: type, sink: list[str]) -> type:
    """A drop-in subclass of an offline provider that records every system prompt it is asked to
    complete, then delegates to the real deterministic double (so the pipeline still runs at $0)."""

    class _Spy(base_cls):  # type: ignore[valid-type,misc]
        def complete(self, *, system: str, user: str, model: str):  # type: ignore[no-untyped-def]
            sink.append(system)
            return super().complete(system=system, user=user, model=model)

    return _Spy


def _seed_lesson(db_path: Path, item_type: str) -> None:
    store = SQLiteStore(str(db_path))
    store.save_lesson(item_type, _LESSON_TEXT)
    store.close()


def _generation_systems(systems: list[str], critique_marker: str) -> list[str]:
    """Keep only the generation prompts (a critique prompt carries the critique marker)."""
    return [s for s in systems if critique_marker not in s]


# ---------------------------------------------------------------------------
# ① generalization wiring — end-to-end through the action layer
# ---------------------------------------------------------------------------


def _capture_flavor(tmp_root: Path, *, seed: bool, inject: bool) -> list[str]:
    content_root = tmp_root / f"flavor_{uuid.uuid4().hex[:8]}"
    ContentStore(content_root).save(ContentBundle())
    db = content_root / "rt.sqlite"
    if seed:
        _seed_lesson(db, "flavor_batch")
    sink: list[str] = []
    spy = _spy_provider(OfflineFlavorProvider, sink)
    with (
        patch("owcopilot.app.actions.assist.OfflineFlavorProvider", spy),
        patch.dict(os.environ, _ALLOW_OFFLINE),
    ):
        run_flavor_action(
            content_root,
            category="item",
            names=["测试道具"],
            sqlite_path=str(db),
            refine_rounds=0,
            inject_lessons=inject,
        )
    return sink


def test_flavor_generation_injects_lesson_when_on(tmp_path) -> None:
    """[硬] flavor: inject on + lesson in archive -> generation prompt carries the lesson block."""
    systems = _capture_flavor(tmp_path, seed=True, inject=True)
    gen = _generation_systems(systems, FLAVOR_CRITIQUE_MARKER)
    assert gen, "expected at least one generation call"
    assert any("[lesson-memory]" in s and _LESSON_TEXT in s for s in gen)


def test_flavor_generation_byte_identical_when_off(tmp_path) -> None:
    """[硬] flavor: the off path (or empty archive) is byte-for-byte the pre-hook prompt.

    inject=False must not fetch lessons even though the archive has one, so its generation prompt is
    identical to a run against an empty archive — proving the hook adds nothing when disabled.
    """
    off = _capture_flavor(tmp_path, seed=True, inject=False)
    empty = _capture_flavor(tmp_path, seed=False, inject=True)
    off_gen = _generation_systems(off, FLAVOR_CRITIQUE_MARKER)
    empty_gen = _generation_systems(empty, FLAVOR_CRITIQUE_MARKER)
    assert off_gen and empty_gen
    assert "[lesson-memory]" not in off_gen[0]
    assert off_gen[0] == empty_gen[0]


def _capture_character(tmp_root: Path, *, seed: bool, inject: bool) -> list[str]:
    content_root = tmp_root / f"char_{uuid.uuid4().hex[:8]}"
    ContentStore(content_root).save(ContentBundle())
    db = content_root / "rt.sqlite"
    if seed:
        _seed_lesson(db, "character_profile")
    sink: list[str] = []
    with (
        # Patch where it is USED: app/actions/assist.py binds the name at import time, so
        # patching the source module (owcopilot.assist.characters) never reaches the action.
        patch(
            "owcopilot.app.actions.assist.OfflineCharacterProvider",
            _spy_provider(OfflineCharacterProvider, sink),
        ),
        patch.dict(os.environ, _ALLOW_OFFLINE),
    ):
        run_character_action(
            content_root,
            brief={"name": "测试角色", "concept": "一个用于测试的角色"},
            sqlite_path=str(db),
            refine_rounds=1,  # default: exercise BOTH generation and the critic
            inject_lessons=inject,
        )
    return sink


def test_character_injects_lesson_into_generation_and_critic(tmp_path) -> None:
    """[硬] character: inject on -> the lesson reaches BOTH the generation and critic prompts."""
    systems = _capture_character(tmp_path, seed=True, inject=True)
    gen = [s for s in systems if CHARACTER_CRITIQUE_MARKER not in s]
    crit = [s for s in systems if CHARACTER_CRITIQUE_MARKER in s]
    assert gen and crit, "expected both a generation and a critique call"
    assert any("[lesson-memory]" in s and _LESSON_TEXT in s for s in gen)
    assert any("[critic-lesson-memory]" in s for s in crit)


def test_character_byte_identical_when_off(tmp_path) -> None:
    """[硬] character: off path is byte-for-byte identical to the empty-archive path."""
    off = _capture_character(tmp_path, seed=True, inject=False)
    empty = _capture_character(tmp_path, seed=False, inject=True)
    off_gen = [s for s in off if CHARACTER_CRITIQUE_MARKER not in s]
    empty_gen = [s for s in empty if CHARACTER_CRITIQUE_MARKER not in s]
    assert off_gen and empty_gen
    assert all("[lesson-memory]" not in s for s in off)
    assert all("[critic-lesson-memory]" not in s for s in off)
    assert off_gen[0] == empty_gen[0]


# ---------------------------------------------------------------------------
# ② revocation — store level
# ---------------------------------------------------------------------------


def _lesson_id(store: SQLiteStore, item_type: str) -> str:
    rows = store.get_lessons_for_type(item_type)
    assert rows, "expected a lesson to exist"
    return str(rows[0]["id"])


def test_revoke_hides_lesson_from_get_lessons() -> None:
    """[硬] A revoked lesson is no longer returned by get_lessons_for_type."""
    store = SQLiteStore(":memory:")
    store.save_lesson("quest_draft", "教训一")
    lid = _lesson_id(store, "quest_draft")
    assert store.revoke_lesson(lid, "错误教训") is True
    assert store.get_lessons_for_type("quest_draft") == []


def test_revoke_is_idempotent() -> None:
    """[硬] Revoking twice succeeds both times and preserves the original tombstone."""
    store = SQLiteStore(":memory:")
    store.save_lesson("bark_variant", "教训")
    lid = _lesson_id(store, "bark_variant")
    assert store.revoke_lesson(lid, "第一次") is True
    first = store.conn.execute(
        "SELECT revoked_at, revoked_reason FROM lessons WHERE id = ?", (lid,)
    ).fetchone()
    assert store.revoke_lesson(lid, "第二次不同原因") is True
    second = store.conn.execute(
        "SELECT revoked_at, revoked_reason FROM lessons WHERE id = ?", (lid,)
    ).fetchone()
    # COALESCE keeps the ORIGINAL revoked_at/reason: a re-revoke is a true no-op on the tombstone.
    assert first["revoked_at"] == second["revoked_at"]
    assert first["revoked_reason"] == second["revoked_reason"] == "第一次"
    assert store.get_lessons_for_type("bark_variant") == []


def test_revoke_unknown_id_returns_false() -> None:
    """[硬] Revoking an id that does not exist returns False (no silent success)."""
    store = SQLiteStore(":memory:")
    assert store.revoke_lesson("no-such-id", "x") is False


def test_revoke_does_not_affect_other_lessons() -> None:
    """Revoking one lesson leaves sibling lessons of the same type injectable."""
    store = SQLiteStore(":memory:")
    store.save_lesson("quest_draft", "A", dimension="voice")
    store.save_lesson("quest_draft", "B", dimension="craft")
    voice_id = str(store.get_lessons_for_type("quest_draft", dimension="voice")[0]["id"])
    store.revoke_lesson(voice_id, "drop voice")
    remaining = store.get_lessons_for_type("quest_draft")
    assert [r["dimension"] for r in remaining] == ["craft"]


# ---------------------------------------------------------------------------
# ② revocation — legacy-schema migration
# ---------------------------------------------------------------------------


def test_revoke_columns_added_by_migration() -> None:
    """[硬] initialize() adds revoked_at/revoked_reason idempotently."""
    store = SQLiteStore(":memory:")
    cols = {str(r["name"]) for r in store.conn.execute("PRAGMA table_info(lessons)")}
    assert {"revoked_at", "revoked_reason"} <= cols
    store.initialize()  # idempotent: re-running must not error or duplicate columns
    cols_again = {str(r["name"]) for r in store.conn.execute("PRAGMA table_info(lessons)")}
    assert cols_again == cols


def test_legacy_db_without_revoked_column_opens_and_revokes(tmp_path) -> None:
    """[硬 迁移] A DB whose lessons table predates the revoked_at column upgrades in place: it
    opens without error, its pre-existing lesson is still injectable, and it can be revoked."""
    db_path = tmp_path / "legacy.sqlite"
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(db_path))
    # The lessons table exactly as it was before this change (no exemplar_ids, no revoked_* cols).
    conn.execute(
        """
        CREATE TABLE lessons (
            id TEXT PRIMARY KEY,
            item_type TEXT NOT NULL,
            dimension TEXT NOT NULL DEFAULT 'general',
            lesson_text TEXT NOT NULL,
            false_pass_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    legacy_id = "legacy-lesson-1"
    conn.execute(
        "INSERT INTO lessons (id, item_type, dimension, lesson_text, false_pass_count, "
        "created_at, last_seen_at) VALUES (?, 'quest_draft', 'general', '旧教训', 1, ?, ?)",
        (legacy_id, now, now),
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(str(db_path))  # __init__ runs the ALTER TABLE migration
    try:
        cols = {str(r["name"]) for r in store.conn.execute("PRAGMA table_info(lessons)")}
        assert {"revoked_at", "revoked_reason"} <= cols
        # The legacy lesson back-fills to revoked_at NULL, so it is still injectable...
        assert [r["id"] for r in store.get_lessons_for_type("quest_draft")] == [legacy_id]
        # ...and can now be revoked.
        assert store.revoke_lesson(legacy_id, "过期") is True
        assert store.get_lessons_for_type("quest_draft") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# ② revocation — action layer (guided error) + end-to-end effect
# ---------------------------------------------------------------------------


def test_revoke_lesson_action_guides_on_missing_id(tmp_path) -> None:
    """[硬] The thin action raises a guided Chinese error for an unknown id."""
    ContentStore(tmp_path).save(ContentBundle())
    with pytest.raises(ValueError, match="找不到 id 为"):
        revoke_lesson_action(tmp_path, lesson_id="does-not-exist", reason="x")


def test_revoke_lesson_action_removes_lesson_end_to_end(tmp_path) -> None:
    """[硬] After the action revokes a lesson, the archive stops returning it."""
    ContentStore(tmp_path).save(ContentBundle())
    db = tmp_path / ".owcopilot" / "runtime.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    _seed_lesson(db, "dialogue_tree")
    seeded = SQLiteStore(str(db))
    lid = str(seeded.get_lessons_for_type("dialogue_tree")[0]["id"])
    seeded.close()

    result = revoke_lesson_action(tmp_path, lesson_id=lid, reason="错误")
    assert result["revoked"] is True

    after = SQLiteStore(str(db))
    try:
        assert after.get_lessons_for_type("dialogue_tree") == []
    finally:
        after.close()


# ---------------------------------------------------------------------------
# ② revocation — CLI subcommand
# ---------------------------------------------------------------------------


def test_cli_lessons_revoke(tmp_path, capsys) -> None:
    """[硬] `lessons revoke <id>` soft-deletes the lesson (exit 0, JSON confirms)."""
    import json

    from owcopilot.cli.main import main

    ContentStore(tmp_path).save(ContentBundle())
    db = tmp_path / ".owcopilot" / "runtime.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    _seed_lesson(db, "flavor_batch")
    seeded = SQLiteStore(str(db))
    lid = str(seeded.get_lessons_for_type("flavor_batch")[0]["id"])
    seeded.close()

    code = main(
        [
            "lessons",
            "revoke",
            lid,
            "--content-root",
            str(tmp_path),
            "--sqlite-path",
            str(db),
            "--reason",
            "错误教训",
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["revoked"] is True and body["lesson_id"] == lid

    after = SQLiteStore(str(db))
    try:
        assert after.get_lessons_for_type("flavor_batch") == []
    finally:
        after.close()


def test_cli_lessons_revoke_unknown_id_guided_error(tmp_path, capsys) -> None:
    """[硬] `lessons revoke <bad-id>` exits non-zero with a guided Chinese error on stderr."""
    import json

    from owcopilot.cli.main import main

    ContentStore(tmp_path).save(ContentBundle())
    code = main(["lessons", "revoke", "nope", "--content-root", str(tmp_path)])
    assert code == 2
    err = json.loads(capsys.readouterr().err)
    assert "找不到 id 为" in err["error"]
