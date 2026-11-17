# 团队协作创作（WS-B）：指派/评论/编辑锁/乐观并发。
"""Team-authoring collaboration (WS-B): assignments, comments, edit locks, optimistic concurrency.

Built on the per-world JSON ledger + the WS-P role model. Lets multiple authors work a world without
clobbering each other (etag conflict detection + locks) and coordinate (assign + comment threads).
"""

from __future__ import annotations

from .models import Assignment, CollabState, Comment, Lock, etag_for
from .service import (
    ConflictError,
    acquire_lock,
    add_comment,
    assign,
    check_etag,
    expire_locks,
    lock_is_expired,
    release_lock,
    unassign,
)
from .store import CollabStore

__all__ = [
    "Assignment",
    "CollabState",
    "CollabStore",
    "Comment",
    "ConflictError",
    "Lock",
    "acquire_lock",
    "add_comment",
    "assign",
    "check_etag",
    "etag_for",
    "expire_locks",
    "lock_is_expired",
    "release_lock",
    "unassign",
]
