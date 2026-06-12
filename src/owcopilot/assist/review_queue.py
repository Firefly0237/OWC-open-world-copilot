"""Review queue for AI drafts and patch candidates.

By default the queue is in-memory (tests, ad-hoc scripts). Pass a `SQLiteStore` to make items
survive across sessions — the human-review loop only works as a product if a draft created today
can be approved tomorrow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..storage import SQLiteStore


class ReviewItemType(str, Enum):
    QUEST_DRAFT = "quest_draft"
    PATCH_CANDIDATE = "patch_candidate"
    BARK_VARIANT = "bark_variant"
    WORLD_SEED = "world_seed"
    IMPORT_DRAFT = "import_draft"
    DIALOGUE_TREE = "dialogue_tree"
    FLAVOR_BATCH = "flavor_batch"
    CHARACTER_PROFILE = "character_profile"


class ReviewItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    item_type: ReviewItemType
    object_ref: str
    payload: dict[str, Any]
    issue_refs: list[str] = Field(default_factory=list)
    status: str = "pending_review"


class ReviewQueue:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self._items: list[ReviewItem] = []
        self._store = store

    def add(self, item: ReviewItem) -> ReviewItem:
        if self._store is not None:
            self._store.save_review_item(
                {
                    "id": item.id,
                    "item_type": item.item_type.value,
                    "object_ref": item.object_ref,
                    "payload": item.payload,
                    "issue_refs": item.issue_refs,
                    "status": item.status,
                }
            )
        self._items.append(item)
        return item

    def add_quest_draft(
        self, payload: dict[str, Any], *, issue_refs: list[str] | None = None
    ) -> ReviewItem:
        quest_id = str(payload.get("id") or "unknown")
        return self.add(
            ReviewItem(
                item_type=ReviewItemType.QUEST_DRAFT,
                object_ref=f"quest:{quest_id}",
                payload=payload,
                issue_refs=issue_refs or [],
            )
        )

    def add_world_seed(
        self, payload: dict[str, Any], *, issue_refs: list[str] | None = None
    ) -> ReviewItem:
        seed_id = str(payload.get("id") or "unknown")
        return self.add(
            ReviewItem(
                item_type=ReviewItemType.WORLD_SEED,
                object_ref=f"world_seed:{seed_id}",
                payload=payload,
                issue_refs=issue_refs or [],
            )
        )

    def add_import_draft(
        self, payload: dict[str, Any], *, issue_refs: list[str] | None = None
    ) -> ReviewItem:
        draft_id = str(payload.get("id") or "unknown")
        return self.add(
            ReviewItem(
                item_type=ReviewItemType.IMPORT_DRAFT,
                object_ref=f"import_draft:{draft_id}",
                payload=payload,
                issue_refs=issue_refs or [],
            )
        )

    def add_dialogue_tree(
        self, payload: dict[str, Any], *, issue_refs: list[str] | None = None
    ) -> ReviewItem:
        tree_id = str(payload.get("id") or "unknown")
        return self.add(
            ReviewItem(
                item_type=ReviewItemType.DIALOGUE_TREE,
                object_ref=f"dialogue_tree:{tree_id}",
                payload=payload,
                issue_refs=issue_refs or [],
            )
        )

    def add_character_profile(
        self, payload: dict[str, Any], *, issue_refs: list[str] | None = None
    ) -> ReviewItem:
        entity_id = str((payload.get("entity") or {}).get("id") or "unknown")
        return self.add(
            ReviewItem(
                item_type=ReviewItemType.CHARACTER_PROFILE,
                object_ref=f"character:{entity_id}",
                payload=payload,
                issue_refs=issue_refs or [],
            )
        )

    def add_flavor_batch(
        self, payload: dict[str, Any], *, issue_refs: list[str] | None = None
    ) -> ReviewItem:
        batch_id = str(payload.get("id") or "unknown")
        return self.add(
            ReviewItem(
                item_type=ReviewItemType.FLAVOR_BATCH,
                object_ref=f"flavor_batch:{batch_id}",
                payload=payload,
                issue_refs=issue_refs or [],
            )
        )

    def get(self, item_id: str) -> ReviewItem:
        if self._store is not None:
            stored = self._store.get_review_item(item_id)
            if stored is None:
                raise KeyError(item_id)
            return _item_from_dict(stored)
        for item in self._items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def list_pending(self) -> list[ReviewItem]:
        if self._store is not None:
            return [
                _item_from_dict(stored)
                for stored in self._store.list_review_items(status="pending_review")
            ]
        return [item for item in self._items if item.status == "pending_review"]

    def mark(self, item_id: str, status: str, *, decided_by: str | None = None) -> ReviewItem:
        if self._store is not None:
            updated = self._store.update_review_item(
                item_id,
                status=status,
                decided_by=decided_by,
                decided_at=datetime.now(UTC).isoformat(),
            )
            for item in self._items:
                if item.id == item_id:
                    item.status = status
            return _item_from_dict(updated)
        for item in self._items:
            if item.id == item_id:
                item.status = status
                return item
        raise KeyError(item_id)


def _item_from_dict(stored: dict[str, Any]) -> ReviewItem:
    return ReviewItem(
        id=str(stored["id"]),
        item_type=ReviewItemType(stored["item_type"]),
        object_ref=str(stored["object_ref"]),
        payload=dict(stored["payload"]),
        issue_refs=list(stored["issue_refs"]),
        status=str(stored["status"]),
    )
