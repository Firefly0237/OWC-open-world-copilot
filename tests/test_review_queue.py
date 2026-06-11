from __future__ import annotations

import pytest

from owcopilot.assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue


def test_review_queue_adds_quest_draft_as_pending_review() -> None:
    queue = ReviewQueue()

    item = queue.add_quest_draft(
        {"id": "quest_missing_caravan", "title": "Missing Caravan"},
        issue_refs=["issue_1"],
    )

    assert item.item_type is ReviewItemType.QUEST_DRAFT
    assert item.object_ref == "quest:quest_missing_caravan"
    assert item.status == "pending_review"
    assert queue.list_pending() == [item]


def test_review_queue_mark_updates_status() -> None:
    queue = ReviewQueue()
    item = queue.add(
        ReviewItem(
            item_type=ReviewItemType.PATCH_CANDIDATE,
            object_ref="patch:p1",
            payload={"id": "p1"},
        )
    )

    updated = queue.mark(item.id, "approved")

    assert updated.status == "approved"
    assert queue.list_pending() == []


def test_review_queue_mark_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        ReviewQueue().mark("missing", "approved")
