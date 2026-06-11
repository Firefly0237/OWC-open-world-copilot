"""Persistence tests for patches and review items in the SQLite runtime store."""

from __future__ import annotations

from owcopilot.assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
from owcopilot.storage import SQLiteStore


def test_patch_lifecycle_persists() -> None:
    store = SQLiteStore(":memory:")
    store.save_patch(
        {
            "id": "patch_abc",
            "issue_id": "issue_1",
            "status": "proposed",
            "ops": [{"op": "remove", "path": "/quests/q1/giver_npc"}],
            "rationale": "drop dangling ref",
            "evidence": [{"rule_code": "UNKNOWN_ENTITY_REF"}],
            "origin": "ai_patch",
        }
    )
    loaded = store.get_patch("patch_abc")
    assert loaded is not None
    assert loaded["status"] == "proposed"
    assert loaded["ops"][0]["path"] == "/quests/q1/giver_npc"
    assert loaded["rollback_ops"] is None

    store.update_patch(
        "patch_abc",
        status="applied",
        applied_by="tester",
        applied_at="2026-06-11T00:00:00+00:00",
        rollback_ops=[{"op": "add", "path": "/quests/q1/giver_npc", "value": "npc_ghost"}],
    )
    applied = store.get_patch("patch_abc")
    assert applied is not None
    assert applied["status"] == "applied"
    assert applied["applied_by"] == "tester"
    assert applied["rollback_ops"][0]["op"] == "add"

    assert store.list_patches(status="applied")
    assert not store.list_patches(status="proposed")
    store.close()


def test_review_queue_persists_across_instances(tmp_path) -> None:
    db = tmp_path / "rt.sqlite"
    store = SQLiteStore(db)
    queue = ReviewQueue(store)
    item = queue.add(
        ReviewItem(
            item_type=ReviewItemType.QUEST_DRAFT,
            object_ref="quest:quest_new",
            payload={"id": "quest_new", "title": "New"},
            issue_refs=["fp1"],
        )
    )
    store.close()

    # New session: the pending item is still there and can be decided.
    store2 = SQLiteStore(db)
    queue2 = ReviewQueue(store2)
    pending = queue2.list_pending()
    assert [p.id for p in pending] == [item.id]
    decided = queue2.mark(item.id, "accepted", decided_by="lead")
    assert decided.status == "accepted"
    assert queue2.list_pending() == []
    stored = store2.get_review_item(item.id)
    assert stored is not None and stored["decided_by"] == "lead"
    store2.close()


def test_in_memory_queue_still_works_without_store() -> None:
    queue = ReviewQueue()
    item = queue.add_quest_draft({"id": "q1"})
    assert queue.list_pending()
    queue.mark(item.id, "rejected")
    assert queue.list_pending() == []


def test_existing_db_gains_rollback_column(tmp_path) -> None:
    """Simulate a pre-upgrade runtime DB: old patches table without rollback_ops_json."""
    import sqlite3

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE patches (
            id TEXT PRIMARY KEY, issue_id TEXT, status TEXT NOT NULL,
            ops_json TEXT NOT NULL, rationale TEXT NOT NULL, evidence_json TEXT NOT NULL,
            origin TEXT NOT NULL, applied_by TEXT, applied_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(db)  # initialize() must add the missing column
    store.save_patch({"id": "p1", "status": "proposed", "ops": [], "rationale": "", "evidence": []})
    assert store.get_patch("p1") is not None
    store.close()
