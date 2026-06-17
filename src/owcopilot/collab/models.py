"""Collaboration primitives for team authoring (WS-B): assignments, comments, locks.

Per-world data tied to canon objects (a quest, an entity, a review item). Stored beside the world
(JSON ledger), not in the canon bundle — so it never pollutes exportable content. Built on the
WS-P role model (who may do what); presence/realtime is out of scope (needs websockets).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ..content.models import Quest


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Assignment(BaseModel):
    object_ref: str  # "quest:q1", "entity:npc_x", "review:<id>" …
    assignee: str
    assigned_by: str
    at: str = Field(default_factory=_now)
    note: str = ""


class Comment(BaseModel):
    id: str
    object_ref: str
    author: str
    body: str
    at: str = Field(default_factory=_now)


class Lock(BaseModel):
    object_ref: str
    holder: str
    at: str = Field(default_factory=_now)


class CollabState(BaseModel):
    """The whole per-world collaboration ledger."""

    assignments: dict[str, Assignment] = Field(default_factory=dict)  # object_ref -> assignment
    comments: dict[str, list[Comment]] = Field(default_factory=dict)  # object_ref -> thread
    locks: dict[str, Lock] = Field(default_factory=dict)  # object_ref -> lock


def etag_for(payload: object) -> str:
    """A short content fingerprint for optimistic concurrency. Two editors holding the same etag are
    looking at the same version; a stale etag on write means someone else changed it first."""
    if isinstance(payload, Quest):
        payload = payload.model_dump(mode="json", exclude_none=True)
    raw = repr(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
